# 审查意见：软件补充项修复版 a9cb9d8

- 审查日期：2026-09-12
- 审查提交：`a9cb9d8e42828e59df365aaf4655bb0f284db65c`
- 环境：Windows，本项目既有 Python 3.12 虚拟环境。
- 方法：从 GitHub 下载独立副本，核对代码差异、运行全部测试，并独立复现关键边界。
- 范围：本文件只记录审查意见，不修改功能代码。下列结论针对上述提交。

## 结论

公平调度和地球曲率计算的修复通过复核。全部 **48 项自动测试通过**，但额外复现仍发现三个测试未覆盖的问题：设备测试台会把无回包超时计成合格时延，防重放缓存淘汰后会接受旧包，离线模式仍然发送 UDP。

当前可确认的是“单节点 MVP、多节点离散事件仿真和配套模块已有实现”。不能据此认定真实设备控制时延验收、防重放或实际多节点 UDP 组网已经完成。

## 已通过复核的修复

| 项目 | 复核结果 |
|---|---|
| 多节点公平轮询 | 改为记录上次服务的节点 ID。16 节点、40 Mbps、全部开主视频、运行 2.5 秒、预热 0.4 秒时，各节点视频吞吐为 2.032～2.159 Mbps，不再出现原来的 6 个零吞吐节点。新增过载回归测试通过。 |
| 地球曲率隆起 | 使用中点公式 `h = d² / (8kR)`。12 km、k=4/3、R=6371 km 时为 2.1189766 m，新增参考值测试通过。 |
| 文档路径及集成边界 | STATUS 改用入库的 docs 路径，并明确接口、鉴权模块尚未接入运行中的 MVP。 |
| iperf3 适配进展 | 已增加 subprocess 调用、JSON 解析和离线吞吐解析测试。控制探测尚有下列问题，不能将整个测试台标为验收完成。 |

多节点复核仅证明该过载场景不再饿死节点，不等于已验证不同包长、不同链路效率下的带宽或空口时间公平性。文档中“最多 10 路”的结论依赖 40 Mbps 容量、每路 ≥3.5 Mbps 可用阈值等模型假设，不是设备实测。

## P1：控制探测把无回包超时当成成功时延

位置：[device_bench.py 第 221～257 行](../nebula_mvp/device_bench.py#L221)，以及第 310～329 行的验收判断。

`probe_control_latency()` 在 `socket.timeout` 分支仍将本次等待时间加入 `samples_ms`。随后 `run_video_plus_control()` 只检查样本数大于零和 P99 小于 250 ms，因此完全没有回包的目标也可能通过验收。

本次复现：在本机绑定一个 UDP 端口但不发送任何回包，配置 50 Hz、0.2 秒。10 次探测全部无响应，仍得到 `n=10`、P99 约 **63.07 ms**、`ok=True`。整个探测约用时 **0.614 秒**。这不是控制链路时延，也不是发送节拍抖动，而主要是等待超时的耗时。阻塞式等待同时使实际发包频率低于配置值。

此外，成功分支没有核对回包来源、序号或载荷，无法保证收到的数据报对应本次探测；迟到回包也可能被记到下一次探测。

### 建议修复

- 超时单独计为丢失，不加入成功时延分布；零有效回包必须失败或标为未测。
- 给探测包增加序号，校验来源和回包内容，只对匹配的有效回包计算 RTT。
- 明确报告是 RTT；没有双端时钟校准时，不标为单向时延。
- 分离定时发送和异步接收，避免每次等待超时阻塞后续 50 Hz 发包。
- 同时报告实际发送频率、有效回包率、超时数和迟到包数，并对交付率设置独立验收条件。

### 回归验收

无回显、全部超时、错误来源、重复回包、乱序/迟到回包都不能成为虚假的成功样本。正常回显应产生与对应序号匹配的 RTT。视频负载未成功启动时，也不能宣称已测得满载下控制时延。

## P1：防重放窗口淘汰后仍接受旧包

位置：[auth.py 第 70～85 行](../nebula_mvp/auth.py#L70)。

该文件未在本次修复中改变。当前只保存最多 64 个序号，超过上限后从集合任意删除，没有记录可接受序号的窗口下界。只要旧包时间戳仍在允许范围内，被淘汰的序号就会再次通过认证。

本次复现：接收同一节点的序号 0～64 共 65 个合法包，随后在 100 ms 后重放被淘汰的序号 0，`open()` 再次返回 `(1, 0, b'test')`，没有抛出 `AuthError`。通过 HMAC 校验只能证明包未被篡改，不能证明它未被执行过。

```python
from nebula_mvp.auth import AuthConfig, AuthGateway

gateway = AuthGateway(AuthConfig(whitelist={1}))
packets = [gateway.seal(1, i, b"test", now_ms=10_000) for i in range(65)]
for packet in packets:
    gateway.open(packet, now_ms=10_000)

# 白盒选择已被当前实现淘汰的序号，避免依赖 set 的遍历顺序。
evicted = next(i for i in range(65) if i not in gateway._seen_seq[1])
print(gateway.open(packets[evicted], now_ms=10_100))  # 当前错误地接受
```

### 建议修复与回归验收

使用最高已接收序号加位图的滑动窗口：低于窗口下界的包拒绝，窗口内已接收的包拒绝，窗口内合法乱序包最多接受一次。明确序号回绕、节点重启和新会话的处理规则，不能简单清空状态后继续接受旧会话的有效包。

至少覆盖：超过 64 包后重放旧包、窗口内乱序、重复包、序号边界和会话重建。本模块尚未接入运行中的 MVP；应修复后再集成，STATUS 中“防重放完成”的状态应相应调整。

## P2：离线 JSON 模式仍发送网络探测

位置：[device_bench.py 的 probe_control_latency](../nebula_mvp/device_bench.py#L221) 与 CLI 的 `--json-dir` 分支。

`--json-dir` 会设置 `run_subprocess=False`，吞吐部分读取 JSON，视频注入部分也跳过真实负载。但是控制探测没有检查离线模式，仍创建 socket 并向配置的终端发送 UDP。

本次在 `Iperf3Transport(..., json_dir=Path('.'), run_subprocess=False)` 下调用 `run_video_plus_control()`，本机接收端实际收到 **10 个 UDP 包**。因此“离线解析”目前既不完全离线，也混合了历史吞吐结果、未实际运行的视频负载和当前网络探测，不能组成同一场景的验收报告。

### 建议修复与回归验收

- 离线模式只能读取已保存的结果，禁止创建网络探测和启动子进程。
- 没有控制测量文件时，将相关步骤标为未测/不可判定，不生成替代成功数据。
- 报告明确区分离线回放、mock 和 live，并保留数据来源及测量条件。
- 对完整 CLI / `run_all()` 离线路径增加测试，断言 socket 与 subprocess 均未被调用；目前测试只覆盖了离线吞吐步骤。

## 无回显与离线发送的共同复现脚本

在仓库根目录，使用已安装依赖的 Python 运行以下代码。它只向本机发送数据，不需要设备或 iperf3。此处调用的是视频＋控制步骤，不读取吞吐 JSON 文件。

```python
import socket
import time
from pathlib import Path
from nebula_mvp.device_bench import BenchConfig, DeviceBench, Iperf3Transport

sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sink.bind(("127.0.0.1", 0))  # 保持端口打开，但永不回显
cfg = BenchConfig(
    mode="iperf3", terminal_host="127.0.0.1",
    control_port=sink.getsockname()[1], control_hz=50, control_seconds=0.2,
)
transport = Iperf3Transport(cfg, json_dir=Path("."), run_subprocess=False)
try:
    started = time.perf_counter()
    print(DeviceBench(cfg, transport).run_video_plus_control())
    print("elapsed_s:", time.perf_counter() - started)
    sink.setblocking(False)
    count = 0
    while True:
        try:
            sink.recvfrom(2048)
            count += 1
        except BlockingIOError:
            break
    print("offline mode sent UDP packets:", count)
finally:
    transport.close()
    sink.close()
```

## 验证范围与下一步

本次实际执行：全部 48 项测试（约 10.9 秒）、16 节点过载复现、曲率参考值复算、缓存溢出后重放、无回显探测与离线发送复现。未连接真实设备，未运行 live iperf3，未重跑完整视频性能基准，也未修改功能代码。

建议下一提交先修复上述三个问题并补回归测试，更新设备测试台和鉴权模块的完成状态。随后再推进显式链路反馈和 1＋4 实际 UDP 终端集成。48 项现有测试通过不能替代这些边界验收。
