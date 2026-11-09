# 可用于接水与湿润状态测试的机器人数据源

查证日期：2026-11-07。扩大检索后，已找到可直接下载的真实机器人接水操作。之前“没有找到完整的夹爪湿／干标签＋π0.5 测试协议”不能理解成“没有适合的机器人图像或视频”。本轮实际下载并解码了以下 DROID 和 Mobile ALOHA 视频，未调用模型或赋予湿／干真值。

## 1. DROID：优先用于 π0.5 相关测试

- 官方项目：https://droid-dataset.github.io/
- 可逐 episode 下载的 LeRobot 转换：https://huggingface.co/datasets/cadene/droid
- 固定版本：`c8fbc029c9786fd4377ea20d9535e86d88199c0f`。
- OpenPI 官方 README 列出 `pi05_droid` checkpoint：https://github.com/Physical-Intelligence/openpi 。这说明有匹配 DROID 机器人的策略入口，并不说明下面的演示由 π0.5 生成，或该模型已经有湿润识别能力。

已下载 **episode 59**，任务索引 29，615 帧、15 FPS、约 41 秒。原任务：

> Pick up the white mug on the left side of the counter, put it under the running faucet then place it down on the right side of the counter.

中文含义：拿起台面左侧的白杯，放到流水下，再放到右侧。

三个视角已下载：外部相机 1、外部相机 2、腕部相机，共约 10.85 MB。另下载了 53,320 字节的对应 parquet，核对有 `observation.state`、`action`、`timestamp`、`frame_index`、`episode_index`、`task_index`，时间范围 0–40.933 秒。

观看抽帧确认：外部相机能看到机器人持杯伸到水槽；腕部相机能看到夹爪、杯子、水槽和水流。**物体接水不等于夹爪必然接水**，仍要逐帧判定实际可见证据。

其他已定位的 episode 元数据：

| episode | 帧数 | 原始任务摘要 |
| --- | --- | --- |
| 13207 | 548 | 把牙刷放到流水下，再关闭水龙头 |
| 15705 | 610 | 开龙头，把水壶放到流水下，再放回台面并关龙头 |
| 17358 | 254 | Wet the toothbrush under the tap then place it back on the counter |
| 22759 | 480 | 把杯子放到水龙头下，再放到台面右侧 |

这些额外条目仅核对了元数据，没有宣称全部视频已下载、可见或具有夹爪湿润证据。对 31,308 条任务描述做较严格接水关键词筛选，找到 19 条候选描述；不是 19 个有湿润真值的样例，也不是完整统计。

## 2. Mobile ALOHA 洗锅：优先用于 ALOHA / FineART 相机输入

- 原项目：https://mobile-aloha.github.io/
- LeRobot 数据：https://huggingface.co/datasets/lerobot/aloha_mobile_wash_pan
- 固定版本：`2d7d8593d81eeec29f5785f21e0a8c92bc78bcc0`。
- 元数据确认：50 条演示、55,000 帧、50 FPS，顶端、左腕、右腕相机。每条演示 1,100 帧，约 22 秒。
- 原任务：`Pick up the pan, rinse it in the sink and then place it in the drying rack.`

已下载顶端相机的 `file-001.mp4`（22,453,197 字节），并依据 episode 元数据从其中的 22–44 秒区间抽取 episode 49。实际图像能看到真实 ALOHA 双臂夹持锅具并移动到水槽，属于机器人相机观测，不是人类洗手图像。两个腕部视频文件存在，但本轮没有下载完整的较大文件。

注意：这是 ACT / Mobile ALOHA 项目的示范数据，不能把它叫作 π0.5 rollout。它与 FineART 同属 ALOHA 类具身形态，但相机配置、机器人和训练分布并不因此完全一致。

## 3. RoboCasa365：洗菜与接水事件的可控仿真来源

- 官方项目：https://robocasa.ai/
- 源码：https://github.com/robocasa/robocasa
- 实际读取的任务：
  - `WashLettuce`：`robocasa/environments/kitchen/composite/making_salads/wash_lettuce.py`。
  - `SoakSponge`：`robocasa/environments/kitchen/composite/washing_dishes/soak_sponge.py`。
  - 龙头操作：`robocasa/environments/kitchen/atomic/kitchen_sink.py`。

`WashLettuce` 使用 `sink.check_obj_under_water()` 累计 25 个满足条件的仿真步；`SoakSponge` 记录海绵在流水区域的累计时间。继续检查 `robocasa/models/fixtures/sink.py` 确认，该接口结合水龙头开关状态与物体相对出水区域的空间关系判断接水。

这可以支持清楚定义的“接水事件→状态保持→后续风险”测试。现成任务没有给出物理准确的夹爪湿润／干燥真值；25 个累计仿真步也不能直接替换成五分钟干燥模型。

官方策略文档提供 OpenPI 分支，明确描述支持的是 **pi0**；没有据此声称已确认可直接运行 π0.5 闭环测试。

## 4. BridgeData V2：补充来源

- 官方项目：https://rail-berkeley.github.io/bridgedata/
- 实际读取的转换数据：https://huggingface.co/datasets/IPEC-COMMUNITY/bridge_orig_lerobot

核对了任务表和 episode 元数据，例如 episode 907 为 `wipe pot with sponge`，episode 1599 为 `pick up sponge and wipe plate`，以及大量龙头操作。仅看到指令，不能确定真水、夹爪接水或湿润状态；本轮没有下载这些片段，因此优先级低于已实际查看的 DROID 和 Mobile ALOHA。

## 建议先用什么

先从 **DROID episode 59** 和 **Mobile ALOHA 洗锅的少量演示** 开始：验证实际图像能提取哪些接水证据，再验证状态保持。每个候选帧可以分别标注“夹爪明确接水”“只有所持物体接水”“无法确定”，不要从任务名自动生成 wet=true 标签。没有明确证据的状态允许保持 unknown。

当前这些都是示范数据或任务环境，缺少原生“湿润后插电”的安全标签。它们适合先测试感知与时序状态层；后续拟执行危险动作的离线查询应单独标明，不能拼接不同 episode 后宣称是实际连续执行或闭环避险成果。

## 本地保存

目录：`results/robot_data_sources_20261107/`。

- `source_manifest.json`：固定版本、episode、视频来源、文件 SHA-256、字节数。
- `droid_downloads.json`：三相机精确下载 URL。
- `droid_episode_000059.parquet`：实际动作、状态与时间戳。
- `droid_episode59_contact_sheet.jpg`：同一 episode 外部与腕部视角抽帧。
- `aloha_contact_sheet.jpg`：episode 49 四个时刻抽帧。
- `droid_water_episode_candidates.json`、`droid_water_task_candidates.json`：后续取样索引。

为避免把模型输出混入数据来源，本轮没有对这些图片调用视觉问答、没有运行安全预测，也没有生成准确率指标。
