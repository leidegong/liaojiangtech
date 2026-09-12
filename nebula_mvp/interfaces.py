"""Interface codecs + failsafe state machine (no real hardware).

- SBUS 25-byte frame pack/unpack (16 channels)
- MAVLink-v1-style sequence gap monitoring (minimal header, not full dialect)
- Separate RC-loss vs data-link-loss timeouts and actions
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence, Tuple


SBUS_FRAME_LEN = 25
SBUS_HEADER = 0x0F
SBUS_FOOTER = 0x00
SBUS_CHANNELS = 16
SBUS_CH_MIN = 172
SBUS_CH_MAX = 1811
SBUS_CH_NEUTRAL = 992


def _clamp_channel(value: int) -> int:
    return max(SBUS_CH_MIN, min(SBUS_CH_MAX, int(value)))


def encode_sbus(channels: Sequence[int], digital1: bool = False,
                digital2: bool = False, frame_lost: bool = False,
                failsafe: bool = False) -> bytes:
    """Pack 16 channels into a 25-byte SBUS frame (packed 11-bit)."""
    if len(channels) != SBUS_CHANNELS:
        raise ValueError(f"Expected {SBUS_CHANNELS} channels")
    ch = [_clamp_channel(c) for c in channels]
    # Pack 16 × 11-bit into 22 bytes
    bitstream = 0
    for i, v in enumerate(ch):
        bitstream |= (v & 0x7FF) << (i * 11)
    payload = bytearray(22)
    for i in range(22):
        payload[i] = (bitstream >> (8 * i)) & 0xFF
    flags = ((1 if digital1 else 0)
             | ((1 if digital2 else 0) << 1)
             | ((1 if frame_lost else 0) << 2)
             | ((1 if failsafe else 0) << 3))
    return bytes([SBUS_HEADER]) + bytes(payload) + bytes([flags, SBUS_FOOTER])


def decode_sbus(frame: bytes) -> dict:
    if len(frame) != SBUS_FRAME_LEN:
        raise ValueError("SBUS frame must be 25 bytes")
    if frame[0] != SBUS_HEADER:
        raise ValueError("Bad SBUS header")
    if frame[24] != SBUS_FOOTER:
        raise ValueError("Bad SBUS footer")
    bitstream = 0
    for i in range(22):
        bitstream |= frame[1 + i] << (8 * i)
    channels = [(bitstream >> (11 * i)) & 0x7FF for i in range(SBUS_CHANNELS)]
    flags = frame[23]
    return {
        "channels": channels,
        "digital1": bool(flags & 0x01),
        "digital2": bool(flags & 0x02),
        "frame_lost": bool(flags & 0x04),
        "failsafe": bool(flags & 0x08),
    }


# --- MAVLink v1 minimal seq monitor (STX=0xFE) ---

MAVLINK_STX = 0xFE


def mavlink_v1_header(payload_len: int, seq: int, sysid: int, compid: int,
                      msgid: int) -> bytes:
    """Build MAVLink v1 header bytes (no checksum) for test streams."""
    if not 0 <= payload_len <= 255:
        raise ValueError("payload_len out of range")
    return bytes([MAVLINK_STX, payload_len & 0xFF, seq & 0xFF,
                  sysid & 0xFF, compid & 0xFF, msgid & 0xFF])


@dataclass
class MavlinkSeqMonitor:
    """Track per-(sys,comp) sequence; count gaps and duplicates."""
    expected: dict = field(default_factory=dict)  # (sys,comp) -> next seq
    gaps: int = 0
    duplicates: int = 0
    packets: int = 0

    def observe(self, raw: bytes) -> Optional[int]:
        """Return gap size if seq jumped; 0 if ok; None if not mavlink v1."""
        if len(raw) < 6 or raw[0] != MAVLINK_STX:
            return None
        seq, sysid, compid = raw[2], raw[3], raw[4]
        key = (sysid, compid)
        self.packets += 1
        if key not in self.expected:
            self.expected[key] = (seq + 1) & 0xFF
            return 0
        exp = self.expected[key]
        if seq == exp:
            self.expected[key] = (seq + 1) & 0xFF
            return 0
        if seq == ((exp - 1) & 0xFF):
            self.duplicates += 1
            return 0
        gap = (seq - exp) & 0xFF
        self.gaps += 1
        self.expected[key] = (seq + 1) & 0xFF
        return gap


class FailsafeAction(Enum):
    HOLD = "hold"
    NEUTRAL_RC = "neutral_rc"
    RTL = "rtl"
    LAND = "land"
    DISARM = "disarm"


@dataclass
class FailsafeConfig:
    rc_timeout_ms: float = 500.0
    data_timeout_ms: float = 2000.0
    rc_action: FailsafeAction = FailsafeAction.NEUTRAL_RC
    data_action: FailsafeAction = FailsafeAction.RTL


@dataclass
class FailsafeState:
    rc_lost: bool = False
    data_lost: bool = False
    active_actions: Tuple[FailsafeAction, ...] = ()
    last_rc_ms: float = 0.0
    last_data_ms: float = 0.0


class FailsafeMachine:
    """Independent RC-link and data-link loss timers."""

    def __init__(self, config: Optional[FailsafeConfig] = None):
        self.config = config or FailsafeConfig()
        self.state = FailsafeState()

    def note_rc(self, now_ms: float):
        self.state.last_rc_ms = now_ms
        self.state.rc_lost = False
        self._recompute()

    def note_data(self, now_ms: float):
        self.state.last_data_ms = now_ms
        self.state.data_lost = False
        self._recompute()

    def tick(self, now_ms: float) -> FailsafeState:
        cfg = self.config
        if self.state.last_rc_ms and now_ms - self.state.last_rc_ms > cfg.rc_timeout_ms:
            self.state.rc_lost = True
        if self.state.last_data_ms and now_ms - self.state.last_data_ms > cfg.data_timeout_ms:
            self.state.data_lost = True
        # If never received, treat as lost only after timeout from 0 — require priming.
        if self.state.last_rc_ms == 0 and now_ms > cfg.rc_timeout_ms:
            self.state.rc_lost = True
        if self.state.last_data_ms == 0 and now_ms > cfg.data_timeout_ms:
            self.state.data_lost = True
        self._recompute()
        return self.state

    def _recompute(self):
        actions = []
        if self.state.rc_lost:
            actions.append(self.config.rc_action)
        if self.state.data_lost:
            actions.append(self.config.data_action)
        # Deduplicate while preserving order
        seen = set()
        ordered = []
        for a in actions:
            if a not in seen:
                seen.add(a)
                ordered.append(a)
        self.state.active_actions = tuple(ordered)


def channels_neutral() -> List[int]:
    return [SBUS_CH_NEUTRAL] * SBUS_CHANNELS
