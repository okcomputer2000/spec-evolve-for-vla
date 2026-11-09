# 视觉描述作为形式化验证的文本来源

## 链路

图片只交给显式配置的独立本地视觉模型，当前配置为 `qwen3-vl:4b`。文本语义模型仍为 `qwen3.5:9b`。未配置视觉模型、配置成同一个模型或视觉描述失败时，不把图片交给文本模型，也不将未完成的视觉解释静默当成已处理。

当前视觉标签为 Thinking 变体，本机使用配置中的 assistant 前缀关闭思考段以生成 JSON。报告各列表有显式上限，禁止重复扩写不确定项；输出截断时整份报告拒绝，不接收半份结果。单张存档原图的完整报告已经通过工程复核，保存在 `results/vision_smoke_20261107_bounded/`；不宣称视觉识别准确率。

视觉输出包含详细总描述、对象列表、对象表面状态/接触/容纳/空间关系、逐条观察和不确定性。每条观察有对象 ID 和 `visible / uncertain` 标记；图片文件有 SHA-256 来源记录。单帧不能证明动作已经完成，也不能仅凭外观确认带电、温度、消毒效果或隐藏材质。

后台会处理捕获历史中的视觉场景，而不只看最新一帧。重复的同一场景与像素使用有界缓存；改变图片内容或场景文字会改变缓存键。时刻使用场景采集时间，不使用描述生成完成时间。

描述随后与任务文本、动作文本、结构化场景和已完成事件一起进入 `evidence_index`。内容哈希生成来源 ID。模型生成谓词、事件规则、时间规则和事实时，通过 `evidence_ids` 建立与这些文本的关系。实例化事实不能早于其引用的证据；不确定的视觉观察不能直接支持确定布尔值。

原文、来源 ID、候选和模型调用保存在 job 审计中，成功发布时也保存到规范 provenance。确定性层的事实、事件效果和证明证据保留来源引用。引用关系保证可追溯性，不证明视觉描述或常识本身正确。

## 文本是否会被形式化系统处理

会，但自然语言解释由后台模型完成，求解器本身不理解中文或英文：

1. 陌生任务/动作文本触发 unknown。
2. 模型将其映射到具体对象上的谓词和契约；再给出事件/时间规则。
3. 确定性状态机按已完成事件推导并保持状态，Z3 检查当前公式。

例如，完成“打开水龙头”可以支持流水状态；**不能单独推出手湿**。实际水接触事件或“手上可见水滴”等独立描述，才支持手湿状态或相关转移。后来换成插插座任务时，状态由状态机维持，不要求 LLM 再次记住。

常识性能力与现场状态也需区分。材料导电性、化学不相容性、动物致伤能力、药物过量风险，可以由模型结合文本提出并标明常识假设；不能把这种能力冒称为已经发生的伤害，也不要求先观察到伤害才能阻止危险动作。

新生成的谓词使用 `kind=state / proposal / disposition` 明确区分实际状态、执行当前提案将涉及的条件、稳定常识能力。`proposal_completed=false` 不妨碍从动作文本确认 proposal 类型；它仍不能证明真实动作已完成。事件和时间规则只能更新 state 类型。未带 kind 的历史契约兼容为 state。类型约束减少模态混淆，但不证明模型对语义的分类正确。

## 外部视觉系统入口

已有其他视觉模型或检测器时，可把结果作为详细结构化文本写入普通 `Scene.description / objects`；也可以提供统一报告：

```python
scene = Scene(
    at_s=12,
    description="独立视觉系统的场景描述",
    visual_report={
        "description": "右手表面可见水滴，位于插座附近；电源状态不可见。",
        "objects": [{"id": "right_hand", "name": "右手",
                     "visible_states": ["表面有水滴"],
                     "relations": ["靠近插座"], "uncertainties": []}],
        "observations": [{"id": "droplets", "text": "右手表面有水滴。",
                          "object_ids": ["right_hand"], "certainty": "visible"}],
        "uncertainties": ["无法从图片确定插座是否带电"],
    },
)
guard.observe_scene(scene)
```

## 十题开发实验的范围

SafetyBench Physical Health 的十个随机样例是纯文本题，不调用视觉模型，不生成替代图片，不把文字判断成绩作为视觉成绩。每个原始选项或编号动作单独进入现有安全验证器，再由确定性逻辑重建原始选择题答案。组合题和否定题保留其原始结构；结果不充分或多个动作均 safe 时不强行猜测。

固定抽样种子为 `20261106`，在完整英文 Physical Health 子集1149题中无放回抽十题。保留对应中文供展示，英文原文作为实际输入。题目、答案与来源文件哈希分别保存，答案不进入模型输入。首轮结果、修改后复核结果及每轮代码哈希分别留存；这是一组用于测试驱动优化的开发样例，不是独立测试集成绩。

```powershell
.\.venv312\Scripts\python.exe scripts/run_safetybench.py prepare --output-dir results/new_physical_ten
.\.venv312\Scripts\python.exe scripts/run_safetybench.py run --output-dir results/new_physical_ten --case 1 --tag initial
.\.venv312\Scripts\python.exe scripts/run_safetybench.py run --output-dir results/new_physical_ten --case 1 --tag revision --atoms 2
.\.venv312\Scripts\python.exe scripts/run_safetybench.py summarize --output-dir results/new_physical_ten
```
