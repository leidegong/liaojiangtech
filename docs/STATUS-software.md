# STATUS — 纯软件推进（多节点 + 问询 + 测试台等）

日期：2026-09-12  
环境：Linux，`source /workspace/liaojiangtech/.venv/bin/activate`  
同步：可整目录同步回 Windows；无密钥、无二进制依赖变更（仍 numpy/opencv）。

## 进度总览

| # | 项 | 状态 | 结果路径 |
|---|---|---|---|
| 1 | 多节点调度仿真（1 中心 + N≤16+） | **完成** | `artifacts/cli-runs/multinode/` |
| 2 | §6.3 问询函可发出稿 | **完成** | `artifacts/cli-runs/inquiry-letter.md` |
| 3 | benchmark→真设备测试台骨架 | **完成** | `artifacts/cli-runs/device-bench*` + `nebula_mvp/device_bench.py` |
| 4 | 链路预算计算器 | **完成** | `artifacts/cli-runs/link-budget/` |
| 5 | SBUS / MAVLink 序号 / 失控状态机 | **完成** | `nebula_mvp/interfaces.py` + 单测 |
| 6 | 白名单 + 防重放 | **完成** | `nebula_mvp/auth.py` + 单测 |

## 第 1 项结论（决策用）

命令：

```bash
.venv/bin/python -m nebula_mvp.multinode --n 16 --mbps 40 --output artifacts/cli-runs/multinode
```

- **几路视频：** 40 Mbps 假设下，16 终端保遥控+遥测时，调度层原始填满约 **9** 路 ≥3.5 Mbps 的 ~4 Mbps 主视频；§2.6 的 70% 余量算法约 **6** 路。**16 路高清并发不成立**（需 ~96 Mbps）。
- **弱链路：** 在 12 Mbps 压力容量下，弱终端（效率 0.25）开主视频时，FIFO 旁观节点控制 P99 ~900 ms+，Fusion 约 **18 ms**（约 50×）。无优先级会被拖垮。
- **切主源：** Fusion fair 下主视频 0→1 切换，旁观节点控制 P99 切换前后均为 ~6.9 ms，**不中断他人遥控**。

详见 `multinode-summary.md` / `multinode-results.json`。

## 其它命令

```bash
.venv/bin/python -m unittest discover -s tests -v          # 44 passed
.venv/bin/python -m nebula_mvp.device_bench --mode dry-run --output artifacts/cli-runs/device-bench
.venv/bin/python -m nebula_mvp.device_bench --mode mock --video-mbps 28 --output artifacts/cli-runs/device-bench-mock
.venv/bin/python -m nebula_mvp.link_budget --mhz 2400 --km 12 --output artifacts/cli-runs/link-budget
```

问询函：复制 `inquiry-letter.md` 填单位信息后发 Support@sinave.com。

## 是否可同步

**是。** 纯 Python + artifacts 文本；样机相关仅 dry-run/mock，不依赖现场硬件。

## STOP 建议

**STOP_READY=GOOD_CHECKPOINT**

依据：第 1+2 完成且可演示；第 3 有可用骨架（dry-run/mock + 样机当日清单）；第 4–6 已落地并有单测。  
下一步应转向：**发出问询函**、等规格/样机，用 `device_bench` 接真 IP；不要在本仓库继续扩 PHY/外场。
