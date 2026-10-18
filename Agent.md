# Agent 项目协作手册（给改代码的 AI / 未来的自己）

> 这是一份操作手册，不是设计文档。改代码前先读这一页；不确定的约定以 `src/` 源码为准，不要凭记忆臆造。

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
.venv\Scripts\python.exe -m pytest -q   # 应 175 passed
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
# 全量测试（当前 175 passed，改完必须保持全绿）
.venv\Scripts\python.exe -m pytest -q

# 起后端
.venv\Scripts\python.exe -m uvicorn app.api:app --port 8017

# 前端
cd frontend; npm run dev      # http://localhost:5173
cd frontend; npm run build     # 产物给 nginx 反代 /api

# CLI 主流程
.venv\Scripts\python.exe main.py batch --limit 5          # 批量分析（停在审批节点）
.venv\Scripts\python.exe main.py review --request-id <id> --decision approve
.venv\Scripts\python.exe main.py resume --decision approve|hold|reject   # 恢复中断线程
.venv\Scripts\python.exe main.py schedule --retry         # 排期+建会+通知，可 --fail-notify/--fail-meeting 注入故障
.venv\Scripts\python.exe main.py export --redact          # 导出；--redact 脱敏后才可外发
.venv\Scripts\python.exe main.py purge --days 30 --dry-run  # 保留期清理；先 dry-run 看清单再删
.venv\Scripts\python.exe main.py clean --document_id <id> # 按文档删除
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

- `parse` / `validate` 失败不中断流程，而是**条件路由进 `review` 转人工**，绝不静默丢弃。
- `review` 节点 `interrupt` 等待人工审批；`Command(resume=decision)` 恢复后进入 `apply` 落库。
- 排期是独立子链：`create_round → close_round → schedule_round（按总分>MiC>海外>管理排序 + 原子占槽）→ run_external（建会→通知）`；**建会/通知失败只重试失败动作**，不整体重跑。

### 4.2 证据引用铁律（最高优先级）

- 每条结论必须带 `quote`（简历原文切片）+ 程序计算的字符偏移，可回溯到原文对应位置。
- 硬条件三态：`pass / fail / unknown`；**unknown = 沉默 ≠ 通过**，一律降级转人工复核。
- 模型抽取受两道防线约束：① 领域词表一致性校验（引文必须语义支撑所贴档位，否则降级 UNKNOWN）；② 任职年限引文校验（见 `tests/test_t11`）。

### 4.3 幂等

- 审批以 `request_id` 幂等消费，重复提交不产生副作用。
- 排期对同一 `(round, candidate)` 唯一约束，冲突不重复占槽。
- 通知 / 建会重试天然幂等，`--retry` 只补失败动作。

### 4.4 身份与多租户边界

- 三类身份严格分离：**文档身份（document_id）/ 评估版本键（version）/ 执行线程身份（thread_id）**。
- `documents` 表已预留 `org_id` 列，为多租户（P2）留边界；当前单租户，不要提前实现多租户逻辑。

### 4.5 PII 红线

- **真实简历目录、`eval/labels/`、`eval/results/` 永不入库、永不写进任何文档或提交。**
- `documents` 表的 `pii_name / pii_phone / pii_email` 走字段级 Fernet 加密（密钥读 `PII_ENCRYPT_KEY`）。
- `.env` 不进版本控制；需要新配置项先加到 `.env.example`。

## 5. 改代码的规则

1. **保持 pytest 全绿**（当前 175 passed）。任何提交前跑一次全量。
2. **新功能必须配测试**，命名 `tests/test_tN_*.py`（N 对应需求条目）。
3. **真实简历 / PII 不得写进仓库或文档**；演示一律用 `demo/` 合成数据。
4. 不要破坏两个开关的语义：`AUTH_ENABLED`（默认关，单用户认证）、`PII_ENCRYPT_KEY`（PII 字段加密）。
5. 数据访问只走 `src/repository.py` 这一层边界，不要在节点里裸写 SQL。
6. 不使用年龄、性别、婚育、民族等受保护属性做评分。

## 6. 评测与安全现状

- 离线评测在 **7 份真实简历**上跑：mock 基线 F1 0.36 vs 本模型 F1 0.73；指标含 P/R/F1、转人工率、成本与延迟 P50/P95、跨运行一致性。聚合结果脱敏后放 `eval/published/`。
- 注入防线：领域词表一致性校验 + 任职年限引文校验（`test_t11`），模型若编造档位会被降级转人工。
- PII 四件套：字段加密 / 上传即提示 / 保留期 `purge` / 导出 `--redact`。
- 认证：默认关闭，可配置开启（单用户、pbkdf2、签名 cookie、启动 fail-fast 校验密钥）。

## 7. 已知边界（别当成 bug 修）

- 单用户本地部署定位，未做多用户/多租户。
- `scheduler / calendar / notifier` 均为 **mock 实现**，真实接入换 `providers/` 即可。
- Docker 未实机构建验证：`compose` 需 v2.24+，前端镜像用 nginx 反代 `/api`。
- OCR：tesseract 可选，缺失时自动回退 rapidocr。

## 8. 易错点速查

- 排期候选排序键：**总分 > 是否 MiC/钢结构直接经验 > 海外经验 > 管理经验**，同分时按此优先级决定谁占 45 分钟时段。
- 解析失败 / 校验失败不是报错退出，而是路由进 `review` 转人工——别把它当异常重试。
- `review` 提交后线程处于 interrupt 态，必须用对应 `--decision` 的 `resume` 才会继续到 `apply/report`。
- 想外发结果先 `export --redact`；不带 `--redact` 的导出含明文 PII，仅限本地看。
- 想演示幂等 / 重试：`schedule --fail-notify N --fail-meeting N --retry`，观察只补失败动作。
- 改完任何东西，第一件事永远是跑一次 `pytest -q`，不要先提交。

---

*项目起源于 AI Agent 工程师二面现场题：基于多智能体框架对 214 份 PDF 简历完成解析→筛选→评分→复核→排期→建会通知的完整链路。*
