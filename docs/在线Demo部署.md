# 在线 Demo 部署

目标：把 AI SDR 工作台部署为 HTTPS 可访问的在线 Demo，使用合成数据，不暴露真实客户、录音、密钥或本机 `var/`。

## 1. 先理解运行约束

- FastAPI + SQLite，单进程、单实例；不要开多个 worker 或多副本。
- 数据库、录音、模型密钥、企查查缓存都在 `var/`。长期保存必须挂持久磁盘；免费演示可以用临时磁盘，每次启动重新 seed。
- 后台分析、ASR 和角色识别使用进程内线程池，实例重启会中断未完成任务。
- 应用默认监听 `127.0.0.1:8765`；在线部署必须监听 `0.0.0.0:$PORT`，并设置 `SDR_COOKIE_SECURE=true`。
- 反向代理终止 HTTPS 时，必须启用代理头（本项目的 `scripts/start_demo.py` 已支持 `SDR_PROXY_HEADERS=true`），否则 CSRF Origin 校验可能失败。
- 不要把本机 `var/` 上传到服务器，也不要把真实录音放进镜像。

## 2. 推荐方案：Render 免费实例（固定 URL，不需要域名）

这是目前最省事、最稳定的无域名方案：Render 提供一个固定的 `https://xxx.onrender.com` 地址。

### 2.1 准备仓库

```bash
git add .
git commit -m "Add online demo bootstrap and deployment config"
git push
```

Render 从 GitHub 拉取代码。仓库里的 `render.yaml` 默认是**免费实例**。

### 2.2 创建服务

1. 登录 Render，选择 **New → Blueprint**，连接仓库。
2. Render 读取 `render.yaml`，创建 `ai-sdr-demo`。
3. 确认实例数为 **1**，不要开启自动扩缩容。
4. 免费实例不需要挂磁盘；应用会把 SQLite 和录音写在临时 `/app/var`。
5. 在 Environment 填写 `sync: false` 的变量：

| 变量 | 是否必需 | 说明 |
|---|---|---|
| `SDR_DEMO_ADMIN_PASSWORD` | 必需 | 至少 12 位；Demo 登录密码 |
| `QCC_MCP_TOKEN` | 建议 | 企查查 MCP token；不填则企业抬头只保留原话 |
| `SDR_LLM_API_KEY` | 可选 | 文字模型；不填只能离线规则，企查查重排也会跳过 |
| `SDR_ASR_API_KEY` | 可选 | 录音转写；不填则录音上传不可用 |
| `SDR_DEMO_ADMIN_USER` | 已配置 | 默认 `admin` |
| `SDR_SEED_DEMO` | 已配置 | 每次空库启动生成 6 条演示分析 |
| `SDR_ENTITY_PROVIDER` | 已配置 | `auto`，有 token 时调用企查查 |
| `QCC_CACHE_DAYS` | 已配置 | `0`，每次分析都调用企查查 MCP |
| `SDR_COOKIE_SECURE` | 已配置 | 必须是 `true` |
| `SDR_PROXY_HEADERS` | 已配置 | 必须是 `true` |
| `SDR_AUTO_PUBLISH` | 已配置 | 保持 `false` |

6. 点击 Deploy，等 Health Check `/healthz` 通过。
7. 打开 `https://<service>.onrender.com`，用 `admin` 和设置的密码登录。

### 2.3 免费实例的边界

- URL 固定，但免费实例会在闲置后休眠，首次打开需要几十秒冷启动。
- 没有持久磁盘：重启、重新部署或休眠恢复后，SQLite 会重置；`SDR_SEED_DEMO=true` 会重新生成 6 条合成数据。
- 访客新建的分析、上传的录音和修改不会长期保存。
- 企查查默认每次分析都调用；QCC token 会消耗额度。
- 只运行一个实例；SQLite 和本地录音不能共享给多副本。

如果演示只需要“打开链接能看、能体验流程”，免费实例足够；如果要求数据不丢，使用下面的持久化版本。

### 2.4 需要持久化时

`render.paid.yaml` 是带 1 GB 持久磁盘的付费版本：

1. 把 `render.paid.yaml` 复制为 `render.yaml`（或按文件内容在 Render 控制台手动配置）。
2. 使用 Starter 或更高 plan。
3. 挂载磁盘到 `/app/var`。
4. 重新 Deploy。

持久化版本会保留 SQLite、录音、模型密钥和企查查缓存；仍然只能单实例运行。

## 3. Fly.io 方案

适合希望控制区域、机器和卷的场景。

```bash
brew install flyctl
fly auth login
fly launch --no-deploy
fly volumes create sdr_data --size 1 --region hkg
```

在生成的 `fly.toml` 中加入或确认：

```toml
[http_service]
  internal_port = 8765
  force_https = true
  auto_stop_machines = false
  auto_start_machines = true
  min_machines_running = 1

[[mounts]]
  source = "sdr_data"
  destination = "/app/var"

[env]
  SDR_COOKIE_SECURE = "true"
  SDR_PROXY_HEADERS = "true"
  SDR_DEMO_ADMIN_USER = "admin"
  SDR_SEED_DEMO = "true"
  SDR_ENTITY_PROVIDER = "auto"
  QCC_MCP_URL = "https://agent.qcc.com/mcp/company/stream"
```

设置密钥并部署：

```bash
fly secrets set \
  SDR_DEMO_ADMIN_PASSWORD='<至少12位密码>' \
  QCC_MCP_TOKEN='<token>' \
  SDR_LLM_API_KEY='<key>' \
  SDR_ASR_API_KEY='<key>'

fly deploy
fly open
```

只保留一个 Machine；不要水平扩容。

## 4. 香港 VPS + Docker + Caddy（无需域名、无需国外卡）

适合长期演示：支付宝买一台香港轻量服务器，2 核 2G 就够；不需要 ICP，也不需要买域名。

服务器 IP 假设是 `1.2.3.4`，用免费的 `sslip.io` 得到公网域名：

```text
1-2-3-4.sslip.io
```

### 4.1 准备

```bash
apt update && apt install -y docker.io docker-compose-plugin git
git clone https://github.com/dhc979917-ux/AI-SDR-Agent.git
cd AI-SDR-Agent
cp .env.example .env
```

编辑 `.env`：

```dotenv
DEMO_HOST=1-2-3-4.sslip.io

SDR_COOKIE_SECURE=true
SDR_PROXY_HEADERS=true
SDR_DEMO_ADMIN_USER=admin
SDR_DEMO_ADMIN_PASSWORD=<至少12位密码>
SDR_SEED_DEMO=true
SDR_ENTITY_PROVIDER=auto
QCC_CACHE_DAYS=0
QCC_MCP_TOKEN=<可选>
SDR_LLM_API_KEY=<可选>
SDR_ASR_API_KEY=<可选>
SDR_AUTO_PUBLISH=false
```

### 4.2 启动

```bash
docker compose -f docker-compose.vps.yml up -d --build
docker compose -f docker-compose.vps.yml logs -f app
```

Caddy 会自动为 `DEMO_HOST` 申请 HTTPS 证书，通常一两分钟内可用：

```text
https://1-2-3-4.sslip.io
```

登录 `admin` + 你设置的密码。

### 4.3 注意

- 服务器安全组/防火墙放行 `80` 和 `443`；应用 8765 不直接暴露。
- 数据在 `demo-var/`，备份时保留 `sdr.sqlite3`、`audio/` 和 `model-settings.key`。
- 如果 `sslip.io` 在你所在网络不可用，买一个国内域名，把 `DEMO_HOST` 改成 `demo.example.com`，A 记录指向服务器 IP 即可；Caddy 会自动申请证书。
- 仍然只能单实例；SQLite 和本地录音不能多副本共享。
- 不要把真实 `var/`、真实录音或真实模型 key 放进公开 Demo。

## 5. 演示数据与账号

`scripts/start_demo.py` 做三件事：

1. 如果用户表为空，用 `SDR_DEMO_ADMIN_USER` / `SDR_DEMO_ADMIN_PASSWORD` 创建管理员。
2. 如果 `SDR_SEED_DEMO=true` 且没有分析记录，用 `data/samples.json` 生成 6 条规则模式合成分析。
3. 启动 Uvicorn，监听 `0.0.0.0:$PORT`。

种子数据全部是合成/脱敏样例，不包含真实通话；不会调用企查查、LLM 或 ASR。首次启动后进入审核队列即可看到演示记录。

如果没设置 `SDR_DEMO_ADMIN_PASSWORD`，启动脚本会生成随机密码并打印到日志。这种方式只适合临时演示；正式 Demo 请使用平台 Secret。

## 6. 安全与成本

- 不要部署本机 `var/`；使用全新数据目录。免费实例使用临时目录，持久版使用新卷。
- 不要开放注册；只分享 Demo 账号。账号、密码、企业数据都要按演示数据处理。
- `SDR_AUTO_PUBLISH=false`，演示数据不会变成正式 SQL。
- 企查查 token 用低额度凭据，并遵守服务条款；只发送企业名称/信用代码。
- 公网 Demo 的录音转写和 LLM 分析会产生费用。最省钱的做法是只配置 `SDR_LLM_API_KEY` 或不配置 ASR，让访客体验文本和规则模式。
- 建议在 Render/Fly/Caddy 前面加基础限流和访问控制；应用已有登录限流、分析待处理上限 20、录音待处理上限 10、录音配额 2 GiB，但它不是面向公网的完整防滥用系统。
- 需要持久化时，持久卷要备份；备份时同时保留 `var/sdr.sqlite3`、`var/audio/` 和 `var/model-settings.key`，否则个人模型密钥无法解密。
- 单实例部署，不要开多 worker；滚动发布期间可能出现短暂中断。

## 7. 验证清单

```bash
curl -fsS https://<domain>/healthz
```

然后：

1. 登录 Demo 账号。
2. 分析工作台能看到 6 条种子记录。
3. 打开「题目样例 · 木里木外」，确认意向评分和企查查候选（如果配置了 token）。
4. 打开一条录音记录，确认自动角色、自动分析状态和待确认数量。
5. 审核页确认「原文证据轮次」已预填。
6. 检查日志没有 `SDR_COOKIE_SECURE`、Origin、SQLite 只读或 QCC 401 错误。

## 8. 常见问题

| 现象 | 处理 |
|---|---|
| 502 / 服务未启动 | 看平台日志；确认 `PORT`、`SDR_COOKIE_SECURE=true`、磁盘挂载 |
| 登录成功但写操作 403 | 反向代理没有传 `X-Forwarded-Proto/Host`；开启 `SDR_PROXY_HEADERS=true` |
| Cookie 不保存 | 必须 HTTPS；`SDR_COOKIE_SECURE=true` |
| SQLite 只读/权限错误 | 确认 `/app/var` 可写；持久版确认挂载；镜像启动时会 chown |
| 录音上传按钮不可用 | 未配置 `SDR_ASR_API_KEY` |
| 企业抬头没有候选 | 未配置 `QCC_MCP_TOKEN`，或 `SDR_ENTITY_PROVIDER=off` |
| 重启后数据丢失 | 免费实例本来就不持久；要保留数据请用持久版并挂载 `/app/var` |
| 多实例后数据错乱 | 改回单实例；SQLite 不支持多副本共享 |

## 9. 不建议的平台

Vercel、Netlify、Cloudflare Workers 这类无状态/边缘平台不适合当前实现：SQLite 本地文件、录音落盘、长任务线程池和单进程状态都需要持久磁盘与常驻进程。要上这些平台，需要先把存储改成 Postgres/对象存储，任务改成外部队列，属于架构改造，不是简单部署。
