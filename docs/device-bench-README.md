# 真设备测试台（骨架）

对应方案 §6.2：吞吐拐点、视频满载下控制时延、控制时延分布。  
样机未到时用 `dry-run` / `mock` 跑通同一套步骤；样机到了改 IP 并接上 `iperf3` 适配层。

## 今天可跑

```bash
source .venv/bin/activate
python -m nebula_mvp.device_bench --mode dry-run --output artifacts/cli-runs/device-bench
python -m nebula_mvp.device_bench --mode mock --video-mbps 28 --output artifacts/cli-runs/device-bench-mock
```

## 样机当日

1. 中心 / 终端以太网互通，写入 `--center` / `--terminal`。
2. 终端：`iperf3 -s`；地面逐步 `-b 5M/10M/...` 找应用层拐点（有效载荷，不要用网口速率代替）。
3. 视频满载：后台 UDP/视频注入；同时 50 Hz 控制小包，记 P50/P99。
4. 将 `BenchConfig.mode` 设为 `iperf3` 前，在 `Iperf3Transport` 中补 subprocess 解析（或手工把 iperf3 `-J` JSON 放进 `artifacts/cli-runs/device-bench/`）。

代码入口：`nebula_mvp/device_bench.py`。
