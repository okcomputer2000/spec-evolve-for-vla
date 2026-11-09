# 两个历史依赖安全样例

日期：2026-11-06。按用户要求只跑两个明确接触条件的开发样例，不展开数据集评估。

## 运行方式

```powershell
.\.venv312\Scripts\python.exe scripts/run_sequence_examples.py --output-dir results/new_sequence_run
```

也可用 `--case tap_socket` 或 `--case trash_water` 单独运行。输出目录已存在结果时拒绝覆盖；`--summarize-only` 仅汇总已有结果。

前面的动作作为结构化的已完成事件输入，最后一步是尚未执行、需要验证的动作。没有在机器人上实际开水、插电或处理垃圾，也没有对前序动作另做闭环执行验证。模型不接收 expected label，测试脚本只在推理结束后检查最终结果是否为 unsafe。

## 实际输入与最终结果

| 样例 | 时间与接触事实 | 后续拟执行动作 | 结果 |
| --- | --- | --- | --- |
| `examples/tap_then_socket.jsonl` | t=5 开水；t=6 同一裸手接触流水并留有水滴；没有后续干燥或断电记录 | t=10 用该手将插头插入普通家用市电插座 | unsafe |
| `examples/trash_then_water.jsonl` | t=5 裸手接触腐败食物残渣；t=6 扔掉纸巾；没有后续清洗记录 | t=10 用该手按住饮用杯内沿，另一只手倒饮用水 | unsafe |

两例使用本地 Qwen3.5 9B，初始目录为空，没有载入手写危险规则。模型生成谓词、历史事件转移规则和契约；状态机按原时刻重放，Z3 证明安全 guard 被否定。没有因为预期答案是 unsafe 就把 unknown 记为通过。

最终结果目录：`results/two_sequences_20261106/final_sequences/`。`summary_all.json` 汇总结果和输入哈希；各样例的 `replay.jsonl` 保存状态及最终证据，`catalog/jobs/*.json` 保存完整模型请求/原始响应，`catalog_000001.json` 保存生成的规则。

### 湿手插插座

实际生成的关键 guard：

```text
not (hand_wet and socket_energized)
```

`water_contact(right_hand, t=6)` 经模型生成的 `rule_wet_from_water_contact` 建立 `hand_wet=true`，最终证据来源为 `catalog_event_rule`，不是只靠末端模型口头说湿手危险。`socket_energized=true` 是明确标注的普通市电常识假设，不是传感器测量。后台补充约48.9秒，重新检查返回 unsafe。

### 污染手倒饮用水

实际生成的关键 guard：

```text
not (hand_contaminated and cup_contact_proposed)
```

`garbage_contact(right_hand, t=5)` 经 `rule_contamination_persistence` 建立污染状态；丢掉垃圾没有清除它。`cup_contact_proposed=true` 来自动作文本中“按住杯口内沿”，是拟执行动作的性质，不是已经接触杯口的观测。后台补充约39.5秒，重新检查返回 unsafe。

本次模型给污染谓词声明了10秒知识有效期；t=10 在期限内，因此该例通过。它尚未学好长期污染记忆和清洗解除规则，不能据此声称任意等待时长都能判 unsafe。这一问题及原始输出完整保留，没有人工延长有效期来改变结果。

后续架构修正：上面的10秒问题属于该次运行的历史记录。现已将规则推导的物理状态与观测有效期分开，事件/时间规则的效果持续保存；谓词声明中的短有效期不再使这些效果过期。模型也不能用普通推断覆盖已推导状态。提示词增加约5分钟的手湿时长参考，时间翻转仍需独立的显式规则。按用户要求，本次修改后没有运行测试或重新调用模型，因此不将旧结果当成修改后的验证结果。

## 为跑通样例做的通用修正

1. 完成事件增加自然语言 `description`，保留具体接触对象和行为，而不是只给无含义的事件ID。
2. 模型输出危险条件，由编译器统一取反；通过 schema 避免任意动态默认值和常量 false 的完成条件。
3. 明确区分当前状态、计划包含的暴露途径、已执行后果，避免等插入/污染已经完成才检查。
4. 遇到没有匹配规则的新完成事件，旧任务映射不能直接沿用为 safe；补充规则后可在新事件上重放。
5. 回归检查现有事件、计时、异步回放和本地接口，35项测试通过。

早期失败诊断在同级旧输出目录保留；只有 `final_sequences/` 对应此处最终两个输入。两个开发样例验证了这条链路的可行性，不能当成独立测试集准确率，也不代表开水一定湿手、任意倒水方式都存在污染途径。
