# Cloudflare 部署

## 1. 先说结论

这个项目是 **FastAPI + SQLite + 本地录音文件 + 进程内后台任务**，不能直接部署到 Cloudflare Pages 或 Workers：

- Pages 只能托管静态文件，没有 FastAPI、SQLite 和本地文件系统。
- Workers 是 V8 隔离环境，不能直接运行 `uvicorn`/`fastapi`，也没有持久本地磁盘。
- Cloudflare Containers 可以跑 Docker，但容器磁盘默认是临时的；要持久化 SQLite/录音，仍需要 D1/R2/队列改造。

**如果只是要一个稳定的公网地址、不想买域名：直接用 Render 免费实例（`render.yaml`），URL 固定，不依赖 Cloudflare Quick Tunnel。**

**Cloudflare 方式：用 Cloudflare Tunnel 把现有 Docker 服务暴露到公网。** Cloudflare 负责域名、HTTPS 和边缘网络，应用仍在你的 Mac、VPS 或云主机上运行。

如果要求“完全托管在 Cloudflare、没有自己的服务器”，需要把存储改成 D1，录音改 R2，后台任务改 Queues/Workflows，并把 API 重写为 Workers 兼容实现，属于架构改造。

## 2. 临时公网 Demo：Quick Tunnel

适合马上给别人看，不需要域名和 Cloudflare 账号。

```bash
brew install cloudflared
```

准备一个**独立 Demo 数据库**，不要用本机真实 `var/`：

```bash
cd ai-sdr-agent
mkdir -p /tmp/ai-sdr-demo
export SDR_DB_PATH=/tmp/ai-sdr-demo/sdr.sqlite3
export SDR_DEMO_ADMIN_PASSWORD='demo-sdr-2026'
export SDR_SEED_DEMO=true
export SDR_ENTITY_PROVIDER=auto
export QCC_CACHE_DAYS=0
export SDR_COOKIE_SECURE=true
export SDR_PROXY_HEADERS=true
export FORWARDED_ALLOW_IPS='*'
export SDR_LLM_API_KEY=''
export SDR_ASR_API_KEY=''
export DASHSCOPE_API_KEY=''
export HOST=127.0.0.1
export PORT=8877
.venv/bin/python scripts/start_demo.py
```

另开一个终端：

```bash
cloudflared tunnel --url http://127.0.0.1:8877
```

日志里会出现：

```text
https://<random>.trycloudflare.com
```

这个地址就是公网入口。登录账号是 `admin`，密码是 `SDR_DEMO_ADMIN_PASSWORD`。

注意：

- Quick Tunnel 是临时的，URL 可能变化，没有可用性保证。
- 你的电脑必须保持开机，进程不能停。
- 默认不配置 LLM/ASR，所以文本使用离线规则、不能上传录音；企查查按 token 每次分析调用，会消耗查询额度。
- 不要把本机真实 `var/` 暴露出去；上面的数据库是独立临时目录。

停止：

```bash
kill $(cat /tmp/cloudflared-sdr.pid) 2>/dev/null
kill $(cat /tmp/ai-sdr-public.pid) 2>/dev/null
```

## 3. 稳定公网 Demo：Named Tunnel + Docker

适合长期在线、固定域名、所有人都能访问。

### 3.1 创建 Tunnel

1. 登录 Cloudflare Zero Trust。
2. 进入 **Networks → Tunnels → Create a tunnel**。
3. 选择 **Cloudflared**，复制 Tunnel Token。
4. 添加 Public Hostname：
   - 域名：`demo.example.com`
   - Service：`http://app:8765`（Docker Compose 内部服务名）
5. 保存。

### 3.2 准备配置

```bash
cp cloudflare/.env.example cloudflare/.env
```

编辑 `cloudflare/.env`：

```dotenv
SDR_DEMO_ADMIN_PASSWORD=<至少12位密码>
SDR_SEED_DEMO=true
SDR_ENTITY_PROVIDER=auto
QCC_MCP_URL=https://agent.qcc.com/mcp/company/stream
QCC_MCP_TOKEN=<可选，企查查 token>
QCC_CACHE_DAYS=0
SDR_LLM_API_KEY=<可选，文字模型>
SDR_ASR_API_KEY=<可选，录音转写>
CLOUDFLARE_TUNNEL_TOKEN=<上一步复制的 token>
```

`cloudflare/.env` 已被 `.gitignore` 排除，不要提交。

### 3.3 启动

```bash
docker compose --env-file cloudflare/.env -f docker-compose.cloudflare.yml up -d --build
```

检查：

```bash
docker compose -f docker-compose.cloudflare.yml logs -f app
docker compose -f docker-compose.cloudflare.yml logs -f cloudflared
curl -fsS https://demo.example.com/healthz
```

打开 `https://demo.example.com`，用 `admin` 和设置的密码登录。

`docker-compose.cloudflare.yml` 的关键点：

- App 只 `expose` 8765，不直接发布到公网；公网入口只有 Cloudflare Tunnel。
- `demo-var/` 挂载到 `/app/var`，保存 SQLite、录音和模型密钥。
- `SDR_COOKIE_SECURE=true`、`SDR_PROXY_HEADERS=true`，否则 Cloudflare HTTPS 下登录/CSRF 会失败。
- `SDR_SEED_DEMO=true` 首次在空库生成 6 条合成分析。
- 单实例部署；不要给 app 开多副本。

停止：

```bash
docker compose -f docker-compose.cloudflare.yml down
```

数据在 `./demo-var/`，不要删除；备份时同时保留 `sdr.sqlite3`、`audio/` 和 `model-settings.key`。

## 4. 如果你坚持“完全 Cloudflare 托管”

需要做以下改造，不能只改 DNS：

| 当前 | Cloudflare 方案 |
|---|---|
| SQLite 本地文件 | Cloudflare D1 |
| 录音本地目录 | R2 |
| 进程内线程池 | Queues / Workflows / Durable Objects |
| FastAPI/Uvicorn | Workers 兼容 API（TypeScript 或 Python Workers） |
| 本地会话/密码 | Cloudflare Access 或重写认证 |
| 文件上传/转写 | R2 + 外部 ASR，或 Workers AI |

这是一次架构迁移，不是部署配置；在改造前建议先用 Tunnel 保持现有功能。

## 5. 安全与成本

- 公网 Demo 只放合成数据；不要上传真实 `var/`。
- 分享 Demo 账号；应用没有公开注册，但管理员能创建账号。
- 不配置 `SDR_LLM_API_KEY`/`SDR_ASR_API_KEY` 就不会产生模型费用，但录音上传会不可用。
- 配置 QCC token 时使用低额度凭据，并遵守企查查条款；公开访客会消耗查询额度。
- Cloudflare 代理对源站有约 100 秒超时，超大文件上传也有限制；长录音/长分析建议缩短或拆分。
- 建议开启 Cloudflare 的基础 WAF/速率限制；应用自身有登录限流和分析/录音待处理上限，但不是完整公网防滥用系统。
- 如果需要限制访问，可启用 Cloudflare Access；这会阻止“所有人都能打开”。
- Quick Tunnel 没有可用性保证；稳定演示用 Named Tunnel + 固定域名。

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| 530 / 1033 | Tunnel 还没连上，等几秒重试；检查 cloudflared 日志；长期演示不要依赖 Quick Tunnel，改用 Render 或 Named Tunnel |
| 登录 403 / CSRF | 确认 `SDR_COOKIE_SECURE=true`、`SDR_PROXY_HEADERS=true`、`FORWARDED_ALLOW_IPS=*` |
| Cookie 不保存 | 必须通过 Cloudflare HTTPS 访问，不要直接访问容器 HTTP |
| 上传录音失败 | 未配置 ASR key，或文件超过 Cloudflare/应用限制 |
| 重启后数据丢失 | 没有挂载 `demo-var` 或持久卷 |
| 多实例数据错乱 | 改回单实例；SQLite 不支持多副本共享 |
| 企查查无候选 | 未配置 `QCC_MCP_TOKEN` 或 `SDR_ENTITY_PROVIDER=off` |
