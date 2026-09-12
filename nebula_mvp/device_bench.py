"""Hardware testbench skeleton: same scenarios against sim mock or real modules.

When lab hardware arrives, swap Transport from MockTransport / DryRun to
Iperf3Transport + UdpControlProbe pointing at real module IPs. No RF code here.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
import json
import math
import random
import statistics
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence


@dataclass
class StepResult:
    name: str
    ok: bool
    metrics: dict
    notes: str = ""


@dataclass
class BenchConfig:
    """Target endpoints — fill with real module addresses when hardware arrives."""
    mode: str = "dry-run"  # dry-run | mock | iperf3
    center_host: str = "192.168.1.10"
    terminal_host: str = "192.168.1.20"
    iperf_port: int = 5201
    control_port: int = 14550
    # Stepped UDP load (Mbps application intent).
    iperf_steps_mbps: Sequence[float] = (5, 10, 20, 30, 40)
    iperf_duration_s: float = 3.0
    video_inject_mbps: float = 8.0
    control_hz: float = 50.0
    control_seconds: float = 5.0
    control_payload_bytes: int = 64
    seed: int = 7


class Transport(ABC):
    @abstractmethod
    def measure_throughput(self, mbps: float, duration_s: float) -> dict:
        """Return goodput_mbps, loss_pct, notes."""

    @abstractmethod
    def inject_video_load(self, mbps: float, duration_s: float) -> dict:
        """Background video-like UDP flood; return actual offered/sent."""

    @abstractmethod
    def probe_control_latency(self, hz: float, seconds: float, payload: int) -> dict:
        """Send paced control-sized datagrams; return latency samples ms."""

    def close(self):
        pass


class DryRunTransport(Transport):
    """No sockets — deterministic placeholder so CI and Windows sync can run today."""

    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)

    def measure_throughput(self, mbps: float, duration_s: float) -> dict:
        # Soft knee around 28 Mbps to mimic a capacity cliff in dry-run demos.
        delivered = mbps if mbps <= 28 else 28 + (mbps - 28) * 0.15
        loss = max(0.0, (mbps - delivered) / mbps * 100) if mbps else 0.0
        return {"offered_mbps": mbps, "goodput_mbps": round(delivered, 3),
                "loss_pct": round(loss, 2), "duration_s": duration_s, "backend": "dry-run"}

    def inject_video_load(self, mbps: float, duration_s: float) -> dict:
        return {"offered_mbps": mbps, "duration_s": duration_s, "backend": "dry-run"}

    def probe_control_latency(self, hz: float, seconds: float, payload: int) -> dict:
        n = max(1, int(hz * seconds))
        # Baseline ~12 ms + load-independent jitter in dry-run (load effect added by runner).
        samples = [12.0 + self.rng.uniform(-2, 4) for _ in range(n)]
        return {"samples_ms": samples, "backend": "dry-run"}


class MockTransport(Transport):
    """In-process model: goodput saturates and control latency rises with video load."""

    def __init__(self, capacity_mbps: float = 30.0, seed: int = 7):
        self.capacity = capacity_mbps
        self.rng = random.Random(seed)
        self._video_mbps = 0.0

    def measure_throughput(self, mbps: float, duration_s: float) -> dict:
        headroom = max(0.5, self.capacity - self._video_mbps * 0.9)
        delivered = min(mbps, headroom)
        loss = max(0.0, (mbps - delivered) / mbps * 100) if mbps else 0.0
        return {"offered_mbps": mbps, "goodput_mbps": round(delivered, 3),
                "loss_pct": round(loss, 2), "duration_s": duration_s, "backend": "mock"}

    def inject_video_load(self, mbps: float, duration_s: float) -> dict:
        self._video_mbps = mbps
        return {"offered_mbps": mbps, "duration_s": duration_s, "backend": "mock"}

    def probe_control_latency(self, hz: float, seconds: float, payload: int) -> dict:
        n = max(1, int(hz * seconds))
        # Queueing rises sharply once video approaches capacity.
        load = self._video_mbps / self.capacity
        base = 8.0 + (120.0 if load > 0.85 else 25.0 * load)
        samples = [max(1.0, base + self.rng.gauss(0, 3 + 10 * load)) for _ in range(n)]
        return {"samples_ms": samples, "backend": "mock", "video_mbps": self._video_mbps}


class Iperf3Transport(Transport):
    """Real-device adapter stub: shells out to iperf3 when mode=iperf3.

    Requires iperf3 on PATH and reachable module IPs. Not invoked in unit tests.
    """

    def __init__(self, cfg: BenchConfig):
        self.cfg = cfg

    def measure_throughput(self, mbps: float, duration_s: float) -> dict:
        # Intentionally not executed unless operator opts in — document the command.
        cmd = (f"iperf3 -c {self.cfg.terminal_host} -u -b {mbps}M "
               f"-t {duration_s} -p {self.cfg.iperf_port} -J")
        raise NotImplementedError(
            "Real iperf3 run not auto-executed in this environment. "
            f"On the lab PC run:\n  {cmd}\n"
            "Then paste JSON into artifacts or extend this method to subprocess + parse."
        )

    def inject_video_load(self, mbps: float, duration_s: float) -> dict:
        raise NotImplementedError(
            f"Start background: iperf3 -c {self.cfg.terminal_host} -u -b {mbps}M "
            f"-t {duration_s} -p {self.cfg.iperf_port}"
        )

    def probe_control_latency(self, hz: float, seconds: float, payload: int) -> dict:
        raise NotImplementedError(
            "Point UdpControlProbe at module Ethernet; record send/recv timestamps "
            f"on {self.cfg.terminal_host}:{self.cfg.control_port} at {hz} Hz."
        )


def _latency_stats(samples: List[float]) -> dict:
    if not samples:
        return {"n": 0, "p50_ms": math.nan, "p99_ms": math.nan, "max_ms": math.nan}
    s = sorted(samples)
    def pct(p):
        k = min(len(s) - 1, max(0, int(math.ceil(p / 100 * len(s)) - 1)))
        return s[k]
    return {"n": len(s), "p50_ms": pct(50), "p99_ms": pct(99), "max_ms": s[-1],
            "mean_ms": statistics.fmean(s)}


def make_transport(cfg: BenchConfig) -> Transport:
    if cfg.mode == "dry-run":
        return DryRunTransport(cfg.seed)
    if cfg.mode == "mock":
        return MockTransport(seed=cfg.seed)
    if cfg.mode == "iperf3":
        return Iperf3Transport(cfg)
    raise ValueError(f"Unknown mode: {cfg.mode}")


class DeviceBench:
    """Three acceptance pillars from §6.2: stepped load, video+control, latency dist."""

    def __init__(self, cfg: BenchConfig, transport: Optional[Transport] = None):
        self.cfg = cfg
        self.transport = transport or make_transport(cfg)

    def run_iperf_steps(self) -> StepResult:
        rows = []
        for mbps in self.cfg.iperf_steps_mbps:
            rows.append(self.transport.measure_throughput(mbps, self.cfg.iperf_duration_s))
        # Knee = first step where loss > 5% or goodput < 0.85 * offered
        knee = None
        for row in rows:
            if row["loss_pct"] > 5 or row["goodput_mbps"] < 0.85 * row["offered_mbps"]:
                knee = row["offered_mbps"]
                break
        return StepResult("iperf_steps", True,
                          {"steps": rows, "knee_offered_mbps": knee},
                          notes="Application goodput vs offered; find saturation knee.")

    def run_video_plus_control(self) -> StepResult:
        self.transport.inject_video_load(self.cfg.video_inject_mbps, self.cfg.control_seconds)
        raw = self.transport.probe_control_latency(
            self.cfg.control_hz, self.cfg.control_seconds, self.cfg.control_payload_bytes)
        stats = _latency_stats(raw["samples_ms"])
        ok = stats["n"] > 0 and stats["p99_ms"] < 250
        return StepResult("video_plus_control", ok,
                          {"video_mbps": self.cfg.video_inject_mbps, "control": stats,
                           "backend": raw.get("backend")},
                          notes="Control latency under video full-load inject (§6.2 QoS).")

    def run_control_distribution(self) -> StepResult:
        # Idle control baseline (no video).
        if hasattr(self.transport, "_video_mbps"):
            self.transport._video_mbps = 0.0
        raw = self.transport.probe_control_latency(
            self.cfg.control_hz, self.cfg.control_seconds, self.cfg.control_payload_bytes)
        stats = _latency_stats(raw["samples_ms"])
        return StepResult("control_distribution", stats["n"] > 0,
                          {"control": stats, "backend": raw.get("backend")},
                          notes="Idle control latency distribution for comparison.")

    def run_all(self) -> dict:
        steps = [
            self.run_iperf_steps(),
            self.run_control_distribution(),
            self.run_video_plus_control(),
        ]
        return {
            "config": {
                "mode": self.cfg.mode,
                "center_host": self.cfg.center_host,
                "terminal_host": self.cfg.terminal_host,
                "iperf_steps_mbps": list(self.cfg.iperf_steps_mbps),
                "video_inject_mbps": self.cfg.video_inject_mbps,
            },
            "steps": [asdict(s) for s in steps],
            "all_ok": all(s.ok for s in steps),
        }


def write_report(result: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "device-bench.json"
    md_path = output_dir / "device-bench.md"
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    lines = [
        "# 真设备测试台运行记录",
        "",
        f"- mode: `{result['config']['mode']}`",
        f"- center: `{result['config']['center_host']}` / terminal: `{result['config']['terminal_host']}`",
        f"- all_ok: **{result['all_ok']}**",
        "",
        "## 步骤",
        "",
    ]
    for step in result["steps"]:
        lines.append(f"### {step['name']} — {'PASS' if step['ok'] else 'FAIL'}")
        lines.append("")
        lines.append(f"{step.get('notes', '')}")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(step["metrics"], indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")
    lines.extend([
        "## 样机到达当日清单",
        "",
        "1. 中心 / 终端以太网互通，记录 IP；改 `BenchConfig.center_host` / `terminal_host`。",
        "2. 终端侧启动 `iperf3 -s`；本机 `--mode iperf3`（需补全 `Iperf3Transport` 的 subprocess 解析，或手工跑命令把 JSON 放进本目录）。",
        "3. 遥控探测：对透传 UDP/UART 隧道发 50 Hz 小包，采集单端或 PPS 对齐时延。",
        "4. 对比 dry-run / mock 曲线与真机拐点；拥塞项以「视频满载 + 控制 P99」为准。",
        "",
        f"原始数据：`{json_path.name}`",
    ])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Device testbench (dry-run/mock/iperf3 stub)")
    parser.add_argument("--mode", choices=("dry-run", "mock", "iperf3"), default="dry-run")
    parser.add_argument("--output", default="artifacts/cli-runs/device-bench")
    parser.add_argument("--center", default="192.168.1.10")
    parser.add_argument("--terminal", default="192.168.1.20")
    parser.add_argument("--video-mbps", type=float, default=8.0)
    args = parser.parse_args(argv)
    cfg = BenchConfig(mode=args.mode, center_host=args.center, terminal_host=args.terminal,
                      video_inject_mbps=args.video_mbps)
    if args.mode == "iperf3":
        print("iperf3 mode is a real-device adapter stub; use dry-run or mock until lab IPs ready.")
        print(Iperf3Transport(cfg).measure_throughput.__doc__)
        return
    bench = DeviceBench(cfg)
    result = bench.run_all()
    path = write_report(result, Path(args.output))
    print(f"all_ok={result['all_ok']} report={path}")


if __name__ == "__main__":
    main()
