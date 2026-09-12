# STATUS — 纯软件推进（多节点 + 问询 + 测试台等）

日期：2026-09-12  
环境：Linux，`source /workspace/liaojiangtech/.venv/bin/activate`  
同步：可整目录同步回 Windows；无密钥、无二进制依赖变更（仍 numpy/opencv）。

> **路径说明：** 运行时默认写入 `artifacts/cli-runs/`（该目录在 `.gitignore` 中，不入库）。  
> 入库的可复查副本在 `docs/`。本地重跑后请把生成的 markdown 同步回 `docs/`。

## 进度总览

| # | 项 | 状态 | 结果路径（入库） |
|---|---|---|---|
| 1 | 多节点调度仿真（1 中心 + N≤16+） | **完成**（fair RR 已修） | `docs/multinode-summary.md` |
| 2 | §6.3 问询函可发出稿 | **完成** | `docs/inquiry-letter.md` |
| 3 | benchmark→真设备测试台 | **部分完成** | `docs/device-bench-README.md` |
| 4 | 链路预算计算器 | **完成**（地球隆起已修） | `docs/link-budget.md` |
| 5 | SBUS / MAVLink 序号 / 失控状态机 | **库+单测完成，未接入 MVP** | `nebula_mvp/interfaces.py` |
| 6 | 白名单 + 防重放 | **库+滑动窗口完成，未接入 MVP** | `nebula_mvp/auth.py` |
| — | 审查意见 | 记录 | `docs/REVIEW-software-extras.md`、`docs/REVIEW-a9cb9d8.md` |

### 项 3 / 项 6 边界（审查 a9cb9d8 后）

- **设备测试台：** dry-run / mock / 离线 JSON 吞吐可跑；**live 控制探测**需回显服务，报告为 **RTT**（非单向时延）。无回包计为丢失/未测，**不得**算作成功时延。离线模式**禁止**发 UDP / 起子进程；无 `control-latency.json` 时控制步骤为 `not_measured`。
- **防重放：** 最高序号 + 位图滑动窗口；淘汰下界以下的旧包一律拒绝。`begin_session()` 用于重启/换钥。**尚未**接入 `air_node` / `ground_node` 收发路径，不能称「链路已加密鉴权」。

## 第 1 项结论（决策用）

```bash
.venv/bin/python -m nebula_mvp.multinode --n 16 --mbps 40 --output artifacts/cli-runs/multinode
```

- 40 Mbps 下约 **10** 路 ≥3.5 Mbps 主视频；70% 余量算法约 **6** 路；16 路 HD 不成立。
- 弱链路：FIFO 旁观控制 P99 ~900 ms vs Fusion ~18 ms。
- 切主源：旁观控制 P99 切换前后 ~6.9 ms，不断遥控。

详见 `docs/multinode-summary.md`（模型假设，非设备实测）。

## 其它命令

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m nebula_mvp.device_bench --mode dry-run --output artifacts/cli-runs/device-bench
.venv/bin/python -m nebula_mvp.device_bench --mode mock --video-mbps 28 --output artifacts/cli-runs/device-bench-mock
.venv/bin/python -m nebula_mvp.device_bench --mode iperf3 --json-dir path/to/saved-json --output artifacts/cli-runs/device-bench-iperf
.venv/bin/python -m nebula_mvp.link_budget --mhz 2400 --km 12 --output artifacts/cli-runs/link-budget
```

问询函：复制 `docs/inquiry-letter.md` 填单位信息后发 Support@sinave.com。

## STOP 建议

**STOP_READY=PARTIAL**（a9cb9d8 审查 P1/P2 已修；真机验收与 MVP 接入仍待样机）

下一步：发出问询函；样机到后用 live `device_bench --mode iperf3`（需控制回显）；再把 `auth` / `interfaces` 接入运行路径。勿扩 PHY/外场仿真。
