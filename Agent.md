# Agent 项目协作手册（给改代码的 AI / 未来的自己）

> 这是一份操作手册，不是设计文档。改代码前先读这一页；不确定的约定以 `src/` 源码为准，不要凭记忆臆造。

> **2026-10-29 收尾定位**：企业面试场景的工程作品集。当前以三条成果的缺陷修复、有限前端整理和已有证据为完成范围，不继续扩成招聘产品。当前说明以 [README](README.md)、[架构与实现限制](docs/architecture.md)、[工程参考](docs/engineering-reference.md) 为准；历史规格与 HTML 升级报告不作为新任务授权。

## 1. 这个项目是什么

基于 **LangGraph** 的 AI 简历筛选 Agent，目标岗位是「海外项目负责人 · 钢结构 / MiC 模块化建筑」。核心链路：PDF 解析 → 证据抽取 → 硬条件校验 → 评分 → **人工复核（interrupt）** → 审批落库 → 排期 → 建会通知。

**技术栈**：Python 3.12 · LangGraph（SqliteSaver checkpointer）· FastAPI · SQLite(WAL) · React18 + TypeScript + Vite · MCP server（stdio / StreamableHTTP）。

### 1.1 首次环境准备

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env      # 再填 PII_ENCRYPT_KEY 等密钥；.env 不入库
cd frontend; npm install; cd ..
.venv\Scripts\python.exe -m pytest -q   # 应 195 passed
```

## 2. 目录职责速览

| 路径 | 一句话职责 |
| --- | --- |
| `main.py` | CLI 入口（analyze/batch/review/resume/schedule/export/clean/purge/graph） |
| `app/api.py` | FastAPI 后端，REST + SSE，端口 8017 |
| `mcp_server.py` | MCP 工具服务，把 API 暴露给外部 Agent（stdio / http） |
| `demo/` | 合成演示数据与脚本（非真实简历，可安全入库演示） |
| `eval/` | 离线评测；`published/` 放脱敏后的聚合指标，**原始结果不入库** |
| `docs/` | 报告、工作流图与截图 |
| `frontend/` | React18 + TS + Vite 前端（dev 5173，构建后由 nginx 反代 `/api`） |
| `src/workflow.py` | LangGraph 图定义、节点链、条件路由、interrupt 恢复 |
| `src/state.py` | 共享 State schema |
| `src/repository.py` | **唯一数据访问边界**，所有 SQL 走这里（Postgres 迁移友好） |
| `src/observability.py` | 日志 / 指标 / 成本与延迟埋点 |
| `src/batch.py` | 批量批次编排 |
| `src/parser.py` | PDF 解析（tesseract 可选，rapidocr 回退） |
| `src/extraction.py` | 字段 / 证据抽取，含领域词表一致性校验 |
| `src/scheduler.py` | 排期：排序 + 原子占槽（mock 日历） |
| `src/review.py` | 人工审批幂等消费 |
| `src/config.py` | 环境变量开关集中读取 |
| `src/schemas.py` | 数据契约（与 state 区分） |
| `src/task_store.py` | 任务身份 / 线程映射 |
| `src/pii.py` | 字段级 Fernet 加解密 |
| `src/auth.py` | 单用户认证（pbkdf2 + 签名 cookie，fail-fast） |
| `src/providers/` | 外部依赖适配（日历 / 通知器 / 模型，均 mock 可替换） |

## 3. 常用命令

Windows PowerShell，虚拟环境已建好：

```powershell
# 全量测试（当前 195 passed，改完必须保持全绿）
.venv\Scripts\python.exe -m pytest -q

# 起后端
.venv\Scripts\python.exe -m uvicorn app.api:app --port 8017

# 前端
cd frontend; npm run dev      # http://localhost:5173
cd frontend; npm run build     # 产物给 nginx 反代 /api

# CLI 主流程
.venv\Scripts\python.exe main.py batch --limit 5          # 批量分析（停在审批节点）
.venv\Scripts\python.exe main.py review <assessment_id> approve --request-id <request_id>
.venv\Scripts\python.exe main.py resume <run_id>           # 恢复中断线程或补收尾
.venv\Scripts\python.exe main.py schedule <batch_id> --retry # 可 --fail-notify/--fail-meeting 注入故障
.venv\Scripts\python.exe main.py export <batch_id> --redact # 附加打码的 PII 列；不等于全面匿名化
.venv\Scripts\python.exe main.py purge --days 30 --dry-run  # 保留期清理；先 dry-run 看清单再删
.venv\Scripts\python.exe main.py clean <document_id>       # 按文档删除
.venv\Scripts\python.exe main.py graph --png             # 导出工作流图

# MCP server
.venv\Scripts\python.exe mcp_server.py                       # stdio（默认，供 MCP 客户端连接）
.venv\Scripts\python.exe mcp_server.py --transport http --port 8018  # StreamableHTTP：/mcp
```

`RESUME_API_BASE` 环境变量可让 MCP server 指向别的后端地址（默认 `http://127.0.0.1:8017`）。

## 4. 架构与核心约定

### 4.1 LangGraph 节点链

```
parse → extract → validate → screen → score → review(interrupt) → apply → report → END
```

- `parse` / `validate` 失败先经 score 保存未知评估（公开得分 null），再进入 review 人工暂停，不对失败输入进行数值评分。
- `review` 节点 `interrupt` 等待人工审批；审批服务先持久化审批，再通过 `Command(resume=decision)` 恢复到 `apply/report`。
- 排期是独立子链：`create_round → close_round → schedule_round（按总分>MiC>海外>管理排序 + 原子占槽）→ run_external（建会→通知）`；**建会/通知失败只重试失败动作**，不整体重跑。

### 4.2 证据引用铁律（最高优先级）

- 新分析的每条事实必须带 `quote`（简历原文切片）+ 程序计算的字符偏移，可回溯到原文对应位置。
- 硬条件三态：`pass / fail / unknown`；**unknown = 沉默 ≠ 通过**，一律降级转人工复核。
- 模型抽取包含两项有限检查：① 引文包含该事实领域的证据词，否则部分事实降级 UNKNOWN；② 任职年份可在引文定位（见 `tests/test_t11`）。这不等同于证明语义足以支持具体档位。

### 4.3 幂等

- 审批以 `request_id` 幂等消费，重复提交不产生副作用。
- 排期对同一 `(round, candidate)` 唯一约束，冲突不重复占槽。
- 通知 / 建会重试天然幂等，`--retry` 只补失败动作。

### 4.4 身份与多租户边界

- 文档身份按内容区分；执行线程包含批次、文档与配置身份；每次执行保存独立评估快照。审批绑定 assessment_id 与岗位版本，不覆盖旧评估，也不能把旧审批用于新执行。
- `documents` 表已预留 `org_id` 列，为多租户（P2）留边界；当前单租户，不要提前实现多租户逻辑。

### 4.5 PII 红线

- **真实简历目录、`eval/labels/`、`eval/results/` 永不入库、永不写进任何文档或提交。**
- `documents` 表的 `pii_name / pii_phone / pii_email` 走字段级 Fernet 加密（密钥读 `PII_ENCRYPT_KEY`）。
- `.env` 不进版本控制；需要新配置项先加到 `.env.example`。

## 5. 改代码的规则

1. **保持 pytest 全绿**（当前 195 passed）。任何提交前跑一次全量。
2. **新功能必须配测试**，命名 `tests/test_tN_*.py`（N 对应需求条目）。
3. **真实简历 / PII 不得写进仓库或文档**；演示一律用 `demo/` 合成数据。
4. 不要破坏两个开关的语义：`AUTH_ENABLED`（默认关，单用户认证）、`PII_ENCRYPT_KEY`（PII 字段加密）。
5. 数据访问只走 `src/repository.py` 这一层边界，不要在节点里裸写 SQL。
6. 不使用年龄、性别、婚育、民族等受保护属性做评分。

## 6. 评测与安全现状

- 历史评测（2026-09-18）在 **7 份 dev 简历**上跑：规则 F1 0.36 vs 模型 F1 0.73；仅代表当次事实抽取，不代表当前代码重测或招聘决策质量。聚合结果在 `eval/published/`；历史 review_rate 是抽取警告触发率，不是实际人工复核率。
- 有限注入风险缓解：领域词表与任职年份引用检查（`test_t11`），不等同于完整语义验证或全面防注入。
- PII：仅 `documents.pii_*` 三列加密，原文等仍明文；上传提示是粗扫；`purge` 需显式执行；`--redact` 只额外附加打码列，不是全面匿名化。
- 认证：默认关闭，可配置开启（单用户、pbkdf2、签名 cookie、启动 fail-fast 校验密钥）。

## 7. 运行边界与已确认限制

- 单用户本地部署定位，未做多用户/多租户。
- `calendar / notifier` 为 **mock 实现**；scheduler 是真实规则排期器，真实接入换 `providers/` 即可。
- Docker 未实机构建验证：`compose` 需 v2.24+，前端镜像用 nginx 反代 `/api`。
- OCR：tesseract 可选，缺失时自动回退 rapidocr。
- 证据快照、文件内容版本、审批绑定与失败重试已修复；历史数据、单进程任务与隐私边界见 [架构说明](docs/architecture.md#收尾修复与当前实现限制)。
- CLI 决策与内部枚举一致：approve / hold / needs_review。MCP 在配置认证后会登录取得 Cookie，审批工具必须传递所查看的评估身份。

## 8. 易错点速查

- 排期候选排序键：**总分 > 是否 MiC/钢结构直接经验 > 海外经验 > 管理经验**，同分时按此优先级决定谁占 45 分钟时段。
- 解析失败 / 校验失败不是报错退出，而是路由进 `review` 转人工——别把它当异常重试。
- `submit_review` 会尝试恢复图；`resume <run_id>` 用于中断恢复，不是每次审批后的必需步骤。
- 默认导出不附加三个独立 PII 列；`--redact` 附加打码列。文件名、报告等仍可能含个人信息，不能据此承诺完整脱敏。
- 现有幂等 / 重试演示：`schedule <batch_id> --fail-notify N --fail-meeting N --retry`，观察只补失败动作。
- 改完任何东西，第一件事永远是跑一次 `pytest -q`，不要先提交。

---

*项目起源于 AI Agent 工程师二面现场题：基于多智能体框架对 214 份 PDF 简历完成解析→筛选→评分→复核→排期→建会通知的完整链路。*

当前收尾验收见 [docs/acceptance.md](docs/acceptance.md)。新增场景测试放在 T16/T17；旧 T8 的“GET 现场抽取”断言已经替换为“GET 仅读取持久快照”，不要恢复旧行为。
