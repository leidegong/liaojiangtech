"""Network byte order. All application datagrams fit within 1200 bytes."""
from dataclasses import dataclass
from enum import IntEnum
import json
import struct
import time
from typing import NamedTuple
from . import fec

MAGIC = b"N7"
VERSION = 2
MAX_DATAGRAM = 1200
IP_UDP_OVERHEAD = 28
HEADER = struct.Struct("!2sBBIQH")
# Frame id, fragment index, fragments including FEC parity, JPEG bytes. The
# data fragment count follows from the JPEG size, so any fragment describes
# the whole block layout.
FRAGMENT = struct.Struct("!IHHI")
MAX_FRAGMENT_DATA = MAX_DATAGRAM - HEADER.size - FRAGMENT.size
MAX_FRAME_BYTES = 2 * 1024 * 1024
VIDEO_PACKET_OVERHEAD = HEADER.size + FRAGMENT.size + IP_UDP_OVERHEAD


class Kind(IntEnum):
    VIDEO = 1       # Full-resolution frame (the only video layer in single-layer mode).
    DATA = 2
    CONTROL = 3
    VIDEO_BASE = 4  # Low-resolution base layer of the same captured frame.


# Video layers in scheduling priority order: the base layer is always tried first.
VIDEO_KINDS = (Kind.VIDEO_BASE, Kind.VIDEO)


def fragment_count(jpeg_bytes):
    """Data fragments one JPEG needs."""
    return -(-jpeg_bytes // MAX_FRAGMENT_DATA)


def video_wire_bytes(jpeg_bytes, loss=None):
    """Link bytes of one fragmented JPEG plus the FEC parity sent at this
    packet loss rate, including UDP/IP overhead."""
    count = fragment_count(jpeg_bytes)
    parity = fec.parity_count(count, loss)
    return (jpeg_bytes + count * VIDEO_PACKET_OVERHEAD
            + parity * (min(jpeg_bytes, MAX_FRAGMENT_DATA) + VIDEO_PACKET_OVERHEAD))


def video_payload_budget(wire_bytes, loss=None):
    """Largest JPEG whose fragments and parity fit within a link-byte budget."""
    low, high = 0, min(MAX_FRAME_BYTES, max(0, int(wire_bytes)))
    while low < high:  # Wire cost grows with JPEG size, so bisect.
        middle = (low + high + 1) // 2
        if video_wire_bytes(middle, loss) <= wire_bytes:
            low = middle
        else:
            high = middle - 1
    return low


class Fragment(NamedTuple):
    frame_id: int
    index: int
    total: int  # Data plus parity fragments.
    data_count: int
    frame_bytes: int
    data: bytes


@dataclass(frozen=True)
class Packet:
    kind: Kind
    sequence: int
    generated_ns: int
    payload: bytes

    @property
    def wire_bytes(self):
        return HEADER.size + len(self.payload) + IP_UDP_OVERHEAD

    def encode(self):
        if len(self.payload) > MAX_DATAGRAM - HEADER.size:
            raise ValueError("Packet exceeds MTU budget")
        return HEADER.pack(MAGIC, VERSION, self.kind, self.sequence,
                           self.generated_ns, len(self.payload)) + self.payload

    @classmethod
    def decode(cls, raw):
        if not HEADER.size <= len(raw) <= MAX_DATAGRAM:
            raise ValueError("Invalid datagram size")
        magic, version, kind, seq, stamp, size = HEADER.unpack_from(raw)
        if magic != MAGIC or version != VERSION or size != len(raw) - HEADER.size:
            raise ValueError("Invalid protocol header")
        packet = cls(Kind(kind), seq, stamp, raw[HEADER.size:])
        if packet.kind in VIDEO_KINDS:
            packet.fragment()
        return packet

    def fragment(self):
        if len(self.payload) <= FRAGMENT.size:
            raise ValueError("Empty video fragment")
        frame_id, index, total, frame_bytes = FRAGMENT.unpack_from(self.payload)
        if not 0 < frame_bytes <= MAX_FRAME_BYTES:
            raise ValueError("Invalid frame size")
        data_count = fragment_count(frame_bytes)
        if not 0 <= index < total or not data_count <= total <= max(data_count, fec.MAX_BLOCKS):
            raise ValueError("Invalid fragment metadata")
        data = self.payload[FRAGMENT.size:]
        if index < data_count - 1:
            expected = MAX_FRAGMENT_DATA
        elif index == data_count - 1:
            expected = frame_bytes - index * MAX_FRAGMENT_DATA
        else:  # Parity blocks are as long as the first data block.
            expected = min(frame_bytes, MAX_FRAGMENT_DATA)
        if len(data) != expected:
            raise ValueError("Fragment length does not match its frame")
        return Fragment(frame_id, index, total, data_count, frame_bytes, data)

    def json(self):
        value = json.loads(self.payload)
        if not isinstance(value, dict):
            raise ValueError("Expected object")
        return value


class PacketFactory:
    def __init__(self):
        self.sequence = 0

    def packet(self, kind, payload, generated_ns=None):
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        return Packet(kind, self.sequence,
                      time.perf_counter_ns() if generated_ns is None else generated_ns, payload)

    def json(self, kind, value):
        return self.packet(kind, json.dumps(value, separators=(",", ":"),
                                            allow_nan=False).encode())

    def video(self, frame_id, jpeg, generated_ns=None, kind=Kind.VIDEO, parity=0):
        """Fragments of one JPEG, followed by `parity` FEC blocks."""
        if kind not in VIDEO_KINDS:
            raise ValueError("Not a video layer")
        if not 0 < len(jpeg) <= MAX_FRAME_BYTES:
            raise ValueError("Invalid frame size")
        blocks = [jpeg[i:i + MAX_FRAGMENT_DATA] for i in range(0, len(jpeg), MAX_FRAGMENT_DATA)]
        if parity:
            if len(blocks) + parity > fec.MAX_BLOCKS:
                raise ValueError("Too many FEC blocks")
            blocks += fec.parity(blocks, parity)
        stamp = time.perf_counter_ns() if generated_ns is None else generated_ns
        return [self.packet(kind, FRAGMENT.pack(frame_id, index, len(blocks), len(jpeg)) + block, stamp)
                for index, block in enumerate(blocks)]


@dataclass
class QueuedPacket:
    packet: Packet
    enqueued_ns: int
    sent_ns: int = 0
    mode: str = "fusion"
    ready_ns: int | None = None  # Earliest transmit time; arrival unless admitted later.

    def __post_init__(self):
        if self.ready_ns is None:
            self.ready_ns = self.enqueued_ns

    def age_ms(self, now_ns):
        return (now_ns - self.packet.generated_ns) / 1e6


class FrameAssembler:
    """Bounded, duplicate-tolerant assembly; timeout starts at first arrival.

    A frame completes as soon as any data_count of its fragments are in, with
    FEC parity filling gaps. Fragments of a finished frame (late parity once
    the data arrived, or anything after a timeout) are ignored.
    """
    def __init__(self, timeout_ms=2000, limit=32, on_drop=None, on_recover=None):
        self.timeout_ns = int(timeout_ms * 1e6)
        self.limit = limit
        self.frames = {}
        self.finished = {}  # Recently finished frame ids, oldest first.
        self.on_drop = on_drop or (lambda frame_id, reason: None)
        self.on_recover = on_recover or (lambda frame_id: None)

    def _finish(self, frame_id):
        self.frames.pop(frame_id, None)
        self.finished[frame_id] = None
        if len(self.finished) > 4 * self.limit:
            self.finished.pop(next(iter(self.finished)))

    def expire(self, now_ns):
        for frame_id, entry in list(self.frames.items()):
            if now_ns - entry["first"] > self.timeout_ns:
                self._finish(frame_id)
                self.on_drop(frame_id, "reassembly_timeout")

    def add(self, packet, now_ns):
        self.expire(now_ns)
        part = packet.fragment()
        frame_id = part.frame_id
        if frame_id in self.finished:
            return None
        if frame_id not in self.frames:
            if len(self.frames) >= self.limit:
                oldest = next(iter(self.frames))
                self._finish(oldest)
                self.on_drop(oldest, "reassembly_overflow")
            self.frames[frame_id] = {"first": now_ns, "stamp": packet.generated_ns,
                                     "layout": (part.total, part.frame_bytes), "parts": {}}
        entry = self.frames[frame_id]
        if (part.total, part.frame_bytes) != entry["layout"] or packet.generated_ns != entry["stamp"]:
            raise ValueError("Inconsistent frame metadata")
        known = entry["parts"].get(part.index)
        if known is not None:
            if known != part.data:
                raise ValueError("Conflicting duplicate fragment")
            return None
        entry["parts"][part.index] = part.data
        if len(entry["parts"]) < part.data_count:
            return None
        self._finish(frame_id)
        if any(i not in entry["parts"] for i in range(part.data_count)):
            self.on_recover(frame_id)
        jpeg = fec.reassemble(part.frame_bytes, part.data_count, entry["parts"])
        return frame_id, entry["stamp"], jpeg
