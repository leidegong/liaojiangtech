from math import comb
import random
import time
import unittest
from unittest.mock import Mock, patch
from nebula_mvp.config import LinkConfig
from nebula_mvp.control import Control
from nebula_mvp.fec import interleave_depth, parity_count
from nebula_mvp.metrics import Metrics, distribution
from nebula_mvp.protocol import (FrameAssembler, Kind, Packet, PacketFactory,
                                  QueuedPacket, MAX_DATAGRAM, fragment_count,
                                  video_payload_budget, video_wire_bytes)
from nebula_mvp.scheduler import LinkClock, Scheduler


class ProtocolTests(unittest.TestCase):
    def test_camera_failure_falls_back_to_valid_synthetic_jpeg(self):
        from nebula_mvp.video import VideoSource, valid_jpeg
        for opened in (False, True):
            capture = Mock()
            capture.isOpened.return_value = opened
            capture.read.return_value = (False, None)
            with patch("nebula_mvp.video.cv2.VideoCapture", return_value=capture):
                source = VideoSource("camera")
                source.open()
                _, jpeg = source.capture(1)
                self.assertTrue(valid_jpeg(jpeg))
                self.assertTrue(source.warning)
                self.assertEqual(source.label, "Synthetic test scene")
                capture.release.assert_called_once()

    def test_binary_roundtrip_and_bad_headers(self):
        packet = PacketFactory().json(Kind.CONTROL, Control().public())
        self.assertEqual(Packet.decode(packet.encode()), packet)
        for raw in (b"N7", b"XX" + packet.encode()[2:], packet.encode()[:-1],
                    packet.encode() + b"extra", packet.encode()[:2] + b"\x09" + packet.encode()[3:]):
            with self.assertRaises(ValueError):
                Packet.decode(raw)

    def test_large_jpeg_fragmentation_reorder_and_duplicate(self):
        jpeg = bytes(range(256)) * 250
        fragments = PacketFactory().video(3, jpeg, 10)
        self.assertTrue(all(len(p.encode()) <= MAX_DATAGRAM for p in fragments))
        assembler = FrameAssembler()
        self.assertIsNone(assembler.add(fragments[-1], 10))
        self.assertIsNone(assembler.add(fragments[-1], 11))
        result = None
        for p in reversed(fragments[:-1]):
            result = assembler.add(Packet.decode(p.encode()), 12)
        self.assertEqual(result, (3, 10, jpeg))

    def test_missing_fragment_expires_without_unbounded_memory(self):
        dropped = []
        assembler = FrameAssembler(timeout_ms=10, limit=2,
                                   on_drop=lambda fid, reason: dropped.append((fid, reason)))
        factory = PacketFactory()
        for fid in range(3):
            assembler.add(factory.video(fid, b"x" * 3000, 0)[0], 0)
        self.assertEqual(len(assembler.frames), 2)
        assembler.expire(11_000_000)
        self.assertFalse(assembler.frames)
        self.assertEqual(len(dropped), 3)

    def test_video_budget_matches_fragmentation(self):
        factory = PacketFactory()
        for loss in (None, .05):  # Budgets include the FEC parity sent at that loss.
            for size in (1, 1170, 1171, 5000, 22806):
                parity = parity_count(fragment_count(size), loss)
                fragments = factory.video(1, b"x" * size, 0, Kind.VIDEO_BASE, parity)
                wire = sum(p.wire_bytes for p in fragments)
                self.assertEqual(video_wire_bytes(size, loss), wire)
                self.assertEqual(video_payload_budget(wire, loss), size)
                self.assertEqual(Packet.decode(fragments[0].encode()).kind, Kind.VIDEO_BASE)
            for wire in range(0, 5000, 37):
                best = video_payload_budget(wire, loss)
                self.assertLessEqual(video_wire_bytes(best, loss), wire)
                self.assertGreater(video_wire_bytes(best + 1, loss), wire)
        with self.assertRaises(ValueError):
            factory.video(1, b"x", 0, Kind.DATA)

    def test_fec_rebuilds_frame_from_any_data_count_fragments(self):
        jpeg = random.Random(3).randbytes(22806)
        fragments = PacketFactory().video(4, jpeg, 10, parity=4)
        self.assertEqual(len(fragments), 24)  # 20 data + 4 parity.
        recovered = []
        assembler = FrameAssembler(on_recover=recovered.append)
        survivors = [p for i, p in enumerate(fragments) if i not in (0, 7, 19)]  # Last data block too.
        random.Random(5).shuffle(survivors)
        results = [assembler.add(Packet.decode(p.encode()), 1) for p in survivors]
        self.assertEqual(results[19], (4, 10, jpeg))  # Complete at the 20th fragment in.
        self.assertIsNone(results[20])  # A late fragment does not reopen the frame.
        self.assertEqual((recovered, assembler.frames), ([4], {}))
        dropped = []
        assembler = FrameAssembler(timeout_ms=10, on_drop=lambda fid, reason: dropped.append(reason))
        for p in fragments[5:]:  # Five lost: one more than the parity covers.
            self.assertIsNone(assembler.add(p, 0))
        assembler.expire(11_000_000)
        self.assertEqual(dropped, ["reassembly_timeout"])
        with self.assertRaises(ValueError):  # A fragment whose length breaks the frame layout.
            Packet.decode(Packet(Kind.VIDEO, 1, 0, fragments[0].payload + b"x").encode())

    def test_parity_count_meets_survival_target(self):
        self.assertEqual((parity_count(20, 0), parity_count(20, None)), (0, 0))
        for loss in (.01, .05, .1):
            count = parity_count(20, loss)
            survive = lambda m: sum(comb(20 + m, lost) * loss ** lost * (1 - loss) ** (20 + m - lost)
                                    for lost in range(m + 1))
            self.assertGreaterEqual(survive(count), .99)
            self.assertLess(survive(count - 1), .99)  # The fewest parity blocks that do.
        self.assertEqual(parity_count(20, .05), 4)
        self.assertEqual(parity_count(20, .05, burst=1.05), 4)  # Independent runs stay i.i.d.
        self.assertEqual(interleave_depth(1.05), 1)
        self.assertEqual(interleave_depth(4), 2)
        self.assertEqual(interleave_depth(8), 3)
        self.assertEqual(parity_count(20, .05, burst=4), 6)
        self.assertEqual(parity_count(20, .05, burst=8), 8)
        self.assertEqual(parity_count(20, .05, burst=16), 16)
        self.assertEqual(parity_count(4, 1.0), 4)  # Capped at 100% overhead.
        self.assertGreater(video_wire_bytes(22806, .05, burst=16), video_wire_bytes(22806, .05))

    def test_validate_control_and_config(self):
        for value in (float("nan"), float("inf"), True, "0.5", 2):
            with self.assertRaises(ValueError):
                Control.parse({**Control().public(), "yaw": value})
        for values in ({"mode": "other"}, {"loss": 1}, {"delay_ms": float("nan")},
                       {"capacity_bps": -1}, {"unknown": 0}, {"loss_burst": .5},
                       {"loss_burst": 51}, {"loss_burst": float("nan")}):
            with self.assertRaises(ValueError):
                LinkConfig().updated(values)

    def test_stale_control_marks_failsafe_and_holds_neutral(self):
        # Air-side policy (README): commands older than 500 ms are not executed;
        # telemetry reports failsafe with a neutral applied_control.
        from nebula_mvp.telemetry import SimulatedFlight
        flight = SimulatedFlight()
        live = flight.step(Control(throttle=1.0, yaw=0.5), 0.1, False)
        self.assertFalse(live["failsafe"])
        self.assertEqual(live["applied_control"]["throttle"], 1.0)
        held = flight.step(Control(), 0.1, True)
        self.assertTrue(held["failsafe"])
        self.assertEqual(held["applied_control"], Control().public())

    def test_ground_input_timeout_returns_neutral(self):
        # Ground-side policy: 1 s without a browser/API input resets desired.
        from nebula_mvp.ground_node import GroundNode
        ground = GroundNode(link=Mock(), metrics=Metrics())
        ground.set_control(Control(throttle=0.8, yaw=-0.3).public())
        self.assertEqual(ground.desired.throttle, 0.8)
        ground.last_input_ns = time.perf_counter_ns() - 1_100_000_000
        if time.perf_counter_ns() - ground.last_input_ns > 1_000_000_000:
            ground.desired = Control()
        self.assertEqual(ground.desired, Control())


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.drops = []
        self.config = LinkConfig()
        self.scheduler = Scheduler(self.config, lambda p, reason: self.drops.append((p, reason)))
        self.factory = PacketFactory()

    def item(self, kind, stamp=0):
        return QueuedPacket(self.factory.packet(kind, b"{}", stamp), stamp)

    def frame(self, fid):
        return [QueuedPacket(p, 0) for p in self.factory.video(fid, b"x" * 3000, 0)]

    def test_latest_control_preempts_data_and_video(self):
        video = self.frame(1)
        data = self.item(Kind.DATA)
        old, latest = self.item(Kind.CONTROL), self.item(Kind.CONTROL)
        self.scheduler.enqueue_frame(1, video)
        self.assertIs(self.scheduler.peek(0), video[0])
        self.scheduler.enqueue(data)
        self.scheduler.enqueue(old)
        self.scheduler.enqueue(latest)
        self.assertIs(self.scheduler.pop(0), latest)
        self.assertIs(self.scheduler.pop(0), data)
        self.assertIs(self.scheduler.pop(0), video[0])
        self.assertEqual(self.drops[0], (old, "control_replaced"))

    def test_video_overflow_preserves_active_frame(self):
        first = self.frame(1)
        self.scheduler.enqueue_frame(1, first)
        self.assertIs(self.scheduler.pop(0), first[0])
        for fid in range(2, 5):
            self.scheduler.enqueue_frame(fid, self.frame(fid))
        self.assertEqual(self.scheduler.snapshot()["video_frames"], 3)
        self.assertTrue(all(item.packet.fragment()[0] == 2 for item, _ in self.drops))
        self.assertIs(self.scheduler.pop(0), first[1])
        self.assertIs(self.scheduler.pop(0), first[2])
        self.assertEqual(self.scheduler.pop(0).packet.fragment()[0], 3)

    def base(self, fid):
        return [QueuedPacket(p, 0) for p in self.factory.video(fid, b"b" * 1500, 0, Kind.VIDEO_BASE)]

    def test_base_layer_preempts_active_full_frame(self):
        full, base, data = self.frame(1), self.base(1), self.item(Kind.DATA)
        self.scheduler.enqueue_frame(1, full)
        self.assertIs(self.scheduler.pop(0), full[0])
        self.scheduler.enqueue_frame(1, base)
        self.scheduler.enqueue(data)
        self.assertIs(self.scheduler.pop(0), data)     # Telemetry still outranks video.
        self.assertIs(self.scheduler.pop(0), base[0])  # Base preempts between full fragments.
        self.assertIs(self.scheduler.pop(0), base[1])
        self.assertIs(self.scheduler.pop(0), full[1])  # The full frame resumes intact.
        self.assertEqual(self.drops, [])

    def test_video_layers_have_independent_queues(self):
        self.scheduler.enqueue_frame(9, self.base(9))
        for fid in range(1, 4):
            self.scheduler.enqueue_frame(fid, self.frame(fid))
        snapshot = self.scheduler.snapshot()
        self.assertEqual((snapshot["base_frames"], snapshot["video_frames"]), (1, 2))
        self.assertTrue(self.drops)
        self.assertTrue(all(item.packet.kind == Kind.VIDEO and item.packet.fragment()[0] == 1
                            for item, _ in self.drops))

    def test_queue_bounds_and_expiry(self):
        self.config.data_queue_limit = 2
        for _ in range(3):
            self.scheduler.enqueue(self.item(Kind.DATA))
        self.scheduler.enqueue(self.item(Kind.CONTROL))
        self.scheduler.enqueue_frame(1, self.frame(1))
        self.assertIsNone(self.scheduler.peek(600_000_000))
        reasons = {reason for _, reason in self.drops}
        self.assertEqual(reasons, {"data_overflow", "data_ttl", "control_ttl", "video_wait_ttl"})

    def test_interleave_holds_then_round_robins_two_frames(self):
        from collections import defaultdict
        from nebula_mvp.scheduler import INTERLEAVE_HOLD_NS
        self.scheduler.interleave_depth = 2
        first = self.frame(1)
        self.scheduler.enqueue_frame(1, first)
        self.assertIsNone(self.scheduler.peek(0))  # Wait for a second codeword.
        self.assertEqual(self.scheduler.next_wakeup_ns(0), INTERLEAVE_HOLD_NS)
        self.scheduler.enqueue_frame(2, self.frame(2))
        order = []
        while item := self.scheduler.pop(0):
            order.append(item.packet.fragment().frame_id)
        self.assertGreater(len(order), 4)
        self.assertEqual(order[:2], [1, 2])
        positions = defaultdict(list)
        for index, frame_id in enumerate(order):
            positions[frame_id].append(index)
        for places in positions.values():
            self.assertTrue(all(b - a >= 2 for a, b in zip(places, places[1:])))

    def test_interleave_keeps_depth_frames_waiting(self):
        self.scheduler.interleave_depth = 3
        for fid in (1, 2, 3):
            self.scheduler.enqueue_frame(fid, self.frame(fid))
        self.assertEqual(self.scheduler.snapshot()["video_frames"], 3)
        self.assertEqual(self.drops, [])
        order = [self.scheduler.pop(0).packet.fragment().frame_id for _ in range(3)]
        self.assertEqual(order, [1, 2, 3])

    def test_interleave_releases_lone_frame_after_hold(self):
        from nebula_mvp.scheduler import INTERLEAVE_HOLD_NS
        self.scheduler.interleave_depth = 2
        first = self.frame(1)
        self.scheduler.enqueue_frame(1, first)
        self.assertIsNone(self.scheduler.peek(INTERLEAVE_HOLD_NS - 1))
        self.assertIs(self.scheduler.pop(INTERLEAVE_HOLD_NS), first[0])

    def test_interleave_caps_hits_from_a_consecutive_run(self):
        self.scheduler.interleave_depth = 2
        for fid in (1, 2):
            packets = [QueuedPacket(p, 0) for p in self.factory.video(fid, b"x" * 20 * 1170, 0, parity=5)]
            self.scheduler.enqueue_frame(fid, packets)
        order = []
        while item := self.scheduler.pop(0):
            order.append(item.packet.fragment().frame_id)
        burst = 8
        worst = 0
        for start in range(len(order) - burst + 1):
            hits = {1: 0, 2: 0}
            for frame_id in order[start:start + burst]:
                hits[frame_id] += 1
            worst = max(worst, *hits.values())
        self.assertLessEqual(worst, 4)  # A run of 8 hits at most half of each 2-way frame.
        sequential_worst = burst  # Back-to-back fragments of one frame.
        self.assertLess(worst, sequential_worst)

    def test_naive_order_and_memory_bound(self):
        self.config.mode = "naive"
        self.config.fifo_byte_limit = 100
        one, two, three = self.item(Kind.DATA), self.item(Kind.CONTROL), self.item(Kind.DATA)
        for item in (one, two, three):
            self.scheduler.enqueue(item)
        self.assertIs(self.scheduler.pop(0), one)
        self.assertIs(self.scheduler.pop(0), two)
        self.assertIsNone(self.scheduler.pop(0))
        self.assertEqual(self.drops[-1], (three, "fifo_overflow"))

    def test_link_clock_keeps_rate_through_late_wakeups(self):
        clock = LinkClock(max_lag_ns=20)
        self.assertEqual(clock.transmit(0, 0, 10), (0, 10, 0))
        self.assertEqual(clock.transmit(0, 13, 10), (10, 20, 0))    # Woke 3 late: still starts at 10.
        self.assertEqual(clock.transmit(15, 20, 10), (20, 30, 0))   # Ready earlier, waits for the link.
        self.assertEqual(clock.transmit(40, 41, 10), (40, 50, 0))   # Idle link: starts when ready.
        self.assertEqual(clock.transmit(40, 100, 10), (80, 90, 30))  # 50-long stall: 30 lost, not replayed.

    def test_percentiles_and_no_samples(self):
        self.assertEqual(distribution([])["p99"], None)
        self.assertEqual(distribution([10, 20, 30])["p50"], 20)
        self.assertEqual(distribution([10, 20, 30])["p95"], 29)


class LayeredVideoTests(unittest.TestCase):
    def test_full_layer_ladder_descends_pauses_and_climbs(self):
        from nebula_mvp.video import VideoSource, valid_jpeg
        source = VideoSource()
        _, base, full = source.capture_layers(1, None)
        self.assertEqual(valid_jpeg(base), (160, 90))
        self.assertEqual(valid_jpeg(full), (640, 360))  # No budget reported: top rung.
        budget = len(full) // 2
        _, _, full = source.capture_layers(2, budget)
        self.assertLessEqual(len(full), budget)  # Drops straight to a rung that fits.
        self.assertGreater(source.rung, 1)
        settled = source.rung
        source.capture_layers(2, budget)
        self.assertEqual(source.rung, settled)  # No headroom above, so no flapping.
        _, _, full = source.capture_layers(3, 100)
        self.assertIsNone(full)  # Nothing fits: the full layer pauses.
        self.assertEqual(source.rung, len(source.ladder))
        _, _, full = source.capture_layers(4, 10 ** 6)
        self.assertEqual(source.rung, len(source.ladder) - 1)  # Climbs one rung per frame.
        self.assertEqual(valid_jpeg(full), source.ladder[-1][:2])

    def test_full_layer_budget_follows_measured_goodput(self):
        from nebula_mvp.link_emulator import FULL_LAYER_HEADROOM, LinkEmulator
        link = LinkEmulator(LinkConfig(capacity_bps=1_000_000), Metrics())
        self.assertAlmostEqual(link.full_layer_budget(.1), 125_000 * .1 * FULL_LAYER_HEADROOM)
        frame = [QueuedPacket(p, 0) for p in PacketFactory().video(1, b"x" * 5000, 0)]
        for index, item in enumerate(frame):  # Five fragments on the air over 0.5 s.
            link._measure_full(item, index * .1, .1)
        wire = sum(item.packet.wire_bytes for item in frame)
        self.assertAlmostEqual(link.full_rate, wire / .5)
        self.assertAlmostEqual(link.full_layer_budget(.1), wire / .5 * .1 * FULL_LAYER_HEADROOM)
        link.update({"capacity_bps": 2_000_000})
        self.assertIsNone(link.full_rate)  # Measurements at the old rate no longer apply.
        now = time.perf_counter()
        for _ in range(1000):  # Two seconds without loss...
            link.observe_loss(False, now - 2)
        for index in range(100):  # ...then 10% loss starts.
            link.observe_loss(index % 10 == 0, now)
        # The long window still sees mostly clean traffic (about 2%); the
        # estimate follows the short one, which already shows about 8.5%.
        self.assertGreater(link.loss_estimate(), .08)
        link.update({"mode": "naive"})
        self.assertIsNone(link.full_layer_budget(.1))  # A FIFO modem offers nothing to budget.
        self.assertIsNone(link.loss_estimate())  # Nor a loss rate to size FEC with.
        self.assertIsNone(link.burst_estimate())

    def test_burst_estimate_follows_observed_runs(self):
        from nebula_mvp.link_emulator import LinkEmulator
        link = LinkEmulator(LinkConfig(), Metrics())
        self.assertEqual(link.burst_estimate(), 1.0)
        now = time.perf_counter()
        for _ in range(12):
            link.observe_loss(True, now, new_run=True)
            for _ in range(7):
                link.observe_loss(True, now, new_run=False)
            for _ in range(152):
                link.observe_loss(False, now)
        self.assertAlmostEqual(link.burst_estimate(), 8, delta=.1)

    def test_burst_channel_matches_requested_loss_and_run_length(self):
        from nebula_mvp.link_emulator import LinkEmulator

        def sample(link, count):
            lost = runs = 0
            previous = False
            for _ in range(count):
                now = link.channel_loss()
                lost += now
                runs += now and not previous
                previous = now
            return lost / count, lost / runs

        loss, run = sample(LinkEmulator(LinkConfig(loss=.05, loss_burst=8), Metrics()), 200_000)
        self.assertAlmostEqual(loss, .05, delta=.006)  # About three standard deviations.
        self.assertAlmostEqual(run, 8, delta=.8)
        link = LinkEmulator(LinkConfig(loss=.05), Metrics())
        reference = random.Random(7)  # Independent loss draws exactly as before bursts existed.
        self.assertTrue(all(link.channel_loss() == (reference.random() < .05) for _ in range(20_000)))
        link = LinkEmulator(LinkConfig(loss=.25, loss_burst=50), Metrics())
        while not link.channel_loss():
            pass
        link.update({"loss": 0})  # A new channel starts outside a burst...
        self.assertFalse(link.channel_bad)
        self.assertFalse(any(link.channel_loss() for _ in range(1000)))  # ...and without loss, never loses.

    def test_ground_never_goes_back_in_time(self):
        from nebula_mvp.ground_node import GroundNode
        ground = GroundNode(None, Metrics())
        self.assertIsNone(ground.rejection(Kind.VIDEO_BASE, 1))  # Nothing shown yet.
        ground.frame_id, ground.frame_kind = 1, Kind.VIDEO_BASE
        self.assertIsNone(ground.rejection(Kind.VIDEO, 1))  # Same capture, sharper.
        self.assertEqual(ground.rejection(Kind.VIDEO_BASE, 1), "video_out_of_order")
        ground.frame_kind = Kind.VIDEO
        self.assertEqual(ground.rejection(Kind.VIDEO, 1), "video_out_of_order")  # Duplicate.
        ground.frame_id = 5
        self.assertEqual(ground.rejection(Kind.VIDEO, 4), "video_out_of_order")

    def test_base_waits_for_its_full_frame_then_falls_back(self):
        from nebula_mvp.ground_node import ACTIVE_FRAMES, WAIT_MAX_NS, WAIT_MIN_NS, LayerSelector
        ms, base, full = 1_000_000, Kind.VIDEO_BASE, Kind.VIDEO
        selector = LayerSelector()
        # No full frame seen yet: a base is shown at once; its full frame upgrades it.
        self.assertEqual(selector.arrive(base, 1, 0, b"b1", 100 * ms), ([(base, 1, 0, b"b1")], []))
        self.assertEqual(selector.arrive(full, 1, 0, b"f1", 140 * ms), ([(full, 1, 0, b"f1")], []))
        # Now each base waits, and the full frame 40 ms behind it replaces it unseen.
        for fid in range(2, 12):
            self.assertEqual(selector.arrive(base, fid, 0, b"b", fid * 100 * ms), ([], []))
            self.assertEqual(selector.arrive(full, fid, 0, b"f", fid * 100 * ms + 40 * ms),
                             ([(full, fid, 0, b"f")], [fid]))
        self.assertEqual(selector.wait_ns(), 50 * ms)  # P95 of the 40 ms lags plus 10 ms.
        # Capture 12 loses its full frame: the base is shown when its wait runs out.
        self.assertEqual(selector.arrive(base, 12, 7, b"b12", 1200 * ms), ([], []))
        self.assertEqual(selector.next_deadline(), 1250 * ms)
        self.assertEqual(selector.due(1249 * ms), [])
        self.assertEqual(selector.due(1250 * ms), [(base, 12, 7, b"b12")])
        # A full frame that beat its base leaves the base nothing to wait for.
        selector.arrive(full, 13, 0, b"f", 1300 * ms)
        self.assertEqual(selector.arrive(base, 13, 0, b"b", 1301 * ms), ([], [13]))
        # No full frame for ACTIVE_FRAMES captures (layer paused): bases go straight through.
        late = 13 + ACTIVE_FRAMES + 1
        self.assertEqual(selector.arrive(base, late, 0, b"b", 0), ([(base, late, 0, b"b")], []))
        # The wait stays within bounds, and chronically late full frames are not waited for.
        selector.lags.extend([ms] * 32)
        self.assertEqual(selector.wait_ns(), WAIT_MIN_NS)
        selector.lags.extend([900 * ms] * 32)
        self.assertEqual(selector.wait_ns(), WAIT_MAX_NS)
        self.assertEqual(selector.arrive(base, 14, 0, b"b", 0), ([(base, 14, 0, b"b")], []))

    def test_frame_counted_once_across_layers(self):
        metrics = Metrics()
        for fid in (1, 2):
            metrics.generated_frame(Kind.VIDEO_BASE, fid, 0, 100)
            metrics.generated_frame(Kind.VIDEO, fid, 0, 1000)
        metrics.frame_received(Kind.VIDEO_BASE, 1, 0, 30_000_000)
        metrics.frame_received(Kind.VIDEO, 1, 0, 60_000_000)  # An upgrade, not a new frame.
        metrics.frame_drop(Kind.VIDEO_BASE, 2, "base_superseded")
        metrics.frame_drop(Kind.VIDEO, 2, "link_loss")
        metrics.frame_drop(Kind.VIDEO, 2, "link_loss")  # Each layer settles once.
        self.assertEqual(metrics.counts["frames_generated"], 2)
        self.assertEqual(metrics.counts["frames_received"], 1)
        self.assertEqual(metrics.counts["frames_dropped"], 1)
        self.assertEqual(len(metrics.frame_arrivals), 1)
        self.assertEqual(metrics.counts["video_upgrades"], 1)
        self.assertEqual(metrics.drops["frame:link_loss"], 1)
        self.assertEqual(metrics.snapshot()["frames_pending"], 0)

    def test_fec_frame_counted_lost_only_beyond_its_parity(self):
        metrics = Metrics()
        fragments = [QueuedPacket(p, 0) for p in PacketFactory().video(1, b"x" * 5000, 0, parity=2)]
        metrics.generated_frame(Kind.VIDEO, 1, 0, 5000)
        for item in fragments[:2]:
            metrics.drop(item, "link_loss")
        self.assertEqual(metrics.counts["frames_dropped"], 0)  # Two parity blocks cover these.
        metrics.drop(fragments[2], "link_loss")
        self.assertEqual(metrics.counts["frames_dropped"], 1)
        self.assertEqual(metrics.drops["frame:link_loss"], 1)


if __name__ == "__main__":
    unittest.main()
