# Aster

**面向电商售后的智能客服 Agent。**

Aster 通过自然语言处理订单查询、商品咨询、退款申请与换货办理。DeepSeek 负责对话和工具选择，LangGraph 编排工具调用与人工审批，MCP 连接订单、知识库和业务服务。客户可以查看历史对话、资料来源与办理进度，管理员可以审核工单、补充处理意见并恢复中断的流程。

项目包含 FastAPI 后端和 React 工作台。退款记入模拟账本，换货以生成受理记录为完成标志，适合在本地演示完整的售后处理过程。

[功能](#功能) · [架构](#架构) · [本地运行](#本地运行) · [演示场景](#演示场景) · [知识库](#知识库) · [测试与验证](#测试与验证) · [目录](#目录)

## 功能

- **统一售后 Agent**：使用同一个 Agent 完成咨询与办理。Qwen2.5:3B 结合当前消息和近期对话识别意图，按查询、退款、换货和澄清结果开放工具；分类服务故障时由规则兜底。
- **MCP 工具调用**：将订单、商品、知识、退款和换货申请封装为 MCP Tools。调用前验证参数，服务端重新核验签名身份、订单归属和业务条件，返回结构化结果。
- **混合知识检索**：通过百炼 Embedding 与 BM25 双路召回，使用 RRF 融合排名，再由百炼 Rerank 重排。按型号、品类和文档版本过滤证据，并提供来源引用。
- **上下文与记忆**：持久化对话历史和增量摘要，按当前订单、工单关联历史会话，结合字符预算裁剪上下文，保留近期对话及当前业务事实。
- **人工审批与恢复**：换货流程通过 LangGraph interrupt/resume 等待材料或管理员审批，以 PostgreSQL Checkpoint 保存执行位置，支持重启后继续办理。
- **业务一致性**：使用事务、行锁、唯一约束和持久化请求记录处理退款与换货互斥、重复提交和重试；根据实际工具结果校验最终回复。
- **客户与管理工作台**：提供黑白界面、历史会话、订单列表、工单详情、常驻处理进度、资料来源及 token 用量，通过 SSE 更新执行过程。

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

| 技术 | 在项目中的职责 |
|---|---|
| FastAPI、Pydantic | HTTP API、身份依赖、请求及工具结果校验 |
| LangGraph | Agent 工具循环、人工中断和工作流恢复 |
| DeepSeek、Ollama / Qwen | 客服对话与摘要、轻量意图分类 |
| MCP | 独立工具服务及客户端调用，采用 Streamable HTTP |
| LlamaIndex、百炼、Qdrant | 知识分块、向量与 BM25 召回、重排和引用 |
| SQLAlchemy、Alembic、PostgreSQL | 业务事务、数据库迁移、对话摘要与 Checkpoint |
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

前端端口以 Vite 实际输出为准。Windows 下通过 `scripts.serve` 启动 API，以满足 PostgreSQL Checkpoint 驱动的事件循环要求。修改 `.env` 后重启 API 与 MCP 服务。

## 演示场景

开发环境使用 `AUTH_MODE=dev`，可以在页面切换预置客户，并通过管理入口查看工单。部署到外部环境时使用 `AUTH_MODE=jwt` 和正式签名配置。

| 场景 | 操作示例 |
|---|---|
| 订单查询 | 选择林晓，询问“看看我的订单”或“查一下 1005 的订单” |
| 商品咨询 | “1005 的耳机只有一边有声音，怎么排查？” |
| 模拟退款 | “帮我退掉 ORD-DEMO-1001”，查看订单状态和退款记录 |
| 换货申请 | “ORD-DEMO-1005 左耳没声音，想换同型号”，按提示补充描述和诉求 |
| 人工审批 | 在管理控制台查看待审工单，批准或拒绝后查看办理结果 |
| 恢复与历史 | 刷新页面或重新选择客户后打开历史对话；处理异常工单时使用继续或重试入口 |

退款默认要求订单已送达、在 30 天内、商品可退且尚未退款；金额超过 500 USD 转人工审核。换货按首次申请时间检查期限，材料齐全后等待管理员审批。待受理期间仍可按退款规则办理退款；退款成功会关闭活跃换货工单，换货受理成功后禁止退款。金额统一使用 USD。

管理员的批准意见、换货受理记录和退款账本都保存在数据库中，回复中的办理结果以这些业务事实为依据。

## 知识库

知识源位于 [`backend/data/knowledge/`](backend/data/knowledge/)，包含通用流程、商品排障、尺寸测量和售后规则。每份 Markdown 文档通过 JSON front matter 声明型号、品类、版本、有效状态与参考来源，索引时按小节分块。

更新文档后重新运行 `python -m app.rag.index_knowledge`，索引脚本同步知识目录并发布当前文档版本。查询先过滤型号、品类、有效状态和版本，再进行 Dense / BM25 召回与排序；最终返回片段、文档标题、版本和来源。

- `RAG_SEARCH_MODE=dense`：仅向量召回。
- `RAG_SEARCH_MODE=hybrid`：Dense 与 BM25 召回，通过 RRF 融合排名。
- `RAG_RERANK_ENABLED=true`：在召回候选上调用百炼 `qwen3-rerank`。

BM25 使用中文二元片段与完整英文、型号词，文档权重包含词频饱和与长度归一化，IDF 由 Qdrant 计算。Dense 与 BM25 分别校准原始分数阈值，任一路满足阈值即可进入混合排序；重排失败时保留融合结果。语料、Embedding 或切词方式变化后重新校准：

```powershell
python -m app.rag.evaluate --live --calibrate --out var/retrieval-bm25-calibration.json
```

将报告的 `suggested_min_dense_score` 和 `suggested_min_bm25_score` 分别填入 `.env` 的 `RAG_MIN_DENSE_SCORE` 和 `RAG_MIN_BM25_SCORE`。初始值 `0` 表示尚未校准；BM25 阈值为正数时才允许独立补召回。旧 log-TF 集合升级时使用新的 BM25 集合重新建立索引。

文档中的商品尺寸和店铺规则用于本项目模拟业务。排障与测量内容注明所参考的官方资料，实际退款与换货资格由业务规则代码校验。

## 测试与验证

后端回归默认使用隔离 SQLite 与内存 Redis，不依赖真实模型。以下命令在 `backend` 目录执行：

```powershell
python -m ruff check .
$env:TEST_DATABASE_URL=""
$env:TEST_REDIS_URL=""
python -m pytest -q
```

真实 PostgreSQL / Redis 集成测试通过 `TEST_DATABASE_URL`、`TEST_REDIS_URL` 显式指定专用测试实例。测试会重建测试表并清空测试 Redis，应使用可丢弃的测试数据库。

```powershell
# 基础退款对话回归：临时数据库 + 脚本模型
python -m evals.run --mode scripted
python -m evals.report

# 真实检索比较：独立评测样本，调用已配置的百炼和 Qdrant
python -m app.rag.evaluate --live --out var/retrieval-bm25-evaluation.json

# 本地意图分类冒烟：仅分类，不访问业务数据库
python -m scripts.intent_smoke --live
```

对话回归核验工具顺序、规则结果、退款账本与成功声明；检索比较记录 Hit@1/4、无证据拒答、引用有效性、跨型号误检和服务降级。检索校准集与评测集分开使用。检索样本规模较小，结果仅用于观察本项目配置下的行为。

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
  data/             知识文档与检索样本
  evals/            基础退款对话回归
  scripts/          本地启动与服务冒烟脚本
  tests/            后端自动化测试
frontend/
  src/              客户工作台、管理控制台与 SSE 客户端
  tests/            界面状态与 API 行为测试
.github/workflows/  持续集成
.env.example        本地配置示例
```
