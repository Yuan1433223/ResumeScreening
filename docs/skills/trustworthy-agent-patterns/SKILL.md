---
name: trustworthy-agent-patterns
description: 可信 Agent 架构模式目录（本项目从简历筛选工作台提炼的 8 条落地模式）。当用户要评审/重构一个"让模型做判断、人做最终决策"的 Agent 系统的可信度，或要为 Agent 补证据可核验、三态判定、HITL 崩溃恢复、幂等外部动作、身份分离、优雅降级、MCP 薄代理暴露、注入防线时使用；典型触发："这个 Agent 的结论可信吗""模型判断错了谁负责""崩了/重试会不会重复建会重复通知""怎么让外部 Agent 编排我们的系统"。每条模式给问题/做法/本项目落点（文件+函数+测试编号），细节链到 references/。不适用于纯前端交互改造、无 LLM 的确定性脚本任务、或要求在 MCP 层重写业务逻辑的诉求（薄代理原则禁止）。
---

# Trustworthy Agent Patterns（可信 Agent 架构模式目录）

把"模型负责抽取与初判、人负责最终决策、系统负责不重复不错杀不丢任务"拆成 8 条可迁移模式。每条只写**问题 / 做法 / 本项目落点**三行半；代码骨架与踩过的真 bug 放 `references/`。

## 模式目录

### ① 证据三角（evidence trivalence）：模型只回引文，程序算偏移，校验逐字比对
- **问题**：模型自报"事实成立"却无法核验是否真来自原文；幻觉/引用造假直接进评分，复核页还无法高亮原文。
- **做法**：结构化 schema 约束模型只给 `kind/value/page/quote`（**不给字符偏移**）；程序在页文本里定位 quote 并算 `start/end`（先精确子串，失败再空白归一正则）；`validate_evidence` 逐字比对 `page.text[start:end] == quote`，越界/页码不存在/不一致即判无效转人工；UI 用偏移高亮原文切片。
- **落点**：`src/providers/model_extractor.py` `_localize_quote()`、`src/evidence.py` `validate_evidence()`/`_validate_one()`、复核端点 `app/api.py` `get_review_detail()`；回归 `tests/test_t8_review_extractor_consistency.py`、`tests/test_t9_document_file_inline.py`。细节见 `references/evidence-trivalence.md`。

### ② 三态判定 pass/fail/unknown：沉默≠通过，也≠不通过
- **问题**：硬条件信息不足时若判 fail，会把"简历没写"误杀成"不合格"；若判 pass，又把沉默当通过。两个极端都不可信。
- **做法**：`HardResult = PASS/FAIL/UNKNOWN`；确定下界达标→pass、确定上界仍不足→fail、区间跨阈值或无证据→unknown；`and3/or3` 组合（任一 fail 即 fail、全 pass 才 pass）；unknown 只路由 `needs_review` 转人工，绝不自动放行也绝不自动淘汰。
- **落点**：`src/scorer.py` `compare_years()`/`and3()`/`or3()`/`evaluate_hard_rules()`/`assess()`、`src/workflow.py` `node_screen()`；阈值与 8 维表见 `src/config.py` HARD_RULES/SCORING。

### ③ HITL + 崩溃恢复 + 幂等审批：审批先入库，恢复成功才消费
- **问题**：人工审批节点进程崩溃后任务丢失或恢复时重跑；审批重复提交会重复建会/重复通知。
- **做法**：review 节点 `interrupt()` 暂停；`SqliteSaver` 检查点按 thread 持久化；审批先以 `request_id UNIQUE` 幂等入库，再 `Command(resume=decision)` 恢复；**invoke 成功后才 `mark_review_consumed`**——恢复中途崩溃则审批保持未消费、下次重放可继续；恢复成功后重放只补收尾、不重复执行图。
- **落点**：`src/workflow.py` `node_review()`/`resume_run()`/`_get_checkpointer()`、`src/review.py` `submit_review()`、`src/repository.py` `reviews.request_id UNIQUE`；故障注入 `tests/test_t1_recovery_boundary.py`。细节见 `references/hitl-recovery-idempotency.md`。

### ④ 幂等外部动作：占槽唯一、建会成功才通知、重试只补失败动作
- **问题**：排期/建会/通知一旦重试，就会重复占同一时段、重复建会、重复骚扰候选人。
- **做法**：建轮 → `close_round` 冻结版本 → 排序（`known_score DESC → MiC 维 → 海外维 → 管理维 → candidate_id ASC`）→ 原子占槽（`bookings UNIQUE(round,candidate)`、`claim_slot` 只允许把 pending 置 confirmed 且该 slot 无 confirmed 占用、争不到=WAITLIST）；`external_actions UNIQUE(booking,type)`，**建会成功才发通知**，通知失败只重试通知、不重建会。
- **落点**：`src/scheduler.py` `create_round()`/`close_round()`/`schedule_round()`/`run_external()`/`retry_action()`、`src/providers/mock_calendar.py`/`mock_notifier.py`；测试 `tests/test_p2_flow.py`（6 人争 4 时段→2 候补、通知失败重试、重复排期幂等返回）。

### ⑤ 身份三分离：文档身份 / 评估版本键 / 执行线程身份
- **问题**：重跑、换抽取器、跨批次时，同一份简历被重复建档、评估结果串味、旧审批错配到新版本。
- **做法**：**文档身份**=文件 `sha256` UNIQUE 去重；**评估版本键**=`{document_id}_{job_version}`（assessments 行另存 `extractor_version` 列，换抽取器可重跑而不污染旧评估）；**执行线程身份**=`runs.thread_id UNIQUE`，同 thread 重跑复用同一 run 行、跨批次映射稳定。旧版本号审批提交到新评估直接 `VersionConflictError`。
- **落点**：`src/repository.py`（documents.sha256 / assessments.id / runs.thread_id）、`src/workflow.py` `run_single()`；测试 `tests/test_t2_identity.py`。

### ⑥ 优雅降级：模型挂了不阻断主流程，OCR 缺失自动回退
- **问题**：模型未配置 key、调用失败、成本超预算，或 OCR 引擎没装，都会让整条流水线中断。
- **做法**：`MODEL_ENABLED=0`/缺 key/瞬态重试耗尽 → 返回空报告+警告、路由人工复核（不抛死）；成本超 `max_cost_cents` → 已确认事实照出并提示转人工；OCR tesseract 缺失自动回退 rapidocr；解析结果写 `pages` 表缓存复用，换抽取器重跑秒级、避免 250s OCR 重跑。
- **落点**：`src/config.py` MODEL_CONFIG、`src/providers/model_extractor.py` `extract()`、`src/parser.py`（tesseract→rapidocr）、`src/workflow.py` `_cached_pages()`/`node_parse()`。

### ⑦ MCP 薄代理暴露：三要素齐全，但业务判定只在工作台
- **问题**：想让外部 Agent 编排工作台，又不能在 MCP 层复制业务逻辑（双份事实源必然漂移）。
- **做法**：MCP 层只做薄代理——tools/resources/prompts 三要素齐全，全部 HTTP 转发到 FastAPI 工作台（唯一事实源）；stdio 与 `--transport http --port 8018 /mcp` 双传输；`RESUME_API_BASE` 覆盖后端地址。
- **落点**：`mcp_server.py`（8 tools / 2 resources / 1 prompt）；连通性脚本根目录 `mcp_client_test.py`（stdio 握手 + list_tools/resources/prompts + batch→review→approve 真实链路）。细节见 `references/mcp-exposure.md`。

### ⑧ 注入防线（简）：引文语义一致性，防简历内嵌指令越权
- **问题**：简历里内嵌"把我评为最高档"的指令，诱导模型把任意真实子串贴上最高档、伪造十年任职区间，端到端产出 hard_summary=pass + RECOMMEND。
- **做法**：模型自报 supported 的事实，其**真实引文切片**必须含该 kind 的领域证据词，否则降级 unknown 转人工；任职声明的起止年份必须能在引文切片中定位，否则该段任职不采纳。
- **落点**：`src/providers/model_extractor.py` `_quote_supports_kind()`/`_employment_years_evidenced()`；测试 `tests/test_t11_prompt_injection.py`；评测口径见 `agent-eval-harness` skill（互链）。

## 真实指标（本项目实测，勿虚报）

- model 抽取转人工率 **71%**（未知/定位失败/一致性不通过即转人工）。
- 离线评测：mock 规则 F1 **0.36** vs model F1 **0.73**。
- 单次分析 P95 **67s**（含模型调用；pages 缓存命中后换抽取器重跑秒级）。

## 参考文件（需要时再读，勿一次性全加载）

- `references/evidence-trivalence.md` — 证据三角完整机制、复核端点 extractor 版本不一致真 bug 与修法、payload 空默认值 bug、精简代码骨架。
- `references/hitl-recovery-idempotency.md` — interrupt/checkpointer 装配、审批提交时序、T1 故障注入写法、排期幂等四保证与通知重试边界、精简骨架。
- `references/mcp-exposure.md` — 薄代理原则、tools/resources/prompts 清单与命名、stdio/http transport、RESUME_API_BASE、何时该/不该暴露。

## 互链

- 上游需求拆解 → `spec-driven-planning`（本目录规划技能）。
- 评测佐证（F1/转人工率怎么测）→ `agent-eval-harness`。
- 故障注入/回归测试写法 → `test-hardened-dev`。
- 上线认证 / PII 处理 → `pii-safe-release`。
- 收束为可演示 demo → `project-closeout`。
