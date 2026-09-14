"""Reproducible live-UDP A/B experiment, never canned latency results."""
import argparse
import asyncio
from datetime import datetime
import json
import math
from pathlib import Path
import time
from .app import Application
from .config import LinkConfig
from .clock import PreciseEventLoop

# Trials are (name, scheduler mode, layered video, FEC, loss burst or None for
# --burst). Every trial warms up at 5 Mbps without loss, then switches to the
# scenario's capacity and loss.
SCENARIOS = {
    # The first two reproduce the original FIFO/Fusion comparison; the third
    # changes only the video layering (FEC stays idle without packet loss).
    "congestion": {"trials": (("naive", "naive", False, False, None), ("fusion", "fusion", False, False, None),
                              ("fusion-layered", "fusion", True, True, None)),
                   "mbps": .5, "loss": 0, "seconds": 10},
    # Capacity stays high and random packet loss starts; the three trials add
    # layering and then FEC one at a time.
    "loss": {"trials": (("fusion", "fusion", False, False, None), ("layered", "fusion", True, False, None),
                        ("layered-fec", "fusion", True, True, None)),
             "mbps": 5, "loss": 5, "seconds": 10},
    # Same average loss as "loss", arriving in ever longer runs of consecutive
    # packets, against layered video with FEC. Long bursts are rare events, so
    # each trial runs longer.
    "burst": {"trials": tuple((f"burst-{b}", "fusion", True, True, b) for b in (1, 2, 4, 8, 16)),
              "mbps": 5, "loss": 5, "seconds": 20},
}
TRANSITION_S = 3  # Window right after the switch.


async def trial(name, mode, layered, fec, burst, args, output):
    app = Application(LinkConfig(mode=mode, capacity_bps=5_000_000, delay_ms=20, jitter_ms=5),
                      output_dir=output / name, seed=args.seed, layered=layered, fec=fec)
    try:
        await app.start()
        start = time.perf_counter()
        while time.perf_counter() - start < args.warmup:
            await asyncio.sleep(.1)
            app.check_tasks()
        baseline = await app.state()
        app.link.update({"capacity_bps": int(args.mbps * 1e6), "loss": args.loss / 100,
                         "loss_burst": args.burst if burst is None else burst})
        start = time.perf_counter()
        transition = None
        while time.perf_counter() - start < args.seconds:
            app.ground.set_control({"throttle": .6, "yaw": .1, "pitch": .2, "roll": -.1})
            await asyncio.sleep(.1)
            app.check_tasks()
            if transition is None and time.perf_counter() - start >= TRANSITION_S:
                # Exactly the seconds right after the switch, including the last
                # picture shown before it.
                transition = app.metrics.snapshot(window=time.perf_counter() - start)
        congested = await app.state()
        # Everything after the transition window, for rare events such as long bursts.
        steady = app.metrics.snapshot(window=args.seconds - TRANSITION_S)
        result = {"baseline": baseline, "transition": transition, "congested": congested, "steady": steady}
        (output / name / "experiment.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
    finally:
        await app.close()


def row(name, scheduler, layered, fec, burst, result, mbps):
    m, before = result["congested"]["metrics"], result["baseline"]["metrics"]
    middle, steady = result["transition"], result["steady"]
    rung = result["congested"]["video"]["full_rung"]

    def between(start, end, key, bucket="counts"):
        return end[bucket].get(key, 0) - start[bucket].get(key, 0)

    def since_switch(key, bucket="counts"):
        return between(before, m, key, bucket)

    def lost_share(start, end):
        """Full-resolution frames (the only layer when single-layer) that loss
        made unrecoverable, between two snapshots."""
        generated = between(start, end, "video_frames_generated")
        return round(between(start, end, "frame:link_loss", "drops") / generated, 4) if generated else None

    full_frames = since_switch("video_frames_generated")
    sent, lost = since_switch("sent"), since_switch("link_loss", "drops")
    runs, captures = since_switch("link_loss_runs"), between(middle, m, "frames_generated")
    return {"mode": name, "scheduler": scheduler, "layered": layered, "fec": fec, "loss_burst": burst,
            # What the channel actually did after the switch, to check the loss model.
            "observed_loss": round(lost / sent, 4) if sent else None,
            "observed_burst": round(lost / runs, 2) if runs else None,
            "packets_sent": sent, "loss_runs": runs,
            "control_p50_ms": m["latency_ms"]["CONTROL"]["p50"],
            "control_p95_ms": m["latency_ms"]["CONTROL"]["p95"],
            "control_p99_ms": m["latency_ms"]["CONTROL"]["p99"],
            "control_samples": m["latency_ms"]["CONTROL"]["count"],
            "data_p95_ms": m["latency_ms"]["DATA"]["p95"],
            "video_fps": round(m["video_fps"], 2),
            "video_latency_ms": m["video_latency_ms"]["mean"],
            "video_latency_p95_ms": m["video_latency_ms"]["p95"],
            "video_full_share": m["video_full_share"],
            "video_max_gap_ms": m["video_max_gap_ms"],
            "drop_freeze_ms": result["transition"]["video_max_gap_ms"],
            "drop_fps": round(result["transition"]["video_fps"], 2),
            # While the loss estimate catches up after the switch, then steady state.
            "full_frames_lost_first": lost_share(before, middle),
            "full_frames_lost_after": lost_share(middle, m),
            "fec_recovered_frames": since_switch("video_frames_recovered"),
            # Full frames that came after their base was already shown (a brief
            # blurry-then-sharp flicker): the wait for the full layer was too short.
            "late_full_frames": since_switch("video_upgrades"),
            "parity_per_frame": round(since_switch("fec_parity_packets") / full_frames, 2) if full_frames else None,
            # Over the whole steady period rather than the last five seconds.
            "captures_shown_after": round(between(middle, m, "frames_received") / captures, 4) if captures else None,
            "steady_fps": round(steady["video_fps"], 2),
            "steady_full_share": steady["video_full_share"],
            "steady_max_gap_ms": steady["video_max_gap_ms"],
            "full_rung_at_end": "x".join(map(str, rung[:2])) + f" q{rung[2]}" if rung else "paused",
            "wire_mbps": round(m["wire_mbps"], 3),
            "link_utilization": round(m["wire_mbps"] / mbps, 3),
            "link_pacing_efficiency": m["link_pacing_efficiency"],
            "dispatch_late_p95_ms": m["link_dispatch_late_ms"]["p95"],
            "dropped_frames": m["counts"].get("frames_dropped", 0)}


def fmt(value, unit="", digits=0):
    return "—" if value is None else f"{value:.{digits}f}{unit}"


def percent(value, digits=0):
    return fmt(None if value is None else value * 100, "%", digits)


COLUMNS = {
    "congestion": (("Control P50", lambda r: f"{r['control_p50_ms']} ms"),
                   ("Control P95", lambda r: f"{r['control_p95_ms']} ms"),
                   ("Control P99", lambda r: f"{r['control_p99_ms']} ms"),
                   ("Data P95", lambda r: f"{r['data_p95_ms']} ms"),
                   ("Video FPS", lambda r: r["video_fps"]),
                   ("Video latency", lambda r: f"{r['video_latency_ms']} ms"),
                   ("Full-res share", lambda r: percent(r["video_full_share"])),
                   ("Longest freeze", lambda r: fmt(r["video_max_gap_ms"], " ms")),
                   ("Freeze after drop", lambda r: fmt(r["drop_freeze_ms"], " ms")),
                   ("Full rung", lambda r: r["full_rung_at_end"]),
                   ("Link use", lambda r: percent(r["link_utilization"]))),
    "loss": (("Control P99", lambda r: f"{r['control_p99_ms']} ms"),
             ("Video FPS", lambda r: r["video_fps"]),
             ("Video latency", lambda r: f"{r['video_latency_ms']} ms"),
             ("Full-res share", lambda r: percent(r["video_full_share"])),
             ("Longest freeze", lambda r: fmt(r["video_max_gap_ms"], " ms")),
             (f"Full frames lost, first {TRANSITION_S} s", lambda r: percent(r["full_frames_lost_first"], 1)),
             ("Full frames lost, after", lambda r: percent(r["full_frames_lost_after"], 1)),
             ("Recovered by FEC", lambda r: r["fec_recovered_frames"]),
             ("Late full frames", lambda r: r["late_full_frames"]),
             ("Parity / frame", lambda r: fmt(r["parity_per_frame"], "", 2)),
             ("Full rung", lambda r: r["full_rung_at_end"]),
             ("Link use", lambda r: percent(r["link_utilization"]))),
    "burst": (("Burst set", lambda r: r["loss_burst"]),
              ("Loss seen", lambda r: percent(r["observed_loss"], 1)),
              ("Burst seen", lambda r: fmt(r["observed_burst"], "", 2)),
              ("Full frames lost, after", lambda r: percent(r["full_frames_lost_after"], 1)),
              ("Recovered by FEC", lambda r: r["fec_recovered_frames"]),
              ("Parity / frame", lambda r: fmt(r["parity_per_frame"], "", 2)),
              ("Captures shown", lambda r: percent(r["captures_shown_after"], 1)),
              ("Full-res share", lambda r: percent(r["steady_full_share"])),
              ("Video FPS", lambda r: r["steady_fps"]),
              ("Longest freeze", lambda r: fmt(r["steady_max_gap_ms"], " ms")),
              ("Control P99", lambda r: f"{r['control_p99_ms']} ms")),
}
INTRO = {
    "congestion": "`naive` and `fusion` send one fixed 640x360 q50 JPEG per frame; `fusion-layered` sends a "
                  "160x90 base layer plus a full-resolution layer sized to the spare capacity.",
    "loss": "`fusion` sends one fixed 640x360 q50 JPEG per frame; `layered` adds the 160x90 base layer; "
            "`layered-fec` also adds Reed-Solomon parity to the full-resolution layer, sized to the measured loss. "
            f"Full frames lost is the share of full-resolution frames loss made unrecoverable: over the first "
            f"{TRANSITION_S} seconds after the switch, while the loss estimate catches up, and over the rest. "
            "Late full frames arrived after their base had already been shown in their place.",
    "burst": "Every trial sends layered video with FEC; only the mean run of consecutive lost packets "
             "(Gilbert channel) changes, at the same average loss. Mean runs of 1.5 packets or more also "
             "interleave two or three full-resolution frames on the wire and may add parity. Loss and burst "
             "seen are what the channel actually did. Lost, captures shown, full-res share, FPS and freeze "
             f"cover everything after the first {TRANSITION_S} seconds; control P99 covers the final 5 seconds.",
}


def check(scenario, rows, results, args):
    by = {r["mode"]: r for r in rows}
    for name, result in results.items():
        baseline = result["baseline"]["metrics"]
        assert baseline["video_fps"] >= 12, f"{name}: high-bandwidth baseline is not healthy"
        assert baseline["latency_ms"]["CONTROL"]["p99"] < 250, f"{name}: baseline already congested"
        # A stalling host loses emulated airtime or delivers late, which would
        # invalidate the comparison (healthy runs: 1.0 and about 1.4 ms).
        congested = result["congested"]["metrics"]
        efficiency = congested["link_pacing_efficiency"]
        late = congested["link_dispatch_late_ms"]["p95"]
        assert efficiency is not None and efficiency >= .98, f"{name}: link lost airtime ({efficiency}); rerun on an idle host"
        assert late is not None and late < 5, f"{name}: deliveries {late} ms late at P95; rerun on an idle host"
    for r in rows:
        # A FIFO only passes controls in proportion to their share of the traffic.
        if r["scheduler"] == "fusion":
            assert r["control_samples"] >= 50, f"{r['mode']}: too few control samples"
        assert r["wire_mbps"] <= args.mbps * 1.12, f"{r['mode']}: shared bandwidth cap exceeded"
    if scenario == "congestion":
        naive, fusion, layered = by["naive"], by["fusion"], by["fusion-layered"]
        assert fusion["control_p99_ms"] is not None and fusion["control_p99_ms"] < 250, "Fusion control unexpectedly delayed"
        assert naive["control_p99_ms"] is not None and naive["control_p99_ms"] > fusion["control_p99_ms"] * 3, "No clear A/B congestion separation"
        assert fusion["video_fps"] > 0, "Fusion must still deliver complete video frames"
        assert layered["control_p99_ms"] is not None and layered["control_p99_ms"] < 250, "Layered video delayed control"
        assert layered["video_fps"] >= 10, "Base layer must keep the picture moving"
        assert layered["video_fps"] >= fusion["video_fps"] * 3, "Layering did not clearly raise the shown frame rate"
        assert layered["video_latency_p95_ms"] is not None and layered["video_latency_p95_ms"] < 250, "Layered video is stale"
        assert layered["video_max_gap_ms"] is not None and layered["video_max_gap_ms"] < 500, "Layered picture froze"
        assert layered["drop_freeze_ms"] is not None and layered["drop_freeze_ms"] < 400, "Layered picture froze after the drop"
        share = results["fusion-layered"]["baseline"]["metrics"]["video_full_share"]
        assert share is not None and share >= .8, "Full-resolution layer unused at 5 Mbps"
    elif scenario == "burst":
        loss = args.loss / 100
        for r in rows:
            assert r["control_p99_ms"] is not None and r["control_p99_ms"] < 250, f"{r['mode']}: control delayed"
            # The channel must do what was asked before its effect on FEC means
            # anything. Long bursts are few, so allow three standard deviations
            # of a sum of geometric runs (the unit tests check the model tightly).
            burst = max(r["loss_burst"], 1 / (1 - loss))
            spread = 3 * math.sqrt(2 * burst * loss / r["packets_sent"])
            assert abs(r["observed_loss"] - loss) <= spread, f"{r['mode']}: loss seen {r['observed_loss']}"
            assert abs(r["observed_burst"] - burst) <= 3 * burst / math.sqrt(r["loss_runs"]), \
                f"{r['mode']}: burst seen {r['observed_burst']}"
        lost = by["burst-1"]["full_frames_lost_after"]
        assert lost is not None and lost <= .02, "Independent loss no longer matches the loss scenario"
        for name, cap in (("burst-2", .05), ("burst-4", .06), ("burst-8", .08), ("burst-16", .12)):
            lost = by[name]["full_frames_lost_after"]
            assert lost is not None and lost <= cap, \
                f"{name}: interleave+FEC left {lost:.1%} full frames unrecoverable (cap {cap:.0%})"
    else:
        plain, protected = by["layered"], by["layered-fec"]
        for r in rows:
            assert r["control_p99_ms"] is not None and r["control_p99_ms"] < 250, f"{r['mode']}: control delayed"
        lost = plain["full_frames_lost_after"]
        assert lost is not None and lost >= .3, "Loss did not hurt unprotected frames"
        # Without FEC most full frames are lost; the base of each capture must stand
        # in. At 5% loss both layers of about 6% of captures are lost, so the
        # ceiling is about 14.1 FPS (the old 150 ms hold stalled it at 8-9).
        assert plain["video_fps"] >= 13, "Lost full frames stalled the layered picture"
        # Two captures in a row losing both layers stop the picture for three
        # frame intervals (~200 ms) in about a quarter of 5 s windows; three in a
        # row (~270 ms) in about 2%. Longer means the display itself stalled.
        assert plain["video_max_gap_ms"] is not None and plain["video_max_gap_ms"] < 300, "Layered picture froze under loss"
        lost = protected["full_frames_lost_after"]
        assert lost is not None and lost <= .02, "FEC left too many frames unrecoverable"
        assert protected["video_full_share"] is not None and protected["video_full_share"] >= .9, "FEC did not keep the full layer on screen"
        assert protected["video_fps"] >= 14, "Protected video is not smooth"
        assert protected["video_latency_p95_ms"] is not None and protected["video_latency_p95_ms"] < 250, "FEC made video stale"


async def run(args):
    output = Path(args.output or f"artifacts/{args.scenario}-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    results, rows = {}, []
    for name, mode, layered, fec, burst in SCENARIOS[args.scenario]["trials"]:
        burst = args.burst if burst is None else burst
        video = ("layered" if layered else "single-layer") + (" + FEC" if fec else "")
        print(f"Running {name} ({video}): 5 Mbps for {args.warmup}s -> {args.mbps} Mbps, "
              f"{args.loss}% loss in bursts of {burst} for {args.seconds}s", flush=True)
        results[name] = await trial(name, mode, layered, fec, burst, args, output)
        rows.append(row(name, mode, layered, fec, burst, results[name], args.mbps))
    report = {"configuration": vars(args), "results": rows}
    (output / "comparison.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    columns = COLUMNS[args.scenario]
    header = ("| Mode | " + " | ".join(title for title, _ in columns) + " |\n"
              "|---|" + "|".join("---" if title == "Full rung" else "---:" for title, _ in columns) + "|\n")
    body = "\n".join("| " + " | ".join([r["mode"], *(str(cell(r)) for _, cell in columns)]) + " |" for r in rows)
    (output / "comparison.md").write_text(f"# Live UDP comparison: {args.scenario}\n\n" +
        f"Same synthetic scene, seed={args.seed}, 15 FPS, 10 Hz telemetry, 50 Hz control. " +
        f"5 Mbps lossless warmup, then {args.mbps} Mbps shared link with {args.loss}% random packet loss "
        f"(mean burst: {'per trial' if args.scenario == 'burst' else args.burst} packets); " +
        "delay 20 ms, jitter ±5 ms. " + INTRO[args.scenario] + " Statistics use the final 5 seconds. " +
        "Video FPS counts newly shown captures; latency ends when the ground adopts the frame as the picture "
        "(complete receipt, plus any wait of a base frame for its full frame), not at browser rendering. " +
        "Longest freeze includes any freeze still in progress when the window closed; freeze after drop is " +
        f"the longest one in the first {TRANSITION_S} seconds after the switch.\n\n" +
        header + body + "\n\nSee packets.csv for raw per-packet timing and timeline.jsonl for the transition.\n",
        encoding="utf-8")
    print(header + body, flush=True)
    print(f"Results: {output.resolve()}", flush=True)
    if args.verify:
        check(args.scenario, rows, results, args)
        print(f"{args.scenario.capitalize()} acceptance checks passed.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="congestion")
    parser.add_argument("--warmup", type=float, default=3)
    parser.add_argument("--seconds", type=float, help="run after the switch (default: per scenario)")
    parser.add_argument("--mbps", type=float, help="capacity after the switch (default: per scenario)")
    parser.add_argument("--loss", type=float, help="packet loss percent after the switch (default: per scenario)")
    parser.add_argument("--burst", type=float, default=1,
                        help="mean run of consecutive lost packets (1 = independent; the burst scenario sets its own)")
    # The seed drives jitter and packet loss. Reruns with one seed see nearly the
    # same loss sequence (shifted by however many packets the warmup sent), so
    # they repeat the timing, not the channel; vary it for independent samples.
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    defaults = SCENARIOS[args.scenario]
    for key in ("mbps", "loss", "seconds"):
        if getattr(args, key) is None:
            setattr(args, key, defaults[key])
    if args.warmup < 0 or args.seconds < 6:
        parser.error("warmup must be nonnegative; seconds must be at least 6 for a full statistics window")
    LinkConfig().updated({"capacity_bps": int(args.mbps * 1e6), "loss": args.loss / 100, "loss_burst": args.burst})
    with asyncio.Runner(loop_factory=PreciseEventLoop) as runner:
        runner.run(run(args))


if __name__ == "__main__":
    main()
