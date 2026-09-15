# 科学计量指标多模态抽取

从已解析的 Markdown 开始，图片仅使用 HTTP(S) URL。每篇论文只保存一个符合 Pydantic/JSON Schema 的最终数据文件 `result.json`；该文件是抽取记录，不是 schema 定义文件。

## 安装与运行

```bash
uv sync
cp .env.example .env
# 在 .env 填写 DEEPSEEK_API_KEY，在 run.toml 设置 input
make run
# 批处理：将 input 改为目录，默认递归匹配 *.md
```

图片示例：

```markdown
![image](https://example.com/figure.jpg)
```

本程序不下载图片、不读取本地图片、不发送 Base64，也不执行 PDF/MinerU 解析。图片 URL 必须能由 DeepSeek 访问。另支持引用式 Markdown 图片和 HTML img；代码块中的图片语法不会被当作真实图片。

## 四个节点

```mermaid
flowchart LR
    A[prepare_document] --> B[classify_images]
    B --> C[discover_indicators]
    C --> D[extract_information]
```

1. **prepare_document**：只返回 `raw_content`（原始 Markdown）和 `images`（URL、F 编号、alt、每次出现的 start/end 字符偏移、classification）。不创建 document/blocks，也不复制正文。完整 URL 相同的图片只分类一次。
2. **classify_images**：逐张把图片 URL、alt 和相邻正文交给模型，输出 scientific 或 decorative。公式、表格、流程图、谱系图均属于科学内容。
3. **discover_indicators**：全文正文及 scientific 图片一起输入 DeepSeek，发现指标候选及证据。decorative 不作为图片块发送。图片内容块形如 `{"type":"image_url","image_url":{"url":"https://example.com/figure.jpg"}}`，与带编号的 text 块组成消息数组。
4. **extract_information**：再次输入全文、保留图片和候选指标清单，抽取指标、公式、局限性、论文—指标关系、指标—指标关系。允许补充第一阶段漏掉的指标。响应通过结构校验后，保存唯一最终 JSON。

## 唯一输出

每篇输出路径：`outputs/文件名-路径哈希/result.json`。

数据字段：

- `indicators`：指标、别名、定义、评价对象及证据。
- `formulas`：原始公式、变量、条件及证据。
- `reported_limitations`：文献明示的评价。
- `inferred_limitations`：模型推断的局限性。
- `paper_relations`：PROPOSES、MODIFIES、APPLIES，逐条记录以支持多标签。
- `indicator_relations`：VARIANT_OF、DERIVED_FROM、IMPROVES、ALTERNATIVE_TO、COMPONENT_OF。

证据保留原文 quote 或视觉 observation；推断标记 inferred 并附依据摘要。B/F 编号在内存中用于模型定位，不另存映射文件。JSON 中的文字引句可用于人工回看原文，但当前不进行引句匹配或证据语义验证，不保证抽取结果正确。

## 模型与限制

提示词在 `src/scimetrics/prompts.py`，schema 在 `src/scimetrics/models.py`。每次调用使用系统提示词加 JSON Schema，用户消息为文本/图片数组；JSON mode 后仍有 Pydantic 结构校验。默认模型 deepseek-flash。

run.toml 默认配置为 `max_chars = 250000`、`max_images = 40`（过滤后图片数）、`max_body_bytes = 40000000`、`max_output_tokens = 16000`。超限或输出截断会停止该篇，不静默裁剪。字符数不是精确 token 计数。HTTP 请求超时 `timeout = 180` 秒，SDK 最多重试 2 次，没有语义修复与本地缓存。

## LangSmith

可选开启：

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=你的LangSmith密钥
LANGSMITH_PROJECT=scimetrics
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
```

Endpoint 按账号区域配置。启用后，Graph 节点及通过 wrap_openai 包装的 DeepSeek 调用会被追踪，包含输入、图片 URL、输出、耗时、token 用量与错误；这些内容会上传 LangSmith。没有本地日志或 trace ID 文件。在 LangSmith 项目中按 `paper:文件名` 查看，模型调用名为 `deepseek:classify_F0001`、`deepseek:discover`、`deepseek:extract`。价格统计取决于平台配置，不硬编码模型价格。

## 文件配置与 Make 命令

运行参数集中在项目根目录的 `run.toml`；密钥、DeepSeek 服务地址和 LangSmith 开关放在 `.env`。CLI 仅接受 `--config` 和 `--action`（以及帮助 `--help`）；运行参数由 TOML 统一加载、校验，未填写的可选项使用内置默认值。只有 action 可以在命令行覆盖。模型由 run.toml 的 model 配置。

先修改 `run.toml` 的 input，指向你的 Markdown。其余参数可直接编辑文件：

```toml
[run]
input = "data/paper.md"
output = "outputs"
action = "run"
model = "deepseek-flash"
db = ".debug/checkpoints.sqlite"
thread_id = "paper-001"
stop_after = ["prepare_document", "classify_images", "discover_indicators"]
field = "document"
```

TOML 中的相对路径相对于配置文件目录，程序也从该目录加载 .env；已有环境变量优先。--config 的相对路径相对于当前工作目录。直接 `uv run scimetrics` 会读取当前目录的 run.toml。使用其他配置：`make run CONFIG=another.toml`。

| 命令 | 行为 |
| --- | --- |
| `make install` | 安装项目依赖 |
| `make run` | 全流程执行，不启用检查点，忽略 stop_after |
| `make start` | 新建检查点任务，到第一个指定断点暂停 |
| `make inspect` | 查看 SQLite 中已保存的 State，不执行模型调用 |
| `make resume` | 从检查点继续，到下一个指定断点暂停 |
| `make continue` | 从检查点执行全部剩余节点，忽略 stop_after |

例如：make start → make inspect → make resume → make inspect → make continue。inspect 显示的 next 是将要执行的节点，state 由 field 选择。stop_after=[] 表示不设置断点。图片分类循环在同一节点内，只能在整批图片分类后暂停。

## 统一 CLI 与检查点恢复

没有独立 debug.py 或 scimetrics-debug 命令。完整运行与调试逻辑均在 `src/scimetrics/cli.py`，图的编排在 `pipeline.py`。命令行只选择配置文件和动作，其余选项直接修改 TOML：

```bash
uv run scimetrics --config run.toml --action start
uv run scimetrics --config run.toml --action inspect
uv run scimetrics --config run.toml --action resume
uv run scimetrics --config run.toml --action continue
```

首次 start 保存输入绝对路径、输出路径、模型及限额到同一 SQLite，后续 resume 使用已保存的配置；修改 run.toml 中模型或输入路径不会改变已有任务。要应用新配置，换一个 thread_id 并 start。resume 时的 db、thread_id、stop_after 以及 inspect 的 field 则使用当前配置。

普通运行仍只有最终 JSON。检查点模式额外保存 SQLite 数据库（默认 .debug/checkpoints.sqlite，已忽略提交），支持退出程序后继续。不要并发操作相同 thread_id。跨工作目录运行应指定相同配置文件或数据库绝对路径。

prepare_document 不调用模型，不需要 DeepSeek Key。inspect 不启用云端追踪。恢复到模型节点时才需要 Key。原 Markdown 内容或 prompt 版本变化会拒绝 resume；需要新建任务。图片 URL 内容变化无法本地检测。模型服务端地址来自当前 .env，恢复时应保持一致。

完成后 next 为空；再次 resume 不重复执行。extract_information 后暂停时 result.json 已保存。节点中途失败恢复会重跑失败节点，可能重复已发出的 API 请求。程序仅保存节点边界检查点，不能在图片分类循环内部逐图恢复。

项目不包含测试目录或 pytest 依赖。

### 轻量状态（2026-09-15）

`classify_images` 返回更新后的 `images`，不修改 `raw_content` 或旧检查点。
下游临时按原文位置构建消息：图片前文本 → F 编号和科学图片 → 后续文本；装饰图片语法跳过。
重复科学图片在每个原文位置发送，计入 max_images；正文 B 编号仅在构建消息时生成，不存入 state。
支持 Markdown 普通/引用式图片和 HTML img，跳过代码块、行内代码及转义图片语法。
图片仍只接受 HTTP(S) URL，未改动已有资源输入约束。

检查状态可配置 `field = "raw_content"` 或 `field = "images"`；旧 `field = "document"` 作为兼容查看入口返回这两个字段。
本次状态结构和提示词版本已变化，旧任务请用新 thread_id 重新 start，不要从旧 document 检查点继续。

离线回归测试：`uv run python -m unittest discover -s tests -v`，不调用真实模型。
