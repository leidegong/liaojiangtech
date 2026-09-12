# 多节点调度仿真小结（§2.6）

容量假设：40.0 Mbps；终端数 N=16。
本结果验证**调度层**行为，不是厂商固件或射频实测。

## 1. 16 节点能跑几路主视频？

| 主视频路数 | 控制 P99 max | 视频合计 Mbps | ≥3.5 Mbps 可用 | 全部可用 |
|---|---:|---:|---:|:---:|
| 0 | 0.3 | 0.0 | 0 | 是 |
| 1 | 6.9 | 3.936 | 1 | 是 |
| 2 | 6.9 | 7.872 | 2 | 是 |
| 3 | 6.9 | 11.808 | 3 | 是 |
| 4 | 6.9 | 15.744 | 4 | 是 |
| 5 | 6.9 | 19.68 | 5 | 是 |
| 6 | 6.9 | 23.616 | 6 | 是 |
| 7 | 6.9 | 27.552 | 7 | 是 |
| 8 | 6.9 | 31.488 | 8 | 是 |
| 9 | 6.9 | 35.424 | 9 | 是 |
| 10 | 6.9 | 38.344 | 10 | 是 |
| 11 | 6.9 | 37.592 | 0 | 否 |
| 12 | 6.9 | 36.83 | 0 | 否 |
| 13 | 6.9 | 36.068 | 0 | 否 |
| 14 | 6.9 | 35.306 | 0 | 否 |
| 15 | 6.9 | 34.544 | 0 | 否 |
| 16 | 6.9 | 33.782 | 0 | 否 |

**结论：** Under 40 Mbps + fusion_fair, with 16 terminals keeping control+telemetry, scheduler delivers ≥3.5 Mbps on each of at most 10 concurrent ~4 Mbps primaries (raw fill). §2.6 70%-margin rule gives ~6 streams. 16 concurrent HD streams are not viable (need ~96 Mbps stable capacity).

> 说明：控制包小且严格最高优先级，上表 `控制 P99 max` 对视频路数几乎不变（≈6.9 ms），不携带「视频是否过载」信息；区分能力靠「≥3.5 Mbps 可用 / 全部可用」。k≥11 时公平均分后每路约 2 Mbps，故可用列变为 0（不再出现修复前饿死导致的 0→9 跳变）。

## 2. 弱链路终端会不会拖垮别人？

| 策略 | 弱节点控制 P99 | 其他节点控制 P99 max | 弱节点视频 Mbps |
|---|---:|---:|---:|
| fifo | 903.6 | 904.4 | 2.564 |
| fusion_global | 17.7 | 18.5 | 2.667 |
| fusion_fair | 18.2 | 18.5 | 2.667 |

**结论：** At 12 Mbps shared capacity, weak terminal (efficiency 0.25) with primary video: FIFO others control P99=904.4 ms vs fusion_fair 18.5 ms (≈49.0×). Without priority isolation, a weak uplink burns shared airtime and raises peers' control latency; Fusion keeps peers' control prioritized.

## 3. 切换主视频源时他人遥控是否中断？

- 策略：fusion_fair；切换时刻 t=2.0 s（节点 0→1）
- 旁观节点（id≥2）控制 P99：切换前 6.9 ms → 切换后 6.9 ms

**结论：** Primary switch 0→1 at t=2 s (fusion_fair): peer nodes (id≥2) control P99 before=6.9 ms, after=6.9 ms. No material interruption of others' control.

## 与 §2.6 算术对照

- 算法一：40 Mbps ÷ 16 ≈ 2.5 Mbps/台（平均）—— 仿真中多路 4 Mbps 主视频会迅速抬高控制时延。
- 算法二：N×4.2/0.7 → 16 台需 ~96 Mbps —— 在 40 Mbps 假设下只能支持极少数主码流。
- **业务含义：** 全部终端保遥控+遥测；主码流按需点名；调度需节点间公平/隔离，否则弱链路或 FIFO 会拖垮全网控制。

原始数据：`multinode-results.json`
