"""Tests for device bench, link budget, interfaces, auth."""
import json
import socket
import threading
import unittest
from pathlib import Path
import tempfile

from nebula_mvp.device_bench import (
    BenchConfig, DeviceBench, MockTransport, write_report,
    parse_iperf3_json, Iperf3Transport,
)
from nebula_mvp.link_budget import LinkBudgetInput, compute, fspl_db, compare_bands, earth_bulge_m
from nebula_mvp.interfaces import (
    encode_sbus, decode_sbus, channels_neutral, mavlink_v1_header,
    MavlinkSeqMonitor, FailsafeConfig, FailsafeMachine, FailsafeAction, SBUS_FRAME_LEN,
)
from nebula_mvp.auth import AuthConfig, AuthGateway, AuthError


class DeviceBenchTests(unittest.TestCase):
    def test_mock_run_all(self):
        cfg = BenchConfig(mode="mock", video_inject_mbps=28, control_seconds=1.0)
        result = DeviceBench(cfg, MockTransport(capacity_mbps=30, seed=1)).run_all()
        self.assertEqual(len(result["steps"]), 3)
        self.assertIn("iperf_steps", result["steps"][0]["name"])
        self.assertTrue(result["all_ok"])
        with tempfile.TemporaryDirectory() as td:
            path = write_report(result, Path(td))
            self.assertTrue(path.exists())

    def test_dry_run_knee(self):
        cfg = BenchConfig(mode="dry-run")
        step = DeviceBench(cfg).run_iperf_steps()
        self.assertIsNotNone(step.metrics["knee_offered_mbps"])

    def test_parse_iperf3_udp_json(self):
        sample = {
            "start": {"target_bitrate": 10_000_000},
            "end": {
                "sum": {
                    "bits_per_second": 9_500_000,
                    "lost_percent": 2.5,
                    "seconds": 3.0,
                    "packets": 1000,
                    "lost_packets": 25,
                }
            },
        }
        row = parse_iperf3_json(sample)
        self.assertAlmostEqual(row["offered_mbps"], 10.0, places=2)
        self.assertAlmostEqual(row["goodput_mbps"], 9.5, places=2)
        self.assertAlmostEqual(row["loss_pct"], 2.5, places=2)

    def test_iperf3_offline_json_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for mbps in (5, 10, 20, 30, 40):
                delivered = mbps if mbps <= 28 else 20.0
                loss = 0.0 if mbps <= 28 else 40.0
                payload = {
                    "start": {"target_bitrate": int(mbps * 1e6)},
                    "end": {"sum": {
                        "bits_per_second": delivered * 1e6,
                        "lost_percent": loss,
                        "seconds": 3.0,
                    }},
                }
                (root / f"iperf-{mbps:g}M.json").write_text(
                    json.dumps(payload), encoding="utf-8")
            cfg = BenchConfig(mode="iperf3", control_seconds=0.2, control_hz=20)
            transport = Iperf3Transport(cfg, json_dir=root, run_subprocess=False)
            step = DeviceBench(cfg, transport).run_iperf_steps()
            self.assertEqual(step.metrics["knee_offered_mbps"], 30)

    def test_no_echo_timeouts_are_not_success_latency(self):
        # REVIEW-a9cb9d8 P1: sink open but never replies must not invent RTT samples.
        sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sink.bind(("127.0.0.1", 0))
        port = sink.getsockname()[1]
        cfg = BenchConfig(
            mode="iperf3", terminal_host="127.0.0.1", control_port=port,
            control_hz=50, control_seconds=0.2, video_inject_mbps=8,
        )
        transport = Iperf3Transport(cfg, run_subprocess=True, echo_timeout_s=0.02)
        transport._video_live = True
        try:
            raw = transport.probe_control_latency(50, 0.2, 64)
            self.assertEqual(raw["matched"], 0)
            self.assertEqual(raw["samples_ms"], [])
            self.assertFalse(raw["measured"])
            self.assertGreater(raw["timeouts"], 0)
            step = DeviceBench(cfg, transport).run_control_distribution()
            self.assertFalse(step.ok)
            self.assertEqual(step.metrics["status"], "not_measured")
        finally:
            transport.close()
            sink.close()

    def test_echo_server_records_matched_rtt(self):
        echo = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        echo.bind(("127.0.0.1", 0))
        port = echo.getsockname()[1]
        stop = threading.Event()

        def loop():
            echo.settimeout(0.05)
            while not stop.is_set():
                try:
                    data, addr = echo.recvfrom(2048)
                    echo.sendto(data, addr)
                except (socket.timeout, OSError):
                    continue

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        cfg = BenchConfig(
            mode="iperf3", terminal_host="127.0.0.1", control_port=port,
            control_hz=40, control_seconds=0.25, control_payload_bytes=32,
        )
        transport = Iperf3Transport(cfg, echo_timeout_s=0.1)
        try:
            raw = transport.probe_control_latency(40, 0.25, 32)
            self.assertTrue(raw["measured"])
            self.assertGreaterEqual(raw["matched"], 8)
            self.assertGreaterEqual(raw["delivery_rate"], 0.9)
            self.assertTrue(all(0 <= s < 100 for s in raw["samples_ms"]))
            self.assertEqual(raw["metric"], "rtt_ms")
        finally:
            stop.set()
            transport.close()
            echo.close()
            thread.join(timeout=1)

    def test_failed_video_inject_cannot_pass_full_load(self):
        class DeadVideo(MockTransport):
            def inject_video_load(self, mbps, duration_s):
                return {"offered_mbps": mbps, "duration_s": duration_s,
                        "backend": "mock", "live": False, "started": False,
                        "note": "iperf3 failed to start"}

        cfg = BenchConfig(mode="mock", video_inject_mbps=28, control_seconds=0.2, control_hz=20)
        step = DeviceBench(cfg, DeadVideo(capacity_mbps=30, seed=1)).run_video_plus_control()
        self.assertFalse(step.ok)
        self.assertEqual(step.metrics["status"], "not_measured")
        self.assertFalse(step.metrics["inject"]["started"])

    def test_video_inject_exception_cannot_pass_full_load(self):
        class BoomVideo(MockTransport):
            def inject_video_load(self, mbps, duration_s):
                raise FileNotFoundError("iperf3 not on PATH")

        cfg = BenchConfig(mode="mock", video_inject_mbps=8, control_seconds=0.2, control_hz=20)
        step = DeviceBench(cfg, BoomVideo()).run_video_plus_control()
        self.assertFalse(step.ok)
        self.assertEqual(step.metrics["status"], "not_measured")

    def test_offline_run_all_sends_no_udp(self):
        # REVIEW-a9cb9d8 P2: --json-dir must not open sockets.
        sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sink.bind(("127.0.0.1", 0))
        port = sink.getsockname()[1]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for mbps in (5, 10, 20, 30, 40):
                payload = {
                    "start": {"target_bitrate": int(mbps * 1e6)},
                    "end": {"sum": {
                        "bits_per_second": min(mbps, 28) * 1e6,
                        "lost_percent": 0.0 if mbps <= 28 else 40.0,
                        "seconds": 3.0,
                    }},
                }
                (root / f"iperf-{mbps:g}M.json").write_text(json.dumps(payload), encoding="utf-8")
            cfg = BenchConfig(
                mode="iperf3", terminal_host="127.0.0.1", control_port=port,
                control_hz=50, control_seconds=0.2,
            )
            transport = Iperf3Transport(cfg, json_dir=root, run_subprocess=False)
            result = DeviceBench(cfg, transport).run_all()
            sink.setblocking(False)
            count = 0
            while True:
                try:
                    sink.recvfrom(2048)
                    count += 1
                except BlockingIOError:
                    break
            sink.close()
            self.assertEqual(count, 0)
            self.assertEqual(result["config"]["mode"], "offline")
            ctrl = [s for s in result["steps"] if s["name"] == "control_distribution"][0]
            video = [s for s in result["steps"] if s["name"] == "video_plus_control"][0]
            self.assertEqual(ctrl["metrics"]["status"], "not_measured")
            self.assertFalse(ctrl["ok"])
            self.assertFalse(video["ok"])
            self.assertFalse(result["all_ok"])


class LinkBudgetTests(unittest.TestCase):
    def test_section_24_reference_2400mhz_12km(self):
        r = compute(LinkBudgetInput(frequency_mhz=2400, distance_km=12))
        self.assertAlmostEqual(r.fspl_db, 121.6, delta=0.2)
        self.assertAlmostEqual(r.rx_power_dbm, -81.6, delta=0.3)
        self.assertAlmostEqual(r.margin_db, 17.4, delta=1.0)

    def test_earth_bulge_midpath_12km(self):
        bulge = earth_bulge_m(12.0)
        self.assertAlmostEqual(bulge, 2.12, delta=0.05)
        r = compute(LinkBudgetInput(frequency_mhz=2400, distance_km=12))
        self.assertAlmostEqual(r.earth_bulge_m, 2.12, delta=0.05)
        self.assertAlmostEqual(r.clearance_hint_m, 21.5, delta=0.2)

    def test_higher_frequency_more_loss(self):
        a = fspl_db(2400, 12)
        b = fspl_db(5800, 12)
        self.assertGreater(b, a)
        rows = compare_bands(12)
        self.assertEqual(len(rows), 3)


class InterfaceTests(unittest.TestCase):
    def test_sbus_roundtrip(self):
        ch = channels_neutral()
        ch[0] = 200
        ch[15] = 1800
        frame = encode_sbus(ch, digital1=True, failsafe=True)
        self.assertEqual(len(frame), SBUS_FRAME_LEN)
        out = decode_sbus(frame)
        self.assertEqual(out["channels"][0], 200)
        self.assertEqual(out["channels"][15], 1800)
        self.assertTrue(out["digital1"])
        self.assertTrue(out["failsafe"])

    def test_mavlink_seq_gaps(self):
        mon = MavlinkSeqMonitor()
        for seq in (0, 1, 2, 5):
            raw = mavlink_v1_header(0, seq, 1, 1, 0) + b""
            mon.observe(raw)
        self.assertEqual(mon.gaps, 1)
        self.assertEqual(mon.packets, 4)

    def test_failsafe_separate_timeouts(self):
        cfg = FailsafeConfig(rc_timeout_ms=100, data_timeout_ms=500,
                             rc_action=FailsafeAction.NEUTRAL_RC,
                             data_action=FailsafeAction.RTL)
        m = FailsafeMachine(cfg)
        m.note_rc(0)
        m.note_data(0)
        s = m.tick(150)
        self.assertTrue(s.rc_lost)
        self.assertFalse(s.data_lost)
        self.assertEqual(s.active_actions, (FailsafeAction.NEUTRAL_RC,))
        s = m.tick(600)
        self.assertTrue(s.data_lost)
        self.assertIn(FailsafeAction.RTL, s.active_actions)


class AuthTests(unittest.TestCase):
    def test_whitelist_and_replay(self):
        gw = AuthGateway(AuthConfig(whitelist={1, 2}, max_skew_ms=1000))
        blob = gw.seal(1, 1, b"hello", now_ms=10_000)
        node, seq, body = gw.open(blob, now_ms=10_100)
        self.assertEqual((node, seq, body), (1, 1, b"hello"))
        with self.assertRaises(AuthError):
            gw.open(blob, now_ms=10_100)
        with self.assertRaises(AuthError):
            gw.seal(99, 1, b"x", now_ms=10_000)

    def test_bad_mac_and_skew(self):
        gw = AuthGateway(AuthConfig(whitelist={1}, max_skew_ms=500))
        blob = bytearray(gw.seal(1, 3, b"x", now_ms=1000))
        blob[-1] ^= 0xFF
        with self.assertRaises(AuthError):
            gw.open(bytes(blob), now_ms=1000)
        good = gw.seal(1, 4, b"y", now_ms=1000)
        with self.assertRaises(AuthError):
            gw.open(good, now_ms=5000)

    def test_sliding_window_rejects_evicted_replay(self):
        gw = AuthGateway(AuthConfig(whitelist={1}, window=64, max_skew_ms=5000))
        packets = [gw.seal(1, i, b"test", now_ms=10_000) for i in range(65)]
        for packet in packets:
            gw.open(packet, now_ms=10_000)
        with self.assertRaises(AuthError):
            gw.open(packets[0], now_ms=10_100)

    def test_sliding_window_allows_reordered_once(self):
        gw = AuthGateway(AuthConfig(whitelist={1}, window=64, max_skew_ms=5000))
        p0 = gw.seal(1, 0, b"a", now_ms=1000)
        p2 = gw.seal(1, 2, b"c", now_ms=1000)
        p1 = gw.seal(1, 1, b"b", now_ms=1000)
        self.assertEqual(gw.open(p0, now_ms=1000)[1], 0)
        self.assertEqual(gw.open(p2, now_ms=1000)[1], 2)
        self.assertEqual(gw.open(p1, now_ms=1000)[1], 1)
        with self.assertRaises(AuthError):
            gw.open(p1, now_ms=1000)

    def test_begin_session_required_after_reboot(self):
        gw = AuthGateway(AuthConfig(whitelist={1}, window=8, max_skew_ms=5000))
        old = [gw.seal(1, i, b"x", now_ms=1000) for i in range(3)]
        for p in old:
            gw.open(p, now_ms=1000)
        with self.assertRaises(AuthError):
            gw.open(old[1], now_ms=1100)
        gw.begin_session(1)
        self.assertEqual(gw.open(gw.seal(1, 0, b"y", now_ms=2000), now_ms=2000)[1], 0)

    def test_begin_session_rejects_old_session_bags(self):
        gw = AuthGateway(AuthConfig(whitelist={1}, window=8, max_skew_ms=5000))
        old = gw.seal(1, 0, b"old", now_ms=1000)
        unused = gw.seal(1, 1, b"unused", now_ms=1000)
        gw.open(old, now_ms=1000)
        gw.begin_session(1)
        with self.assertRaises(AuthError) as seen:
            gw.open(old, now_ms=1100)
        self.assertEqual(str(seen.exception), "stale session")
        with self.assertRaises(AuthError) as unseen:
            gw.open(unused, now_ms=1100)
        self.assertEqual(str(unseen.exception), "stale session")
        self.assertEqual(gw.open(gw.seal(1, 0, b"new", now_ms=2000), now_ms=2000)[1], 0)


if __name__ == "__main__":
    unittest.main()
