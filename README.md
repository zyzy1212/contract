# Contract Review Agent

多租户合同审核系统：FastAPI + PostgreSQL/pgvector 后端，Vue 双栏审核前端，Celery 异步任务，A2A Agent 接口。所有发布的审核结论都绑定可追溯证据，证据不足或复审不通过时不发布草稿。

## 一键启动

新环境首次使用请先按 [启动指南](docs/startup.md) 完成 `.env`、后端依赖、数据库迁移、
身份 seed 和前端依赖安装；之后日常启动统一使用下面的脚本。

```powershell
.\scripts\dev.ps1 start
```

启动后访问：

- 前端：http://localhost:5173
- 后端：http://localhost:8000/health
- API 文档：http://localhost:8000/docs

常用命令：

```powershell
.\scripts\dev.ps1 stop     # 停止所有服务
.\scripts\dev.ps1 restart  # 全部重启
.\scripts\dev.ps1 status   # 查看端口状态
.\scripts\dev.ps1 logs     # 查看所有服务日志
```

脚本会依次启动 PostgreSQL、数据库迁移、后端 API、知识入库 worker、合同审核 worker 和前端。
审核条款默认 4 个并发，可通过 `.env` 中的 `REVIEW_CLAUSE_CONCURRENCY` 调整。

## 手动启动（备用）

```powershell
# 1. 启动依赖服务
docker compose up -d postgres redis minio

# 2. 安装并迁移后端
cd backend
Copy-Item ..\.env.example ..\.env
uv sync --extra dev
uv run --locked --extra dev alembic upgrade head
uv run --locked --extra dev python scripts/seed_dev_identities.py
uv run --locked --extra dev uvicorn app.main:app --reload --port 8000

# 3. 启动 Celery worker（另开终端）
cd backend
# Windows 本地开发使用 threads 池，单进程内并发处理多个任务
# 3a. 知识入库 worker
uv run --locked --extra dev celery -A app.tasks.celery_app:celery_app worker -Q ingestion --pool=threads --concurrency=2 --loglevel=INFO
# 3b. 合同审核 worker
uv run --locked --extra dev celery -A app.tasks.celery_app:celery_app worker -Q review --pool=threads --concurrency=4 --loglevel=INFO

# 4. 启动前端（另开终端）
cd frontend
npm install
npm run dev
```

开发环境身份通过请求头注入：`X-Actor-User`、`X-Actor-Tenant`、`X-Actor-Role`。生产环境替换为 OAuth/API 凭证映射。

## 测试

```powershell
cd backend
uv run --locked --extra dev pytest -v

cd frontend
npm test -- --run
npm run build
```

可选 PostgreSQL 安全测试需要一个以 `_test` 结尾的一次性数据库。应用角色必须是非超级用户，
以便行级安全策略真实生效；迁移角色负责创建/回滚 `vector` 扩展：

```powershell
cd backend
$env:TEST_DATABASE_URL="postgresql+asyncpg://app_rls:app_rls@localhost:5432/contract_review_test"
$env:TEST_DATABASE_ADMIN_URL="postgresql+asyncpg://contract:contract@localhost:5432/contract_review_test"
$env:TEST_DATABASE_DISPOSABLE="1"
uv run --locked --extra dev pytest -m postgresql -v
```

## 发布评估

```powershell
cd backend
uv run --locked --extra dev python eval/run_grounding_eval.py
```

期望输出 `release_gate.pass=true`、`citation_complete=true`、`unsupported_published=0`、`insufficient_refusal_rate>=0.95`。

实模型评估需设置 `DEEPSEEK_API_KEY` 后执行：

```powershell
uv run --locked --extra dev python eval/run_grounding_eval.py --live
```

## 运维

数据库迁移、worker 启动、对象存储备份、卡住任务恢复、模型故障与密钥轮换等操作说明见 `docs/operations.md`。

新电脑的环境安装、依赖配置、三种启动方式与常见问题排查见 [docs/startup.md](docs/startup.md)。
