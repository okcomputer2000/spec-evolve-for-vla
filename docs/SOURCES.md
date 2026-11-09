# 查阅与借鉴记录

日期：2026-11-06。以下区分论文概念、公开源码和接口文档；没有将作者报告的指标当成本项目结果。相关旧全文笔记保存在实验归档中，本次未再次保存下载缓存或复制第三方源码。

本轮追加的源码核查、固定提交链接、已完成修改、数据集适配限制和实验建议见 [相近实现与实验数据集](RELATED_IMPLEMENTATIONS_20261106.md)。包括实际读取的 Deductive Steering evaluator/solver/controller、VIRF 状态序列处理与实验入口、SafeAgentBench 数据样例与评分器、EMBGuard 指标聚合，以及已有 HazardArena 阶段谓词。ToolGate 和 RoboSafe 仅作为论文来源；未确认可读的公开实现。

| 来源 | 实际借鉴点 / 阅读范围 |
| --- | --- |
| [VIRF 原文](https://arxiv.org/html/2602.08373v1) / [源码](https://github.com/Sn0wm1an/VIRF/tree/ec41fb88182209d066855260ed107ac0e6bdee88) | Logic Tutor、UNKNOWN、规则演化展望；本轮实际读取状态序列处理、实验入口和成功率定义，固定版本及细节见新增报告 |
| [ToolGate 原文](https://arxiv.org/html/2601.04688v1) | pre/post 门控及其契约正确性前提；不能撤销机器人已发生的动作 |
| [SkiROS2 原文](https://arxiv.org/html/2306.17030v1) / [官方 skill.py](https://github.com/RobotLabLTH/SkiROS2/blob/master/skiros2_skill/src/skiros2_skill/core/skill.py) | 实际读取技能描述和 pre/hold/post 条件接口 |
| [Deductive Steering 仓库](https://github.com/chl19940220/Deductive-Steering/tree/f490b04b3c2b7b707eb50c07cc83decf15e9dd19) | 本轮实际读取 evaluator/solver/controller，借鉴不可满足核心对应的前提追踪与验证反馈；保留本项目 unknown 不放行的语义，没有使用其云 API 集成 |
| [pytransitions core.py](https://github.com/pytransitions/transitions/blob/master/transitions/core.py) / [states.py](https://github.com/pytransitions/transitions/blob/master/transitions/extensions/states.py) | 实际读取守卫、条件与 Timeout 的进入/退出取消机制，借鉴状态相关定时的生命周期 |
| [RTAMT README](https://github.com/nickovic/rtamt) / [PyPI](https://pypi.org/project/rtamt/) | 实际核查 dense-time specification/evaluate 接口和 Python 版本要求；项目已实际调用 RTAMT |
| [Z3Py 指南](https://ericpony.github.io/z3py-tutorial/guide-examples.htm) | 双向可满足性查询、模型/反例；每个证明使用独立 context，避免跨线程共享 Z3 context |
| [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs) | 原生 JSON Schema、非流式本地调用、严格验证生成结果 |
| [Ollama vision](https://docs.ollama.com/capabilities/vision) | 图片通过视觉阶段发送，后续文本阶段消费独立描述 |
| [Python queue](https://docs.python.org/3/library/queue.html) | 有界队列、非阻塞提交、后台任务生命周期 |

`fm-agent` 名称存在歧义，本次 GitHub 仓库检索未确定其对应项目，因此不声称已经复现或直接借用了该仓库。既有思想通过已核实的 Deductive Steering 和上述项目落地。

与附件建议存在的有意区别：不要求所有新谓词都有传感器计算器，允许基于文本和常识的经验事实，但标明来源；不强迫候选把触发 unknown 的快照判成 unsafe，否则会给模型制造先验危险标签；不采用未经检查的固定 π0.5 action chunk 宽度，当前先做执行模型无关的语义接口。
