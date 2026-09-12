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


@dataclass
class AuthConfig:
    whitelist: Set[int] = field(default_factory=set)
    # Shared secret for HMAC-SHA256 truncated to 16 bytes. Tests use a fixed value;
    # production must inject from env/HSM — never commit real secrets.
    secret: bytes = b"nebula-mvp-dev-only-not-for-production"
    max_skew_ms: int = 2000
    # How many recent seq numbers to remember per node for replay detection.
    window: int = 64


class AuthError(ValueError):
    pass


class AuthGateway:
    def __init__(self, config: Optional[AuthConfig] = None):
        self.config = config or AuthConfig()
        # node_id -> set of recent seq, and last timestamp
        self._seen_seq: Dict[int, Set[int]] = {}
        self._last_ts: Dict[int, int] = {}

    def allow(self, node_id: int):
        self.config.whitelist.add(node_id)

    def revoke(self, node_id: int):
        self.config.whitelist.discard(node_id)
        self._seen_seq.pop(node_id, None)
        self._last_ts.pop(node_id, None)

    def _mac(self, node_id: int, seq: int, ts_ms: int, body: bytes) -> bytes:
        msg = struct.pack("!HIQ", node_id, seq, ts_ms) + body
        return hmac.new(self.config.secret, msg, hashlib.sha256).digest()[:16]

    def seal(self, node_id: int, seq: int, body: bytes,
             now_ms: Optional[int] = None) -> bytes:
        if self.config.whitelist and node_id not in self.config.whitelist:
            raise AuthError("node not on whitelist")
        ts = int(now_ms if now_ms is not None else time.time() * 1000)
        tag = self._mac(node_id, seq, ts, body)
        return body + AUTH_TRAILER.pack(node_id, seq, ts, tag)

    def open(self, blob: bytes, now_ms: Optional[int] = None) -> tuple:
        """Return (node_id, seq, body). Raises AuthError on failure."""
        if len(blob) < AUTH_SIZE:
            raise AuthError("truncated")
        body, trailer = blob[:-AUTH_SIZE], blob[-AUTH_SIZE:]
        node_id, seq, ts, tag = AUTH_TRAILER.unpack(trailer)
        if self.config.whitelist and node_id not in self.config.whitelist:
            raise AuthError("node not on whitelist")
        expect = self._mac(node_id, seq, ts, body)
        if not hmac.compare_digest(expect, tag):
            raise AuthError("bad mac")
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        if abs(now - ts) > self.config.max_skew_ms:
            raise AuthError("timestamp skew")
        seen = self._seen_seq.setdefault(node_id, set())
        if seq in seen:
            raise AuthError("replay")
        last = self._last_ts.get(node_id)
        if last is not None and ts + self.config.max_skew_ms < last:
            raise AuthError("timestamp rewind")
        seen.add(seq)
        if len(seen) > self.config.window:
            # Drop arbitrary old ids — window is a bound, not a strict bitmap.
            for old in list(seen)[: len(seen) - self.config.window]:
                seen.discard(old)
        self._last_ts[node_id] = max(ts, last or ts)
        return node_id, seq, body
