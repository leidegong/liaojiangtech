import asyncio
import time
from .control import Control
from .fec import interleave_depth, parity_count
from .protocol import Kind, Packet, PacketFactory, fragment_count, video_payload_budget
from .telemetry import SimulatedFlight


class AirNode(asyncio.DatagramProtocol):
    def __init__(self, link, metrics, source, fps=15, layered=True, fec=True):
        self.link = link
        self.metrics = metrics
        self.source = source
        self.fps = fps
        self.layered = layered
        self.fec = fec  # Parity on the full-resolution layer, sized to measured loss.
        self.last_parity = 0
        self.transport = None
        self.link_address = None
        self.factory = PacketFactory()
        self.control = Control()
        self.last_control_stamp = 0
        self.last_control_receive_ns = 0
        self.flight = SimulatedFlight()
        self.frame_id = 0
        self.video_lock = asyncio.Lock()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, raw, address):
        now = time.perf_counter_ns()
        try:
            if address != self.link_address:
                raise ValueError("Unexpected sender")
            packet = Packet.decode(raw)
            if packet.kind != Kind.CONTROL:
                raise ValueError("Unexpected direction")
            control = Control.parse(packet.json())
        except (ValueError, UnicodeError, TypeError):
            self.metrics.counts["invalid_packets"] += 1
            return
        self.link.received(packet, now)
        # Jitter can reorder delivery; never apply an older state after a newer one.
        if packet.generated_ns > self.last_control_stamp:
            self.control = control
            self.last_control_stamp = packet.generated_ns
            self.last_control_receive_ns = now
        else:
            self.metrics.counts["control_out_of_order"] += 1

    async def telemetry_loop(self):
        previous = time.perf_counter()
        deadline = previous
        while True:
            now = time.perf_counter()
            # This is a toy model, not a flight safety controller. Stale commands
            # from a congested FIFO are observed, but not executed by the model.
            stale = (time.perf_counter_ns() - self.last_control_stamp) > 500_000_000
            applied = Control() if stale else self.control
            state = self.flight.step(applied, min(.2, now - previous), stale)
            state["control_age_ms"] = round((time.perf_counter_ns() - self.last_control_stamp) / 1e6, 1) if self.last_control_stamp else None
            previous = now
            packet = self.factory.json(Kind.DATA, state)
            self.transport.sendto(packet.encode(), self.link_address)
            self.metrics.counts["data_generated"] += 1
            deadline += .1
            await asyncio.sleep(max(0, deadline - time.perf_counter()))
            if time.perf_counter() - deadline > .1:
                deadline = time.perf_counter()

    async def video_loop(self):
        deadline = time.perf_counter()
        while True:
            self.frame_id += 1
            layered = self.layered
            # One loss/burst reading sizes the budget, the parity, and interleave.
            protect = layered and self.fec
            loss = self.link.loss_estimate() if protect else None
            burst = self.link.burst_estimate() if protect else None
            self.link.scheduler.interleave_depth = interleave_depth(burst) if protect else 1
            if layered:
                wire = self.link.full_layer_budget(1 / self.fps)
                job = (self.source.capture_layers, self.frame_id,
                       None if wire is None else video_payload_budget(wire, loss, burst))
            else:
                job = (self.source.capture, self.frame_id)
            async with self.video_lock:
                capture = asyncio.create_task(asyncio.to_thread(*job))
                try:
                    result = await asyncio.shield(capture)
                except asyncio.CancelledError:
                    # Join the in-progress camera read before releasing its handle.
                    await capture
                    raise
            if layered:
                stamp, base, full = result
                # The base layer carries no parity: it is the cheap floor that
                # must fit the lowest capacities.
                parity = parity_count(fragment_count(len(full)), loss, burst) if full else 0
                layers = [(Kind.VIDEO_BASE, base, 0)] + ([(Kind.VIDEO, full, parity)] if full else [])
            else:
                stamp, full = result
                parity = 0
                layers = [(Kind.VIDEO, full, 0)]
            self.last_parity = parity
            self.metrics.counts["fec_parity_packets"] += parity
            # Register every layer before sending any, so metrics know how many to expect.
            for kind, jpeg, _ in layers:
                self.metrics.generated_frame(kind, self.frame_id, stamp, len(jpeg))
            sent = 0
            for kind, jpeg, parity in layers:  # Base first: it must never queue behind the full layer.
                for packet in self.factory.video(self.frame_id, jpeg, stamp, kind, parity):
                    self.transport.sendto(packet.encode(), self.link_address)
                    sent += 1
                    if sent % 8 == 0:
                        await asyncio.sleep(0)  # Drain local UDP ingress during a frame burst.
            deadline += 1 / self.fps
            await asyncio.sleep(max(0, deadline - time.perf_counter()))
            if time.perf_counter() - deadline > 1 / self.fps:
                deadline = time.perf_counter()
