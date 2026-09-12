import asyncio
import json
import time
import unittest
import urllib.error
import urllib.request
from nebula_mvp.app import Application
from nebula_mvp.config import LinkConfig
from nebula_mvp.clock import PreciseEventLoop
from nebula_mvp.protocol import Kind, Packet, PacketFactory


class IntegrationTests(unittest.TestCase):
    def run_case(self, method):
        async def scenario():
            await self.asyncSetUp()
            try:
                await method()
            finally:
                await self.asyncTearDown()
        with asyncio.Runner(loop_factory=PreciseEventLoop) as runner:
            runner.run(scenario())

    def test_real_udp_video_telemetry_control_and_http(self):
        self.run_case(self.check_real_udp_video_telemetry_control_and_http)

    def test_loss_and_reordered_controls(self):
        self.run_case(self.check_loss_and_reordered_controls)

    def test_layered_video_falls_back_to_base_when_capacity_drops(self):
        self.run_case(self.check_layered_fallback)

    def test_backlogged_link_sends_at_configured_rate(self):
        self.run_case(self.check_backlogged_rate)

    def test_fec_keeps_full_layer_through_packet_loss(self):
        self.run_case(self.check_fec_under_loss)

    async def asyncSetUp(self):
        self.app = Application(LinkConfig(capacity_bps=5_000_000, jitter_ms=0), fps=10)
        await self.app.start(port=0)

    async def asyncTearDown(self):
        await self.app.close()

    async def check_real_udp_video_telemetry_control_and_http(self):
        desired = {"throttle": .7, "yaw": .2, "pitch": .1, "roll": -.1}
        url = f"http://127.0.0.1:{self.app.server.server_port}"

        def post(path, value):
            req = urllib.request.Request(url + path, json.dumps(value).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.load(response)

        self.assertTrue((await asyncio.to_thread(post, "/api/control", desired))["ok"])
        await asyncio.sleep(.8)
        self.app.check_tasks()
        state = await self.app.state()
        self.assertGreater(state["frame_id"], 0)
        self.assertEqual(state["telemetry"]["applied_control"], desired)
        self.assertGreater(state["metrics"]["latency_ms"]["CONTROL"]["count"], 10)
        self.assertGreater(state["metrics"]["latency_ms"]["DATA"]["count"], 3)
        self.assertGreater(state["metrics"]["latency_ms"]["VIDEO"]["count"], 1)
        samples = self.app.metrics.latencies["CONTROL"]
        self.assertTrue(all(value >= 20 for _, value in samples), "Propagation must not fire early")
        self.assertEqual(len({tuple(v) for v in state["udp"].values()}), 3)

        def get(path):
            with urllib.request.urlopen(url + path, timeout=5) as response:
                return response.read()

        self.assertTrue((await asyncio.to_thread(get, "/frame.jpg")).startswith(b"\xff\xd8"))
        self.assertIn(b"LINK LAB", await asyncio.to_thread(get, "/"))
        await asyncio.to_thread(post, "/api/link", {"mode": "naive", "capacity_bps": 500_000})
        self.assertEqual(self.app.link.config.mode, "naive")
        self.assertEqual(self.app.link.config.capacity_bps, 500_000)
        with self.assertRaises(urllib.error.HTTPError) as error:
            await asyncio.to_thread(post, "/api/link", {"capacity_bps": -1})
        self.assertEqual(error.exception.code, 400)

    async def check_loss_and_reordered_controls(self):
        self.app.link.update({"loss": .25, "jitter_ms": 50, "capacity_bps": 500_000})
        await asyncio.sleep(1)
        self.assertGreater(self.app.metrics.drops["link_loss"], 0)
        elapsed = (time.perf_counter_ns() - self.app.metrics.started_ns) / 1e9
        self.assertLessEqual(self.app.metrics.counts["wire_bytes"], elapsed * 62500 + 1228)
        stamp = time.perf_counter_ns()
        newer = Packet(Kind.CONTROL, 99991, stamp,
                       b'{"throttle":0.8,"yaw":0,"pitch":0,"roll":0}')
        older = Packet(Kind.CONTROL, 99990, stamp - 10,
                       b'{"throttle":0.2,"yaw":0,"pitch":0,"roll":0}')
        self.app.air.datagram_received(newer.encode(), self.app.air.link_address)
        self.app.air.datagram_received(older.encode(), self.app.air.link_address)
        self.assertEqual(self.app.air.control.throttle, .8)
        self.assertGreater(self.app.metrics.counts["control_out_of_order"], 0)
        self.app.check_tasks()

    async def check_layered_fallback(self):
        await asyncio.sleep(1)
        self.assertEqual((await self.app.state())["video"]["frame_layer"], "full")
        self.app.link.update({"capacity_bps": 400_000})
        await asyncio.sleep(1.5)
        self.app.check_tasks()
        state = await self.app.state()
        self.assertIsNone(state["video"]["full_rung"])  # Nothing fits: full layer paused.
        self.assertEqual(state["video"]["frame_layer"], "base")
        self.assertEqual(state["video"]["frame_size"], [160, 90])
        self.assertLess(state["frame_age_ms"], 300)  # Still a live picture.

    async def check_backlogged_rate(self):
        link, rate = self.app.link, 5_000_000 / 8
        link.update({"mode": "naive"})
        factory, pad = PacketFactory(), json.dumps({"pad": "x" * 1100}).encode()
        for _ in range(1500):  # About 1.7 MB: keeps 5 Mbps busy for about 3 s.
            link.datagram_received(factory.packet(Kind.DATA, pad).encode(), link.air_address)
        await asyncio.sleep(.3)
        before, start = self.app.metrics.counts["wire_bytes"], time.perf_counter()
        await asyncio.sleep(1.5)
        sent, elapsed = self.app.metrics.counts["wire_bytes"] - before, time.perf_counter() - start
        self.assertGreater(sent, .97 * rate * elapsed)  # Per-packet sleeps reached only ~62% here.
        self.assertLess(sent, rate * (elapsed + .02) + 1228)  # Never above the configured rate.
        self.app.check_tasks()

    async def check_fec_under_loss(self):
        self.app.link.update({"loss": .05})
        await asyncio.sleep(3)
        self.app.check_tasks()
        state = await self.app.state()
        self.assertGreater(state["video"]["fec_parity"], 0)
        self.assertGreater(state["metrics"]["counts"].get("video_frames_recovered", 0), 0)
        # Unprotected, a 20-fragment frame survives 5% loss only 36% of the time.
        self.assertGreater(state["metrics"]["video_full_share"], .8)
