"""One shared half-duplex bottleneck, used by BOTH traffic directions."""
import asyncio
import random
import time
from .protocol import Kind, Packet, QueuedPacket, VIDEO_KINDS
from .scheduler import LinkClock, RateMeter, Scheduler

# Share of the spare capacity a full-resolution frame may plan to use, leaving
# room for telemetry bursts and jitter so frames finish within one interval.
FULL_LAYER_HEADROOM = 0.85
# How far the link's virtual timeline may trail the wall clock. Late wake-ups
# within this are caught up exactly; longer host stalls count as lost airtime.
MAX_TIMELINE_LAG_NS = 20_000_000
# Loss is estimated over both windows and the worse one is used: it rises
# within a fraction of a second when loss starts, but a lucky streak on the
# short window does not strip FEC from frames while the long one remembers.
LOSS_WINDOWS_S = (0.5, 2.0)


class LinkEmulator(asyncio.DatagramProtocol):
    def __init__(self, config, metrics, seed=7):
        self.config = config
        self.metrics = metrics
        self.scheduler = Scheduler(config, metrics.drop)
        self.clock = LinkClock(MAX_TIMELINE_LAG_NS)
        # Airtime used by everything the full-resolution layer must yield to.
        self.priority_rate = RateMeter()
        # Rate full-resolution frames actually achieved on the air. A nominal
        # rate can overestimate goodput (on a real link: retransmissions and MAC
        # overhead), so the budget also follows this measurement.
        self.full_rate = None
        self.full_sending = None  # [frame_id, start_s, wire_bytes] of the frame on the air
        # Recent packet loss over all traffic, as a MAC would learn from
        # acknowledgements, as (sent, lost) meters over a short and a long window.
        self.loss_windows = [(RateMeter(tau), RateMeter(tau)) for tau in LOSS_WINDOWS_S]
        self.channel_bad = False  # State of the two-state burst-loss channel.
        self.last_lost = False
        self.rng = random.Random(seed)
        self.ready = asyncio.Event()
        self.transport = None
        self.air_address = self.ground_address = None
        self.partial = {}
        self.inflight = {}
        self.handles = set()
        self.task = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, raw, address):
        now = time.perf_counter_ns()
        try:
            packet = Packet.decode(raw)
            expected = self.ground_address if packet.kind == Kind.CONTROL else self.air_address
            if address != expected:
                raise ValueError("Unexpected sender")
        except (ValueError, TypeError):
            self.metrics.counts["invalid_packets"] += 1
            return
        self.metrics.counts["enqueued"] += 1
        item = QueuedPacket(packet, now, mode=self.config.mode)
        if self.config.mode == "fusion" and packet.kind in VIDEO_KINDS:
            self._admit_video(item)
        else:
            self.scheduler.enqueue(item)
        self.ready.set()

    def _admit_video(self, item):
        part = item.packet.fragment()
        # Both layers of one capture share the frame id. A frame is admitted once
        # all its fragments, FEC parity included, are in.
        fid, index, count = part.frame_id, part.index, part.total
        key = (item.packet.kind, fid)
        if key not in self.partial:
            if len(self.partial) >= 8:
                self._drop_partial(next(iter(self.partial)), "ingress_overflow")
            self.partial[key] = {"first": item.enqueued_ns, "count": count,
                                 "stamp": item.packet.generated_ns, "parts": {}}
        entry = self.partial[key]
        if entry["count"] != count or entry["stamp"] != item.packet.generated_ns:
            self.metrics.drop(item, "invalid_fragment")
            return
        if index in entry["parts"]:
            self.metrics.drop(item, "duplicate_ingress")
            return
        entry["parts"][index] = item
        if len(entry["parts"]) == count:
            self.partial.pop(key)
            parts = [entry["parts"][i] for i in range(count)]
            for part in parts:
                part.ready_ns = item.enqueued_ns  # Admitted once the whole frame is in.
            self.scheduler.enqueue_frame(fid, parts)

    def _drop_partial(self, key, reason):
        for item in self.partial.pop(key)["parts"].values():
            self.metrics.drop(item, reason)

    def update(self, values):
        config = self.config.updated(values)
        if config.mode != self.config.mode:
            self.scheduler.clear()
            for key in list(self.partial):
                self._drop_partial(key, "mode_change")
        if (config.capacity_bps, config.mode) != (self.config.capacity_bps, self.config.mode):
            self.full_rate = None  # Measured under another rate or scheduler; no longer applies.
        if (config.loss, config.loss_burst) != (self.config.loss, self.config.loss_burst):
            self.channel_bad = False  # A new channel starts outside a burst.
        self.config = self.scheduler.config = config
        self.ready.set()

    def full_layer_budget(self, interval_s):
        """Link bytes one full-resolution frame may use per frame interval.

        Models a self-built MAC that knows its current rate, per-class load and
        the goodput its lowest class achieved. Returns None in FIFO mode: a
        black-box FIFO modem exposes no such accounting to budget against.
        """
        if self.config.mode != "fusion":
            return None
        spare = self.config.capacity_bps / 8 - self.priority_rate.rate(time.perf_counter())
        if self.full_rate is not None:
            spare = min(spare, self.full_rate)
        return max(0.0, spare * interval_s * FULL_LAYER_HEADROOM)

    def channel_loss(self):
        """Whether the next packet is lost, from a two-state (Gilbert) channel.

        Packets are lost while the channel is bad. It turns bad with probability
        loss * r / (1 - loss) and recovers with r = 1 / loss_burst per packet, so
        the long-run loss is `loss` and runs of lost packets average `loss_burst`.
        A mean run of 1 / (1 - loss) or less is independent loss, drawn exactly
        as the model without bursts did, so those runs stay reproducible.
        """
        loss = self.config.loss
        if loss <= 0:
            return False
        if self.config.loss_burst <= 1 / (1 - loss):
            return self.rng.random() < loss
        recover = 1 / self.config.loss_burst
        if self.channel_bad:
            self.channel_bad = self.rng.random() >= recover
        else:
            self.channel_bad = self.rng.random() < loss * recover / (1 - loss)
        return self.channel_bad

    def observe_loss(self, lost, now):
        for sent, dropped in self.loss_windows:
            sent.add(1, now)
            if lost:
                dropped.add(1, now)

    def loss_estimate(self):
        """Recent packet loss ratio, the worse of the two windows. None in FIFO
        mode, like the budget: a black-box modem reports nothing to size FEC with."""
        if self.config.mode != "fusion":
            return None
        now = time.perf_counter()
        ratios = [dropped.rate(now) / total for sent, dropped in self.loss_windows
                  if (total := sent.rate(now)) > 0]
        return max(ratios, default=0.0)

    def _measure_full(self, item, now, serialization):
        """Rate one full-resolution frame got while on the air, preemptions included."""
        part = item.packet.fragment()
        fid, index, count = part.frame_id, part.index, part.total
        if index == 0 or not self.full_sending or self.full_sending[0] != fid:
            self.full_sending = [fid, now, 0]
        self.full_sending[2] += item.packet.wire_bytes
        if index == count - 1:
            _, start, size = self.full_sending
            self.full_sending = None
            if count > 1:
                rate = size / (now + serialization - start)
                self.full_rate = rate if self.full_rate is None else .7 * self.full_rate + .3 * rate

    async def run(self):
        loop = asyncio.get_running_loop()
        while True:
            self.ready.clear()
            now_ns = time.perf_counter_ns()
            # Selection happens only once the link is free, so a higher-priority
            # packet that arrived during the previous transmission wins the slot.
            item = self.scheduler.pop(now_ns)
            if item is None:
                await self.ready.wait()
                continue
            # Non-preemptive serialization on the virtual timeline. Propagation may
            # overlap later transmissions, but air->ground and ground->air never
            # serialize together.
            serialization = item.packet.wire_bytes * 8 / self.config.capacity_bps
            start, finish, lost = self.clock.transmit(item.ready_ns, now_ns, round(serialization * 1e9))
            self.metrics.sent(item, start)
            self.metrics.paced(start, finish - start, lost)
            if item.packet.kind == Kind.VIDEO:
                self._measure_full(item, start / 1e9, serialization)
            else:
                self.priority_rate.add(item.packet.wire_bytes, start / 1e9)
            delay = max(0, self.config.delay_ms + self.rng.uniform(-self.config.jitter_ms,
                                                                 self.config.jitter_ms)) / 1000
            lost = self.channel_loss()
            if lost and not self.last_lost:
                self.metrics.counts["link_loss_runs"] += 1
            self.last_lost = lost
            self.observe_loss(lost, start / 1e9)
            if lost:
                self.metrics.drop(item, "link_loss")  # Lost packets still consume airtime.
            else:
                key = (item.packet.kind, item.packet.sequence)
                self.inflight[key] = item
                due = finish + round(delay * 1e9)
                # Each callback needs its own holder (avoid closing over a loop cell).
                handle_box = [None]
                def dispatch(item=item, key=key, box=handle_box, due=due):
                    self.handles.discard(box[0])
                    self.metrics.dispatched(due, time.perf_counter_ns())
                    target = self.air_address if item.packet.kind == Kind.CONTROL else self.ground_address
                    self.transport.sendto(item.packet.encode(), target)
                handle = loop.call_later((due - time.perf_counter_ns()) / 1e9, dispatch)
                handle_box[0] = handle
                self.handles.add(handle)
            # Sleep until the virtual finish, not for a fixed duration: an
            # overslept wake-up is absorbed by the timeline instead of idling it.
            await asyncio.sleep(max(0.0, (finish - time.perf_counter_ns()) / 1e9))

    def received(self, packet, now_ns):
        item = self.inflight.pop((packet.kind, packet.sequence), None)
        if item:
            self.metrics.received(item, now_ns)

    def housekeeping(self):
        now = time.perf_counter_ns()
        for key, entry in list(self.partial.items()):
            if now - entry["first"] > 1_000_000_000:
                self._drop_partial(key, "ingress_timeout")
        for key, item in list(self.inflight.items()):
            if now - item.sent_ns > 3_000_000_000:
                self.inflight.pop(key)
                self.metrics.drop(item, "local_receive_timeout")

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for handle in self.handles:
            handle.cancel()
        self.handles.clear()
        self.scheduler.clear("shutdown")
        for key in list(self.partial):
            self._drop_partial(key, "shutdown")
        for item in self.inflight.values():
            self.metrics.drop(item, "shutdown")
        self.inflight.clear()
        if self.transport:
            self.transport.abort()
