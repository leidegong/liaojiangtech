import asyncio
from collections import deque
from functools import partial
import time
from .control import Control
from .protocol import FrameAssembler, Kind, Packet, PacketFactory, VIDEO_KINDS
from .video import valid_jpeg

# How long a base frame may wait for the full frame of its capture: recent P95
# of how far full frames trailed their base, plus a margin, within these bounds.
WAIT_MIN_NS = 20_000_000
WAIT_MAX_NS = 150_000_000
WAIT_MARGIN_NS = 10_000_000
# Stop waiting once no full frame has arrived for this many captures.
ACTIVE_FRAMES = 8


class LayerSelector:
    """Decides when each reassembled video layer should become the picture.

    A base frame is parked until the full-resolution frame of the same capture
    arrives, which then replaces it unseen, so the picture does not flicker
    between layers. If that full frame does not arrive in time, the base is
    shown instead: a lost full frame costs sharpness, never motion. Bases go
    straight through while the full layer is paused or off (no full frame for
    the last ACTIVE_FRAMES captures) or chronically later than the longest wait.
    """
    def __init__(self):
        self.parked = {}  # frame id -> (generated_ns, jpeg, deadline_ns)
        self.base_seen = {}  # frame id -> reassembly time of recent bases
        self.lags = deque(maxlen=32)  # How long full frames trailed their base.
        self.newest_full = None

    def wait_ns(self):
        if not self.lags:
            return WAIT_MAX_NS
        ordered = sorted(self.lags)
        return min(WAIT_MAX_NS, max(WAIT_MIN_NS, ordered[round(.95 * (len(ordered) - 1))] + WAIT_MARGIN_NS))

    def _full_expected(self, frame_id):
        if self.newest_full is None or self.newest_full < frame_id - ACTIVE_FRAMES:
            return False
        return not self.lags or sorted(self.lags)[len(self.lags) // 2] <= WAIT_MAX_NS

    def arrive(self, kind, frame_id, generated_ns, jpeg, received_ns):
        """A layer finished reassembly. Returns (frames to show now, superseded
        base frame ids); frames are (kind, frame_id, generated_ns, jpeg)."""
        frame = (kind, frame_id, generated_ns, jpeg)
        if kind == Kind.VIDEO_BASE:
            if self.newest_full is not None and frame_id <= self.newest_full:
                return [], [frame_id]  # Its full frame, or a newer one, is already in.
            self.base_seen[frame_id] = received_ns
            if len(self.base_seen) > 4 * ACTIVE_FRAMES:
                self.base_seen.pop(next(iter(self.base_seen)))
            if self._full_expected(frame_id):
                self.parked[frame_id] = (generated_ns, jpeg, received_ns + self.wait_ns())
                return [], []
            return [frame], []
        self.newest_full = frame_id if self.newest_full is None else max(self.newest_full, frame_id)
        if frame_id in self.base_seen:
            self.lags.append(received_ns - self.base_seen[frame_id])
        superseded = sorted(fid for fid in self.parked if fid <= frame_id)
        for fid in superseded:
            del self.parked[fid]
        return [frame], superseded

    def next_deadline(self):
        return min((entry[2] for entry in self.parked.values()), default=None)

    def due(self, now_ns):
        """Parked bases whose wait ran out, oldest capture first."""
        ready = sorted(fid for fid, entry in self.parked.items() if entry[2] <= now_ns)
        return [(Kind.VIDEO_BASE, fid, *self.parked.pop(fid)[:2]) for fid in ready]


class GroundNode(asyncio.DatagramProtocol):
    def __init__(self, link, metrics):
        self.link = link
        self.metrics = metrics
        self.transport = None
        self.link_address = None
        self.factory = PacketFactory()
        self.desired = Control()
        self.telemetry = {}
        self.telemetry_received_ns = 0
        self.telemetry_generated_ns = 0
        self.assemblers = {kind: FrameAssembler(on_drop=partial(metrics.frame_drop, kind),
                                                on_recover=partial(metrics.frame_recovered, kind))
                           for kind in VIDEO_KINDS}
        self.selector = LayerSelector()
        self.jpeg = None
        self.frame_id = 0
        self.frame_kind = None
        self.frame_size = None
        self.frame_generated_ns = 0
        self.decode_queue = asyncio.Queue(maxsize=4)
        self.last_input_ns = 0

    def connection_made(self, transport):
        self.transport = transport

    def set_control(self, value):
        self.desired = Control.parse(value)
        self.last_input_ns = time.perf_counter_ns()

    def datagram_received(self, raw, address):
        now = time.perf_counter_ns()
        try:
            if address != self.link_address:
                raise ValueError("Unexpected sender")
            packet = Packet.decode(raw)
            if packet.kind == Kind.DATA:
                state = packet.json()
                if packet.generated_ns > self.telemetry_generated_ns:
                    self.telemetry = state
                    self.telemetry_received_ns = now
                    self.telemetry_generated_ns = packet.generated_ns
            elif packet.kind in VIDEO_KINDS:
                result = self.assemblers[packet.kind].add(packet, now)
                if result:
                    if self.decode_queue.full():
                        old = self.decode_queue.get_nowait()
                        self.metrics.frame_drop(old[0], old[1], "decode_queue_overflow")
                    self.decode_queue.put_nowait((packet.kind, *result, now))
            else:
                raise ValueError("Unexpected direction")
        except (ValueError, UnicodeError, TypeError):
            self.metrics.counts["invalid_packets"] += 1
            return
        self.link.received(packet, now)

    def rejection(self, kind, frame_id):
        """Why a layer must not replace the current picture, or None.

        The picture never goes back in time. A full-resolution frame may upgrade
        the base picture of the same capture.
        """
        if frame_id < self.frame_id:
            return "video_out_of_order"
        if frame_id == self.frame_id and (kind == Kind.VIDEO_BASE or self.frame_kind == Kind.VIDEO):
            return "video_out_of_order"
        return None

    async def _next_decoded(self):
        """The next reassembled layer, or None when a parked base falls due first."""
        deadline = self.selector.next_deadline()
        if deadline is None:
            return await self.decode_queue.get()
        timeout = (deadline - time.perf_counter_ns()) / 1e9
        if timeout <= 0:
            return None if self.decode_queue.empty() else self.decode_queue.get_nowait()
        try:
            return await asyncio.wait_for(self.decode_queue.get(), timeout)
        except TimeoutError:
            return None

    async def decode_loop(self):
        while True:
            item = await self._next_decoded()
            show = []
            if item:
                show, superseded = self.selector.arrive(*item)
                for frame_id in superseded:
                    self.metrics.frame_drop(Kind.VIDEO_BASE, frame_id, "base_superseded")
            now = time.perf_counter_ns()
            for frame in show + self.selector.due(now):
                await self._show(*frame, now)

    async def _show(self, kind, frame_id, generated_ns, jpeg, decided_ns):
        reason = self.rejection(kind, frame_id)
        if reason:
            self.metrics.frame_drop(kind, frame_id, reason)
            return
        size = await asyncio.to_thread(valid_jpeg, jpeg)
        if not size:
            self.metrics.frame_drop(kind, frame_id, "invalid_jpeg")
            return
        self.jpeg = jpeg
        self.frame_id = frame_id
        self.frame_kind = kind
        self.frame_size = size
        self.frame_generated_ns = generated_ns
        # Latency runs to when the layer became the picture, so a base shown after
        # waiting for its full frame carries that wait.
        self.metrics.frame_received(kind, frame_id, generated_ns, decided_ns)

    async def control_loop(self):
        deadline = time.perf_counter()
        while True:
            if time.perf_counter_ns() - self.last_input_ns > 1_000_000_000:
                self.desired = Control()  # Browser closed / lost connection.
            packet = self.factory.json(Kind.CONTROL, self.desired.public())
            self.transport.sendto(packet.encode(), self.link_address)
            self.metrics.counts["control_generated"] += 1
            deadline += .02
            await asyncio.sleep(max(0, deadline - time.perf_counter()))
            if time.perf_counter() - deadline > .02:
                deadline = time.perf_counter()
