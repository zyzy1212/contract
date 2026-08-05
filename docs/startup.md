# Contract Review Agent 启动指南

本文档面向在新电脑上安装、配置并启动 Contract Review Agent 的完整流程，包含环境要求、三种启动方式、配置文件说明与常见问题排查。

## 项目结构

```text
contract-review-agent/
├── backend/            FastAPI 后端 + Celery worker + 数据库迁移
├── frontend/           Vue 3 + Vite 前端
├── scripts/dev.ps1     本地一键启动/停止脚本
├── docker-compose.yml  PostgreSQL / Redis / MinIO / 全容器化编排
├── .env.example        环境变量模板（提交到仓库）
└── .env                本机环境变量（已加入 .gitignore，不提交）
```

## 端口一览

| 端口 | 服务 | 说明 |
| ---- | ---- | ---- |
| 5173 | 前端 Vite | 本地开发前端入口 |
| 8000 | 后端 API | `/health` 健康检查，`/docs` API 文档 |
| 5432 | PostgreSQL | pgvector 扩展，合同/知识库数据 |
| 6379 | Redis | Celery broker 与结果存储 |
| 9000 / 9001 | MinIO | 对象存储 API / 控制台（容器化方式自动启动） |
| 8080 | Nginx 前端 | Docker Compose 全容器化方式的前端入口 |

## 环境要求

### 本地开发方式

| 依赖 | 版本要求 | 用途 |
| ---- | ---- | ---- |
| Docker Desktop | 任意较新版本 | 运行 PostgreSQL、Redis、MinIO |
| uv | 最新版 | 管理 Python 3.12 环境和锁定依赖 |
| Python | >= 3.12 | 由 uv 自动管理或手动安装 |
| Node.js | 20 或 22 LTS | 运行前端 Vite |

### 全容器化方式

只需要 Docker Desktop，Python、uv、Node.js 均不需要安装。

## 获取项目代码

两种方式任选其一：

```powershell
# 方式一：git clone
git clone <仓库地址> contract-review-agent
cd contract-review-agent

# 方式二：直接拷贝整个项目文件夹到新电脑
# 注意：直接拷贝会带上本机 .env（含 API Key），git clone 则不会
```

## 配置环境变量

项目读取根目录 `.env` 文件。使用 git 获取代码后需要先创建：

```powershell
Copy-Item .env.example .env
```

至少需要修改以下配置：

| 变量 | 说明 |
| ---- | ---- |
| `DEEPSEEK_API_KEY` | DeepSeek API Key，支持逗号分隔多个 key |
| `DEEPSEEK_GENERATION_MODEL` | 生成模型，默认 `deepseek-v4-flash` |
| `DEEPSEEK_REVIEW_MODEL` | 审核模型，默认 `deepseek-v4-pro` |

其余变量通常使用默认值即可：

| 变量 | 默认值 | 说明 |
| ---- | ---- | ---- |
| `POSTGRES_DB` | `contract_review` | 数据库名 |
| `POSTGRES_USER` | `contract` | 数据库用户 |
| `POSTGRES_PASSWORD` | `contract` | 数据库密码 |
| `POSTGRES_PORT` | `5432` | PostgreSQL 宿主机端口 |
| `REDIS_PORT` | `6379` | Redis 宿主机端口 |
| `DATABASE_URL` | `postgresql+asyncpg://contract:contract@localhost:5432/contract_review` | 后端连接串 |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker |
| `OBJECT_STORE_ENDPOINT` | `http://localhost:9000` | 对象存储地址 |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | `minioadmin` | MinIO 访问凭证 |
| `REVIEW_CLAUSE_CONCURRENCY` | `4` | 条款审核并发数 |
| `REVIEW_QUERY_EXPANSION_ENABLED` | `true` | 检索查询扩写开关 |

## 方式一：本地一键启动（推荐开发用）

### 1. 安装工具

```powershell
# Docker Desktop：安装后启动一次，等待右下角状态变为 Running
winget install Docker.DockerDesktop

# uv（PowerShell）
irm https://astral.sh/uv/install.ps1 | iex
# 或 winget install --id=astral-sh.uv

# Node.js LTS
winget install OpenJS.NodeJS.LTS
```

### 2. 安装后端依赖

```powershell
cd backend
uv sync --locked --extra dev
cd ..
```

命令会在 `backend\.venv` 创建虚拟环境并安装 `uv.lock` 锁定的依赖（含 pytest 等开发依赖）。要求 Python 3.12+，uv 会优先使用本机 Python，必要时自动下载。

### 3. 安装前端依赖

```powershell
cd frontend
npm install
cd ..
```

### 4. 一键启动

```powershell
.\scripts\dev.ps1 start
```

脚本依次完成：

1. 检查 `backend\.venv` 是否存在，不存在则报错退出
2. 检查 8000 端口未被占用
3. 自动启动 Docker Desktop（若未运行）
4. 启动 PostgreSQL（Docker），Redis 未监听时也通过 Docker 启动
5. 执行数据库迁移 `alembic upgrade head`
6. 启动后端 API、知识入库 worker、合同审核 worker、前端

启动成功后访问：

- 前端：http://localhost:5173
- 健康检查：http://localhost:8000/health
- API 文档：http://localhost:8000/docs

### 5. 常用管理命令

```powershell
.\scripts\dev.ps1 stop      # 停止后端、worker、前端和数据库容器
.\scripts\dev.ps1 restart   # 全部重启
.\scripts\dev.ps1 status    # 查看 5432/6379/8000/5173 端口状态
.\scripts\dev.ps1 logs      # 查看所有服务日志（logs 目录下的 *.log）
```

服务日志位于根目录 `logs/`：

```text
logs/
├── backend.out.log
├── ingestion.out.log
├── review.out.log
└── frontend.out.log
```

## 方式二：Docker Compose 全容器化（推荐部署/演示）

不需要本机 Python 和 Node，在项目根目录执行：

```powershell
docker compose --profile app up -d --build
```

会自动构建后端/前端镜像，并启动：

- PostgreSQL + pgvector
- Redis
- MinIO + bucket 初始化
- 数据库迁移（成功后才启动后端）
- 后端 API
- Celery worker
- Nginx 前端

访问：

- 前端：http://localhost:8080
- 后端健康检查：http://localhost:8000/health
- API 文档：http://localhost:8000/docs

常用命令：

```powershell
# 查看日志
docker compose --profile app logs -f backend worker frontend

# 停止并删除容器（保留数据卷）
docker compose --profile app down

# 停止并删除容器和数据卷（谨慎，会清空数据库和对象存储）
docker compose --profile app down -v

# 只重建后端镜像并重启
docker compose --profile app up -d --build backend worker
```

## 方式三：手动逐个启动（备用）

适合不想用 `dev.ps1` 或需要分别调试的情况。

### 1. 启动依赖服务

```powershell
docker compose up -d postgres redis minio
```

首次使用 MinIO 时初始化 bucket：

```powershell
docker compose --profile app run --rm minio-init
```

### 2. 迁移并启动后端

```powershell
cd backend
uv run --locked --extra dev alembic upgrade head
uv run --locked --extra dev python scripts/seed_dev_identities.py
uv run --locked --extra dev uvicorn app.main:app --reload --port 8000
```

`seed_dev_identities.py` 写入开发环境身份，本地开发通过请求头模拟用户：`X-Actor-User`、`X-Actor-Tenant`、`X-Actor-Role`。

### 3. 启动 Celery worker（另开终端）

```powershell
cd backend

# 知识入库 worker
uv run --locked --extra dev celery -A app.tasks.celery_app:celery_app worker -Q ingestion --pool=threads --concurrency=2 --loglevel=INFO

# 合同审核 worker
uv run --locked --extra dev celery -A app.tasks.celery_app:celery_app worker -Q review --pool=threads --concurrency=4 --loglevel=INFO
```

Windows 本地开发使用 `--pool=threads` 单进程并发；生产 Linux 建议 `--pool=prefork`。

### 4. 启动前端（另开终端）

```powershell
cd frontend
npm install
npm run dev
```

## 首次使用注意事项

1. 首次调用知识入库或审核时，系统会从 HuggingFace 下载 embedding 模型 `BAAI/bge-small-zh-v1.5`，新机器需要外网访问，下载后缓存在本地。
2. 审核需要真实调用 DeepSeek API，`.env` 中必须有有效且余额充足的 `DEEPSEEK_API_KEY`。
3. `scripts/dev.ps1` 不启动 MinIO；若需要上传/读取合同原文等对象存储功能，手动执行 `docker compose up -d minio` 并初始化 bucket。

## 常见问题排查

### `ERROR: backend venv not found. Run setup first.`

没有执行后端依赖安装。运行：

```powershell
cd backend
uv sync --locked --extra dev
```

### 前端没有启动

`scripts/dev.ps1` 中写死了当前机器的 Node 路径 `C:\nvm4w\nodejs\node.exe`（第 97 行）。新电脑 Node 装在其他位置时需要修改该行，或不用脚本、手动执行 `cd frontend; npm run dev`。

### Docker Desktop 未运行

脚本会自动尝试启动，等待约 150 秒。也可以先手动打开 Docker Desktop，确认 `docker info` 正常后再执行 `.\scripts\dev.ps1 start`。

### 端口被占用

```powershell
.\scripts\dev.ps1 status
netstat -ano | findstr ":8000 :5173 :5432 :6379"
```

若 8000 已被占用，脚本会拒绝启动；先停止占用进程，或修改 `.env` 中的端口并同步修改配置。

### DeepSeek 调用失败 / 401

检查 `.env` 中 `DEEPSEEK_API_KEY` 是否有效，模型名是否可访问。多个 key 使用逗号分隔，系统会在限流/超时后自动轮换。

### 数据库迁移失败

```powershell
.\scripts\dev.ps1 logs
docker compose logs postgres
```

常见原因：PostgreSQL 未启动、端口冲突、`.env` 中的数据库连接串与 docker-compose 不一致。

### 审核任务一直 pending / failed

确认两个 Celery worker 都已启动，且 Redis 可访问：

```powershell
.\scripts\dev.ps1 status
.\scripts\dev.ps1 logs
```

## 测试与构建

```powershell
# 后端测试
cd backend
uv run --locked --extra dev pytest -v

# 前端测试与构建
cd frontend
npm test -- --run
npm run build
```

## 关联文档

- [运维手册](operations.md)：数据库迁移、对象存储备份、worker 扩容、模型故障处理
- [项目 README](../README.md)：一键启动命令与评估流程
