"""Join whitelist + timestamp/sequence anti-replay for MVP datagrams."""
from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import hashlib
import struct
import time
from typing import Dict, Optional, Set


# node_id(u16) + seq(u32) + timestamp_ms(u64) + mac(16)
AUTH_TRAILER = struct.Struct("!HIQ16s")
AUTH_SIZE = AUTH_TRAILER.size
SEQ_MASK = 0xFFFFFFFF


@dataclass
class AuthConfig:
    whitelist: Set[int] = field(default_factory=set)
    # Shared secret for HMAC-SHA256 truncated to 16 bytes. Tests use a fixed value;
    # production must inject from env/HSM — never commit real secrets.
    secret: bytes = b"nebula-mvp-dev-only-not-for-production"
    max_skew_ms: int = 2000
    # Sliding window size (highest seq + bitmap of recent accepts).
    window: int = 64


class AuthError(ValueError):
    pass


class _ReplayWindow:
    """Highest-seen sequence plus a bitmap of the last `size` sequence numbers.

    Bit 0 marks `highest`; bit i marks `highest - i`. Packets below the lower
    bound are rejected; duplicates inside the window are rejected. A jump ahead
    larger than the window resets the bitmap to only the new sequence (catch-up
    after loss). `AuthGateway.begin_session` increments the MAC epoch so
    previously sealed packets cannot be replayed after a reset.
    """

    def __init__(self, size: int):
        self.size = size
        self.highest: Optional[int] = None
        self.bitmap: int = 0

    def accept(self, seq: int):
        seq &= SEQ_MASK
        if self.highest is None:
            self.highest = seq
            self.bitmap = 1
            return
        ahead = (seq - self.highest) & SEQ_MASK
        if 0 < ahead < (SEQ_MASK // 2):
            if ahead >= self.size:
                self.highest = seq
                self.bitmap = 1
            else:
                self.bitmap = ((self.bitmap << ahead) | 1) & ((1 << self.size) - 1)
                self.highest = seq
            return
        if ahead == 0:
            raise AuthError("replay")
        back = (self.highest - seq) & SEQ_MASK
        if back >= self.size or back >= (SEQ_MASK // 2):
            raise AuthError("replay")
        bit = 1 << back
        if self.bitmap & bit:
            raise AuthError("replay")
        self.bitmap |= bit


class AuthGateway:
    def __init__(self, config: Optional[AuthConfig] = None):
        self.config = config or AuthConfig()
        self._windows: Dict[int, _ReplayWindow] = {}
        self._last_ts: Dict[int, int] = {}
        self._epoch: Dict[int, int] = {}

    def allow(self, node_id: int):
        self.config.whitelist.add(node_id)

    def revoke(self, node_id: int):
        self.config.whitelist.discard(node_id)
        self._windows.pop(node_id, None)
        self._last_ts.pop(node_id, None)
        self._epoch.pop(node_id, None)

    def begin_session(self, node_id: int):
        """Start a new session after reboot / rekey.

        Increments the per-node epoch bound into the MAC so previously sealed
        packets fail even if their timestamp is still inside the skew window.
        Does not accept old-session bags after a reset.
        """
        self._windows.pop(node_id, None)
        self._last_ts.pop(node_id, None)
        self._epoch[node_id] = self._epoch.get(node_id, 0) + 1

    def _mac(self, node_id: int, seq: int, ts_ms: int, body: bytes,
             epoch: Optional[int] = None) -> bytes:
        if epoch is None:
            epoch = self._epoch.get(node_id, 0)
        msg = struct.pack("!HIQI", node_id, seq, ts_ms, epoch) + body
        return hmac.new(self.config.secret, msg, hashlib.sha256).digest()[:16]

    def seal(self, node_id: int, seq: int, body: bytes,
             now_ms: Optional[int] = None) -> bytes:
        if self.config.whitelist and node_id not in self.config.whitelist:
            raise AuthError("node not on whitelist")
        ts = int(now_ms if now_ms is not None else time.time() * 1000)
        tag = self._mac(node_id, seq, ts, body)
        return body + AUTH_TRAILER.pack(node_id, seq & SEQ_MASK, ts, tag)

    def open(self, blob: bytes, now_ms: Optional[int] = None) -> tuple:
        """Return (node_id, seq, body). Raises AuthError on failure."""
        if len(blob) < AUTH_SIZE:
            raise AuthError("truncated")
        body, trailer = blob[:-AUTH_SIZE], blob[-AUTH_SIZE:]
        node_id, seq, ts, tag = AUTH_TRAILER.unpack(trailer)
        if self.config.whitelist and node_id not in self.config.whitelist:
            raise AuthError("node not on whitelist")
        epoch = self._epoch.get(node_id, 0)
        expect = self._mac(node_id, seq, ts, body, epoch)
        if not hmac.compare_digest(expect, tag):
            for old_epoch in range(epoch):
                if hmac.compare_digest(self._mac(node_id, seq, ts, body, old_epoch), tag):
                    raise AuthError("stale session")
            raise AuthError("bad mac")
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        if abs(now - ts) > self.config.max_skew_ms:
            raise AuthError("timestamp skew")
        last = self._last_ts.get(node_id)
        if last is not None and ts + self.config.max_skew_ms < last:
            raise AuthError("timestamp rewind")
        window = self._windows.setdefault(node_id, _ReplayWindow(self.config.window))
        window.accept(seq)
        self._last_ts[node_id] = max(ts, last or ts)
        return node_id, seq, body
