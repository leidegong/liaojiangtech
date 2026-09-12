"""1 center + N terminals: shared-capacity scheduling simulation (no RF/PHY).

Answers §2.6 questions that arithmetic alone cannot:
  - How many concurrent video streams fit under a given capacity?
  - Does a weak-link terminal drag down others' control latency?
  - Does switching the primary video source interrupt others' control?

This is a discrete-event model of the *scheduling layer* (Fusion priorities +
optional per-node fairness). It does not emulate OFDM, TDMA slots, or real
Nebula-7 firmware.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Tuple


NS = 1_000_000_000


@dataclass(frozen=True)
class TrafficProfile:
    """Offered load per terminal (application-layer intent)."""
    control_hz: float = 50.0
    control_bytes: int = 80
    data_hz: float = 10.0
    data_bytes: int = 200
    primary_video_bps: int = 4_000_000
    preview_video_bps: int = 500_000
    video_frame_hz: float = 15.0


@dataclass
class TerminalSpec:
    node_id: int
    video: str = "off"  # off | preview | primary
    # Airtime multiplier: weak link burns more air for the same goodput.
    # efficiency 0.25 ⇒ each packet occupies 4× serialization time.
    link_efficiency: float = 1.0


@dataclass
class SimConfig:
    n_terminals: int = 16
    capacity_bps: int = 40_000_000
    duration_s: float = 4.0
    policy: str = "fusion_fair"  # fifo | fusion_global | fusion_fair
    profile: TrafficProfile = field(default_factory=TrafficProfile)
    # Mid-run primary switch: (time_s, from_id, to_id) or None.
    switch_primary: Optional[Tuple[float, int, int]] = None
    # Warm-up discarded from latency stats.
    warmup_s: float = 0.5
    seed: int = 7


@dataclass
class PacketEvent:
    node_id: int
    kind: str  # control | data | video
    size: int
    generated_ns: int
    airtime_ns: int  # serialization under capacity × 1/efficiency


@dataclass
class DeliveryRecord:
    node_id: int
    kind: str
    size: int
    generated_ns: int
    start_ns: int
    finish_ns: int
    queue_ms: float
    e2e_ms: float


KIND_PRIORITY = {"control": 0, "data": 1, "video": 2}


class MultiNodeScheduler:
    """Shared serial link with Fusion-style priorities and optional fairness."""

    def __init__(self, policy: str, capacity_bps: int):
        if policy not in ("fifo", "fusion_global", "fusion_fair"):
            raise ValueError(f"Unknown policy: {policy}")
        self.policy = policy
        self.capacity_bps = capacity_bps
        self.fifo: Deque[PacketEvent] = deque()
        self.control: Dict[int, PacketEvent] = {}
        self.data: Dict[int, Deque[PacketEvent]] = defaultdict(deque)
        self.video: Dict[int, Deque[PacketEvent]] = defaultdict(deque)
        self._rr = {"control": 0, "data": 0, "video": 0}
        self.free_at = 0
        self.dropped = defaultdict(int)

    def enqueue(self, pkt: PacketEvent):
        if self.policy == "fifo":
            self.fifo.append(pkt)
            return
        if pkt.kind == "control":
            if pkt.node_id in self.control:
                self.dropped["control_replaced"] += 1
            self.control[pkt.node_id] = pkt
        elif pkt.kind == "data":
            q = self.data[pkt.node_id]
            if len(q) >= 32:
                q.popleft()
                self.dropped["data_overflow"] += 1
            q.append(pkt)
        else:
            q = self.video[pkt.node_id]
            if len(q) >= 8:
                q.popleft()
                self.dropped["video_overflow"] += 1
            q.append(pkt)

    def _pop_control_fair(self) -> Optional[PacketEvent]:
        ids = sorted(self.control)
        if not ids:
            return None
        start = self._rr["control"] % len(ids)
        nid = ids[start]
        self._rr["control"] = start + 1
        return self.control.pop(nid)

    def _pop_queue_fair(self, kind: str, store: Dict[int, Deque[PacketEvent]]) -> Optional[PacketEvent]:
        ids = sorted(nid for nid, q in store.items() if q)
        if not ids:
            return None
        start = self._rr[kind] % len(ids)
        nid = ids[start]
        self._rr[kind] = start + 1
        return store[nid].popleft()

    def _pop_control_global(self) -> Optional[PacketEvent]:
        if not self.control:
            return None
        nid = min(self.control, key=lambda n: self.control[n].generated_ns)
        return self.control.pop(nid)

    def _pop_queue_global(self, store: Dict[int, Deque[PacketEvent]]) -> Optional[PacketEvent]:
        best_nid = None
        for nid, q in store.items():
            if q and (best_nid is None or q[0].generated_ns < store[best_nid][0].generated_ns):
                best_nid = nid
        if best_nid is None:
            return None
        return store[best_nid].popleft()

    def pop(self, now_ns: int) -> Optional[PacketEvent]:
        if self.policy == "fifo":
            return self.fifo.popleft() if self.fifo else None
        if self.policy == "fusion_fair":
            pkt = self._pop_control_fair()
            if pkt:
                return pkt
            pkt = self._pop_queue_fair("data", self.data)
            if pkt:
                return pkt
            return self._pop_queue_fair("video", self.video)
        pkt = self._pop_control_global()
        if pkt:
            return pkt
        pkt = self._pop_queue_global(self.data)
        if pkt:
            return pkt
        return self._pop_queue_global(self.video)

    def pending(self) -> int:
        if self.policy == "fifo":
            return len(self.fifo)
        return (len(self.control)
                + sum(len(q) for q in self.data.values())
                + sum(len(q) for q in self.video.values()))


def _video_frame_bytes(bps: int, hz: float) -> int:
    return max(1, int(bps / hz / 8))


def _airtime_ns(size: int, capacity_bps: int, efficiency: float) -> int:
    eff = max(0.05, min(1.0, efficiency))
    bits = size * 8
    return int(bits / (capacity_bps * eff) * NS)


def build_terminals(cfg: SimConfig, primary_ids: Iterable[int],
                    preview_ids: Iterable[int] = (),
                    weak: Optional[Dict[int, float]] = None) -> List[TerminalSpec]:
    primary = set(primary_ids)
    preview = set(preview_ids)
    weak = weak or {}
    out = []
    for i in range(cfg.n_terminals):
        if i in primary:
            video = "primary"
        elif i in preview:
            video = "preview"
        else:
            video = "off"
        out.append(TerminalSpec(i, video=video, link_efficiency=weak.get(i, 1.0)))
    return out


def generate_arrivals(cfg: SimConfig, terminals: List[TerminalSpec]
                      ) -> List[Tuple[int, PacketEvent]]:
    """Return (enqueue_ns, packet) sorted by time. Switch applied in-place on specs."""
    p = cfg.profile
    events: List[Tuple[int, PacketEvent]] = []
    duration_ns = int(cfg.duration_s * NS)
    switch = cfg.switch_primary

    # Snapshot video mode timeline per node.
    efficiency = {t.node_id: t.link_efficiency for t in terminals}
    base_mode = {t.node_id: t.video for t in terminals}

    def mode_at(nid: int, t_ns: int) -> str:
        if switch is None:
            return base_mode[nid]
        sw_ns = int(switch[0] * NS)
        src, dst = switch[1], switch[2]
        if t_ns < sw_ns:
            if nid == src:
                return "primary"
            if nid == dst:
                return "off"
            return base_mode[nid]
        if nid == src:
            return "off"
        if nid == dst:
            return "primary"
        return base_mode[nid]

    for t in terminals:
        nid = t.node_id
        # Downlink control (center → terminal): still shares the half-duplex link.
        interval = int(NS / p.control_hz)
        t_ns = 0
        while t_ns < duration_ns:
            size = p.control_bytes
            air = _airtime_ns(size, cfg.capacity_bps, efficiency[nid])
            events.append((t_ns, PacketEvent(nid, "control", size, t_ns, air)))
            t_ns += interval
        # Uplink data
        interval = int(NS / p.data_hz)
        t_ns = interval // 3
        while t_ns < duration_ns:
            size = p.data_bytes
            air = _airtime_ns(size, cfg.capacity_bps, efficiency[nid])
            events.append((t_ns, PacketEvent(nid, "data", size, t_ns, air)))
            t_ns += interval
        # Video frames — bitrate depends on mode at generation time
        interval = int(NS / p.video_frame_hz)
        t_ns = interval // 2
        while t_ns < duration_ns:
            mode = mode_at(nid, t_ns)
            if mode == "primary":
                bps = p.primary_video_bps
            elif mode == "preview":
                bps = p.preview_video_bps
            else:
                t_ns += interval
                continue
            size = _video_frame_bytes(bps, p.video_frame_hz)
            air = _airtime_ns(size, cfg.capacity_bps, efficiency[nid])
            events.append((t_ns, PacketEvent(nid, "video", size, t_ns, air)))
            t_ns += interval

    events.sort(key=lambda x: (x[0], KIND_PRIORITY[x[1].kind], x[1].node_id))
    return events


def run_sim(cfg: SimConfig, terminals: List[TerminalSpec]) -> dict:
    arrivals = generate_arrivals(cfg, terminals)
    sched = MultiNodeScheduler(cfg.policy, cfg.capacity_bps)
    deliveries: List[DeliveryRecord] = []
    ai = 0
    now = 0
    duration_ns = int(cfg.duration_s * NS)
    warmup_ns = int(cfg.warmup_s * NS)

    while now <= duration_ns or sched.pending() or ai < len(arrivals):
        # Enqueue everything ready at `now`
        while ai < len(arrivals) and arrivals[ai][0] <= now:
            _, pkt = arrivals[ai]
            sched.enqueue(pkt)
            ai += 1

        pkt = sched.pop(now)
        if pkt is None:
            if ai >= len(arrivals):
                break
            now = arrivals[ai][0]
            continue

        start = max(now, sched.free_at, pkt.generated_ns)
        finish = start + pkt.airtime_ns
        sched.free_at = finish
        queue_ms = (start - pkt.generated_ns) / 1e6
        e2e_ms = (finish - pkt.generated_ns) / 1e6
        if pkt.generated_ns >= warmup_ns and finish <= duration_ns:
            deliveries.append(DeliveryRecord(
                pkt.node_id, pkt.kind, pkt.size, pkt.generated_ns, start, finish,
                queue_ms, e2e_ms))
        now = finish
        if now > duration_ns and ai >= len(arrivals) and not sched.pending():
            break
        # Stop enqueueing past the horizon so drain does not inflate goodput.
        while ai < len(arrivals) and arrivals[ai][0] <= min(now, duration_ns):
            _, p2 = arrivals[ai]
            sched.enqueue(p2)
            ai += 1
        if now > duration_ns:
            # Discard remaining work — experiment window closed.
            break

    return summarize(cfg, terminals, deliveries, sched.dropped)


def _pct(values: List[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(math.ceil(p / 100 * len(s)) - 1)))
    return s[k]


def summarize(cfg: SimConfig, terminals: List[TerminalSpec],
              deliveries: List[DeliveryRecord], dropped: dict) -> dict:
    by_node: Dict[int, Dict[str, list]] = defaultdict(lambda: defaultdict(list))
    bytes_video: Dict[int, int] = defaultdict(int)
    for d in deliveries:
        by_node[d.node_id][d.kind].append(d.e2e_ms)
        if d.kind == "video":
            bytes_video[d.node_id] += d.size

    steady_s = max(1e-9, cfg.duration_s - cfg.warmup_s)
    per_node = []
    for t in terminals:
        nid = t.node_id
        ctrl = by_node[nid]["control"]
        data = by_node[nid]["data"]
        video_lat = by_node[nid]["video"]
        ctrl_p99 = _pct(ctrl, 99)
        v_bps = bytes_video[nid] * 8 / steady_s
        per_node.append({
            "node_id": nid,
            "video": t.video,
            "link_efficiency": t.link_efficiency,
            "control_samples": len(ctrl),
            "control_p50_ms": _pct(ctrl, 50),
            "control_p99_ms": ctrl_p99,
            "data_samples": len(data),
            "data_p99_ms": _pct(data, 99),
            "video_frames": len(video_lat),
            "video_delivered_mbps": round(v_bps / 1e6, 3),
            "video_p99_ms": _pct(video_lat, 99),
        })

    video_nodes = [n for n in per_node if n["video_frames"] > 0]
    max_ctrl = max((n["control_p99_ms"] for n in per_node if n["control_samples"]),
                   default=float("nan"))
    min_ctrl = min((n["control_p99_ms"] for n in per_node if n["control_samples"]),
                   default=float("nan"))

    return {
        "config": {
            "n_terminals": cfg.n_terminals,
            "capacity_mbps": cfg.capacity_bps / 1e6,
            "duration_s": cfg.duration_s,
            "policy": cfg.policy,
            "warmup_s": cfg.warmup_s,
            "switch_primary": cfg.switch_primary,
            "profile": asdict(cfg.profile),
        },
        "dropped": dict(dropped),
        "nodes": per_node,
        "aggregate": {
            "control_p99_ms_max": max_ctrl,
            "control_p99_ms_min": min_ctrl,
            "control_p99_ms_spread": (max_ctrl - min_ctrl) if math.isfinite(max_ctrl) else float("nan"),
            "nodes_with_video": len(video_nodes),
            "sum_video_mbps": round(sum(n["video_delivered_mbps"] for n in per_node), 3),
            "arithmetic_share_mbps": round(cfg.capacity_bps / 1e6 / cfg.n_terminals, 3),
        },
    }


# ---------------------------------------------------------------------------
# Decision scenarios (§2.6 closed loop)
# ---------------------------------------------------------------------------

def scenario_how_many_videos(capacity_bps: int = 40_000_000, n: int = 16,
                             policy: str = "fusion_fair") -> dict:
    """Sweep primary video count; usable = each primary delivers ≥3.5 Mbps."""
    rows = []
    for k in range(0, n + 1):
        cfg = SimConfig(n_terminals=n, capacity_bps=capacity_bps, policy=policy,
                        duration_s=2.5, warmup_s=0.4)
        terminals = build_terminals(cfg, primary_ids=range(k))
        result = run_sim(cfg, terminals)
        primaries = [x for x in result["nodes"] if x["node_id"] < k]
        usable = sum(1 for x in primaries if x["video_delivered_mbps"] >= 3.5)
        starved = sum(1 for x in primaries if x["video_delivered_mbps"] < 1.0)
        rows.append({
            "primary_videos": k,
            "control_p99_ms_max": result["aggregate"]["control_p99_ms_max"],
            "sum_video_mbps": result["aggregate"]["sum_video_mbps"],
            "usable_ge_3_5_mbps": usable,
            "starved_lt_1_mbps": starved,
            "ok_control_under_100ms": result["aggregate"]["control_p99_ms_max"] < 100,
            "all_primaries_usable": k == 0 or usable == k,
        })
    ok = [r for r in rows if r["ok_control_under_100ms"] and r["all_primaries_usable"]]
    best_k = ok[-1]["primary_videos"] if ok else 0
    # §2.6 algorithm 2 engineering margin
    eng = int(capacity_bps * 0.7 / 4.2e6)
    conclusion = (
        f"Under {capacity_bps/1e6:.0f} Mbps + {policy}, with {n} terminals keeping "
        f"control+telemetry, scheduler delivers ≥3.5 Mbps on each of at most "
        f"{best_k} concurrent ~4 Mbps primaries (raw fill). §2.6 70%-margin rule "
        f"gives ~{eng} streams. {n} concurrent HD streams are not viable "
        f"(need ~{n * 4.2 / 0.7:.0f} Mbps stable capacity)."
    )
    return {"name": "how_many_videos", "rows": rows, "conclusion": conclusion,
            "max_usable_primaries": best_k, "engineering_max_algo2": eng}


def scenario_weak_link_isolation(capacity_bps: int = 40_000_000, n: int = 16) -> dict:
    """One primary video on a weak terminal; compare policies for other nodes' control.

    Uses a tighter capacity than the §2.6 40 Mbps headline so FIFO queues actually
    build: a weak uplink holding long serializations is the drag mechanism.
    """
    # Stress the shared pipe: 12 Mbps leaves little slack once a 4 Mbps stream
    # burns 4× airtime (efficiency 0.25 ⇒ ~16 Mbps air demand).
    stress_bps = min(capacity_bps, 12_000_000)
    comparison = []
    for policy in ("fifo", "fusion_global", "fusion_fair"):
        cfg = SimConfig(n_terminals=n, capacity_bps=stress_bps, policy=policy,
                        duration_s=3.0, warmup_s=0.4)
        terminals = build_terminals(cfg, primary_ids=[0], weak={0: 0.25})
        result = run_sim(cfg, terminals)
        others = [x for x in result["nodes"] if x["node_id"] != 0]
        victim = result["nodes"][0]
        comparison.append({
            "policy": policy,
            "capacity_mbps": stress_bps / 1e6,
            "weak_node_control_p99_ms": victim["control_p99_ms"],
            "others_control_p99_ms_max": max(x["control_p99_ms"] for x in others),
            "others_control_p99_ms_median": _pct([x["control_p99_ms"] for x in others], 50),
            "weak_video_mbps": victim["video_delivered_mbps"],
        })
    fair = next(c for c in comparison if c["policy"] == "fusion_fair")
    fifo = next(c for c in comparison if c["policy"] == "fifo")
    ratio = (fifo["others_control_p99_ms_max"] / fair["others_control_p99_ms_max"]
             if fair["others_control_p99_ms_max"] > 0 else float("inf"))
    conclusion = (
        f"At {stress_bps/1e6:.0f} Mbps shared capacity, weak terminal (efficiency 0.25) "
        f"with primary video: FIFO others control P99="
        f"{fifo['others_control_p99_ms_max']:.1f} ms vs fusion_fair "
        f"{fair['others_control_p99_ms_max']:.1f} ms (≈{ratio:.1f}×). "
        f"Without priority isolation, a weak uplink burns shared airtime and raises "
        f"peers' control latency; Fusion keeps peers' control prioritized."
    )
    return {"name": "weak_link", "rows": comparison, "conclusion": conclusion}


def scenario_primary_switch(capacity_bps: int = 40_000_000, n: int = 16,
                            policy: str = "fusion_fair") -> dict:
    """Switch primary from node 0 → 1 mid-run; check peer control continuity."""
    cfg = SimConfig(
        n_terminals=n, capacity_bps=capacity_bps, policy=policy,
        duration_s=4.0, warmup_s=0.3,
        switch_primary=(2.0, 0, 1),
    )
    terminals = build_terminals(cfg, primary_ids=[0])
    # Split deliveries into before/after by re-running with tagged phases via two windows.
    # Simpler: run once and split control samples by generated time in a custom pass.
    arrivals_result = _run_with_phases(cfg, terminals, switch_s=2.0)
    ok = (arrivals_result["peers_after_p99_ms"] < 100
          and arrivals_result["peers_after_p99_ms"]
          < arrivals_result["peers_before_p99_ms"] * 2 + 20)
    verdict = ("No material interruption of others' control."
               if ok else "Investigate spike.")
    conclusion = (
        f"Primary switch 0→1 at t=2 s ({policy}): peer nodes (id≥2) control P99 "
        f"before={arrivals_result['peers_before_p99_ms']:.1f} ms, "
        f"after={arrivals_result['peers_after_p99_ms']:.1f} ms. {verdict}"
    )
    return {"name": "primary_switch", **arrivals_result, "conclusion": conclusion}


def _run_with_phases(cfg: SimConfig, terminals: List[TerminalSpec], switch_s: float) -> dict:
    arrivals = generate_arrivals(cfg, terminals)
    sched = MultiNodeScheduler(cfg.policy, cfg.capacity_bps)
    before: List[float] = []
    after: List[float] = []
    peers_before: List[float] = []
    peers_after: List[float] = []
    ai = 0
    now = 0
    duration_ns = int(cfg.duration_s * NS)
    warmup_ns = int(cfg.warmup_s * NS)
    switch_ns = int(switch_s * NS)

    while now <= duration_ns or sched.pending() or ai < len(arrivals):
        while ai < len(arrivals) and arrivals[ai][0] <= now:
            sched.enqueue(arrivals[ai][1])
            ai += 1
        pkt = sched.pop(now)
        if pkt is None:
            if ai >= len(arrivals):
                break
            now = arrivals[ai][0]
            continue
        start = max(now, sched.free_at, pkt.generated_ns)
        finish = start + pkt.airtime_ns
        sched.free_at = finish
        e2e = (finish - pkt.generated_ns) / 1e6
        if pkt.kind == "control" and pkt.generated_ns >= warmup_ns:
            bucket = before if pkt.generated_ns < switch_ns else after
            bucket.append(e2e)
            if pkt.node_id >= 2:
                (peers_before if pkt.generated_ns < switch_ns else peers_after).append(e2e)
        now = finish
        while ai < len(arrivals) and arrivals[ai][0] <= now:
            sched.enqueue(arrivals[ai][1])
            ai += 1

    return {
        "policy": cfg.policy,
        "switch_s": switch_s,
        "all_before_p99_ms": _pct(before, 99),
        "all_after_p99_ms": _pct(after, 99),
        "peers_before_p99_ms": _pct(peers_before, 99),
        "peers_after_p99_ms": _pct(peers_after, 99),
        "peers_before_samples": len(peers_before),
        "peers_after_samples": len(peers_after),
    }


def run_all_scenarios(capacity_mbps: float = 40.0, n: int = 16) -> dict:
    bps = int(capacity_mbps * 1e6)
    return {
        "how_many_videos": scenario_how_many_videos(bps, n),
        "weak_link": scenario_weak_link_isolation(bps, n),
        "primary_switch": scenario_primary_switch(bps, n),
        "meta": {
            "capacity_mbps": capacity_mbps,
            "n_terminals": n,
            "note": "Scheduling-layer discrete event sim; not RF/PHY or vendor firmware.",
        },
    }


def write_report(results: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "multinode-results.json"
    md_path = output_dir / "multinode-summary.md"
    json_path.write_text(json.dumps(results, indent=2, allow_nan=True), encoding="utf-8")

    h = results["how_many_videos"]
    w = results["weak_link"]
    s = results["primary_switch"]
    lines = [
        "# 多节点调度仿真小结（§2.6）",
        "",
        f"容量假设：{results['meta']['capacity_mbps']} Mbps；终端数 N={results['meta']['n_terminals']}。",
        "本结果验证**调度层**行为，不是厂商固件或射频实测。",
        "",
        "## 1. 16 节点能跑几路主视频？",
        "",
        "| 主视频路数 | 控制 P99 max | 视频合计 Mbps | ≥3.5 Mbps 可用 | 全部可用 |",
        "|---|---:|---:|---:|:---:|",
    ]
    for row in h["rows"]:
        all_ok = "是" if row.get("all_primaries_usable") else "否"
        p99 = row["control_p99_ms_max"]
        p99_s = f"{p99:.1f}" if isinstance(p99, float) and math.isfinite(p99) else "n/a"
        lines.append(
            f"| {row['primary_videos']} | {p99_s} | {row['sum_video_mbps']} | "
            f"{row.get('usable_ge_3_5_mbps', 0)} | {all_ok} |"
        )
    lines.extend(["", f"**结论：** {h['conclusion']}", "",
                  "## 2. 弱链路终端会不会拖垮别人？", "",
                  "| 策略 | 弱节点控制 P99 | 其他节点控制 P99 max | 弱节点视频 Mbps |",
                  "|---|---:|---:|---:|"])
    for row in w["rows"]:
        lines.append(
            f"| {row['policy']} | {row['weak_node_control_p99_ms']:.1f} | "
            f"{row['others_control_p99_ms_max']:.1f} | {row['weak_video_mbps']} |"
        )
    lines.extend(["", f"**结论：** {w['conclusion']}", "",
                  "## 3. 切换主视频源时他人遥控是否中断？", "",
                  f"- 策略：{s['policy']}；切换时刻 t={s['switch_s']} s（节点 0→1）",
                  f"- 旁观节点（id≥2）控制 P99：切换前 {s['peers_before_p99_ms']:.1f} ms → "
                  f"切换后 {s['peers_after_p99_ms']:.1f} ms",
                  "", f"**结论：** {s['conclusion']}", "",
                  "## 与 §2.6 算术对照", "",
                  "- 算法一：40 Mbps ÷ 16 ≈ 2.5 Mbps/台（平均）—— 仿真中多路 4 Mbps 主视频会迅速抬高控制时延。",
                  "- 算法二：N×4.2/0.7 → 16 台需 ~96 Mbps —— 在 40 Mbps 假设下只能支持极少数主码流。",
                  "- **业务含义：** 全部终端保遥控+遥测；主码流按需点名；调度需节点间公平/隔离，否则弱链路或 FIFO 会拖垮全网控制。",
                  "",
                  f"原始数据：`{json_path.name}`",
                  ])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path


def main(argv: Optional[List[str]] = None):
    import argparse
    parser = argparse.ArgumentParser(description="1 center + N terminal scheduling simulation")
    parser.add_argument("--n", type=int, default=16, help="Terminal count (1..64)")
    parser.add_argument("--mbps", type=float, default=40.0, help="Shared capacity Mbps")
    parser.add_argument("--output", default="artifacts/cli-runs/multinode")
    parser.add_argument("--policy", choices=("fifo", "fusion_global", "fusion_fair"),
                        default=None, help="If set, run a single custom sim instead of scenarios")
    parser.add_argument("--primaries", type=int, default=1)
    args = parser.parse_args(argv)
    if not 1 <= args.n <= 64:
        parser.error("n must be in 1..64")
    out = Path(args.output)
    if args.policy:
        cfg = SimConfig(n_terminals=args.n, capacity_bps=int(args.mbps * 1e6),
                        policy=args.policy, duration_s=3.0)
        terminals = build_terminals(cfg, primary_ids=range(args.primaries))
        result = run_sim(cfg, terminals)
        out.mkdir(parents=True, exist_ok=True)
        path = out / "single-run.json"
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result["aggregate"], indent=2))
        print(f"Wrote {path}")
    else:
        results = run_all_scenarios(args.mbps, args.n)
        md = write_report(results, out)
        print(results["how_many_videos"]["conclusion"])
        print(results["weak_link"]["conclusion"])
        print(results["primary_switch"]["conclusion"])
        print(f"Report: {md}")


if __name__ == "__main__":
    main()
