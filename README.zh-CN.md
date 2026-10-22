# 智能售后工作台

基于 FastAPI、LangGraph 和 React 的售后 Agent 项目。客户可以查询本人订单、检索商品说明、申请模拟退款或换货，并在工单中补充文本、取消审批前的申请；管理员审核工单、查看执行记录或继续中断流程。

核心原则：**大模型提出请求，代码决定能否退款。** 身份来自签名 JWT，退款工具会在写入数据库前重新检查规则，并通过行锁和唯一索引防止重复成功退款。

## 启动

已运行过项目时，继续使用原来的启动方式和 `.env`，无需重建或清空数据库。

当前采用本地分别运行后端与前端的方式，容器化和部署工作暂缓。首次配置可参考根目录 `.env.example`，保留已有 `.env` 和 Python 环境。

```powershell
# 后端目录；使用你原有的 Python 环境
python -m pip install -r requirements-dev.txt
python -m scripts.serve

# 另一个终端，在 frontend 目录运行
npm install
npm run dev
```

前端开发地址以终端输出为准，API 代理配置在 `frontend/vite.config.ts`。`backend/.env` 保存本机配置；已有数据时保持 `SEED_ON_STARTUP=false`。新空库可显式运行下文演示数据命令，或启用仅对空库生效的 `SEED_ON_STARTUP=true`。

新建本地演示数据库时，先在后端目录运行 `python -m alembic upgrade head`，再启动服务。数据库地址由 `DATABASE_URL` 决定；迁移与应用必须使用同一份配置。未设置时使用 `backend/var/app.db`，不会自动连接 PostgreSQL。

- 客服对话与管理控制台分别位于前端地址的 `/chat`、`/console`。
- 本地开发模式 `AUTH_MODE=dev`：在页面上方“开发”区域选择客户；开启“管理”后可查看控制台。
- 无模型密钥时设置 `LLM_PROVIDER=fake`，脚本模型支持基本中文和英文的订单查询、退款请求。
- 未配置 PostgreSQL/Redis 时，单进程本地开发可使用 SQLite 和内存 Redis 替代；换货持久化工作流需要 PostgreSQL 和 Redis。

## 中文化范围

- 客服界面：导航、欢迎页、聊天、订单、场景卡片、主题切换、加载/空状态、错误提示及无障碍标签。
- 管理界面：统计、会话搜索、对话记录、执行过程和决策标签。
- 回复：真实模型默认简体中文；脚本模型、规则原因、异常兜底、额度通知及成功声明纠正均提供中文。
- 演示商品：后端使用精确名称映射，旧数据库的英文演示商品无需迁移即可显示中文；自定义商品名称原样保留。
- 核心注释：重点说明 Agent 循环、身份隔离、规则判断、数据库防重、SSE 和模型降级。
- 日期：中文格式，北京时间（`Asia/Shanghai`）；无时区的后端时间按 UTC 解释。后端存储及退款期限计算保持原有逻辑。

历史会话和历史审计记录保留原文，不进行批量翻译。独立展示网站、压测工具、红队测试与提示词注入检测已移除；身份归属、退款规则、数据库防重复、用量和运行边界继续保留。

## 退款规则

| 场景 | 处理方式 |
|---|---|
| 当前客户的已送达普通商品，在 30 天内且金额不超过 500 美元 | 可以自动退款 |
| 满足其他条件，但金额超过 500 美元 | 转人工审核 |
| 清仓不退商品、超期、已退款、未送达 | 拒绝退款 |
| 他人订单或不存在的订单 | 返回相同的未找到提示，避免泄露订单是否存在 |

金额始终为 **美元（USD）**，没有改成人民币；恰好 500 美元不因金额转人工，第 30 天仍在期限内，第 31 天超期。实际参数仍由原配置控制。

API 路径、工具名、数据库字段、状态值和环境变量保留英文，例如 `issue_refund`、`approved`、`LLM_PROVIDER`。它们是程序之间的契约，界面通过中文标签展示含义。

## 代码阅读顺序

1. `backend/app/agent/prompts.py`：客服职责、中文回复要求与权限边界。
2. `backend/app/agent/graph.py`：模型与工具交替执行，主模型失败时使用备用模型。
3. `backend/app/agent/runner.py`：会话历史、事件持久化、用量限制和中英文成功声明校验。
4. `backend/app/agent/tools.py`：身份与订单归属校验、规则复核、退款落库和防重。
5. `backend/app/policy/engine.py`：确定性退款规则。
6. `backend/app/agent/context.py`、`memory.py`：上下文预算、相关工单与增量摘要。
7. `frontend/src/api.ts`：请求、中文错误处理与 SSE 解析。
8. `frontend/src/lib/locale.ts`：中文工具名称、状态与日期展示。

回复中的成功声明会与本轮退款工具的真实结果核对；是否允许退款始终由工具内的确定性规则校验决定。本项目记录模拟退款账本，没有接入真实支付。

## 换货工单后端

换货领域服务已支持建单、补充材料、人工审批、审批前取消和幂等模拟受理。申请期限按每张工单首次提交时间判断，等待审批不会导致过期；被拒绝或取消后可以在期限内重新申请，同一订单最多成功受理一次。

待受理期间仍按原规则允许退款，退款成功会在同一事务内终止活跃换货工单；换货受理成功后禁止退款。审批和受理需要持久化业务事实，模型不能直接批准换货。

先在后端目录升级数据库，再按需增量添加本项目的售后演示样本：

```powershell
python -m alembic upgrade head
python -m app.db.seed --demo-samples
```

样本包含 2 款商品、3 名中文虚构客户、13 个订单和 5 张示例工单，全部金额为 USD。商品对应知识库的 `HP-01` 耳机和 `TSHIRT-01` 服装，订单均关联商品目录。重复执行只添加缺失样本，不更新已有订单日期、退款或审批，也不重新创建已取消/拒绝的工单；初始化中断时可使用相同命令继续本次 seed 尚未完成的审批/受理步骤。

| 客户 | 可演示内容 |
|---|---|
| 林晓 `CUST-DEMO-001` | `ORD-DEMO-1001` 普通退款、`ORD-DEMO-1002` 尺码换货、`ORD-DEMO-1003` 高金额转人工、`ORD-DEMO-1004` 未送达、`ORD-DEMO-1005` 耳机故障换货 |
| 陈悦 `CUST-DEMO-002` | `ORD-DEMO-2001` 超期、`ORD-DEMO-2002` 清仓、`ORD-DEMO-2003` 已退款；也可验证跨客户访问被拒绝 |
| 周宁 `CUST-DEMO-003` | `ORD-DEMO-3001` 待补充、`ORD-DEMO-3002` 待审、`ORD-DEMO-3003` 已取消、`ORD-DEMO-3004` 已拒绝、`ORD-DEMO-3005` 已模拟受理 |

示例取消、审批和受理均经领域服务生成业务记录，示例工单关联标记为 `demo-seed:` 的会话；没有接入真实付款或发货。原退款样本和早期换货样本已移至 `backend/tests/fixtures/`，仅供回归测试，不再自动灌入运行时数据库。已有本地旧数据保留；需要从干净状态重复录制时，可为每轮指定新的演示数据库，迁移后重新添加样本。

已提供的工单查询接口均使用现有 Bearer JWT：

| 接口 | 返回内容 |
|---|---|
| `GET /api/me/tickets` | 当前客户的工单列表 |
| `GET /api/me/tickets/{ticket_id}` | 当前客户的工单详情与事件 |
| `GET /api/admin/tickets` | 管理员授权范围内的工单列表，可按 `status` 筛选 |
| `GET /api/admin/tickets/{ticket_id}` | 工单、订单、审批及事件详情 |

列表支持 `limit`（1～100，默认 50）与 `offset`（默认 0）。他人工单与不存在的工单统一返回 404，受限管理员仍按 JWT 的客户范围过滤。

### 审批与持久化恢复

T5 已接入独立换货工作流：聊天建单提交后，启动校验并暂停在补充材料或人工审批节点。每张工单固定使用 `exchange:<ticket_id>` 线程，`AsyncPostgresSaver` 保存执行位置；业务工单、审批、请求幂等记录始终是权威事实。关闭重开后端后，可通过管理员恢复入口继续同一工单，受理前再次检查退款和业务规则，不重复生成模拟受理单。

| POST 接口 | 请求体与用途 |
|---|---|
| `/api/me/tickets/{ticket_id}/supplement` | `request_id`、`description`，可选 `reason_type`、`requested_change`；仅待补充时提交 |
| `/api/me/tickets/{ticket_id}/cancel` | `request_id`；仅待补充或待审批时首次取消 |
| `/api/admin/tickets/{ticket_id}/decision` | `request_id`、`decision`（`approved`/`rejected`），可选 `comment` |
| `/api/admin/tickets/{ticket_id}/resume` | `request_id`；重试启动、唤醒或执行，不能代替材料或审批 |

这些接口先提交事实，再在请求内有限时地尝试推进，返回 `202` 和接收时快照。即使推进已完成，响应里的 `APPROVED` 也不等于最新状态；以 GET 详情为准。同一身份、接口、工单与 `request_id` 的相同内容重放返回原快照，并可再次尝试推进；改内容须用新 ID，否则返回 409。推进超时、Redis 忙或进程退出后，可重放原请求或在 `/console` 的“工单审批”页点击“重试继续处理”。没有后台扫描或自动重试队列；`FAILED_TERMINAL` 需人工处置，恢复接口返回 409。

业务表仍由 Alembic 迁移。后端启动时，由 SDK `setup()` 在同一 PostgreSQL 数据库内维护 checkpoint 表（`checkpoint_migrations`、`checkpoints`、`checkpoint_blobs`、`checkpoint_writes`），数据库账号需有建表/索引权限。进程复用 Saver 与连接池，通过 Redis 工单锁串行推进；`WORKFLOW_TIMEOUT_S` 默认 30 秒，释放锁另有最多 5 秒清理时间，`WORKFLOW_POOL_SIZE` 默认 4。Windows 的 psycopg 异步连接要求 SelectorEventLoop，前文的 `python -m scripts.serve` 已处理；其他平台也可使用此入口。

同一前端项目提供 `/chat` 客户工作台与 `/console` 管理工作台。客户工单入口展示最新状态、补充文本和审批前取消；管理端提供待审列表、批准/拒绝、继续处理与会话记录。操作结果以刷新后的详情为准；网络结果不确定时保留原请求标识，重试不会生成第二次业务受理。界面只提供已实现的操作，没有电话、物流、附件上传等入口。

## 只读商城 MCP

Commerce MCP Server 由本项目实现，复用官方 Python SDK（固定为本阶段验证的 `mcp==1.26.0`），通过 Streamable HTTP 暴露现有演示数据库的四个查询工具：

| 工具 | 模型可提供的参数 | 结果 |
|---|---|---|
| `list_my_orders` | `limit`，1～20，默认 10 | 本人最近订单 |
| `get_order` | `order_id` | 本人订单及可信商品型号，旧订单型号可为空 |
| `get_product` | `model_code` | 公开商品信息 |
| `get_exchange_policy_facts` | `order_id` | 本人订单的妥投、清仓、退款和换货受理事实 |

在后端 `.env` 增加 `COMMERCE_MCP_URL=http://127.0.0.1:8001/mcp` 和独立的 `COMMERCE_MCP_SERVICE_SECRET`（至少 32 字节）。可用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成一次，后端与商城进程读取同一份本地配置。此密钥仅用于服务之间，不填写模型 Key 或前端 JWT。

在后端目录、相同 Python 环境中另开终端启动：

本机本轮使用基于 `yolo` Python 3.13.9 的项目环境 `backend/.venv-yolo`，可在后端目录先运行 `.\.venv-yolo\Scripts\Activate.ps1`。使用其他环境时先安装更新后的 `requirements.txt`，避免加载旧版 MCP SDK。

```powershell
python -m uvicorn app.commerce_mcp.server:create_app --factory --host 127.0.0.1 --port 8001
```

使用上述演示数据执行一次只读检查，不调用模型或写业务数据：

```powershell
python -m scripts.commerce_mcp_smoke
```

调用链为 `LangChain 工具适配器 → MCP Client → Commerce MCP Server → 数据库`。适配器从后端已验证的 `ToolContext` 取得客户身份，签发有效期 60 秒、限定 audience 的内部令牌；商城服务重新验证并在 SQL 中过滤订单归属。模型参数没有 `customer_id`，他人记录与不存在记录统一返回 `NOT_FOUND`。成功数据与错误均经 Pydantic 合同校验，PostgreSQL 查询使用只读事务。

超时和离线分别返回 `MCP_TIMEOUT`、`MCP_UNAVAILABLE`，默认每次最多重试一次，本轮失败后停止后续商城调用；没有本地写入兜底。Order Agent 使用这四个工具；退款子图继续使用原退款工具。服务仅在本机监听，不提供退款、审批、换货执行或其他写工具。

## 售后知识检索

知识源位于 `backend/data/knowledge/`：8 份原创演示文档覆盖通用流程、HP-01 耳机和 TSHIRT-01 服装。文档以 JSON front matter 声明型号、品类、版本、有效状态和参考来源，正文按小节切分。排障与测量写法参考 [Apple 官方说明](https://support.apple.com/en-ie/100494) 和 [UNIQLO 官方测量指南](https://faq-sg.uniqlo.com/articles/en_US/Knowledge/Product-Measurement-Guidelines-SG)；模拟商品尺寸由本项目设定，店铺规则以业务源码为准，没有移植外部品牌的售后承诺。

从根目录 `.env.example` 复制知识检索字段到 `backend/.env`，配置百炼 `EMBED_API_KEY`、`QDRANT_URL` 和 `QDRANT_API_KEY`。Embedding 默认使用 `text-embedding-v3`、1024 维；Qdrant 使用专属集合 `after_sales_knowledge_v1`，不要填写其他项目的集合名。数据库先按前文迁移至最新版本；索引脚本仅同步 `knowledge_documents` 目录，不重建订单/工单表。后端目录运行：

```powershell
python -m app.rag.index_knowledge
```

重复运行同一语料返回 `unchanged`，23 个片段保持稳定，另有 1 个集合 manifest。manifest 记录 Embedding 模型、维度、端点、索引版本和语料哈希；不兼容时要求换新集合。文档停用/删除或更新版本后重新运行脚本，只发布当前有效版本并回收本项目的旧片段；不自动删除整个集合。首次创建/更新时会调用真实 Embedding API。

LlamaIndex 负责切分、向量库适配和 Retriever，明确注入百炼 Embedding；没有默认 OpenAI 调用或 QueryEngine 生成答案。`KnowledgeRetriever.retrieve(query, model_code=..., category=...)` 返回最多 4 条带文档 ID、版本、小节编号、摘录、分数类型及参考来源的证据。型号/品类必须由调用方从已验证订单或商品目录取得；未知或不匹配的型号要求核实，缺少型号时仅检索通用文档。检索按型号/品类、active 状态、当前文档版本和语料代次过滤；证据不负责授予退款或换货权限。

支持三个可比较配置：`RAG_SEARCH_MODE=dense`；`hybrid`；以及 `hybrid` 配合 `RAG_RERANK_ENABLED=true`。Hybrid 用中文二元片段和完整英文/型号词的 log-TF 稀疏向量，与语义向量按 RRF 排名融合；这是轻量词法召回，不是 BM25 或额外的稀疏大模型。可选 `qwen3-rerank` 重排候选，增加一次百炼调用，失败时结果携带 `rerank_unavailable` 并保留原召回顺序。初始化时注入现有 Qdrant store/client，关闭时一并释放，示例：

```python
from app.config import get_settings
from app.rag.retriever import KnowledgeRetriever
from app.rag.store import open_store

async def search_example():
    settings = get_settings()
    async with open_store(settings) as (store, client):
        retriever = KnowledgeRetriever(store, client, settings)
        return await retriever.retrieve("右耳没有声音怎么排查", model_code="HP-01", category="digital")
```

真实小型验证包含独立的 8 条阈值校准样本和 16 条评测样本，不使用 fake Qdrant 或 mocked Embedding 替代效果验证：

```powershell
python -m app.rag.evaluate --live --calibrate --out var/retrieval-calibration.json
# 查看 suggested_min_dense_score，再填写本地 RAG_MIN_DENSE_SCORE。
python -m app.rag.evaluate --live --out var/retrieval-evaluation.json
```

评测共享各模式的真实查询向量，记录 Hit@1/4、引用有效率、跨型号误检、无证据拒答、错误、重排降级及检索耗时（不含共享 Embedding 批次）。拒答阈值只用于当前模型的 dense 余弦分数，不比较 RRF/Rerank 与余弦分数；换模型或语料需重新校准。默认阈值为 0，表示尚未做分数校准，不能宣称能识别所有无关问题。知识不足返回 `insufficient_evidence`，依赖故障返回 `unavailable`。这仍是少量人工标注的演示数据，不能证明通用检索效果，也不能保证候选里的每一段都回答了问题。聊天 Graph 的 Knowledge Agent 已绑定此检索器，并通过 SSE 返回知识来源。

2026-10-22 使用真实百炼与云端 Qdrant 的结果：

| 模式 | 正例 Hit@1 | 正例 Hit@4 | 无证据正确拒答 | 平均检索耗时 |
|---|---:|---:|---:|---:|
| Dense | 10/12 | 11/12 | 4/4 | 1265 ms |
| Hybrid | 9/12 | 11/12 | 4/4 | 1871 ms |
| Hybrid + Rerank | 11/12 | 11/12 | 4/4 | 1941 ms |

三种模式引用有效率均为 100%，跨型号误检和依赖错误均为 0。独立校准得到本地阈值 `0.734367`，评测中的 E05（平铺胸宽与人体胸围区别）被误判为无证据；未用评测集重新调阈值。当前默认打开 Hybrid + Rerank，以这组样本的首条命中改善为依据；纯 Hybrid 未显示收益。结果只适用于此小型语料，耗时不含共享查询向量化且不代表稳定性能基准；原始报告由上述命令生成于忽略的 `backend/var/`。

## 聊天编排

在后端 `.env` 设置 `AGENT_V2_ENABLED=true`，并分别启动 FastAPI 和上面的 Commerce MCP 进程。配置好 DeepSeek、知识索引及百炼/Qdrant 后，聊天会先用结构化 Router 区分退款、换货申请、知识咨询、订单查询和工单跟进。设为 `false` 可切回原退款聊天；退款/换货互斥和数据库约束始终有效。公开演示额度耗尽时仍使用原零费用脚本模型。

Order Agent 只能调用只读 MCP 工具，每轮最多 3 次；Knowledge Agent 只能检索 LlamaIndex，商品范围从已验证订单或商品目录确定。普通查询和工单跟进不会创建工单或自动恢复工作流。明确申请换货时，需要唯一的本人订单与足够知识证据，模型提案经过后端核验，再调用领域服务创建或返回已有工单，并尝试启动审批工作流；建单不代表换货已批准或受理。

SSE 保留原事件，增加 `knowledge_sources`、`ticket_update` 和 `workflow_status`。客户正式消息只取图的 `final_response`，中间 JSON 和专业代理文本仅为执行步骤；所有模型调用统一记录用量，缺失用量标为 `unknown`。入口：`app/agent/chat_graph.py`；事件适配：`streaming.py`；用量入口：`runtime.py`。

### 上下文与增量摘要

每轮使用最近 8 条消息（包含当前输入）、当前客户的相关工单事实，以及已覆盖历史的摘要。跨会话跟进会重新查询本人相关工单；读取进度不会自动恢复工作流，也不以摘要中的订单号或审批结论代替数据库校验。

会话达到 12 条消息，且最近 8 条之前还有至少 8 条未摘要消息时，回复保存后尝试增量摘要。摘要及已覆盖的消息指针保存在 PostgreSQL，失败或超时不阻断已生成的回复；摘要调用也计入用量。每次仅处理预算内的连续历史片段，不删除原始消息。

`CONTEXT_MAX_CHARS` 默认 14000，`CONTEXT_EVIDENCE_MAX_CHARS` 默认 1200，`SUMMARY_MAX_CHARS` 默认 1600，`SUMMARY_TIMEOUT_S` 默认 8 秒。上下文超限先裁剪旧消息、摘要和次要证据，保留当前请求、系统规则与关键业务字段；必要内容仍放不下时明确失败。摘要属于辅助上下文，不能授予业务权限。

### 本地演示顺序

先迁移、添加演示样本并建立知识索引，再分别启动商城 MCP、后端和前端。开发菜单可选择上述客户及管理员身份；生产认证模式仍使用签名 JWT。

1. **D1 普通退款**：客户选择林晓，询问 `ORD-DEMO-1001` 并明确申请退款；核对回复、订单退款状态和执行步骤。这里写入的是模拟退款账本。
2. **D2 换货审批**：林晓说明 `ORD-DEMO-1005` 的 HP-01 右耳无声、重新配对和重置后仍异常，并明确申请同型号换货。核对知识引用和待审工单；切到管理工作台批准，再刷新详情核对模拟受理单。周宁的 `ORD-DEMO-3001` 可演示补充原因和更换要求，`ORD-DEMO-3002` 可演示审批前取消。
3. **D3 重启与跨会话**：在另一笔换货待审时记录工单号，重启后端并发起新对话询问进度，核对查询没有重复建单；管理员批准并继续处理。重复提交相同请求只返回原接收快照，最终状态以重新查询为准。

演示会修改所选订单。需要从头再演示时使用新的专用演示数据库，不清空已有业务库。需要切回旧退款聊天时设置 `AGENT_V2_ENABLED=false` 并重启后端；这不会删除工单或放宽退款与换货互斥规则。

## DeepSeek

在后端 `.env` 中配置独立的 DeepSeek 连接信息，保留原有供应商配置以便切换：

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=替换为你的专用密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
```

适配器通过现有 OpenAI 兼容客户端使用非思考模式，支持工具调用、JSON mode 和流式输出，并沿用超时与重试限制。模型名称可按账号能力调整；无密钥时仍可使用 `LLM_PROVIDER=fake` 推进开发。

从后端目录运行 `python -m scripts.deepseek_smoke --live`，会使用专用密钥检查回复、静态工具往返、JSON 和流式输出并消耗模型额度。未配置 `DEEPSEEK_API_KEY` 时明确跳过；不带 `--live` 不发起请求。超时、429 和异常处理由本地模拟请求测试覆盖。

## 验证

开发阶段按改动范围选择必要测试，完整回归留到最终验收。后端测试默认使用临时 SQLite 和 fakeredis；只有明确指定专用测试实例时才设置 `TEST_DATABASE_URL` / `TEST_REDIS_URL`，测试会重建测试表和清空测试 Redis。

```powershell
# backend 目录
python -m pytest -q
python -m ruff check app tests evals
python -m evals.run --mode scripted --tags chinese --no-opik --out evals/chinese-results.json

# frontend 目录
npm test
npm run build
```

脚本评测运行前设置 `LLM_PROVIDER=fake`、`FAKE_LLM_LATENCY_MS=0`。使用真实模型评测会调用所配置的模型接口并消耗额度，按需运行。

中文回归重点覆盖：订单号紧邻中文、正常退款、清仓拒绝、大额转人工、重复退款、跨客户访问，以及“退款尚未批准”“此前已退款”“审核通过后才能退款”等不应误报为本轮退款成功的表达。
