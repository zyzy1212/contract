# Contract Review Agent 运维手册

## 数据库迁移

本地开发直接执行：

```powershell
cd backend
uv run --locked --extra dev alembic upgrade head
```

生产环境先备份，再按以下顺序升级：

```powershell
pg_dump -Fc contract_review > backup-before-$(Get-Date -Format yyyyMMdd).dump
uv run --locked --extra dev alembic upgrade head
```

回滚单个版本使用 `alembic downgrade -1`，回滚前必须确认没有依赖新 schema 的写入流量。

## 服务启动

依赖服务：

```powershell
docker compose up -d postgres redis minio
```

后端 API：

```powershell
cd backend
uv run --locked --extra dev uvicorn app.main:app --reload --port 8000
```

Celery worker（知识入库 `ingestion` 与合同审核 `review` 使用独立队列，需分别启动）：

```powershell
cd backend
uv run --locked --extra dev celery -A app.tasks.celery_app:celery_app worker -Q ingestion --pool=threads --concurrency=2 --loglevel=INFO
uv run --locked --extra dev celery -A app.tasks.celery_app:celery_app worker -Q review --pool=threads --concurrency=4 --loglevel=INFO
```

Windows 本地开发使用 `--pool=threads` 单进程并发；生产 Linux 建议使用
`--pool=prefork --concurrency=8`，并发再大时通过增加 worker 副本水平扩容。

前端：

```powershell
cd frontend
npm install
npm run dev
```

## 对象存储备份

对象存储保存合同原文和知识原文，属于不可变内容寻址对象。备份策略：

- 每日对 MinIO/S3 bucket 做增量备份，每周做完整快照。
- 保留对象版本；`If-None-Match` 与 ETag/VersionId 用于避免覆盖。
- 禁止直接删除对象；待实现引用感知的孤儿对象回收后再清理。

## API 重试行为

- DeepSeek 请求对超时、429 和 5xx 指数退避重试，最多 3 次。
- 混合检索对单个通道的瞬时失败降级为另一通道，不泄漏安全错误。
- 合同上传幂等键为 `tenant + 文件 SHA-256 + 审核配置`，重复提交复用任务。
- 客户端轮询 `/api/reviews/{id}` 直到状态进入 `complete`、`partial` 或 `failed`。

## 知识库查询扩写

- 首轮检索默认开启查询扩写，由 LLM 生成最多 `REVIEW_QUERY_EXPANSION_MAX_QUERIES` 条补充查询词。
- 原始条款始终参与检索，扩写查询结果合并去重后再交给证据充分性判断。
- 扩写请求失败时自动回退为原始条款检索，不阻塞审核任务。
- 可通过 `REVIEW_QUERY_EXPANSION_ENABLED=false` 关闭。
- 短于 `REVIEW_QUERY_EXPANSION_MIN_CHARACTERS` 的条款跳过扩写，减少无效检索调用。
- `REVIEW_RETRIEVAL_MAX_ROUNDS` 控制证据追问轮数上限，调小可加快审核但会降低召回上限。

## 检索通道与精排

- 混合检索包含三个通道：向量召回、关键词召回、法条引用召回；法条引用通道按
  `article_number`、法规名称、章节标题匹配，每个通道各自召回前 20 条。
- 三通道结果经 RRF 融合后交给 Cross-Encoder 精排，最终只保留前 5 条作为
  证据候选（`REVIEW_RETRIEVAL_CHANNEL_TOP_K=20`、`REVIEW_RERANK_TOP_K=5`）。
- 向量召回片段需满足 `REVIEW_EVIDENCE_MIN_SIMILARITY`（默认 0.5）才进入证据
  判断和大模型生成；法条精确命中不受相似度门槛限制。
- 知识库关键词索引包含法规标题、发布机关、文号、章节与法条号（迁移
  `0007_domain_keyword_index`），新入库知识在分块时自动写入。
- Cross-Encoder 精排默认启用（`REVIEW_RERANK_ENABLED=true`），默认使用
  `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`；中文场景可切换为
  `BAAI/bge-reranker-base` 或 `maidalun1020/bce-reranker-base_v1`，并确认机器
  内存足够。模型会在任务开始时预热，避免首次推理占用检索超时预算。

## DeepSeek 多 Key

- `DEEPSEEK_API_KEY` 支持逗号分隔多个 key，例如 `sk-a, sk-b`。
- 请求按 key 轮询分配；单个 key 超时、429 或 5xx 时自动切换到下一个 key。
- 每个事件循环独立创建 OpenAI 客户端，避免 Celery 多线程下跨 loop 复用。
- `REVIEW_CLAUSE_CONCURRENCY` 与 review worker 的 `--concurrency` 应保持同步；本地双 key 配置默认使用 8。

## 卡住任务恢复

- 每个条款有独立 checkpoint；Celery 任务从 `review_clause_checkpoints` 中恢复未完成条款。
- 若任务卡在 `running` 超过阈值，先检查 worker 存活，再从 `backend` 目录重投递：

  ```powershell
  uv run --locked --extra dev python scripts/resume_review_jobs.py --minutes 15
  ```

  该命令会把超过 15 分钟未更新的 `running` 任务重新放入 review 队列，从已有
  `review_clause_checkpoints` 继续审核未完成条款。
- 单条款失败不会终止整份合同，最终状态为 `partial` 并列出未审核条款。

## 模型故障行为

- 模型不可用时抛 `ModelUnavailable`，Celery 按指数退避重试 3 次。
- 最终仍失败时，该条款标记 `failed`，任务进入 `partial`，不展示未通过草稿。
- 模型输出非法 JSON 或不满足 schema 时抛 `InvalidModelOutput`，同样不发布。

## 知识停用

管理员通过 `/api/admin/knowledge/{document_id}/deactivate` 停用知识。停用后：

- 新检索不再返回该文档的片段。
- 已完成报告中的证据快照不受影响，继续保留原始引用。

## 租户离场

- 先停用客户私有知识，再导出该租户审计与报告。
- 将租户状态置为 `suspended`，禁止新上传和 A2A 调用。
- 保留证据快照与审计事件；仅在法定保留期结束后由专人执行删除。

## 密钥轮换

- `DEEPSEEK_API_KEY` 通过环境变量注入，不入库、不入日志。
- 审计服务对所有 `api_key`、`authorization`、`password`、`token` 键递归脱敏。
- 轮换后验证新密钥可访问，再吊销旧密钥。

## 证据快照保留

- `evidence_snapshots` 不可更新、不可删除（数据库触发器强制）。
- 知识库后续更新不会改变已完成任务的证据。
- 保留期按法规要求配置，期满后由具备平台权限的维护人员执行归档。
