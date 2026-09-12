# 真设备测试台

对应方案 §6.2：吞吐拐点、视频满载下控制时延、控制时延分布。

| 模式 | 行为 |
|---|---|
| `dry-run` / `mock` | 合成数据，可 CI |
| `iperf3 --json-dir DIR` | **纯离线**：只读 JSON，不发 UDP、不起子进程；控制步骤无 `control-latency.json` 则为 `not_measured` |
| `iperf3`（live） | subprocess 调 `iperf3 -J`；控制探测为带序号的 UDP **RTT**（需对端回显） |

## 今天可跑

```bash
source .venv/bin/activate
python -m nebula_mvp.device_bench --mode dry-run --output artifacts/cli-runs/device-bench
python -m nebula_mvp.device_bench --mode mock --video-mbps 28 --output artifacts/cli-runs/device-bench-mock
python -m nebula_mvp.device_bench --mode iperf3 --json-dir path/to/json --output artifacts/cli-runs/device-bench-iperf
```

离线 JSON 文件名：`iperf-5M.json`、`iperf-10M.json`、…；可选 `control-latency.json` 回放控制结果。

## 样机当日

1. 中心 / 终端以太网互通，写入 `--center` / `--terminal`。
2. 终端：`iperf3 -s`；地面 live：`python -m nebula_mvp.device_bench --mode iperf3 …`。
3. 控制探测：对端需回显探测包（magic `N7P1` + seq）；报告为 RTT，超时计丢失，零回包不能 PASS。
4. 无回显时步骤为 FAIL/`not_measured`，不会用超时等待冒充时延。

代码：`nebula_mvp/device_bench.py`（`parse_iperf3_json` / `Iperf3Transport`）。
