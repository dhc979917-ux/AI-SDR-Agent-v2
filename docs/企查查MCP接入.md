# 企查查 MCP 企业抬头接入

本文说明 `entity-4.0` 如何通过企查查 MCP 做企业抬头候选召回，以及为什么多候选必须人工确认。

## 1. 目标与边界

目标是从客户口语、品牌、简称和可能的转写错误中召回候选企业全称，输出到 SQL 线索的 `company_name` 字段。

边界：

- 只发送企业名称或统一社会信用代码，不发送整份通话、客户姓名、手机号或联系人。
- 企查查返回的是候选，不是“客户一定属于该主体”的证明。
- 多候选不自动选第一名；人工选择候选会记录确认人和信用代码，并进入独立复核。
- 检索失败或无结果时保留客户原话，不编造全称，不阻断分析。
- 品牌方、经销商、分支机构需要人工区分。

## 2. 配置

推荐使用环境变量：

```dotenv
SDR_ENTITY_PROVIDER=auto
QCC_MCP_URL=https://agent.qcc.com/mcp/company/stream
QCC_MCP_TOKEN=your-token
# QCC_MCP_TIMEOUT=8
# QCC_CACHE_PATH=var/qcc-cache.json
# QCC_CACHE_DAYS=30
```

也可以使用本机配置文件 `var/qcc-mcp.json`：

```json
{
  "url": "https://agent.qcc.com/mcp/company/stream",
  "headers": {"Authorization": "Bearer your-token"}
}
```

要求：

- 文件权限 `0600`，`var/` 已被 `.gitignore` 排除。
- 不要把 token 写入 README、设计文档、截图、录屏或交付 zip。
- 如果 token 曾经外发或进入公开仓库，应轮换。
- `SDR_ENTITY_PROVIDER=off` 完全关闭外部检索；`local` 使用本地目录；`auto` 在检测到凭据时使用企查查。

## 3. MCP 协议

客户端位于 `sdr/qcc_mcp.py`，按 Streamable HTTP MCP 调用：

1. `initialize`，协议版本 `2025-03-26`。
2. `notifications/initialized`，服务端返回 `202` 是正常现象。
3. `tools/call`。

请求头：

```text
Authorization: Bearer <token>
Content-Type: application/json
Accept: application/json, text/event-stream
MCP-Protocol-Version: 2025-03-26
```

响应可能是普通 JSON，也可能是 SSE `data:` 行。工具结果在 `result.content[0].text` 中，是一个字符串化的 JSON；字段名是中文，例如 `匹配结果`、`企业信息`、`企业名称`。适配器必须集中映射这些字段，不能把上游原始结构直接散落到业务代码。

## 4. 使用的工具

| 工具 | 用途 |
|---|---|
| `get_company_by_query` | 按企业名称、简称、品牌模糊搜索，返回唯一精确匹配或多候选 |
| `get_company_registration_info` | 按信用代码或名称查询工商登记信息、所属地区、国标行业、注册地址等 |

当前不在主链路调用股权、实控人、财务、联系方式等工具。它们不解决企业抬头问题，反而会扩大数据面和配额消耗。

## 5. 处理流程

1. 从校验后的 `raw_company` 或 `brand_name` 取客户原话。
2. 归一化：NFKC、去引号/括号、压缩空白、去尾部标点。
3. 生成查询变体，实际最多调用 2 次：
   - 原话；
   - 加地区词；
   - 同音/后缀变体（例如“七木定制”→“栖木定制”）。
4. 调用 `get_company_by_query`，第一个查询无结果时再试 1 个。
5. 对前三名候选补充工商详情，并做本地打分与标注：名称相似、品牌/简称、地区、行业、登记状态。
6. 第二层重排：使用同一个文字分析模型，对候选做 `exact / alias / brand / related / unrelated` 判定和置信度排序；重排结果直接按顺序展示。模型不可用时回退企查查返回顺序。
7. 自动确认：唯一 `exact` 且置信度 ≥0.9，并满足全称/地区等确定性条件时，直接写入 `confirmed`，记录 `auto_confirmed=true`、`ranker=llm`、`rerank_model`；不再要求人工确认企业抬头。
8. 品牌简称、多候选或模型只给出 `alias/brand/related` 时，不自动确认；按重排顺序展示，状态为 `suggested` 或 `ambiguous`。
9. 无结果：状态 `unresolved`，保留原话。
10. 超时/鉴权/协议错误：状态 `lookup_failed`，保留原话，分析继续。

## 6. 候选标注与状态

候选会附带本地匹配分、重排置信度和理由，用于审核页展示。默认对前三名补充工商详情（`QCC_ENRICH_TOP=3`），其余候选保留搜索级信息。模型重排成功时按重排置信度展示；模型不可用或调用失败时保留企查查返回顺序，并记录 `rerank_error`。

| 信号 | 权重 |
|---|---:|
| 名称相似/包含 | 0.50 |
| 品牌/简称匹配 | 0.30 |
| 地区一致 | 0.10 |
| 行业一致 | 0.05 |
| 登记状态存续 | 0.05 |

状态：

| 状态 | 含义 |
|---|---|
| `captured` | 未启用企查查，保留原话 |
| `suggested` | 唯一候选/品牌候选，待人工确认客户关系 |
| `ambiguous` | 多候选，按重排顺序展示，必须人工选择 |
| `confirmed` | 全称已确认；可能是人工确认，也可能是唯一 exact 自动确认 |
| `manual` | 人工填写 |
| `unresolved` | 企查查无匹配，保留原话 |
| `lookup_failed` | 企查查失败，保留原话 |

`suggested` 会写入 `company_name` 全称，但仍带 `ENTITY_UNCONFIRMED` 阻断项，不能直接正式交付。`ambiguous` 保留原话并带 `ENTITY_AMBIGUOUS`。自动确认和人工确认都使用 `confirmed`，通过 `auto_confirmed` 区分；只有 `confirmed` 才移除企业抬头阻断。

自动确认的确定性门槛：

- 客户口述的是完整企业全称（含有限公司/集团等后缀），或名称归一化后完全一致；
- 重排中只有一个 `exact` 候选，置信度 ≥0.9；
- 地区存在时，候选注册地区与客户经营地区不冲突；
- 品牌简称、经销商、分支机构、多个 exact 竞争者、模型失败都不自动确认。

自动确认仍保留候选列表、重排理由、信用代码和 `_meta`，审核人可以在审核页改选其他主体；改选会进入独立复核。

## 7. 人工确认

审核页展示候选的全称、信用代码、登记状态、地区、行业和匹配理由。审核人可以：

- 选择一个候选；
- 选择“使用原话 / 人工填写”；
- 提交后进入不同账号独立复核。

选择候选会记录 `credit_code`、`source_type=qcc_mcp`、`verification_method=human_confirmed_qcc`、操作者和时间；不要求额外填写客户归属依据。企业抬头变更属于高影响修正，不能由同一账号完成复核。

## 8. 缓存、限流与错误处理

- 缓存：`var/qcc-cache.json`，键为归一化查询、信用代码或重排输入哈希。默认 `QCC_CACHE_DAYS=0`，每次分析都重新调用企查查 MCP；设置 `QCC_CACHE_DAYS=30` 可启用缓存，降低延迟和额度消耗。
- 每次分析最多 2 次搜索 + 最多 3 次详情；默认对前三名候选补充工商详情。
- 限流：请求间隔至少 0.5 秒。
- 超时：默认 8 秒，可配置；超时/网络错误重试 1 次。
- 429/5xx 重试 1 次；401/403 直接失败，不重试。
- 任何 QCC 错误都回退到“保留原话 + `lookup_failed`”，不让整个分析失败。

未实现跨进程熔断；单进程内通过超时、重试和缓存控制影响面。

## 9. 安全与合规

- token 只在服务端读取，页面不回显。
- 日志不记录 token 和上游完整响应。
- 只向企查查发送企业名称/信用代码；重排会把客户上下文发送给已配置的文字分析模型，发送前对完整手机号做占位符替换，但姓名、企业名称和业务内容仍可能保留。
- 演示数据和真实数据分开标注；企查查返回的真实企业资料在合成通话中也会标注来源。
- 企业抬头确认不替代其他事实的证据校验，也不替代正式交付门槛。

## 10. 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/smoke_qcc.py --query 木里木外
.venv/bin/python scripts/evaluate_entities.py
node --check static/app.js
```

`tests/test_qcc_mcp.py` 使用 `httpx.MockTransport` 覆盖普通 JSON、SSE、401、畸形 JSON、缓存和超时；`tests/test_entity_rerank.py` 覆盖模型重排解析、缓存、未知候选拒绝和缺少模型时跳过；`tests/test_entity_resolver.py` 覆盖多候选、精确自动确认、品牌不自动确认、重排失败回退、无匹配和检索失败；`tests/test_entity_review.py` 覆盖选择候选、无需归属依据和独立复核。

真实冒烟使用公开企业名，不发送客户通话。`eval/entity_cases.json` 目前有 7 条（6 条有预期主体），开发时实测 Top-1、Top-3、候选召回均为 6/6；这是小型冒烟集，不是冻结盲测或业务准确率。实际结果会受企查查数据、配额和网络影响。

## 11. 已知限制

- “木里木外”会返回品牌方、同名主体和分支机构多个候选；品牌简称不会自动确认，只有唯一完整全称 exact 才会自动确认。
- 客户说“杭州”，企查查第一名可能在启东；地区不一致必须在审核页展示，不能静默采用。
- 企查查的“多候选”提示和返回条数不一定完全一致，仍以候选列表和人工确认为准。
- 数据更新、配额、字段变化由外部服务决定；失败时只能保留原话。
- 当前没有把人工选择自动发布为全局别名；选择只作用于本次记录，避免一次错误扩散。
