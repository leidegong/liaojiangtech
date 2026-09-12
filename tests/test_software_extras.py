"""Tests for device bench, link budget, interfaces, auth."""
import math
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
                    __import__("json").dumps(payload), encoding="utf-8")
            cfg = BenchConfig(mode="iperf3", control_seconds=0.2, control_hz=20)
            transport = Iperf3Transport(cfg, json_dir=root, run_subprocess=False)
            step = DeviceBench(cfg, transport).run_iperf_steps()
            self.assertEqual(step.metrics["knee_offered_mbps"], 30)


class LinkBudgetTests(unittest.TestCase):
    def test_section_24_reference_2400mhz_12km(self):
        # §2.4: FSPL ≈ 121.6 dB, Rx ≈ -81.6 dBm, margin ≈ 17 dB
        r = compute(LinkBudgetInput(frequency_mhz=2400, distance_km=12))
        self.assertAlmostEqual(r.fspl_db, 121.6, delta=0.2)
        self.assertAlmostEqual(r.rx_power_dbm, -81.6, delta=0.3)
        self.assertAlmostEqual(r.margin_db, 17.4, delta=1.0)

    def test_earth_bulge_midpath_12km(self):
        # REVIEW §2.2: h = d²/(8kR) → ~2.12 m at 12 km, k=4/3 (not 8.42 m).
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
            gw.open(blob, now_ms=10_100)  # replay
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


if __name__ == "__main__":
    unittest.main()
