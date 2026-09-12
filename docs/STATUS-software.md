# STATUS — 纯软件推进（多节点 + 问询 + 测试台等）

日期：2026-09-12  
环境：Linux，`source /workspace/liaojiangtech/.venv/bin/activate`  
同步：可整目录同步回 Windows；无密钥、无二进制依赖变更（仍 numpy/opencv）。

> **路径说明：** 运行时默认写入 `artifacts/cli-runs/`（该目录在 `.gitignore` 中，不入库）。  
> 入库的可复查副本在 `docs/`（本文件、`multinode-summary.md`、`link-budget.md`、`inquiry-letter.md` 等）。  
> 本地重跑命令见下文；重跑后请把生成的 markdown 同步回 `docs/`。

## 进度总览

| # | 项 | 状态 | 结果路径（入库） |
|---|---|---|---|
| 1 | 多节点调度仿真（1 中心 + N≤16+） | **完成**（fair RR 已按审查修复） | `docs/multinode-summary.md` |
| 2 | §6.3 问询函可发出稿 | **完成** | `docs/inquiry-letter.md` |
| 3 | benchmark→真设备测试台 | **完成**（含 `iperf3 -J` 解析） | `docs/device-bench-README.md` + `nebula_mvp/device_bench.py` |
| 4 | 链路预算计算器 | **完成**（地球隆起系数已修正） | `docs/link-budget.md` |
| 5 | SBUS / MAVLink 序号 / 失控状态机 | **完成**（库 + 单测，未接入运行中 MVP） | `nebula_mvp/interfaces.py` |
| 6 | 白名单 + 防重放 | **完成**（库 + 单测，未接入运行中 MVP） | `nebula_mvp/auth.py` |
| — | 审查意见 | 记录 | `docs/REVIEW-software-extras.md` |

## 第 1 项结论（决策用）

命令：

```bash
.venv/bin/python -m nebula_mvp.multinode --n 16 --mbps 40 --output artifacts/cli-runs/multinode
# 同步入库副本：cp artifacts/cli-runs/multinode/multinode-summary.md docs/
```

- **几路视频：** 40 Mbps 假设下，16 终端保遥控+遥测时，调度层原始填满约 **10** 路 ≥3.5 Mbps 的 ~4 Mbps 主视频（fair RR 修复后；此前错误饿死导致过载区表不可信）；§2.6 的 70% 余量算法约 **6** 路。**16 路高清并发不成立**（需 ~96 Mbps）。
- **弱链路：** 在 12 Mbps 压力容量下，弱终端（效率 0.25）开主视频时，FIFO 旁观节点控制 P99 ~900 ms+，Fusion 约 **18 ms**（约 50×）。无优先级会被拖垮。
- **切主源：** Fusion fair 下主视频 0→1 切换，旁观节点控制 P99 切换前后均为 ~6.9 ms，**不断他人遥控**。

说明：控制包小且严格最高优先级，扫描表里 `control_p99_ms_max` 对视频负载几乎不敏感；真正区分「能否撑住 k 路」的是 `all_primaries_usable` / ≥3.5 Mbps 列。

详见 `docs/multinode-summary.md`。

## 其它命令

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m nebula_mvp.device_bench --mode dry-run --output artifacts/cli-runs/device-bench
.venv/bin/python -m nebula_mvp.device_bench --mode mock --video-mbps 28 --output artifacts/cli-runs/device-bench-mock
.venv/bin/python -m nebula_mvp.device_bench --mode iperf3 --json-dir path/to/saved-json --output artifacts/cli-runs/device-bench-iperf
.venv/bin/python -m nebula_mvp.link_budget --mhz 2400 --km 12 --output artifacts/cli-runs/link-budget
```

问询函：复制 `docs/inquiry-letter.md` 填单位信息后发 Support@sinave.com。

## 是否可同步

**是。** 纯 Python + docs 文本；样机相关可用 dry-run/mock，或 `--json-dir` 离线解析 iperf3 JSON。

## STOP 建议

**STOP_READY=GOOD_CHECKPOINT**（含审查 §四 修复）

下一步应转向：**发出问询函**、等规格/样机，用 `device_bench --mode iperf3` 接真 IP；不要在本仓库继续扩 PHY/外场。
