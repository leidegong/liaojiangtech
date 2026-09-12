"""Unit tests for multi-node scheduling simulation."""
import math
import unittest
from nebula_mvp.multinode import (
    SimConfig, build_terminals, run_sim, scenario_how_many_videos,
    scenario_weak_link_isolation, scenario_primary_switch, run_all_scenarios,
)


class MultiNodeTests(unittest.TestCase):
    def test_n_configurable_to_16(self):
        cfg = SimConfig(n_terminals=16, capacity_bps=40_000_000, policy="fusion_fair",
                        duration_s=1.5, warmup_s=0.3)
        terminals = build_terminals(cfg, primary_ids=[0])
        result = run_sim(cfg, terminals)
        self.assertEqual(len(result["nodes"]), 16)
        self.assertTrue(all(n["control_samples"] > 0 for n in result["nodes"]))

    def test_fair_does_not_starve_under_overload(self):
        # REVIEW §2.1: old RR used index into a shrinking non-empty list and
        # deterministically starved 6 of 16 primaries to exactly 0 Mbps.
        cfg = SimConfig(n_terminals=16, capacity_bps=40_000_000, policy="fusion_fair",
                        duration_s=2.5, warmup_s=0.4)
        result = run_sim(cfg, build_terminals(cfg, primary_ids=range(16)))
        rates = [n["video_delivered_mbps"] for n in result["nodes"]]
        self.assertEqual(len(rates), 16)
        self.assertGreater(min(rates), 0.5)
        self.assertLess(max(rates) / min(rates), 3.0)

    def test_more_videos_starves_beyond_budget(self):
        low = scenario_how_many_videos(40_000_000, 16, "fusion_fair")
        self.assertGreaterEqual(low["max_usable_primaries"], 4)
        self.assertLess(low["max_usable_primaries"], 16)
        self.assertEqual(low["engineering_max_algo2"], 6)
        row16 = low["rows"][16]
        self.assertLess(row16["usable_ge_3_5_mbps"], 16)
        self.assertFalse(row16["all_primaries_usable"])
        # After fair RR fix: overload must not show the old 0→9 non-monotone jump.
        u15 = low["rows"][15]["usable_ge_3_5_mbps"]
        u16 = low["rows"][16]["usable_ge_3_5_mbps"]
        self.assertLessEqual(abs(u15 - u16), 2)
        self.assertLess(u16, 4)  # equal share ≈ 2 Mbps; not 9 full streams

    def test_weak_link_fifo_worse_than_fair_for_peers(self):
        result = scenario_weak_link_isolation(40_000_000, 16)
        by = {r["policy"]: r for r in result["rows"]}
        self.assertGreater(by["fifo"]["others_control_p99_ms_max"],
                           by["fusion_fair"]["others_control_p99_ms_max"] * 1.5)
        self.assertLess(by["fusion_fair"]["others_control_p99_ms_max"], 150)

    def test_primary_switch_peers_stay_ok(self):
        result = scenario_primary_switch(40_000_000, 16, "fusion_fair")
        self.assertLess(result["peers_after_p99_ms"], 100)
        self.assertGreater(result["peers_after_samples"], 50)

    def test_run_all_writes_conclusions(self):
        results = run_all_scenarios(40.0, 8)  # smaller N for speed in suite
        self.assertIn("how_many_videos", results)
        self.assertTrue(results["how_many_videos"]["conclusion"])
        self.assertTrue(math.isfinite(results["primary_switch"]["peers_before_p99_ms"]))


if __name__ == "__main__":
    unittest.main()
