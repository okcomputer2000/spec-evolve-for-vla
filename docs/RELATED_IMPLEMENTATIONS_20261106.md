# 相近实现、代码改进与实验数据集

查阅日期：2026-11-06。本轮实际读取公开源码、论文和已有本地 HazardArena 源码；没有下载大型数据集、安装新依赖、调用模型或运行测试。旧实验和模型保留。

## 1. 结论

保留当前主线：确定性形式化检查 → unknown → 本地 LLM 异步补充语义、谓词、事件/时间规则 → 按原时间回放 → 在当前状态上重新验证。LLM 提供规则和带来源的推断，状态引擎维护手湿、污染等状态。

这轮最值得借鉴的是可追溯的验证反馈、独立世界状态快照、契约的执行生命周期，以及将危险识别与真实执行结果分开评估。没有引入每个动作都做 ToT 搜索的路径，也没有增加云模型依赖。

当前优先考虑 **SafeAgentBench 的长程任务和少量详细任务**，用来检查跨步骤状态、语义补充和危险前提。**EMBGuard** 适合补充“有危险物但当前动作不危险”的判断。**HazardArena** 留作接入实际 VLA 后的执行评估。

## 2. 已完成的实现改进

| 修改 | 原有问题 | 现在的行为 |
| --- | --- | --- |
| 规范分阶段发布，`catalog.py`、`worker.py`、`runtime.py` | 先提交磁盘活动版本，再回放状态；回放失败会让磁盘规范领先于运行时 | 先保存未激活的不可变候选；回放、追上新增事件并准备 hold 审计后，才激活活动指针并切换内存状态 |
| 发布代际检查，`catalog.py` | 候选准备期间如果活动规范被切换，旧候选可能覆盖新版本 | 激活时检查准备阶段捕获的 generation；不匹配就拒绝发布 |
| 同刻来源区分，`state.py` | 刚由事件推导出的状态与同刻后续明确观测不同，会被误认为两个独立观测冲突 | 事件/时间推导后允许明确观测更新；同刻独立观测互相矛盾仍为 unknown；同一事件的冲突效果仍为 unknown |
| 事实实例化也进入有限修复，`worker.py` | 规则格式修复覆盖不到后续 grounding 的格式、谓词覆盖和时间错误 | 将实例化及合并验证放入原有修复预算，错误和被拒绝的 grounding 一同反馈；默认仍只有一次修复 |
| hold 轨迹重建，`runtime.py` | 规范更新回放后把整段轨迹再次追加，重复数据会随更新累积 | 回放时替换重建轨迹，保留已经记录的 hold 违规和曾经 unknown 的标记；准备失败不修改活动执行上下文 |
| 判定证据和求解次数，`logic.py` | 返回所有引用事实；已证明安全仍执行另一方向的查询 | 使用 Z3 跟踪前提和不可满足核心，safe/unsafe 返回支撑本次决定的前提；证明 safe 后直接返回 |
| 模型审计缓存，`local.py`、`worker.py` | 全量请求与响应一直留在模型对象中 | 调用记录成功写入 job 审计后才释放；审计写入失败时保留尚未落盘的调用记录 |

“支撑前提”是相对于公式的充分前提集合，不承诺集合最小，也不说明这些前提在物理世界中必然为真。unknown 仍保留引用事实供后续诊断。

发布的具体顺序：

1. 验证候选并写入 `catalog_N.json`、`provenance_N.json`，不更新 `current.json`。
2. 在后台按原时间重放全部状态；进入发布锁后追上准备期间新来的事件。
3. 准备新的历史、解释上下文和执行监视记录，完成可能失败的验证工作。
4. 检查 generation，原子替换磁盘活动指针，然后在同一运行时锁内替换已准备的内存对象。

因此，常规的回放失败、历史预算耗尽或代际冲突不会提前激活新规则。失败候选保留为未激活版本和 job 诊断，后续版本号不会复用它。后台发布占用锁时，同步 `check()` 立即返回 unknown；磁盘操作仍在后台线程中完成。

这不是跨进程事务或断电恢复协议。活动规范可以从磁盘重新加载，当前 episode 的状态与事件历史仍由运行时内存维护，尚未增加进程重启后的自动续接。

## 3. 读了哪些实现，具体借鉴什么

### Deductive Steering

本次核查版本：`f490b04b3c2b7b707eb50c07cc83decf15e9dd19`。

- [evaluator.py](https://github.com/chl19940220/Deductive-Steering/blob/f490b04b3c2b7b707eb50c07cc83decf15e9dd19/deductive_steering/evaluator.py)：分别保存验证状态、反例、不可满足核心和转换失败信息。
- [solver.py](https://github.com/chl19940220/Deductive-Steering/blob/f490b04b3c2b7b707eb50c07cc83decf15e9dd19/smt/solver.py)：`assert_and_track` 将求解结果关联回输入约束。
- [tot_controller.py](https://github.com/chl19940220/Deductive-Steering/blob/f490b04b3c2b7b707eb50c07cc83decf15e9dd19/core/tot_controller.py)：候选评估、传播和回溯的组织。

这次直接借鉴的是证据追踪和有限验证反馈。其程序推理搜索场景与机器人放行门控不同：该版本 evaluator 的 Python-expression 路径存在将非解析失败的 UNKNOWN 按超时警告后视为 verified 的分支。本项目保持自身三值语义，求解不确定时不放行，也不照搬该分支。

### VIRF

论文：[2602.08373v1](https://arxiv.org/html/2602.08373v1)。源码核查版本：`ec41fb88182209d066855260ed107ac0e6bdee88`。

- [action_sequence_processor.py](https://github.com/Sn0wm1an/VIRF/blob/ec41fb88182209d066855260ed107ac0e6bdee88/agent_bench/verify_core/verify_core/action_sequence_processor.py)：世界初始化、独立状态序列及标准化安全报告的组织。
- [run_experiment.py](https://github.com/Sn0wm1an/VIRF/blob/ec41fb88182209d066855260ed107ac0e6bdee88/agent_bench/run_experiment.py)：场景/任务过滤、方法选择及数据加载。
- [实验 README](https://github.com/Sn0wm1an/VIRF/blob/ec41fb88182209d066855260ed107ac0e6bdee88/agent_bench/README.md)：实验入口与多种成功率口径。

借鉴独立状态快照与验证结果分层。本项目进一步把新规范先回放后发布，避免候选准备失败修改活动状态。这个发布协议是本项目针对自身异步实现的修复，不声称是 VIRF 的原实现。

### SkiROS2

实际读取 [skill.py](https://github.com/RobotLabLTH/SkiROS2/blob/master/skiros2_skill/src/skiros2_skill/core/skill.py) 的技能描述、pre/hold/post 生命周期及模拟状态缓存/恢复接口；该链接跟随上游 master，未固定提交。

借鉴点是区分技能契约、运行状态和模拟更新。我们的后台候选也应先在独立状态上完成回放与审计，再发布；执行中的 hold 违规不能因重建状态而被抹去。

### HazardArena

读取现有 `vendor/HazardArena/README.md` 和 `HazardArena/evaluation/stage_predicates.py`，包括 `commit_gate_open()` 的首次命中门控。[上游仓库](https://github.com/HazardArena-Team/HazardArena)，[论文](https://arxiv.org/abs/2604.12447)。

借鉴点是阶段事件与首次违规时间：危险曾经发生，不能用最后一帧的安全状态覆盖。当前代码继续保留首次 hold 违规；本轮同时修正了回放轨迹重复问题。

### EMBGuard

核查版本：`45856cccf919fba7d9abb271e8665520a1577378`。

- [README](https://github.com/dongwxxkchoi/EMBGuard/blob/45856cccf919fba7d9abb271e8665520a1577378/README.md)：数据构造、四类场景、公开字段和评测入口。
- [aggregate_results.py](https://github.com/dongwxxkchoi/EMBGuard/blob/45856cccf919fba7d9abb271e8665520a1577378/src/evals/aggregate_results.py)：多次运行聚合、t 分布置信区间、条件风险类别/危害解释正确率。

主要借鉴实验划分。当前任务不需要为此新增视觉模型或训练模块。

### ToolGate 与 RoboSafe：仅论文层面

- [ToolGate](https://arxiv.org/html/2601.04688v1)：pre/post 契约门控；实验是 ToolBench 与 MCP-Universe。本次未确认其公开实现，因此不声称读过该论文的项目源码。
- [RoboSafe](https://arxiv.org/html/2512.21220v2)：上下文、短期轨迹、长期安全记忆及动态安全谓词。该版本写明代码将在录用后公开；本次没有确认可读源码。其实验使用 SafeAgentBench、8 个模拟种子样例，并检查安全拒绝、危险执行和安全任务表现。

RoboSafe 的效率表将 runtime time cost 定义为验证与动作执行的时间，不能直接拿来与本项目完整本地 LLM 补充耗时比较。后续应独立记录模型生成、回放、发布和确定性检查的时间。

## 4. 他们如何做实验、哪些数据适合我们

| 项目 / 数据 | 实验方式及指标 | 对当前项目的用途与限制 |
| --- | --- | --- |
| **SafeAgentBench** | AI2-THOR 中 750 个任务：300 个详细安全、300 个详细不安全、100 个抽象、50 个长程；评估计划、对象终态和安全完成 | 当前最合适的起点。任务有结构化文本，长程任务能检查历史依赖；但不天然提供手湿、微生物污染的真实物理状态 |
| **VIRF 的 SafeAgentBench 整理数据与 RSSG-THOR-30** | 规范知识可见性对照、带反馈规划、噪声鲁棒性；30 场景感知评估；化学/交叉污染等挑战 | 可借鉴场景结构和危险解释方式。RSSG-THOR-30 主要评估场景表示，不是30条动作安全轨迹；部分污染危险来自语义模型，不能当成模拟器真实污染测量 |
| **EMBGuardTest_v2 / heldout_set** | `causal_risky`、`selective_risky`、`decoupled_benign`、`absent_benign`；风险判断、类别和解释；多次运行聚合 | 适合检查动作相关风险与误报。主要是图像/动作场景，不能替代跨步骤记忆实验；文本字段要审查是否已经直接给出风险答案 |
| **HazardArena** | 51 个 UNSAFE 场景库存；原受控评估10对安全孪生场景，扩展评估16对；ATTEMPT → COMMIT → SUCCESS 首次命中时间与条件化比较 | 适合后续验证 VLA 收到反馈是否真的避险。当前未接执行器，不能把离线文本拒绝计作物理避险成功，也不把51个库存都当成已训练受控任务 |
| **ToolGate 的 ToolBench / MCP-Universe** | API/MCP 工具任务完成、ToolBench pass/win、MCP 场景成功率，以及 pre/post 消融与开销 | 有助于学习契约接口和评测分层；不是具身物理安全的首选主数据集 |
| **Deductive Steering 的程序推理任务** | 仓库含循环不变量与程序合成任务入口，候选接受、反例和搜索统计 | 借鉴约束推理和反馈接口；不直接拿代码生成得分说明 VLA 安全能力 |

SafeAgentBench 实际核查版本：`d23a3c2177c3c61681f31db0b312472a52fa8302`。读取了：

- [long_horizon_1009.jsonl](https://github.com/SafeAgentBench/SafeAgentBench/blob/d23a3c2177c3c61681f31db0b312472a52fa8302/dataset/long_horizon_1009.jsonl)：实际50行；字段为 `scene_name`、`instruction`。第一条就要求开水龙头后不超过两步关闭。
- [unsafe_detailed_1009.jsonl](https://github.com/SafeAgentBench/SafeAgentBench/blob/d23a3c2177c3c61681f31db0b312472a52fa8302/dataset/unsafe_detailed_1009.jsonl)：实际300行，含指令、参考步骤、风险类别及目标终态等字段。
- [detail_evaluate.py](https://github.com/SafeAgentBench/SafeAgentBench/blob/d23a3c2177c3c61681f31db0b312472a52fa8302/evaluator/detail_evaluate.py)：同时包含对象状态比较和 GPT 计划评分。
- [long_horizon_evaluate.py](https://github.com/SafeAgentBench/SafeAgentBench/blob/d23a3c2177c3c61681f31db0b312472a52fa8302/evaluator/long_horizon_evaluate.py)：使用 GPT 区分完成且安全、完成但不安全、未完成，并有完整性/安全性拆分评分入口。

数据入口：[SafeAgentBench](https://github.com/SafeAgentBench/SafeAgentBench)、[EMBGuardTest_v2](https://huggingface.co/datasets/EMBGuard/EMBGuardTest_v2)、[EMBGuard heldout_set](https://huggingface.co/datasets/EMBGuard/heldout_set)。

### 不能直接照搬的评测细节

**成功率的定义。** VIRF 实验 README 同时提供：所有执行步骤成功的 Original Success；允许与参考步骤完全一致但执行失败的 Enhanced Success；进一步忽略特定 slice/sinkbasin 失败的宽松口径。后两种可用于诊断规划与控制器问题，不能当成机器人真实执行成功率。

**评分器。** SafeAgentBench 的 GPT 评分只是一个评测来源，不是独立的形式化或物理证明。本项目保持本地推理；若以后复用其评分脚本，需要单独替换云调用，并明确报告评分器差异。终态、轨迹事件和文字评分应分别保存。

**步数与秒数。** 长程任务的“不超过两步”不能转换为“两秒”。当前时间规则使用秒，布尔事件规则不自带通用步数计数器。正式接入这类任务需要适配器提供真实的离散步序，或补充确定性计数能力，不能凭空编造时间戳来宣称满足原标准。

**输入与答案隔离。** LLM 可以看到原始任务里明示的安全要求，以及运行中实际可得的场景、事件和形式化结果；不能给它 `risk_category`、参考解 `step`、目标 `final_state`、测试类别、解释答案或未来轨迹作为输入。EMBGuard 的 `situation`、`hazard`、`risk` 等字段也需逐字段审查，不能把答案换个名字喂入模型。

## 5. 建议的下一次小实验，尚未执行

从 SafeAgentBench 长程与详细任务中先挑 **8–12 条能清楚给出事件和场景的任务**，完整使用当前方案，先观察最佳效果。选择水/电、污染/饮用、热源/易燃物、容器/溢出等机制，但只使用数据中实际支持的状态；不把“打开龙头”自动标注成“手已经沾水”。

每条样例分别记录：

1. **初次未知原因与语义补充**：陌生文本是否触发 unknown，新谓词、守卫和转换依据是什么。
2. **状态演化与判定证据**：哪个完成事件改变状态，状态是否跨动作持续，是否正确处理清洁/干燥等恢复事件；unsafe 的支撑前提对应哪些真实输入。
3. **两类耗时**：首次未知到可用规范的异步耗时，以及已有规范下的确定性检查耗时，分别记录。
4. **结果范围**：safe、unsafe、未解决的 unknown 分开统计；实际执行成功与离线语义判断分开。已有规则复用效果只能与冷启动结果分别说明。

如果只做结构化文本重放，应命名为基于公开任务的语义轨迹实验。人工补充且无法由原数据支持的水接触、污染或干燥过程，应另外列为合成场景，不能冒称原基准原生标签。后续接入 VLA 后，再使用 HazardArena 的安全孪生任务和阶段命中时间检查真正的避险效果。

## 6. 本轮验证状态

按照要求，本轮没有运行单元测试、格式检查、编译检查、模型推理或数据集实验。上述代码改进尚未获得执行验证，不能宣称准确率或耗时已提高。现有两个顺序样例的历史成绩仍保存在 `results/two_sequences_20261106/final_sequences/`，其来源和代码快照不作改写，也不作为本轮修改通过的证据。
