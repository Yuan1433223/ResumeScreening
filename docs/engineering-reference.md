# 工程参考

核对日期：2026-10-29。当前架构与实现限制见 [架构说明](architecture.md)；本页保留代码导航、配置和命令，避免 README 承担开发档案的全部内容。

## 代码地图

> 想改某块功能时，先定位这一层，再下钻。每条 = 关键文件/类 + 一句话职责 + 对外契约。

**入口**

- `main.py` — CLI 闭环（`analyze/batch/review/resume/schedule/export/clean/graph`）。
- `app/api.py` — FastAPI 工作台后端；契约：`/api/auth/*`（login/logout/me）、
  `/api/upload`、`POST /api/batches`、`GET /api/batches/tasks/{id}`、
  `GET /api/batches/tasks/{id}/events`（SSE）、`GET /api/dashboard`、`GET /api/traces`、
  `/api/documents/{id}/review|approve|file`、`/api/scheduling*`。

**工作流层**

- `src/workflow.py` — LangGraph 节点链 `parse→extract→validate→screen→score→review→apply→report`；
  `review` 用 `interrupt` 挂起，`SqliteSaver` 持久化线程。
- `src/state.py` — LangGraph 共享状态（含 `skip_parse` 测试注入开关）。
- `src/scheduler.py` — 排期器：冻结名单排序、原子占槽、外部建会/通知动作。
- `src/review.py` — 审批服务：版本校验、`request_id` 幂等、恢复未消费审批。

**数据与安全层**

- `src/repository.py` — SQLite 单点边界 + 只读聚合（KPI/分布/漏斗 SQL 在此下沉）；连接级 RLock。
- `src/task_store.py` — 批次任务落 `batch_tasks` 表，跨进程可读；同时是 SSE 的数据源。
- `src/pii.py` — PII 提取/打码 + Fernet 字段级加解密（透明加解密在 repository 层）。
- `src/auth.py` — 会话 Cookie 签名、pbkdf2 密码哈希、`get_current_user` 依赖。
- `src/config.py` — env 装配（岗位画像/评分表/路径/MODEL_*/AUTH_*/PII_*）。

**抽取与评测**

- `src/providers/` — `mock_extractor`（规则）/ `model_extractor`（云端大模型，含有限领域词表与年份检查）
  / `mock_calendar` / `mock_notifier`。
- `src/extraction.py` — 抽取服务层入口，mock/model 分派。
- `eval/` — `run_eval.py`（runner）+ `published/`（脱敏聚合摘要，可入库对外发布）。

**前端**

- `frontend/src/pages/` — `BatchesPage` / `ReviewPage` / `SchedulingPage` /
  `DashboardPage` / `TracePage` / `LoginPage` 六页，分别对应上述 API。
- `frontend/src/api.ts`、`types.ts` — 前端 HTTP 客户端与 TS 契约。

**部署与集成**

- `mcp_server.py` — MCP 服务（tools/resources/prompts，stdio 或 HTTP，薄代理后端 8017）。
- `Dockerfile` / `docker-compose.yml` / `frontend/Dockerfile` + `nginx.conf`
  — 后端镜像 + 前端多阶段构建（nginx 反代 `/api`）；compose v2.24+。

**精简调用链**

```
上传（后缀→魔数→20MB 校验，任一失败不落盘）
  → 创建批次（任务落 task_store，SSE 推进度）
  → parse/extract（证据 quote=原文切片，有限一致性检查）
  → 复核（左原文右事实 + 证据点击高亮 + 8 维雷达）
  → 审批（服务层 request_id 幂等入库后恢复图；前端未传幂等键）
  → 独立发起模拟排期（冻结→排序→原子占槽→建会→通知，CLI 可重试失败动作）
```

## 评分规则

| 维度 | 满分 | 分档 |
| --- | ---: | --- |
| 直接钢结构/MiC/模块化经验 | 30 | 参与 10 / 承担交付责任 20 / 主导并有成果 30 |
| 钢结构项目全流程 | 15 | 1 阶段 5 / 2 阶段 10 / ≥3 且含交付 15 |
| 海外项目/跨国协作 | 15 | 跨国协作 5 / 海外项目职责 10 / 海外交付 15 |
| 项目/团队管理 | 15 | 管理职责 5 / 职责对象明确 10 / 另有范围或结果 15 |
| 生产/质量/安全/体系 | 10 | 一项 5 / 两项且有举措 10 |
| 英语/跨文化沟通 | 5 | 能力信息 2 / 业务使用实例 5 |
| 项目规模/量化成果 | 5 | 有单位有所属项目的数值 5 |
| 岗位适配/机动性 | 5 | 接受工作地点 2 / 另接受出差驻场 5 |

未提及维度 score=null、status=unknown；有明确反向事实计 0；known_score 仅累加已确认项。

## 环境变量字典

> 与 `src/config.py` + `.env.example` 逐条对齐；`RESUME_API_BASE` 来自 `mcp_server.py`。
> 所有键均为环境变量，复制 `.env.example` 为 `.env` 后按需填写（`.env` 不入版本控制）。

| 键 | 用途 | 默认值 | 出处 | 是否必填 |
| --- | --- | --- | --- | --- |
| `RESUME_DIR` | 简历库目录，可用于隔离验收 | 项目根目录内 `海外项目负责招聘简历/` | `src/config.py` | 否 |
| `ANALYSIS_DATE` | 任职年限计算截止日，可固定历史口径 | 启动进程的本地日期 | `src/config.py` | 否 |
| `RESUME_DB_PATH` | 业务 SQLite 路径 | `data/resume.db` | `src/config.py` | 否 |
| `RESUME_CHECKPOINT_PATH` | LangGraph 检查点 SQLite 路径 | `data/resume.checkpoints.db` | `src/config.py` | 否 |
| `MODEL_ENABLED` | 云端模型抽取开关（1/true/yes 开） | `0`（关，规则版） | `src/config.py` / `.env.example` | 否 |
| `MODEL_PROVIDER` | provider 名（自由文本，仅标识） | 空串 | `src/config.py` / `.env.example` | 否 |
| `MODEL_BASE_URL` | OpenAI 兼容 `/chat/completions` 地址 | 空串 | `src/config.py` / `.env.example` | 开模型时必填 |
| `MODEL_API_KEY` | 云端 API 密钥（`SecretStr` 防日志泄露） | 空串 | `src/config.py` / `.env.example` | 开模型时必填 |
| `MODEL_NAME` | 模型名（如 deepseek-chat） | 空串 | `src/config.py` / `.env.example` | 开模型时必填 |
| `MODEL_TIMEOUT_SECONDS` | 单次请求超时（秒） | `60` | `src/config.py` / `.env.example` | 否 |
| `MODEL_MAX_RETRIES` | 429/5xx 重试次数 | `2` | `src/config.py` / `.env.example` | 否 |
| `MODEL_MAX_COST_CENTS` | 调用后估算费用的告警阈值（美分），0=不限；非调用前硬限额 | `0` | `src/config.py` / `.env.example` | 否 |
| `MODEL_PROMPT_VERSION` | 提示词版本键 | `p1` | `src/config.py` / `.env.example` | 否 |
| `MODEL_TEMPERATURE` | 采样温度 | `0` | `src/config.py` / `.env.example` | 否 |
| `MODEL_COST_PER_1K_INPUT_CENTS` | 输入计费（美分/千 token），填了才估成本 | `0` | `src/config.py` / `.env.example` | 否 |
| `MODEL_COST_PER_1K_OUTPUT_CENTS` | 输出计费（美分/千 token） | `0` | `src/config.py` / `.env.example` | 否 |
| `PII_ENCRYPT_KEY` | Fernet 对称密钥（base64 urlsafe 32 字节）；空=新增 PII 列明文写入+启动警告 | 空串（明文模式） | `src/config.py` | 否（生产建议填） |
| `PII_RETENTION_DAYS` | 简历保留天数，早于此由 `purge` 清理；0=禁用默认保留期；清理需显式执行 purge | `90` | `src/config.py` | 否 |
| `AUTH_ENABLED` | 认证开关（1/true/yes 开） | `0`（关） | `src/config.py` / `.env.example` | 否 |
| `AUTH_USERNAME` | 登录用户名；开启认证后必填 | 空串 | `src/config.py` / `.env.example` | 开认证时必填 |
| `AUTH_PASSWORD` | 登录密码（运行时 pbkdf2 哈希比对，不落库）；开启认证后必填 | 空串 | `src/config.py` / `.env.example` | 开认证时必填 |
| `AUTH_SECRET_KEY` | 会话 Cookie 签名密钥；空则由 `AUTH_PASSWORD` 派生并告警 | 空串（派生） | `src/config.py` / `.env.example` | 否（生产建议显式填） |
| `RESUME_API_BASE` | MCP 薄代理的后端地址 | `http://127.0.0.1:8017` | `mcp_server.py` | 否 |

> 生成 Fernet 密钥：`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`。

## CLI 与 MCP

以下 `<...>` 表示从分析结果取得的实际 ID，执行时替换。`review` 会尝试恢复任务；`resume` 用于中断恢复或补收尾，不是每次审批后都必须执行。

```powershell
python main.py analyze "demo/samples/zhang_wei.pdf" --batch-id portfolio
python main.py batch --dir demo/samples --limit 4
python main.py review <assessment_id> approve --version v1 --request-id req001
python main.py resume <run_id>
python main.py schedule <batch_id> --fail-notify 1 --retry
python main.py export <batch_id>
python main.py export <batch_id> --redact
python main.py purge --days 30 --dry-run
python main.py clean <document_id>
python main.py graph --png
```

CLI 支持 `approve / hold / needs_review`，与内部枚举一致。`graph --png` 可能依赖外部渲染服务，失败时回退输出 Mermaid 文本；文档中的架构 SVG 可直接离线阅读。

默认导出不附加三个独立 PII 列；`--redact` 额外附加打码后的姓名、手机号和邮箱。它不会全面清洗报告、文件名或原文中的所有个人信息，因此不能把该开关当作完整匿名化证明。

```powershell
# 先运行 FastAPI，再启动 MCP（默认 stdio）
python mcp_server.py
# 或仅在本机监听 HTTP
python mcp_server.py --transport http --port 8018
```

MCP 是 API 薄代理，不能直接当作新的 LangGraph deployment server 使用。代理使用 Cookie 会话；后端返回 401 且本地开启 AUTH_ENABLED 时，用同一配置中的用户名和密码登录后重试一次。不会通过未认证旁路访问业务接口。

## 验证基线

2026-10-29 当前代码：`python -m pytest -q` 为 **194 passed**；`frontend` 下 `npm run build` 通过，首页主脚本约 180 KB，辅助看板单独按需加载。测试不调用真实模型或真实会议服务，构建成功也不代表 Docker 已验证。

## 文档分工

- [README](../README.md)：项目定位、能力、界面与启动入口。
- [架构说明](architecture.md)：真实流程、模型边界和已知实现限制。
- [前端展示取舍](frontend-options.md)：开源参考和迁移必要性。
- [协作手册](../Agent.md)：维护约定。
- [原始开发规格](../DEVELOPMENT_SPEC.md)：历史设计记录，不是当前待办。
- `architecture-upgrade.html`、`competitive-benchmark.html`：历史探索报告，保留旧内容并醒目标记；其中升级路线和成熟度评分不代表当前承诺。


## 审批请求与旧数据兼容

POST `/api/documents/{document_id}/approve` 的端点保持不变；请求必须包括以下字段：

```json
{
  "assessment_id": "从 GET review 的 assessment.id 取得",
  "expected_version": "从 GET review 的 assessment.job_version 取得",
  "request_id": "客户端为这次决定保存的稳定唯一键",
  "decision": "approve",
  "reason": "人工核对理由"
}
```

缺少评估身份返回 428；旧页面提交到更新评估、复用幂等键改决定、已关闭批次新增决定等返回 409。审批保存后恢复失败返回 `recovery_pending=true`，客户端保留相同键和原决定重试；不会新增一条审批。当前前端和 MCP 客户端均传递该契约。

历史评估仍可读取，但没有快照时显示说明，不在 GET 内重抽取。新执行使用独立评估 ID，不覆盖旧事实和旧审批。原文件按内容存于业务库同级 `documents/` 目录；原展示文件名单独保存。修改输入文件不会改变旧评估所展示的原文。该目录含原文，适用与业务库相同的数据保护边界；`purge --delete-files` 才会删除对应文件。

GET `/api/documents/{id}/review?assessment_id=...` 可只读查看特定历史评估。GET `/api/scheduling` 与 POST `/api/scheduling/run` 接受可选 `batch_id` 查询参数，默认沿用最近批次。重复 POST 返回已有预约并补失败动作，当前前端会固定到所选批次。单进程启动时将遗留 running 任务标记为 interrupted；不提供分布式自动续跑。
