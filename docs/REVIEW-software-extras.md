# 审查意见：纯软件补充项（提交 `d483be8`）

**审查日期：** 2026-09-12
**审查环境：** Windows 11，`.venv` Python 3.12.14（与 `VERIFICATION.md` 同一台机器）
**审查范围：** `d483be8` 新增的 5 个模块、5 份文档与 2 个测试文件
**自动测试：** `python -m unittest discover -s tests` → **44 项通过**

本文件只记录审查意见，不含修改。修复请另开提交。

---

## 一、结论摘要

对照上一轮列出的「不需要硬件、但还没做」六项：

| # | 项 | 判定 | 说明 |
|---|---|---|---|
| 1 | 多节点调度仿真 | **完成，但过载区结论不可用** | 见 §2.1 |
| 2 | §6.3 厂商问询函 | **干净完成** | `docs/inquiry-letter.md`，填写单位信息后可直接发出 |
| 3 | 真设备测试台 | **骨架完成，最后一步未做** | 见 §3.2 |
| 4 | 链路预算计算器 | **完成，一处系数错误** | 见 §2.2 |
| 5 | SBUS / MAVLink / 失控状态机 | **完成（库 + 单测）** | 未接入运行中的 MVP，见 §3.1 |
| 6 | 入网白名单 + 防重放 | **完成（库 + 单测）** | 未接入运行中的 MVP，见 §3.1 |
| 7 | 物理层 OFDM 仿真 | **未做（正确）** | 按方案 §4.4，应等路线 A 结论后再启动 |

---

## 二、必须修复的问题

### 2.1 【高】多节点仿真在过载区确定性饿死节点

**位置：** [`nebula_mvp/multinode.py:130`](../nebula_mvp/multinode.py) `MultiNodeScheduler._pop_queue_fair`（`_pop_control_fair` 同样写法）

**现象。** 16 个终端同时开主视频（总需求约 63 Mbps，容量 40 Mbps）时，`fusion_fair` 给出的每节点视频速率为：

```
[3.936, 0.0, 3.809, 3.809, 3.936, 3.048, 3.936, 0.0,
 3.936, 0.0, 3.936, 0.0, 3.936, 0.0, 3.936, 0.0]
```

6 个节点分到**恰好 0.0 Mbps**，另外 9 个拿满标称速率。把 `duration_s` 从 2.5 改到 5.0 重跑，被饿死的仍是同样 6 个节点 —— 这是确定性的实现缺陷，不是统计涨落。

**对照。** 同样过载条件下换 `fusion_global`，一个节点都不饿死（最低 0.29–0.64 Mbps，随时长变化）。也就是说，名为 `fair` 的策略是三个策略里唯一不公平的那个。

**复现：**

```bash
.venv/bin/python -c "
from nebula_mvp.multinode import SimConfig, build_terminals, run_sim
cfg = SimConfig(n_terminals=16, capacity_bps=40_000_000, policy='fusion_fair', duration_s=2.5, warmup_s=0.4)
r = run_sim(cfg, build_terminals(cfg, primary_ids=range(16)))
print([n['video_delivered_mbps'] for n in r['nodes']])
"
```

**根因。** 轮转指针存的是「当前非空队列列表里的下标」，而不是「上一个被服务的节点 id」：

```python
ids = sorted(nid for nid, q in store.items() if q)
start = self._rr[kind] % len(ids)
nid = ids[start]
self._rr[kind] = start + 1
```

`ids` 的长度随队列空/非空不断变化，`% len(ids)` 于是把指针映射到不同的节点上。队列上限 8 帧、满了丢最老帧（`enqueue` 中的 `popleft`）会把这种偏置固化下来：始终排不到的节点，其队列里的帧全部被后来的帧挤掉，最终交付为 0。

**影响范围。**

- `docs/multinode-summary.md` 第 1 张表中 **k ≥ 10 的各行不可信**。「≥3.5 Mbps 可用」列为 9→8→9→8→8→**0**→9，非单调；k=15 给 0 而 k=16 给 9，物理上讲不通。
- **头条结论「原始填满约 9 路、按 70% 余量约 6 路」仍然成立**，因为它只取自 k ≤ 9 的区间，那里每个节点都跑满标称速率、不存在真正的争抢。但也正因如此，**这个数字等价于一次除法**：40 Mbps 减去遥控与遥测约 0.77 Mbps，除以每路 3.936 Mbps ≈ 9.9 → 9。仿真本应回答的「过载时谁被饿死、是否公平」，恰好落在有缺陷的区间内。
- **第 2、3 条结论不受影响**，它们是本次仿真真正的增量（算术推不出来）：
  - 弱链路终端（效率 0.25）开主视频时，FIFO 下旁观节点控制 P99 约 904 ms，Fusion 约 18 ms；
  - 主视频源 0→1 切换，旁观节点控制 P99 切换前后均为 6.9 ms。

**建议修法。** 记录「上次服务的 node_id」，按 id 升序找下一个大于它的非空队列，找不到则回到最小的那个；或改用赤字轮询（DRR），按字节配额而非包数轮转 —— 视频包远大于控制包，按包轮转本身也不等于带宽公平。修完需重跑并更新 `docs/multinode-summary.md`。

**附带建议。** 表中 `control_p99_ms_max` 在 k=1…16 全部为 6.9 ms。控制包小且严格最高优先级，其时延对视频负载本就不敏感，因此判据 `ok_control_under_100ms` 恒为真、不携带信息，真正起作用的只有 `all_primaries_usable`。文档中宜说明这一点，避免读者误以为「控制时延不受影响」是本次扫描验证出来的结论。

---

### 2.2 【中】链路预算的地球曲率隆起偏大 4 倍

**位置：** [`nebula_mvp/link_budget.py:58`](../nebula_mvp/link_budget.py) `earth_bulge_m`

```python
return 0.078 * (distance_km ** 2) / k
```

**正确式。** 中点隆起 `h = d₁·d₂ / (2·k·R)`。取 d₁ = d₂ = 6 km、k = 4/3、R = 6371 km：

```
h = 6000 × 6000 / (2 × 1.3333 × 6,371,000) ≈ 2.12 m
```

等价写法 `h = d² / (8kR)` 给出同一结果。换算成「km 的平方」形式，系数应为 **0.0196**，而不是 0.078 —— 后者恰好是前者的 4 倍，来源是把 `d₁·d₂` 当成了 `d²` 而不是 `d²/4`。

**影响。** `docs/link-budget.md` 中「地球曲率隆起（估）8.42 m」应为约 **2.1 m**；「建议净空（半径+隆起）27.79 m」应为约 **21.5 m**。方向偏保守，不会把算不通的链路算成通，但数值是错的。

**已复核无误的量**（12 km / 2400 MHz / 5 MHz 带宽）：FSPL 121.63 dB、接收功率 −81.63 dBm、热噪声底 −107.01 dBm、灵敏度 −99.01 dBm、链路余量 17.38 dB、第一菲涅尔区半径 19.36 m，以及三频段对照表中对应各列。

---

### 2.3 【低】STATUS 文档指向的结果路径不存在

**位置：** `docs/STATUS-software.md`「进度总览」表的「结果路径」列。

其中引用的 `artifacts/cli-runs/multinode/`、`artifacts/cli-runs/inquiry-letter.md`、`artifacts/cli-runs/device-bench*`、`artifacts/cli-runs/link-budget/` 在本地与 GitHub 上均不存在：`artifacts/` 已列入 `.gitignore`（未纳入版本控制），本地 `artifacts/cli-runs/` 下只有 `acceptance-linux/`、`STATUS.md`、`software-gap.md` 和一个 prompt 文本。

内容本身在 `docs/` 下有副本，没有丢失。建议把路径改指到 `docs/`，或在文档开头说明「artifacts 不入库，需本地重跑生成」并给出命令。

---

## 三、定位说明（非缺陷，但不应误读）

### 3.1 `auth.py` 与 `interfaces.py` 尚未接入运行中的 MVP

两个模块目前**只被测试导入**，`air_node.py` / `ground_node.py` / `app.py` 都没有引用。运行中的 MVP 仍然收发不带鉴权的明文 JSON。

这与原计划一致（「逻辑和单元测试先写，硬件到了只是接上去」），但对外描述时不应表述为「链路已支持加密鉴权」。`auth.py` 中的 `AuthConfig.secret` 是写死的开发用字符串，注释已声明不得用于生产 —— 接入时必须改为从环境变量或密钥管理注入。

### 3.2 `Iperf3Transport` 未实现，而补齐它不需要硬件

`device_bench.py` 的 `Iperf3Transport` 三个方法全部 `raise NotImplementedError`，仅打印应在实验室执行的命令。`dry-run` / `mock` 两种模式可以跑通完整流程与报告格式。

**补齐 subprocess 调用与 `iperf3 -J` JSON 解析并不依赖样机** —— 取一份现成的 iperf3 JSON 输出即可编写并单测解析逻辑。这是本轮清单中唯一「当前条件下本可完成却未完成」的一项。

---

## 四、建议的修复顺序

1. **修 `_pop_queue_fair` 的轮转指针**，重跑 `scenario_how_many_videos`，更新 `docs/multinode-summary.md`（它影响已写入文档的结论，优先级最高）。
2. **改 `earth_bulge_m` 的系数**为 0.0196（或直接用 `d₁·d₂/(2kR)`），重新生成 `docs/link-budget.md`。
3. **修正 `docs/STATUS-software.md` 的结果路径。**
4. **补 `Iperf3Transport` 的 subprocess 与 JSON 解析**，用离线 JSON 样本做单测。

上述四项均不影响 `docs/inquiry-letter.md` —— **问询函可以立即发出，不必等修复完成。**

---

## 五、本次审查覆盖了什么、没覆盖什么

**已实际执行：** 运行全部 44 项测试；运行 `multinode` 的 k=9/15/16 与两种策略、两种时长共 6 组对照；手工复核 `link_budget` 的 FSPL、噪声底、灵敏度、余量、菲涅尔半径、曲率隆起六个量；阅读 `auth.py` 全文、`interfaces.py` 的 SBUS 编解码段、`device_bench.py` 的配置与 Transport 抽象、`multinode.py` 的调度与实验段；核对 `git ls-files` 与 `.gitignore` 确认入库范围。

**未覆盖：** `device_bench.py` 的 mock 统计量未逐项复核；`interfaces.py` 的 MAVLink 序号监测与失控状态机只读了接口未验证语义；SBUS 编解码仅核对了帧结构与位序（25 字节、0x0F 头、11 位 × 16 通道小端打包、标志位 bit2 = frame_lost / bit3 = failsafe），**未对真实接收机做互操作验证**；`multinode` 的弱链路与切主源两个场景采用其报告数值，未独立重跑。

**一并提示：** 仓库的 `git config user.name` / `user.email` 当前为占位值 `userName` / `userEmail`，提交历史中的作者信息因此不可用于追溯。
