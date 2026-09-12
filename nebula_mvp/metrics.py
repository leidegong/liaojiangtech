from collections import Counter, deque
import csv
from pathlib import Path
import time
from .protocol import Kind, VIDEO_KINDS


def distribution(values):
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    ordered = sorted(values)

    def percentile(p):
        pos = (len(ordered) - 1) * p
        lower = int(pos)
        upper = min(lower + 1, len(ordered) - 1)
        return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (pos - lower), 3)

    return {"count": len(values), "mean": round(sum(values) / len(values), 3),
            "p50": percentile(.5), "p95": percentile(.95), "p99": percentile(.99),
            "max": round(ordered[-1], 3)}


class Metrics:
    def __init__(self, output_dir=None):
        self.started_ns = time.perf_counter_ns()
        self.counts = Counter()
        self.drops = Counter()
        self.latencies = {kind.name: deque(maxlen=50000) for kind in Kind}
        self.queues = {kind.name: deque(maxlen=50000) for kind in Kind}
        self.tx = deque(maxlen=50000)
        self.frame_arrivals = deque(maxlen=50000)  # (shown_ns, latency_ms, kind), first display per capture
        self.offered = deque(maxlen=10000)
        self.pacing = deque(maxlen=50000)  # (start_ns, airtime_ns, lost_ns) per transmitted packet
        self.lateness = deque(maxlen=50000)  # (fired_ns, ms after the modelled delivery time)
        self.layers = {}  # (kind, frame_id) -> [stamp, "pending" | "shown" | "dropped", fragments missing]
        self.frames = {}  # frame_id -> [stamp, layers expected, layers settled, shown]
        self.file = None
        if output_dir:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            self.file = (Path(output_dir) / "packets.csv").open("w", newline="", encoding="utf-8")
            self.writer = csv.writer(self.file)
            self.writer.writerow(["mode", "type", "sequence", "frame_id", "wire_bytes",
                                  "generation_ns", "enqueue_ns", "send_ns", "receive_ns",
                                  "queue_ms", "e2e_ms", "outcome"])

    def generated_frame(self, kind, frame_id, stamp, jpeg_bytes):
        self.counts[kind.name.lower() + "_frames_generated"] += 1
        self.layers[(kind, frame_id)] = [stamp, "pending", 0]
        frame = self.frames.get(frame_id)
        if frame is None:
            frame = self.frames[frame_id] = [stamp, 0, 0, False]
            self.counts["frames_generated"] += 1
        frame[1] += 1
        self.offered.append((stamp, kind, jpeg_bytes))

    def _settle(self, kind, frame_id, shown):
        """Settle one layer once. Returns None if already settled, else whether
        this is the first layer of its frame to be shown. A frame counts as
        dropped only when every layer settled without being shown."""
        layer = self.layers.get((kind, frame_id))
        if not layer or layer[1] != "pending":
            return None
        layer[1] = "shown" if shown else "dropped"
        frame = self.frames[frame_id]
        frame[2] += 1
        first = shown and not frame[3]
        if first:
            frame[3] = True
            self.counts["frames_received"] += 1
        elif frame[2] == frame[1] and not frame[3]:
            self.counts["frames_dropped"] += 1
        return first

    def frame_drop(self, kind, frame_id, reason):
        if self._settle(kind, frame_id, False) is not None:
            self.drops[("base:" if kind == Kind.VIDEO_BASE else "frame:") + reason] += 1

    def frame_recovered(self, kind, frame_id):
        self.counts[kind.name.lower() + "_frames_recovered"] += 1

    def frame_received(self, kind, frame_id, generated_ns, shown_ns):
        """A layer became the picture at shown_ns."""
        first = self._settle(kind, frame_id, True)
        if first is None:
            return
        self.counts[kind.name.lower() + "_frames_shown"] += 1
        if first:
            self.frame_arrivals.append((shown_ns, (shown_ns - generated_ns) / 1e6, kind))
        else:  # A late full frame replaced the base already shown for its capture.
            self.counts["video_upgrades"] += 1

    def paced(self, start_ns, airtime_ns, lost_ns):
        self.pacing.append((start_ns, airtime_ns, lost_ns))

    def dispatched(self, due_ns, fired_ns):
        self.lateness.append((fired_ns, (fired_ns - due_ns) / 1e6))

    def sent(self, item, now_ns):
        item.sent_ns = now_ns
        self.counts["sent"] += 1
        self.counts["wire_bytes"] += item.packet.wire_bytes
        self.tx.append((now_ns, item.packet.kind, item.packet.wire_bytes))
        self.queues[item.packet.kind.name].append((now_ns, (now_ns - item.enqueued_ns) / 1e6))

    def received(self, item, now_ns):
        self.counts["received"] += 1
        self.counts[item.packet.kind.name.lower() + "_received"] += 1
        self.latencies[item.packet.kind.name].append((now_ns, (now_ns - item.packet.generated_ns) / 1e6))
        self._record(item, "received", now_ns)

    def drop(self, item, reason):
        self.counts["packets_dropped"] += 1
        self.drops[reason] += 1
        if item.packet.kind in VIDEO_KINDS:
            part = item.packet.fragment()
            layer = self.layers.get((item.packet.kind, part.frame_id))
            if layer is not None:
                layer[2] += 1
                # A frame is lost only once more fragments are gone than it has parity.
                if layer[2] > part.total - part.data_count:
                    self.frame_drop(item.packet.kind, part.frame_id, reason)
        self._record(item, reason)

    def _record(self, item, outcome, received_ns=0):
        if self.file:
            p = item.packet
            self.writer.writerow([item.mode, p.kind.name, p.sequence,
                                  p.fragment()[0] if p.kind in VIDEO_KINDS else "", p.wire_bytes,
                                  p.generated_ns, item.enqueued_ns, item.sent_ns or "", received_ns or "",
                                  (item.sent_ns - item.enqueued_ns) / 1e6 if item.sent_ns else "",
                                  (received_ns - p.generated_ns) / 1e6 if received_ns else "", outcome])

    def snapshot(self, window=5.0):
        now = time.perf_counter_ns()
        cutoff = now - int(window * 1e9)
        seconds = max(.05, min(window, (now - self.started_ns) / 1e9))
        tx = [row for row in self.tx if row[0] >= cutoff]
        arrivals, shown_ns, previous_ns = [], [], None
        for stamp, latency, kind in self.frame_arrivals:
            if stamp >= cutoff:
                arrivals.append((latency, kind))
                shown_ns.append(stamp)
            else:
                previous_ns = stamp
        # Longest time the picture stood still, including an ongoing freeze.
        edges = ([previous_ns] if previous_ns is not None else []) + shown_ns + [now]
        max_gap = max((b - a for a, b in zip(edges, edges[1:])), default=None)
        airtime = lost = 0
        for stamp, used, missed in self.pacing:
            if stamp >= cutoff:
                airtime += used
                lost += missed
        finalized = self.counts["frames_received"] + self.counts["frames_dropped"]
        return {"elapsed_s": round((now - self.started_ns) / 1e9, 2), "window_s": window,
                "counts": dict(self.counts), "drops": dict(self.drops),
                "latency_ms": {k: distribution([v for stamp, v in data if stamp >= cutoff])
                               for k, data in self.latencies.items()},
                "queue_ms": {k: distribution([v for stamp, v in data if stamp >= cutoff])
                             for k, data in self.queues.items()},
                "wire_mbps": sum(row[2] for row in tx) * 8 / seconds / 1e6,
                "video_mbps": sum(row[2] for row in tx if row[1] in VIDEO_KINDS) * 8 / seconds / 1e6,
                "base_mbps": sum(row[2] for row in tx if row[1] == Kind.VIDEO_BASE) * 8 / seconds / 1e6,
                "video_offered_mbps": sum(size for stamp, _, size in self.offered if stamp >= cutoff) * 8 / seconds / 1e6,
                "video_fps": len(arrivals) / seconds,
                "video_latency_ms": distribution([latency for latency, _ in arrivals]),
                "video_full_share": (sum(kind == Kind.VIDEO for _, kind in arrivals) / len(arrivals)
                                     if arrivals else None),
                "video_max_gap_ms": round(max_gap / 1e6, 1) if max_gap is not None else None,
                # Share of transmit time not lost to host stalls; 1.0 means the
                # link ran at exactly its configured rate whenever it had traffic.
                "link_pacing_efficiency": (round(airtime / (airtime + lost), 4)
                                           if airtime else None),
                "link_dispatch_late_ms": distribution(
                    [late for stamp, late in self.lateness if stamp >= cutoff]),
                "frame_drop_rate": self.counts["frames_dropped"] / finalized if finalized else 0,
                "frames_pending": sum(frame[2] < frame[1] for frame in self.frames.values())}

    def housekeeping(self):
        if self.file:
            self.file.flush()
        cutoff = time.perf_counter_ns() - 30_000_000_000
        for frame_id, (stamp, expected, settled, _) in list(self.frames.items()):
            if stamp < cutoff and settled >= expected:
                self.frames.pop(frame_id)
                for kind in VIDEO_KINDS:
                    self.layers.pop((kind, frame_id), None)

    def close(self):
        if self.file:
            self.file.close()
