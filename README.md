# Aster

**面向电商售后的智能客服 Agent。**

Aster 将订单查询、商品咨询、退款申请和换货办理放在同一段对话中处理。客户可以查看历史对话、资料来源与办理进度，管理员可以审核工单、补充处理意见并继续办理。

项目重点处理售后 Agent 中的几个具体问题：咨询被误当成办理、相关资料缺少所问事实、长会话上下文膨胀、审批后流程中断，以及重试造成重复业务记录。DeepSeek 负责对话与工具选择，LangGraph 编排工具循环和人工审批，MCP 连接订单、知识库与业务服务；身份、资格和办理结果由程序与数据库核验。

项目包含 FastAPI 后端和 React 工作台。退款记入模拟账本，换货以生成受理记录为完成标志，适合在本地演示完整的售后处理过程。

[功能](#功能) · [运行界面](#运行界面) · [架构](#架构) · [本地运行](#本地运行) · [演示场景](#演示场景) · [知识库](#知识库) · [测试与验证](#测试与验证) · [目录](#目录)

## 功能

- **咨询与办理分流**：同一个 Agent 完成咨询与办理。Qwen2.5:3B 结合当前消息和近期对话识别意图，按查询、退款、换货和澄清结果开放工具；方案尚未选定时由程序收紧为澄清，分类服务故障时由规则兜底。
- **结构化工具调用**：通过 MCP Tools 统一接入订单、商品、知识、退款和换货申请，复用身份签名、超时与错误返回约定。调用前验证参数和工具范围，服务端重新核验身份、订单归属与业务条件；结果未知时提示查询状态。
- **退款最终确认**：Agent 核验资格后生成待确认单，弹框展示数据库中的订单号、商品、型号与退款金额。客户点击确认后，服务端复核归属、有效期和订单快照，再在同一事务中消费确认单并执行；取消不退款，重复点击返回已有结果。
- **有依据的知识回答**：使用百炼 Embedding 与 BM25 双路召回、RRF 融合及百炼 Rerank，按型号、品类和文档版本过滤资料。回答拆成带原文引用的事实与缺失事项，程序核对引用和数字；资料缺失、检索故障与回答校验失败分别处理。
- **有预算的上下文**：持久化对话历史和增量摘要，按当前订单、工单关联历史会话。按 token 预算依次裁剪旧消息、摘要和证据，最后压缩可选业务描述，保留当前问题、必要业务事实与完整工具调用配对。调用前计入工具定义并预留回复空间；必需内容仍超限时显式终止请求。
- **人工审批与恢复**：换货流程通过 LangGraph interrupt/resume 等待材料或管理员审批，将 LangGraph Checkpoint 持久化到 PostgreSQL，支持重启后继续办理。
- **业务一致性**：使用事务、行锁、唯一约束和持久化请求记录处理退款与换货互斥、重复提交和重试；根据实际工具结果校验最终回复。
- **客户与管理工作台**：提供黑白界面、历史会话、订单列表、工单详情、常驻处理进度、资料来源及 token 用量，通过 SSE 更新执行过程。

## 运行界面

以下为本地运行截图，使用项目内置的虚构客户、商品与工单。

**客户工作台**：在同一页面查看订单、咨询商品，并跟踪工具调用与 token 用量。示例回答引用了当前型号的尺码资料。

![客户工作台：尺码咨询、订单信息与处理进度](assets/screenshots/customer-workspace.jpg)

**管理工作台**：按状态筛选换货工单，查看订单与申请材料，再进行审批或后续处理。

![管理工作台：待审批工单筛选与订单详情](assets/screenshots/admin-console.jpg)

**退款确认**：办理前展示具体订单与金额，客户确认后才执行模拟退款。超额申请显示“确认提交审核”。

![退款确认：订单号、商品型号、金额与操作按钮](assets/screenshots/refund-confirmation.jpg)

## 架构

```mermaid
flowchart TD
    UI[React 客户与管理工作台] <-->|HTTP / SSE| API[FastAPI · 身份与请求边界]
    API --> Context[历史消息 · 摘要 · 业务事实]
    Context --> Classifier[Qwen 意图分类 / 规则兜底]
    Classifier --> Agent[LangGraph 售后 Agent · DeepSeek]
    Agent <-->|Function Calling| MCP[MCP 工具服务]
    MCP --> Read[订单与商品查询]
    MCP --> RAG[向量 + BM25 → RRF → Rerank]
    MCP --> Business[退款与换货领域服务]
    UI -->|客户最终确认| RefundAPI[退款确认 API]
    RefundAPI -->|复核确认单后执行| Business
    API --> Workflow[换货工作流 · 补充 / 审批 / 恢复]
    Workflow --> Business
    Context --> PG[(PostgreSQL)]
    Read --> PG
    Business --> PG
    Workflow --> PG
    RAG --> Qdrant[(Qdrant)]
    API --> Redis[(Redis · 限流 / 锁 / 用量)]
```

聊天图负责多轮工具循环，换货图负责需要等待人工处理的长流程。申请提交后，业务服务保存工单，工作流根据工单状态暂停或继续。管理员审批写入数据库后，再由工作流执行模拟受理。

知识回答在聊天图已有的 `finalize` 节点中完成：DeepSeek 根据检索资料输出 `claims`（带原文引用的回答内容）和 `missing`（尚缺依据的所问事项），程序核验后组装最终回复。格式、引用或数字校验失败时最多修正一次，模型调用统一记录用量。语义对应关系仍由模型判断，需要结合样本检查回答与原文是否一致。

| 技术 | 在项目中的职责 |
|---|---|
| FastAPI、Pydantic | HTTP API、身份依赖、请求及工具结果校验 |
| LangGraph | Agent 工具循环、人工中断和工作流恢复 |
| DeepSeek、Ollama / Qwen | 客服对话与摘要、轻量意图分类 |
| MCP | 独立工具服务及客户端调用，采用 Streamable HTTP |
| LlamaIndex、百炼、Qdrant | 知识分块、向量与 BM25 召回、重排和引用 |
| SQLAlchemy、Alembic、PostgreSQL | 业务事务、数据库迁移、对话摘要与 LangGraph Checkpoint 存储 |
| Redis | 限流、会话轮次锁、并发控制与 token 预算 |
| React、TypeScript、Vite、Tailwind CSS | 客户工作台与管理员控制台 |

## 本地运行

### 1. 准备环境

- Python 3.12 或 3.13、Node.js 20。
- PostgreSQL 16、Redis 7 和可访问的 Qdrant 服务。
- DeepSeek API Key、阿里云百炼 Embedding / Rerank 权限。
- Ollama 与 `qwen2.5:3b`，用于本地意图分类。

完整的换货审批与持久化恢复演示使用 PostgreSQL 和 Redis。SQLite 与进程内 Redis 替代实现用于部分本地开发和自动化测试。

以下命令使用 PowerShell；其他系统按对应方式激活 Python 虚拟环境。

```powershell
git clone https://github.com/shuijiushikun/Aster.git
cd Aster
python -m venv backend/.venv
.\backend\.venv\Scripts\Activate.ps1
python -m pip install -r backend/requirements-dev.txt
Copy-Item .env.example backend/.env
```

已有 `backend/.env` 时保留原文件，按示例补充缺失配置。

### 2. 配置服务

编辑 `backend/.env`，填写以下字段。后端、索引脚本与 MCP 服务都从后端目录读取这份配置。

| 配置 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL` | 主对话模型；选择支持工具调用的模型 |
| `DATABASE_URL` | PostgreSQL 连接串，例如 `postgresql://aster:your-password@127.0.0.1:5432/aster` |
| `REDIS_URL` | Redis 地址，例如 `redis://127.0.0.1:6379/0` |
| `COMMERCE_MCP_SERVICE_SECRET` | API 与 MCP 共用的独立签名密钥 |
| `EMBED_API_KEY` | 百炼 Embedding 密钥；Rerank 默认复用此密钥 |
| `QDRANT_URL`、`QDRANT_API_KEY` | Qdrant 地址和认证信息 |
| `QDRANT_COLLECTION` | 本项目独立知识集合，默认 `after_sales_knowledge_bm25_v1` |

生成 MCP 共享密钥后填入配置：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
ollama pull qwen2.5:3b
```

Ollama 未启动时运行 `ollama serve`。设置 `INTENT_CLASSIFIER_ENABLED=false` 可直接使用规则分类；`LLM_PROVIDER=fake` 可使用脚本模型验证基础查询和退款链路。

### 3. 初始化数据库和知识库

在已激活虚拟环境的终端运行：

```powershell
cd backend
python -m alembic upgrade head
python -m app.db.seed --demo-samples
python -m app.rag.index_knowledge
```

演示数据命令增量添加中文虚构客户、商品、订单和工单，重复运行会保留已有业务状态。知识索引重复运行时返回 `unchanged`。Embedding 和后续真实检索评测会调用外部服务并消耗额度。

### 4. 启动三个进程

分别打开终端。两个 Python 终端都在 `backend` 目录激活虚拟环境。

```powershell
# 终端一：backend 目录，FastAPI
.\.venv\Scripts\Activate.ps1
python -m scripts.serve
```

```powershell
# 终端二：backend 目录，MCP 工具服务
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.commerce_mcp.server:create_app --factory --host 127.0.0.1 --port 8001
```

```powershell
# 终端三：frontend 目录
npm ci
npm run dev
```

| 入口 | 默认地址 |
|---|---|
| 客户工作台 | `http://localhost:5173/chat` |
| 管理员控制台 | `http://localhost:5173/console` |
| API 文档 | `http://127.0.0.1:8000/docs` |
| MCP 服务 | `http://127.0.0.1:8001/mcp` |

前端端口以 Vite 实际输出为准。Windows 下通过 `scripts.serve` 启动 API，以满足 LangGraph PostgreSQL Checkpoint 存储驱动的事件循环要求。修改 `.env` 后重启 API 与 MCP 服务。

### 会话并发与锁过期

Redis 会话锁限制正常情况下的并发请求，TTL 为 Agent 超时时间加 30 秒。释放时比较锁的随机标识，防止旧请求删除新请求的锁。

数据库复用最新用户消息的 `Message.id` 作为执行版本。新轮次写入用户消息前，先锁住会话行并检查 Redis 锁的所有权；此后每个轮次事务都先锁会话行，再核对自己的消息 ID 是否仍是最新版本，锁保持到提交或回滚。新消息落库后，旧轮次的消息、执行事件、摘要和业务工具写入会被拒绝。MCP 服务从签名上下文读取同一版本，在实际数据库事务中重新校验；退款确认入口也使用同一会话行锁。

会话行锁只覆盖短事务，等待模型期间释放；PostgreSQL 等待行锁最多 5 秒。已经持锁的旧事务先结束，新轮次再接手。旧模型请求可能继续运行一段时间，但其过期结果无法继续入库。此机制无需新增表或迁移，业务幂等和订单状态校验继续生效。

### 上下文 token 预算

`CONTEXT_MAX_TOKENS` 默认 8192，包含本次请求的输入、输出预留和安全余量。默认预留 `MAX_OUTPUT_TOKENS=1024` 和 `CONTEXT_TOKEN_MARGIN=256`，因此消息与工具定义合计最多使用 6912 个本地估算 token。该值是应用控制的预算，应小于所用主模型及备用模型的上下文窗口。

计数使用 [tiktoken](https://github.com/openai/tiktoken) 的 `cl100k_base` BPE tokenizer，复用 LlamaIndex 随包提供的词表，无需在请求中下载。统计内容包括系统提示词、历史消息、当前问题、摘要、业务事实、证据、消息角色、工具定义、工具调用参数及结果。DeepSeek 与备用模型的服务端模板和分词方式存在差异，本地计数用于控制预算，页面用量、配额和持久化用量仍以模型返回的真实 usage 为准。

单条证据按 `CONTEXT_EVIDENCE_MAX_TOKENS=800` 截断，保留完整 Unicode 字符；摘要输入沿用同一请求预算，输出按 `SUMMARY_MAX_TOKENS=1024` 校验。裁剪顺序为旧消息、旧摘要、末尾证据、可选业务描述，保留当前问题、系统规则、身份/订单/工单状态和工具调用配对。

已有配置中的 `CONTEXT_MAX_CHARS`、`CONTEXT_EVIDENCE_MAX_CHARS`、`SUMMARY_MAX_CHARS` 已由对应 token 配置替代，字符数与 token 数没有固定换算比例，更新 `.env` 时请按示例重新设置。HTTP 消息长度和业务字段的字符校验继续独立生效。

## 演示场景

开发环境使用 `AUTH_MODE=dev`，可以在页面切换预置客户，并通过管理入口查看工单。部署到外部环境时使用 `AUTH_MODE=jwt` 和正式签名配置。

| 场景 | 操作示例 |
|---|---|
| 订单查询 | 选择林晓，询问“看看我的订单”或“查一下 1005 的订单” |
| 商品咨询 | “1005 的耳机只有一边有声音，怎么排查？” |
| 模拟退款 | “帮我退掉 ORD-DEMO-1001”，在弹框核对订单和金额，点击确认后查看退款记录 |
| 换货申请 | “ORD-DEMO-1005 左耳没声音，想换同型号”，按提示补充描述和诉求 |
| 人工审批 | 在管理控制台查看待审工单，批准或拒绝后查看办理结果 |
| 恢复与历史 | 刷新页面或重新选择客户后打开历史对话；处理异常工单时使用继续或重试入口 |

退款默认要求订单已送达、在 30 天内、商品可退且尚未退款；金额超过 500 USD 转人工审核。换货按首次申请时间检查期限，材料齐全后等待管理员审批。待受理期间仍可按退款规则办理退款；退款成功会关闭活跃换货工单，换货受理成功后禁止退款。金额统一使用 USD。

管理员的批准意见、换货受理记录和退款账本都保存在数据库中，回复中的办理结果以这些业务事实为依据。

退款确认单有效期为 10 分钟，刷新页面可以恢复。对话中的“好的”只是一条普通消息；执行入口要求客户登录态和确认单 ID，不接受客户端传入订单或金额。订单、型号、金额等信息变化，或客户发送新消息后，原确认单失效。超过自动批准额度时，点击确认仅提交人工审核。已有数据库需运行 `python -m alembic upgrade head`，创建 `refund_confirmations` 表后重启后端。

## 知识库

知识源位于 [`backend/data/knowledge/`](backend/data/knowledge/)，包含通用流程、商品排障、尺寸测量和售后规则。每份 Markdown 文档通过 JSON front matter 声明型号、品类、版本、有效状态与参考来源，索引时按小节分块。

更新文档后重新运行 `python -m app.rag.index_knowledge`，索引脚本同步知识目录并发布当前文档版本。查询先过滤型号、品类、有效状态和版本，再进行 Dense / BM25 召回与排序；最终返回片段、文档标题、版本和来源。

- `RAG_SEARCH_MODE=dense`：仅向量召回。
- `RAG_SEARCH_MODE=hybrid`：Dense 与 BM25 召回，通过 RRF 融合排名。
- `RAG_RERANK_ENABLED=true`：在召回候选上调用百炼 `qwen3-rerank`。

BM25 使用中文二元片段与完整英文、型号词，文档权重包含词频饱和与长度归一化，IDF 由 Qdrant 计算。Dense 与 BM25 分别校准原始分数阈值，任一路满足阈值即可进入混合排序；重排失败时保留融合结果。语料、Embedding 或切词方式变化后重新校准：

```powershell
python -m app.rag.evaluate --live --calibrate --out var/retrieval-expanded-calibration.json
```

校准命令仅输出建议值。先用建议阈值运行对照，检查有答案误拒和无答案放行，再决定是否更新 `.env` 中的 `RAG_MIN_DENSE_SCORE` 与 `RAG_MIN_BM25_SCORE`：

```powershell
python -m app.rag.evaluate --live --thresholds-from var/retrieval-expanded-calibration.json --out var/retrieval-calibrated-evaluation.json
```

本次 50 题校准给出的候选阈值减少了无答案放行，同时增加了有答案误拒，当前配置保留原阈值。Rerank 目前用于排序，尚未设置独立的拒答阈值；所问事实是否有依据由后续证据回答环节判断。初始值 `0` 表示尚未校准，BM25 阈值为正数时才允许独立补召回。旧 log-TF 集合升级时使用新的 BM25 集合重新建立索引。

文档中的商品尺寸和店铺规则用于本项目模拟业务。排障与测量内容注明所参考的官方资料，实际退款与换货资格由业务规则代码校验。

## 测试与验证

后端回归默认使用隔离 SQLite 与内存 Redis，不依赖真实模型。以下命令在 `backend` 目录执行：

```powershell
python -m ruff check .
$env:TEST_DATABASE_URL=""
$env:TEST_REDIS_URL=""
python -m pytest -q
```

只验证会话锁过期、旧轮次写入拦截、MCP 事务校验和退款确认时，可运行 `python -m pytest tests/test_turn_fencing.py tests/test_refund_confirmation.py tests/test_mcp_actions.py -q`。

真实 PostgreSQL / Redis 集成测试通过 `TEST_DATABASE_URL`、`TEST_REDIS_URL` 显式指定专用测试实例。测试会重建测试表并清空测试 Redis，应使用可丢弃的测试数据库。

```powershell
# 离线检查数据格式、集合隔离和知识依据，不调用外部服务
python -m evals.datasets

# 基础退款对话回归：临时数据库 + 脚本模型
python -m evals.run --mode scripted
python -m evals.report

# 真实检索方式对照：调用已配置的百炼和 Qdrant
python -m app.rag.evaluate --live --out var/retrieval-expanded-evaluation.json

# 本地意图分类冒烟：仅分类，不访问业务数据库
python -m scripts.intent_smoke --live

# 本地意图分类留出测试集，单独保存成绩
python -m scripts.intent_smoke --live --split evaluation --out var/intent-evaluation.json

# 新增措辞的分类回归，单独保存成绩
python -m scripts.intent_smoke --live --split evaluation --dataset data/intent_holdout_cases.json --out var/intent-additional.json

# 检索后的最终知识回答：核对原文引用、参数依据和资料不足说明
python -m scripts.answer_eval --live --out var/answer-evaluation.json

# 真实业务场景回放：自动新建临时 PostgreSQL 库，完成后删除这个临时库
python -m scripts.business_eval --live --out var/business-evaluation.json
```

对话回归核验工具顺序、规则结果、退款账本与成功声明；检索比较记录 Hit@1/4、证据不足处理、引用有效性、跨型号误检和服务降级。自建数据包含 272 条意图样本、另行冻结的 48 条分类题、200 条检索样本，以及 40 个业务场景。开发、校准和测试集合按分组隔离；业务回放器支持 36 条现有场景，办理退款的样本显式增加客户确认步骤。另 4 条确认目标场景的机制已由独立专项测试覆盖，尚未接入统一的真实模型回放器。字段与运行边界见[评测说明](backend/evals/评测说明.md)，原始测试记录见[评测结果](backend/evals/评测结果-20261027.md)，后续修改与复测见[修复记录](backend/evals/修复复测-20261027.md)。

### 已记录的评测结果

以下为 2026-10-27 的真实服务评测。使用本地 Qwen2.5:3B、配置的 DeepSeek、百炼和 Qdrant，样本均为自建合成数据。

| 项目 | 结果 | 检查范围 |
|---|---|---|
| 意图分类，原测试集 168 题 | 139/168 → 157/168（82.7% → 93.5%） | 最终分类包含程序保护；模型原始预测为 152/168（90.5%） |
| 意图分类，新增措辞 48 题 | 44/48（91.7%） | 单独冻结的新措辞，不与开发集混计 |
| 检索，64 道有答案题 | Hybrid 的 Hit@4 为 95.3%，加入 Rerank 后为 96.9% | 前四条中包含预期文档及原文依据；无答案放行仍为 30/62 |
| 最终知识回答，126 题 | 自动检查通过 124/126；无答案题 62/62 说明资料不足 | 检查回答或拒答及预期依据范围，逐句语义仍需人工审核 |
| 业务回放，36 个现有场景 | 36/36 | 工具、接口与测试数据库状态断言，包含重试、审批恢复与退款/换货互斥 |

原 168 道分类题已用于定位和修复问题，新增 48 题单独记录。知识回答复用检索测试题；不同检索方式和阈值重复运行同一批题时，题目数量分别统计。样本范围、调用条件和失败原因见上述评测记录。

业务回放使用随机创建的 PostgreSQL 测试库与真实 MCP 实现，传输采用进程内 ASGI，Redis 使用 fakeredis。表中 36/36 是增加最终确认前的历史记录；新版本尚未全量重跑业务场景。人工处理耗时、转接率和线上办理量尚未采集。

2026-10-28 提交 `cc35ecd` 前复验：后端 `pytest -q` 为 395 项通过、51 项条件跳过，Ruff 与数据集离线校验通过。测试使用临时 SQLite 和 fakeredis；需要显式开启的真实业务回放与 PostgreSQL 专项未在这次复验中重跑，上表仍引用 2026-10-27 的真实服务记录。

随后将上下文预算改为 token 计数，仅运行相关回归：token 预算、上下文与摘要、聊天图、MCP 和知识回答共 93 项通过，主备切换与熔断恢复 3 项通过。新增测试覆盖中英文 BPE 计数、Unicode 截断、工具定义开销、回复空间预留及摘要 token 上限；本次未重跑全量测试或真实模型评测。

增加退款最终确认后，仅运行相关回归：退款、工具、聊天图与评测器相关测试 133 项通过、1 项条件跳过；最后补充的确认边界与幂等复验 21 项通过；真实 PostgreSQL 下确认专项 18 项通过，并验证迁移升级、回退和再次升级；前端 9 项相关测试及构建通过。真实模型的普通退款 B-008 通过，大额退款 B-016 首次多问一轮准备意愿，调整提示词后复测通过。浏览器验证了取消、刷新恢复、确认后更新订单与历史记录。旧退款基准会单独记录模拟客户点击的步骤，确认安全专项直接验证生产入口。确认时还会复核处理方式；自动退款与人工审核发生切换时要求重新确认，资格变化时返回结构化拒绝。

本次发布前专项复验：后端相关测试 189 项通过、1 项条件跳过；独立临时 PostgreSQL 下 64 项通过；64 条脚本退款对话回归及门禁通过。退款确认表的迁移升级、回退、再次升级和模型一致性检查通过；前端 12 项测试及生产构建通过。上述为不同环境下的相关验证，未重新执行本地全量测试。

前端在 `frontend` 目录运行 `npm test` 与 `npm run build`。GitHub Actions 执行后端检查、PostgreSQL / Redis 集成测试、数据库迁移、脚本对话回归和前端构建。

## 目录

```text
backend/
  app/
    agent/          聊天图、Prompt、上下文、摘要与事件处理
    after_sales/    意图分类、退款和换货服务、幂等请求
    commerce_mcp/   MCP 服务与业务工具实现
    mcp/            MCP 客户端、签名身份、工具适配和 Schema
    rag/            分块、索引、BM25、向量检索、重排与评测
    workflow/       换货审批图、Checkpoint 与恢复
    policy/         退款和换货规则
    db/             SQLAlchemy 模型、演示数据与数据库会话
    maintenance/    会话归档与数据保留任务
  alembic/          数据库增量迁移
  data/             知识文档、意图与检索样本、业务场景
  evals/            数据校验、指标统计、退款回归与评测记录
  scripts/          本地启动、意图分类、证据回答与业务回放
  tests/            后端自动化测试
frontend/
  src/              客户工作台、管理控制台与 SSE 客户端
  tests/            界面状态与 API 行为测试
.github/workflows/  持续集成
.env.example        本地配置示例
```
