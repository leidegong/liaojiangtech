from collections import deque
from dataclasses import dataclass
import math
from .protocol import Kind, VIDEO_KINDS


@dataclass
class VideoFrame:
    frame_id: int
    packets: deque


class Scheduler:
    """The link asks for the next packet only once it is free, so a low-priority
    packet is never reserved while a higher-priority one could still arrive.

    Fusion order: newest control > telemetry > base video > full video. Each video
    layer has its own frame queue, so a slow full-resolution frame never blocks
    the base layer.
    """
    def __init__(self, config, on_drop):
        self.config = config
        self.on_drop = on_drop
        self.control = None
        self.data = deque()
        self.video = {kind: deque() for kind in VIDEO_KINDS}
        self.active = dict.fromkeys(VIDEO_KINDS)
        self.fifo = deque()
        self.fifo_bytes = 0

    def enqueue(self, item):
        if self.config.mode == "naive":
            if self.fifo_bytes + item.packet.wire_bytes > self.config.fifo_byte_limit:
                self.on_drop(item, "fifo_overflow")
                return
            self.fifo.append(item)
            self.fifo_bytes += item.packet.wire_bytes
        elif item.packet.kind == Kind.CONTROL:
            if self.control:
                self.on_drop(self.control, "control_replaced")
            self.control = item
        elif item.packet.kind == Kind.DATA:
            if len(self.data) >= self.config.data_queue_limit:
                self.on_drop(self.data.popleft(), "data_overflow")
            self.data.append(item)
        else:
            raise ValueError("Fusion video must be admitted as a complete frame")

    def enqueue_frame(self, frame_id, packets):
        if self.config.mode == "naive":
            for item in packets:
                self.enqueue(item)
            return
        lane = self.video[packets[0].packet.kind]
        while len(lane) >= self.config.video_pending_limit:
            self._drop_frame(lane.popleft(), "video_overflow")
        lane.append(VideoFrame(frame_id, deque(packets)))

    def _drop_frame(self, frame, reason):
        for item in frame.packets:
            self.on_drop(item, reason)

    def peek(self, now_ns):
        if self.config.mode == "naive":
            return self.fifo[0] if self.fifo else None
        if self.control and self.control.age_ms(now_ns) > self.config.control_ttl_ms:
            self.on_drop(self.control, "control_ttl")
            self.control = None
        while self.data and self.data[0].age_ms(now_ns) > self.config.data_ttl_ms:
            self.on_drop(self.data.popleft(), "data_ttl")
        for kind in VIDEO_KINDS:
            lane = self.video[kind]
            while lane and lane[0].packets[0].age_ms(now_ns) > self.config.video_wait_ttl_ms:
                self._drop_frame(lane.popleft(), "video_wait_ttl")
            active = self.active[kind]
            if active and active.packets[0].age_ms(now_ns) > self.config.video_active_ttl_ms:
                self._drop_frame(active, "video_active_ttl")
                self.active[kind] = None
        if self.control:
            return self.control
        if self.data:
            return self.data[0]
        # Protect each layer's in-progress frame from overflow eviction. Higher
        # priority packets, the base layer included, still preempt it between
        # individual video fragments.
        for kind in VIDEO_KINDS:
            if self.active[kind]:
                return self.active[kind].packets[0]
            if self.video[kind]:
                return self.video[kind][0].packets[0]
        return None

    def pop(self, now_ns):
        item = self.peek(now_ns)
        if item is None:
            return None
        if self.config.mode == "naive":
            self.fifo_bytes -= item.packet.wire_bytes
            return self.fifo.popleft()
        kind = item.packet.kind
        if kind == Kind.CONTROL:
            self.control = None
        elif kind == Kind.DATA:
            self.data.popleft()
        else:
            if self.active[kind] is None:
                self.active[kind] = self.video[kind].popleft()
            self.active[kind].packets.popleft()
            if not self.active[kind].packets:
                self.active[kind] = None
        return item

    def clear(self, reason="mode_change"):
        if self.control:
            self.on_drop(self.control, reason)
        for item in self.data:
            self.on_drop(item, reason)
        for item in self.fifo:
            self.on_drop(item, reason)
        for kind in VIDEO_KINDS:
            for frame in self.video[kind]:
                self._drop_frame(frame, reason)
            if self.active[kind]:
                self._drop_frame(self.active[kind], reason)
            self.video[kind].clear()
            self.active[kind] = None
        self.control = None
        self.data.clear()
        self.fifo.clear()
        self.fifo_bytes = 0

    def snapshot(self):
        frames = {kind: len(self.video[kind]) + int(self.active[kind] is not None)
                  for kind in VIDEO_KINDS}
        return {"control": int(self.control is not None), "data": len(self.data),
                "video_frames": frames[Kind.VIDEO], "base_frames": frames[Kind.VIDEO_BASE],
                "fifo_packets": len(self.fifo), "fifo_bytes": self.fifo_bytes}


class LinkClock:
    """Virtual transmit timeline of one serial link, in integer nanoseconds.

    A packet starts once the link is free and the packet is ready, then holds
    the link for exactly its serialization time. Waking up late (Windows sleeps
    overshoot by about 1 ms) therefore delays when a packet is handed over but
    no longer shrinks the long-run rate. The timeline may trail the wall clock
    by at most max_lag_ns; a longer host stall is lost airtime, not replayed as
    a burst, and is returned so it can be reported.
    """
    def __init__(self, max_lag_ns):
        self.max_lag_ns = max_lag_ns
        self.free_at = None

    def transmit(self, ready_ns, now_ns, duration_ns):
        """Place one packet on the timeline. Returns (start, finish, lost)."""
        earliest = ready_ns if self.free_at is None else max(self.free_at, ready_ns)
        start = max(earliest, now_ns - self.max_lag_ns)
        self.free_at = start + duration_ns
        return start, self.free_at, start - earliest


class RateMeter:
    """Exponentially decayed byte counter; rate() approximates bytes/s over ~tau."""
    def __init__(self, tau=1.0):
        self.tau = tau
        self.total = 0.0
        self.last = None

    def _decay(self, now):
        if self.last is not None:
            self.total *= math.exp(-max(0.0, now - self.last) / self.tau)
        self.last = now

    def add(self, size, now):
        self._decay(now)
        self.total += size

    def rate(self, now):
        self._decay(now)
        return self.total / self.tau
