"""Hardware testbench skeleton: same scenarios against sim mock or real modules.

When lab hardware arrives, swap Transport from MockTransport / DryRun to
Iperf3Transport pointing at real module IPs. No RF code here.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
import json
import math
import random
import shutil
import statistics
import struct
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union


@dataclass
class StepResult:
    name: str
    ok: bool
    metrics: dict
    notes: str = ""


@dataclass
class BenchConfig:
    mode: str = "dry-run"  # dry-run | mock | iperf3
    # Target endpoints: fill with real module addresses when hardware arrives.
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
        return {"offered_mbps": mbps, "duration_s": duration_s, "backend": "dry-run",
                "live": True, "started": True}

    def probe_control_latency(self, hz: float, seconds: float, payload: int) -> dict:
        n = max(1, int(hz * seconds))
        # Baseline ~12 ms + load-independent jitter in dry-run (load effect added by runner).
        samples = [12.0 + self.rng.uniform(-2, 4) for _ in range(n)]
        return {"samples_ms": samples, "backend": "dry-run", "mode": "dry-run",
                "measured": True, "metric": "synthetic_ms", "sent": n, "matched": n,
                "timeouts": 0, "delivery_rate": 1.0, "actual_hz": hz}


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
        return {"offered_mbps": mbps, "duration_s": duration_s, "backend": "mock",
                "live": True, "started": True}

    def probe_control_latency(self, hz: float, seconds: float, payload: int) -> dict:
        n = max(1, int(hz * seconds))
        # Queueing rises sharply once video approaches capacity.
        load = self._video_mbps / self.capacity
        base = 8.0 + (120.0 if load > 0.85 else 25.0 * load)
        samples = [max(1.0, base + self.rng.gauss(0, 3 + 10 * load)) for _ in range(n)]
        return {"samples_ms": samples, "backend": "mock", "mode": "mock",
                "measured": True, "metric": "synthetic_ms", "video_mbps": self._video_mbps,
                "sent": n, "matched": n, "timeouts": 0, "delivery_rate": 1.0, "actual_hz": hz}


def parse_iperf3_json(payload: Union[str, bytes, dict]) -> dict:
    """Parse `iperf3 -J` client JSON into the bench throughput row shape.

    Works offline from a saved report; no live server required for unit tests.
    Prefers UDP end fields; falls back to TCP sum_received / sum_sent.
    """
    data: Any = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    end = data.get("end") or {}
    # UDP: end["sum"] has bits_per_second and lost_percent; TCP uses sum_received.
    summary = end.get("sum") or end.get("sum_received") or end.get("sum_sent") or {}
    bps = float(summary.get("bits_per_second") or 0.0)
    goodput_mbps = bps / 1e6
    if "lost_percent" in summary:
        loss_pct = float(summary["lost_percent"])
    else:
        lost = float(summary.get("lost_packets") or 0)
        sent = float(summary.get("packets") or summary.get("retransmits") or 0)
        loss_pct = (lost / sent * 100.0) if sent else 0.0
    start = data.get("start") or {}
    target = start.get("target_bitrate")  # bits/s when present
    offered = (float(target) / 1e6) if target else goodput_mbps
    # Some builds only put -b in the command line string.
    if not target:
        for key in ("test_start",):
            ts = start.get(key) or {}
            if ts.get("target_bitrate"):
                offered = float(ts["target_bitrate"]) / 1e6
                break
    duration = float(summary.get("seconds") or start.get("test_start", {}).get("duration") or 0)
    return {
        "offered_mbps": round(offered, 3),
        "goodput_mbps": round(goodput_mbps, 3),
        "loss_pct": round(loss_pct, 2),
        "duration_s": duration,
        "backend": "iperf3-json",
        "raw_keys": sorted(end.keys()),
    }


PROBE_MAGIC = b"N7P1"
PROBE_HDR = struct.Struct("!4sI")  # magic + seq


class Iperf3Transport(Transport):
    """Lab adapter: subprocess `iperf3 -J` or offline JSON (no RF code).

    Offline (`run_subprocess=False`) only reads saved JSON — no sockets, no
    subprocess. Live mode needs `iperf3` on PATH and a reachable echo/iperf server.
    """

    def __init__(self, cfg: BenchConfig, json_dir: Optional[Path] = None,
                 run_subprocess: bool = True, echo_timeout_s: float = 0.05):
        self.cfg = cfg
        self.json_dir = Path(json_dir) if json_dir else None
        self.run_subprocess = run_subprocess
        self.echo_timeout_s = echo_timeout_s
        self._video_proc: Optional[subprocess.Popen] = None
        self._video_mbps = 0.0
        self._video_live = False

    @property
    def offline(self) -> bool:
        return not self.run_subprocess

    def _cmd(self, mbps: float, duration_s: float) -> List[str]:
        return [
            "iperf3", "-c", self.cfg.terminal_host, "-u",
            "-b", f"{mbps}M", "-t", str(duration_s),
            "-p", str(self.cfg.iperf_port), "-J",
        ]

    def measure_throughput(self, mbps: float, duration_s: float) -> dict:
        if self.json_dir:
            path = self.json_dir / f"iperf-{mbps:g}M.json"
            if path.exists():
                row = parse_iperf3_json(path.read_text(encoding="utf-8"))
                row["offered_mbps"] = mbps
                row["duration_s"] = duration_s
                row["source"] = str(path)
                row["mode"] = "offline" if self.offline else "live-or-cache"
                return row
        if self.offline:
            raise FileNotFoundError(
                f"Offline mode: missing {self.json_dir}/iperf-{mbps:g}M.json"
            )
        if not shutil.which("iperf3"):
            raise FileNotFoundError(
                "iperf3 not on PATH. Install it, or drop `iperf3 -J` JSON under "
                f"{self.json_dir or 'json_dir'} as iperf-<Mbps>M.json"
            )
        cmd = self._cmd(mbps, duration_s)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=duration_s + 30)
        if proc.returncode != 0:
            raise RuntimeError(
                f"iperf3 failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout[:200]}"
            )
        row = parse_iperf3_json(proc.stdout)
        row["offered_mbps"] = mbps
        row["duration_s"] = duration_s
        row["backend"] = "iperf3"
        row["mode"] = "live"
        row["cmd"] = " ".join(cmd)
        return row

    def inject_video_load(self, mbps: float, duration_s: float) -> dict:
        self._video_mbps = mbps
        if self.offline:
            self._video_live = False
            return {"offered_mbps": mbps, "duration_s": duration_s,
                    "backend": "iperf3-json-offline", "live": False, "started": False,
                    "note": "offline: no live video flood"}
        if not shutil.which("iperf3"):
            self._video_live = False
            return {"offered_mbps": mbps, "duration_s": duration_s,
                    "backend": "iperf3", "live": False, "started": False,
                    "note": "iperf3 not on PATH for video inject"}
        cmd = self._cmd(mbps, duration_s)
        self._video_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.perf_counter() + 0.2
        while time.perf_counter() < deadline:
            rc = self._video_proc.poll()
            if rc is not None:
                err = ""
                if self._video_proc.stderr:
                    err = (self._video_proc.stderr.read() or "")[:200]
                self._video_live = False
                return {"offered_mbps": mbps, "duration_s": duration_s,
                        "backend": "iperf3", "live": False, "started": False,
                        "returncode": rc, "cmd": " ".join(cmd),
                        "note": f"video load exited immediately ({rc}): {err}"}
            time.sleep(0.02)
        self._video_live = True
        return {"offered_mbps": mbps, "duration_s": duration_s,
                "backend": "iperf3", "live": True, "started": True,
                "pid": self._video_proc.pid, "cmd": " ".join(cmd)}

    def video_load_running(self) -> bool:
        return self._video_proc is not None and self._video_proc.poll() is None

    def probe_control_latency(self, hz: float, seconds: float, payload: int) -> dict:
        """Measure RTT to an echo peer using sequenced UDP probes.

        Timeouts are losses, not latency samples. Offline mode never opens a socket.
        Metric is RTT (requires echo); not one-way delay without clock sync.
        """
        n = max(1, int(hz * seconds))
        if self.offline:
            path = self.json_dir / "control-latency.json" if self.json_dir else None
            if path and path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                data.setdefault("measured", True)
                data.setdefault("mode", "offline")
                data.setdefault("metric", "rtt_ms")
                data["source"] = str(path)
                return data
            return {
                "samples_ms": [],
                "backend": "offline-not-measured",
                "mode": "offline",
                "measured": False,
                "metric": "rtt_ms",
                "sent": 0,
                "matched": 0,
                "timeouts": 0,
                "bad_echo": 0,
                "late": 0,
                "delivery_rate": 0.0,
                "actual_hz": 0.0,
                "video_mbps": self._video_mbps,
                "note": "offline mode does not send UDP; provide control-latency.json to replay",
            }

        import socket
        import select

        interval = 1.0 / hz if hz > 0 else 0.02
        pad_len = max(0, payload - PROBE_HDR.size)
        pad = bytes([0xA5]) * pad_len
        addr = (self.cfg.terminal_host, self.cfg.control_port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        pending: Dict[int, float] = {}
        samples: List[float] = []
        timeouts = matched = bad_echo = late = 0
        sent = 0
        t_start = time.perf_counter()
        next_send = t_start
        try:
            while sent < n or pending:
                now = time.perf_counter()
                for seq, t_send in list(pending.items()):
                    if now - t_send > self.echo_timeout_s:
                        del pending[seq]
                        timeouts += 1
                readable, _, _ = select.select([sock], [], [], 0.001)
                if readable:
                    while True:
                        try:
                            data, src = sock.recvfrom(2048)
                        except BlockingIOError:
                            break
                        if src[0] != addr[0] or src[1] != addr[1]:
                            bad_echo += 1
                            continue
                        if len(data) < PROBE_HDR.size:
                            bad_echo += 1
                            continue
                        magic, seq = PROBE_HDR.unpack_from(data)
                        if magic != PROBE_MAGIC:
                            bad_echo += 1
                            continue
                        t_send = pending.pop(seq, None)
                        if t_send is None:
                            late += 1
                            continue
                        samples.append((time.perf_counter() - t_send) * 1000.0)
                        matched += 1
                if sent < n and now >= next_send:
                    pkt = PROBE_HDR.pack(PROBE_MAGIC, sent) + pad
                    try:
                        sock.sendto(pkt, addr)
                        pending[sent] = time.perf_counter()
                        sent += 1
                        next_send = t_start + sent * interval
                    except OSError:
                        timeouts += 1
                        sent += 1
                        next_send = t_start + sent * interval
                if sent >= n and not pending:
                    break
                if time.perf_counter() - t_start > seconds + self.echo_timeout_s + 1.0:
                    timeouts += len(pending)
                    pending.clear()
                    break
        finally:
            sock.close()
            if self._video_proc and self._video_proc.poll() is None:
                self._video_proc.terminate()
        elapsed = max(time.perf_counter() - t_start, 1e-9)
        delivery = (matched / sent) if sent else 0.0
        return {
            "samples_ms": samples,
            "backend": "udp-rtt-probe",
            "mode": "live",
            "measured": matched > 0,
            "metric": "rtt_ms",
            "sent": sent,
            "matched": matched,
            "timeouts": timeouts,
            "bad_echo": bad_echo,
            "late": late,
            "delivery_rate": round(delivery, 4),
            "actual_hz": round(sent / elapsed, 2),
            "video_mbps": self._video_mbps,
            "video_live": self._video_live,
        }

    def close(self):
        if self._video_proc and self._video_proc.poll() is None:
            self._video_proc.terminate()
            try:
                self._video_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._video_proc.kill()


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
        try:
            inject = self.transport.inject_video_load(
                self.cfg.video_inject_mbps, self.cfg.control_seconds)
        except Exception as exc:
            return StepResult(
                "video_plus_control", False,
                {"video_mbps": self.cfg.video_inject_mbps, "status": "not_measured",
                 "inject": {"live": False, "started": False, "error": str(exc)}},
                notes="Video load failed to start; cannot claim full-load control RTT.",
            )
        if hasattr(self.transport, "video_load_running") and not self.transport.video_load_running():
            inject = {**inject, "live": False, "started": False,
                      "note": inject.get("note", "video load is not running")}
        # Missing keys default to False — do not treat a failed inject as live.
        video_started = bool(inject.get("started")) and bool(inject.get("live"))
        raw = self.transport.probe_control_latency(
            self.cfg.control_hz, self.cfg.control_seconds, self.cfg.control_payload_bytes)
        stats = _latency_stats(raw.get("samples_ms") or [])
        measured = bool(raw.get("measured", stats["n"] > 0))
        delivery = float(raw.get("delivery_rate", 1.0 if measured else 0.0))
        ok = (
            video_started
            and measured
            and stats["n"] > 0
            and delivery >= 0.9
            and stats["p99_ms"] < 250
        )
        if not video_started or not measured:
            status = "not_measured"
        elif ok:
            status = "pass"
        else:
            status = "fail"
        return StepResult(
            "video_plus_control", ok,
            {"video_mbps": self.cfg.video_inject_mbps, "control": stats,
             "probe": {k: raw[k] for k in (
                 "backend", "mode", "measured", "metric", "sent", "matched",
                 "timeouts", "bad_echo", "late", "delivery_rate", "actual_hz",
                 "video_live", "note") if k in raw},
             "inject": inject, "status": status},
            notes=("Control RTT under live video inject (§6.2). "
                   "Timeouts count as loss; failed/offline video load cannot pass."),
        )

    def run_control_distribution(self) -> StepResult:
        if hasattr(self.transport, "_video_mbps"):
            self.transport._video_mbps = 0.0
        if hasattr(self.transport, "_video_live"):
            self.transport._video_live = False
        raw = self.transport.probe_control_latency(
            self.cfg.control_hz, self.cfg.control_seconds, self.cfg.control_payload_bytes)
        stats = _latency_stats(raw.get("samples_ms") or [])
        measured = bool(raw.get("measured", stats["n"] > 0))
        delivery = float(raw.get("delivery_rate", 1.0 if measured else 0.0))
        ok = measured and stats["n"] > 0 and delivery >= 0.9
        status = "pass" if ok else ("not_measured" if not measured else "fail")
        return StepResult(
            "control_distribution", ok,
            {"control": stats,
             "probe": {k: raw[k] for k in (
                 "backend", "mode", "measured", "metric", "sent", "matched",
                 "timeouts", "bad_echo", "late", "delivery_rate", "actual_hz",
                 "note") if k in raw},
             "status": status},
            notes="Idle control RTT distribution; not measured offline without saved JSON.",
        )

    def run_all(self) -> dict:
        steps = [
            self.run_iperf_steps(),
            self.run_control_distribution(),
            self.run_video_plus_control(),
        ]
        mode = self.cfg.mode
        if isinstance(self.transport, Iperf3Transport):
            mode = "offline" if self.transport.offline else "iperf3-live"
        return {
            "config": {
                "mode": mode,
                "center_host": self.cfg.center_host,
                "terminal_host": self.cfg.terminal_host,
                "iperf_steps_mbps": list(self.cfg.iperf_steps_mbps),
                "video_inject_mbps": self.cfg.video_inject_mbps,
            },
            "steps": [asdict(s) for s in steps],
            "all_ok": all(s.ok for s in steps),
            "all_measured": all(
                (s.metrics.get("status") != "not_measured") for s in steps
                if "status" in s.metrics
            ),
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
        "2. 终端侧启动 `iperf3 -s`；本机 `--mode iperf3`（或 `--json-dir` 喂入已保存的 `-J` JSON）。",
        "3. 遥控探测：对透传 UDP/UART 隧道发 50 Hz 小包，采集单端或 PPS 对齐时延。",
        "4. 对比 dry-run / mock 曲线与真机拐点；拥塞项以「视频满载 + 控制 P99」为准。",
        "",
        f"原始数据：`{json_path.name}`",
    ])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Device testbench (dry-run/mock/iperf3)")
    parser.add_argument("--mode", choices=("dry-run", "mock", "iperf3"), default="dry-run")
    parser.add_argument("--output", default="artifacts/cli-runs/device-bench")
    parser.add_argument("--center", default="192.168.1.10")
    parser.add_argument("--terminal", default="192.168.1.20")
    parser.add_argument("--video-mbps", type=float, default=8.0)
    parser.add_argument("--json-dir", default="",
                        help="Offline iperf3 -J JSON dir (files named iperf-<Mbps>M.json)")
    args = parser.parse_args(argv)
    cfg = BenchConfig(mode=args.mode, center_host=args.center, terminal_host=args.terminal,
                      video_inject_mbps=args.video_mbps)
    json_dir = Path(args.json_dir) if args.json_dir else Path(args.output)
    if args.mode == "iperf3":
        transport = Iperf3Transport(
            cfg, json_dir=json_dir,
            run_subprocess=not bool(args.json_dir),
        )
        bench = DeviceBench(cfg, transport)
        try:
            result = bench.run_all()
        finally:
            transport.close()
    else:
        bench = DeviceBench(cfg)
        result = bench.run_all()
    path = write_report(result, Path(args.output))
    print(f"all_ok={result['all_ok']} report={path}")


if __name__ == "__main__":
    main()
