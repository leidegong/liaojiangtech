"""§2.4 link budget calculator — ITU-R P.525 FSPL + simple margin (revisable params)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from typing import Optional


@dataclass
class LinkBudgetInput:
    frequency_mhz: float = 2400.0
    distance_km: float = 12.0
    tx_power_dbm: float = 30.0          # 1 W
    tx_antenna_dbi: float = 5.0
    rx_antenna_dbi: float = 8.0
    cable_polar_loss_db: float = 3.0
    bandwidth_hz: float = 5e6
    noise_figure_db: float = 5.0
    required_snr_db: float = 3.0         # e.g. QPSK 1/2 class assumption
    # Regulatory / scenario labels (not enforced physically).
    band_label: str = "2.4 GHz ISM (assumption)"
    regulatory_note: str = "Not a type-approval; change frequency freely for what-if."


@dataclass
class LinkBudgetResult:
    fspl_db: float
    rx_power_dbm: float
    thermal_noise_dbm: float
    sensitivity_dbm: float
    margin_db: float
    fresnel_radius_m: float
    earth_bulge_m: float
    clearance_hint_m: float
    inputs: dict


def fspl_db(frequency_mhz: float, distance_km: float) -> float:
    """ITU-R P.525-style free-space path loss."""
    if frequency_mhz <= 0 or distance_km <= 0:
        raise ValueError("frequency_mhz and distance_km must be positive")
    return 32.44 + 20 * math.log10(frequency_mhz) + 20 * math.log10(distance_km)


def fresnel_radius_m(distance_km: float, frequency_ghz: float) -> float:
    """First Fresnel zone radius at midpoint: 8.66 * sqrt(d_km / f_GHz)."""
    if frequency_ghz <= 0 or distance_km <= 0:
        raise ValueError("distance and frequency must be positive")
    return 8.66 * math.sqrt(distance_km / frequency_ghz)


def earth_bulge_m(distance_km: float, k: float = 4 / 3, earth_radius_km: float = 6371.0) -> float:
    """Mid-path earth bulge (m) for effective-earth-radius factor k.

    h = d1·d2 / (2·k·R) with d1 = d2 = d/2 → h = d² / (8·k·R).
    In km² form: h_m ≈ 0.0196 · d_km² / k  (R = 6371 km). The older 0.078
    coefficient treated d1·d2 as d² instead of (d/2)² and was 4× too large.
    """
    if distance_km < 0 or k <= 0 or earth_radius_km <= 0:
        raise ValueError("distance, k, and earth radius must be positive")
    # metres: (d_km * 1000)² / (8 * k * R_km * 1000) = d_km² * 125 / (k * R_km)
    return (distance_km ** 2) * 125.0 / (k * earth_radius_km)


def compute(inp: LinkBudgetInput) -> LinkBudgetResult:
    pl = fspl_db(inp.frequency_mhz, inp.distance_km)
    rx = (inp.tx_power_dbm + inp.tx_antenna_dbi + inp.rx_antenna_dbi
          - pl - inp.cable_polar_loss_db)
    noise = -174.0 + 10 * math.log10(inp.bandwidth_hz)
    sens = noise + inp.noise_figure_db + inp.required_snr_db
    margin = rx - sens
    f_ghz = inp.frequency_mhz / 1000.0
    r1 = fresnel_radius_m(inp.distance_km, f_ghz)
    bulge = earth_bulge_m(inp.distance_km)
    return LinkBudgetResult(
        fspl_db=round(pl, 2),
        rx_power_dbm=round(rx, 2),
        thermal_noise_dbm=round(noise, 2),
        sensitivity_dbm=round(sens, 2),
        margin_db=round(margin, 2),
        fresnel_radius_m=round(r1, 2),
        earth_bulge_m=round(bulge, 2),
        clearance_hint_m=round(r1 + bulge, 2),
        inputs=asdict(inp),
    )


def compare_bands(distance_km: float = 12.0) -> list:
    """Quick table for common what-if bands (compliance is separate)."""
    rows = []
    for mhz, label in ((1400, "1.4 GHz — physics only; CN compliance separate"),
                       (2400, "2.4 GHz"),
                       (5800, "5.8 GHz")):
        r = compute(LinkBudgetInput(frequency_mhz=mhz, distance_km=distance_km,
                                    band_label=label))
        rows.append({"label": label, "frequency_mhz": mhz, "fspl_db": r.fspl_db,
                     "rx_power_dbm": r.rx_power_dbm, "margin_db": r.margin_db,
                     "fresnel_radius_m": r.fresnel_radius_m})
    return rows


def write_report(result: LinkBudgetResult, output_dir: Path,
                 bands: Optional[list] = None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {"result": asdict(result), "band_comparison": bands or compare_bands(result.inputs["distance_km"])}
    json_path = output_dir / "link-budget.json"
    md_path = output_dir / "link-budget.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    r, inp = result, result.inputs
    lines = [
        "# 链路预算计算（§2.4 可复查）",
        "",
        "> 自由空间模型推算，**不是**对 Nebula-7 工作频段的认定，也不是实测损耗。",
        "",
        "## 输入",
        "",
        f"- 频率：{inp['frequency_mhz']} MHz（{inp['band_label']}）",
        f"- 距离：{inp['distance_km']} km",
        f"- 发射功率：{inp['tx_power_dbm']} dBm；Tx/Rx 天线：{inp['tx_antenna_dbi']} / {inp['rx_antenna_dbi']} dBi",
        f"- 馈线/极化损耗：{inp['cable_polar_loss_db']} dB",
        f"- 带宽：{inp['bandwidth_hz']:.0f} Hz；NF={inp['noise_figure_db']} dB；所需 SNR={inp['required_snr_db']} dB",
        "",
        "## 结果",
        "",
        f"| 量 | 值 |",
        f"|---|---:|",
        f"| FSPL | {r.fspl_db} dB |",
        f"| 接收功率 | {r.rx_power_dbm} dBm |",
        f"| 热噪声底 | {r.thermal_noise_dbm} dBm |",
        f"| 灵敏度（估） | {r.sensitivity_dbm} dBm |",
        f"| **链路余量** | **{r.margin_db} dB** |",
        f"| 第一菲涅尔区半径（中点） | {r.fresnel_radius_m} m |",
        f"| 地球曲率隆起（估） | {r.earth_bulge_m} m |",
        f"| 建议净空（半径+隆起） | {r.clearance_hint_m} m |",
        "",
        "## 频段对照（同距离）",
        "",
        "| 频段 | FSPL (dB) | 接收功率 (dBm) | 余量 (dB) | 菲涅尔半径 (m) |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in payload["band_comparison"]:
        lines.append(
            f"| {row['label']} | {row['fspl_db']} | {row['rx_power_dbm']} | "
            f"{row['margin_db']} | {row['fresnel_radius_m']} |"
        )
    lines.extend([
        "",
        "## 解读",
        "",
        "- 余量为正只说明**该窄带低阶调制工作点**在自由空间模型下成立；不能承诺同距离下的高码率视频。",
        "- 必须分别做上下行预算，并与目标 MCS 灵敏度比较。",
        "- 1.4 GHz 路损更小是物理结论，**不构成中国大陆可用性结论**（见方案 §3.3）。",
        "",
        f"原始数据：`{json_path.name}`",
    ])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Link budget calculator (§2.4)")
    parser.add_argument("--mhz", type=float, default=2400.0)
    parser.add_argument("--km", type=float, default=12.0)
    parser.add_argument("--tx-dbm", type=float, default=30.0)
    parser.add_argument("--tx-dbi", type=float, default=5.0)
    parser.add_argument("--rx-dbi", type=float, default=8.0)
    parser.add_argument("--loss-db", type=float, default=3.0)
    parser.add_argument("--bw-hz", type=float, default=5e6)
    parser.add_argument("--nf", type=float, default=5.0)
    parser.add_argument("--snr", type=float, default=3.0)
    parser.add_argument("--output", default="artifacts/cli-runs/link-budget")
    args = parser.parse_args(argv)
    inp = LinkBudgetInput(
        frequency_mhz=args.mhz, distance_km=args.km, tx_power_dbm=args.tx_dbm,
        tx_antenna_dbi=args.tx_dbi, rx_antenna_dbi=args.rx_dbi,
        cable_polar_loss_db=args.loss_db, bandwidth_hz=args.bw_hz,
        noise_figure_db=args.nf, required_snr_db=args.snr,
    )
    result = compute(inp)
    path = write_report(result, Path(args.output))
    print(f"FSPL={result.fspl_db} dB  Rx={result.rx_power_dbm} dBm  "
          f"margin={result.margin_db} dB")
    print(f"Report: {path}")


if __name__ == "__main__":
    main()
