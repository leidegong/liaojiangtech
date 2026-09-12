# 真设备测试台

对应方案 §6.2：吞吐拐点、视频满载下控制时延、控制时延分布。  
样机未到时用 `dry-run` / `mock`，或 `--json-dir` 离线解析已保存的 `iperf3 -J` JSON；样机到了改 IP 并接 live `iperf3`。

## 今天可跑

```bash
source .venv/bin/activate
python -m nebula_mvp.device_bench --mode dry-run --output artifacts/cli-runs/device-bench
python -m nebula_mvp.device_bench --mode mock --video-mbps 28 --output artifacts/cli-runs/device-bench-mock
# 离线 JSON（文件名：iperf-5M.json, iperf-10M.json, …）
python -m nebula_mvp.device_bench --mode iperf3 --json-dir path/to/json --output artifacts/cli-runs/device-bench-iperf
```

## 样机当日

1. 中心 / 终端以太网互通，写入 `--center` / `--terminal`。
2. 终端：`iperf3 -s`；地面：`python -m nebula_mvp.device_bench --mode iperf3 --center … --terminal …`（会 subprocess 调 `iperf3 -J` 并解析）。
3. 视频满载：后台 UDP 注入；同时 50 Hz 控制小包，记 P50/P99（无回显时为本地发送节拍代理，实验室宜改为双端/PPS）。
4. 入库说明见 `docs/STATUS-software.md`；`artifacts/` 默认不进 git。

代码入口：`nebula_mvp/device_bench.py`（`parse_iperf3_json` / `Iperf3Transport`）。
