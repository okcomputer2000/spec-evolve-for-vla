# 机器人视觉谓词与异步状态保持

## 当前实现

LLM 遇到未知任务时补充契约和谓词，也为可由图像回答的谓词提供 `evaluation`。形式化快路径只读取状态并调用求解器；后台视觉模块读取机器人相机图像，回答短问题，状态引擎接收带时间和来源的事实。模型不负责记住上一次的 wet 值。

配置示例仅解释接口，不是经过验证的湿润检测器：

```json
{
  "method": "visual_question",
  "question": "Is the robot gripper physically wet? Answer unknown if wetness or dryness cannot be established.",
  "yes_value": true,
  "when": true,
  "query_interval_s": 1,
  "true_hold_s": 300,
  "false_hold_s": 1,
  "max_frame_age_s": 2
}
```

`yes_value` 指明 yes 对应的谓词值，避免反向问题造成极性错误。另一个视觉谓词可以设置 `when={"ref":"gripper_wet"}`，只在湿润已知为 true 时询问；门控依赖出现环会被拒绝。电器类别可从图像识别，但“实际带电”不能仅由外观证明；文本常识假设必须保留为明确的假设。

## 时间语义

- 湿润为 true 时仍然接收新帧并询问。采集时刻为 20 秒的新 true 使保持截止时刻变为 320 秒，与响应返回时刻无关。
- 未到期时，no 或 unknown 不清除已有 true，也不延长它。
- 保持到期且没有新证据时回到 unknown，不把五分钟当成保证干燥的物理定律。
- 同一帧不能反复续期；旧帧、旧结果和已失效的短期结果不能覆盖较新的事实。
- 新帧到来通过 `observe_frame()` 驱动查询；没有新帧时不会反复询问旧图片。模型繁忙时每个谓词最多保留一个在途查询，完成后检查最新帧和门控依赖。

默认每个谓词约一秒的询问间隔，整个视觉队列至少间隔 0.5 秒启动请求。实际频率受本地模型延迟限制；这不是已经测得的 1 Hz 性能。

## 接入相机

```python
from safety_guard import SafetyGuard, Scene
from safety_guard.local import LocalModel

model = LocalModel.from_config("llm_local_config.json")
with SafetyGuard("results/robot_episode", model) as guard:
    guard.observe_scene(Scene(
        0, "A robot gripper and a work surface viewed by robot cameras.",
        images=("/absolute/immutable/frame_000.png",),
    ))
    # 首次 check 注册当前任务的契约，后续实际相机帧驱动后台查询。
    guard.observe_frame(1, ("/absolute/immutable/frame_001.png",))
```

适配器必须传入同一 episode 时间轴下的真实采集时刻和不可覆盖的帧文件；不要把持续覆盖的 `latest.png` 当成不可变历史证据。JSONL 回放新增 `frame` 类型：`{"type":"frame","data":{"at_s":1,"images":["/absolute/immutable/frame_001.png"]}}`。新帧不会触发一次完整文本语义分析，也不无限追加进场景语义历史。场景物体或任务上下文确实改变时，仍通过 `observe_scene()` 登记。

`llm_local_config.json` 启用 `use_visual_predicates`。当前可运行后端是独立的本地 `qwen3-vl:4b`，不把它称作 π0.5 或 FineART。后台视觉调用与文本模型有各自的审计缓存；只有图片和问题进入短问答，没有标准答案或任务安全标签。

## FineART 原生语言头接口

`safety_guard.predicates.FineARTVisualQuestions` 接受已经加载的原生 FineART policy、对应 tokenizer 和有序相机键；通过官方 `select_message()` 接口生成短答案，不调用动作专家，不训练新参数。将它作为 `SafetyGuard(..., predicate_backend=backend)` 即可替换视觉后端。相机键必须与该 checkpoint 的 `config.image_features` 对应，不能把人类洗手照片当作机器人观测。

这个原生接口适配器尚未在本机完成权重加载和真实推理验证。公开的 [FineART-VLA-subtask-midtrained 模型卡](https://huggingface.co/lerobot/FineART-VLA-subtask-midtrained) 明确说该权重不能据此认定具备通用 VQA 或记忆能力。无法解析为完整 yes/no/unknown 的回答保留原文并变为 unknown。接口存在不意味着湿润识别可靠；后续需要用真实夹爪图像单独验证。

此前尝试的视觉语言骨干下载已暂停，部分文件和恢复清单保留在 `models/fineart_vqa_backbone/`；它不是已部署模型。`scripts/prepare_fineart_backbone.py` 仅用于按固定 checkpoint 版本恢复该实验性提取，不属于默认运行链路，也不等同于原生 FineART policy。

## 数据集查证

后续扩大检索已经实际下载并查看 **DROID 接水 episode 59** 和 **Mobile ALOHA 洗锅**视频，详见 [可用机器人数据源与具体样例](ROBOT_DATA_SOURCES.md)。下面所说的缺口是现成的湿／干安全标注与完整测试协议，不能理解为没有真实机器人接水视频。

截至本次查阅，没有确认同时包含“真实机器人夹爪湿／干标注、时间连续观测、π0.5 接入协议”的现成数据集。以下是查到的具体范围，不代表不存在其他数据。

| 数据或环境 | 已确认用途 | 对本问题的缺口 |
| --- | --- | --- |
| [OpenPI / LIBERO](https://github.com/Physical-Intelligence/openpi) | 提供 π0.5 的 LIBERO 专家模型和测试入口；机器人仿真相机 | 没有查到原生夹爪湿／干标注 |
| [DROID](https://droid-dataset.github.io/) | 真实机器人多视角图像；OpenPI 提供 DROID 专家模型 | 没有确认有可靠湿／干标签，需另选片段标注 |
| [FineART](https://huggingface.co/datasets/lerobot/FineART) | 与公开 FineART 权重相近的 ALOHA 机器人与腕部相机 | 检查了 151 个任务名称：包含 Plug in charger 等插电任务，没有发现名称为洗菜／洗手的任务；不能据任务名推定夹爪状态 |
| [HazardArena](../vendor/HazardArena/README.md) | 本地源码有 OpenPI `pi05` 策略接口和水／电场景 | 本地 `tasks/condition.py` 注明没有 MuJoCo 液体模拟；倾倒判据不能证明夹爪实际变湿 |
| [SafeAgentBench](https://github.com/SafeAgentBench/SafeAgentBench) | AI2-THOR 高层危险任务，包含水和电器 | 不是原生 π0.5 夹爪视觉测试，不能直接证明该谓词识别能力 |

因此本轮先完成状态链路；没有把人类洗手照片、倾倒姿态、手写 wet 标签或论文插图包装成机器人湿润识别实验。

## 检查与保存

`tests/test_visual_predicates.py` 用显式标记的合成回答检查续期、门控、延迟、过期、后台执行和审计。这些是工程测试，不是视觉模型准确率。测试日志保存在 `results/visual_predicates_20261107/unit_tests.txt`；原始网页、模型卡和 FineART 元数据也保存在同目录。旧实验结果没有删除或改写。
