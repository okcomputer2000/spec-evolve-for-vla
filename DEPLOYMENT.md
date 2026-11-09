# 本地部署

## 当前环境

| 组件 | 路径或配置 |
| --- | --- |
| 当前项目 Python | `.venv312/Scripts/python.exe`，CPython 3.12.16 |
| 旧 Python 环境 | `.venv/`，保留供已有依赖使用 |
| 依赖锁定 | `uv.lock`，`pyproject.toml` |
| 求解器与监控 | Z3 4.16、RTAMT 0.4.10、ANTLR 4.7 |
| 本地模型 | Ollama，`qwen3.5:9b` Q4_K_M |
| 模型与运行程序 | `models/ollama/`、`tools/ollama/` |
| VLA 资源 | `vendor/openpi/`、`vendor/lerobot/`、`checkpoints/pi05_libero/` |
| WSL | `vla-lab`，已有 `/home/researcher/openpi-venv` 保留 |

使用 3.12 是因为 RTAMT 0.4.10 明确要求 Python `>=3.8,<3.13`。已经安装独立解释器和新环境，未删模型、未重装 WSL。

重建当前环境可使用已有 uv：

```powershell
.\.venv\Scripts\uv.exe python install 3.12
$env:UV_PROJECT_ENVIRONMENT = '.venv312'
.\.venv\Scripts\uv.exe sync --python 3.12 --locked
```

显式指定 `.venv312`，避免覆盖旧 `.venv`。运行命令直接使用 `.venv312/Scripts/python.exe`，不必依赖终端是否激活环境。

## 本地推理

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_ollama.ps1
```

配置文件 `llm_local_config.json` 中的上下文为 16384、输出上限 4096，温度 0，关闭 thinking。语义生成和事实实例化调用 `qwen3.5:9b`；`vision_model` 单独配置为 `qwen3-vl:4b`，不会退回文本模型处理像素。视觉模型未安装或未配置时明确保留 unknown。完整视觉报告及其他视觉系统的接入方式见 [视觉文本证据](docs/VISUAL_TEXT_EVIDENCE.md)。

客户端只允许 loopback HTTP Ollama，关闭系统代理继承。项目不调用云端模型、不需要 API key。输入超过保守上下文预算时会明确报错并保留 unknown，不静默截断。视觉图片由后台读取，最多三张，每张最多 12 MB、最大边 4096；没有隐式缩放。

当前配置启用 `interpret_completed_events`：对没有匹配规则的完成报告，先用同一本地文本模型独立提取状态变化，再生成事件规则和契约。该解释不接收未来动作，结果按输入内容缓存，解释本身不直接更改世界状态。后台单次文本请求超时配置为300秒；这不是同步动作检查的等待时间。

`tokenizer_path` 指向本地 `models/tokenizers/qwen3_5_9b.json`，来自 Qwen/Qwen3.5-9B 官方 tokenizer。仅用于文本 token 计数，不加载远程推理；相邻 metadata 文件保存下载地址与 SHA-256。预算预留 768 个模板 token 和完整输出上限。未配置 tokenizer 或调用不同视觉模型时使用 UTF-8 字节上界；不会因为预算不足而截断历史。更换文本模型时应同时更换匹配的 tokenizer。

本机 8 GB 显存下，大上下文可能影响模型吞吐；后台调用允许较慢，在线检查不等待模型。没有主动卸载或删除任何模型。模型本身的空闲驻留由 Ollama `keep_alive` 管理。

文本 `think` 配置现在实际传给 Ollama，默认 false；开发实验另有开启思考的配置，输出额度包含思考消耗。视觉使用独立的 `vision_max_output_tokens=8192`、`vision_timeout_s=300`；当前 `qwen3-vl:4b` 标签实际指向 Thinking 变体，关闭思考的请求未能阻止它输出思考。图片路径不作为视觉推理内容，像素通过独立 images 字段发送，文件来源与哈希保留在审计。达到输出上限时整份候选拒绝，不接受截断 JSON。

该视觉变体在本机使用 `vision_assistant_prefill="<think>\n</think>\n"`，以 assistant 前缀关闭思考段，再生成 JSON；前缀也记录到审计。它只应用于视觉调用，普通文本推理不受影响。其他视觉模型可省略此设置。使用图片的最小接口复核已经返回完整描述；结构化详细报告的独立结果保存在 results 下。

## 时钟与生命周期

所有时间统一为 episode 起点后的秒数。外部事实、场景、事件按非递减时间输入；模型返回按采集时间插入并回放。调用方负责提供完成事件；仅提交动作请求不会修改实际状态。

每个 `SafetyGuard` 是一段 episode 的状态，catalog 可以跨 episode 复用，具体世界状态不跨 episode 自动延续。默认最多 2048 条更新、128 个场景、16 个活动执行上下文；达到上限明确停止判为 safe，不丢弃历史。后台关闭默认等一秒；需要持久化完整模型结果时先 `wait_idle()` 或适当增大 `close(timeout_s=...)`。

当前代码未连接 π0.5 服务或执行器。保留的原实验成果见 `experiments/archive_20261106/`；旧采集、消融和反馈脚本按要求移除，旧报告中的原始命令仅供历史理解。
