---
name: test-hardened-dev
description: 测试护栏开发法（改动即配测试 + 硬隔离 + 故障注入 + 契约断言 + 根因修复）。当用户要给本简历筛选系统新增功能、修 bug、改 API/数据层/抽取/审批链路时使用；典型触发：写新功能同提交测试、修 bug 先定位根因而非改现象、跑全量回归防退化、补契约/边界/幂等/安全用例、怀疑漏斗统计或加密/认证行为回退。执行七步：基线记录 → 隔离 → 故障注入 → 契约测试 → 边界与幂等 → 根因修复+回归 → 安全两态。不适用于：纯文档/文案/前端静态资源改动（无代码路径可测）、一次性且无复用价值的脚本、以及不读源码凭记忆写断言的"凑数测试"——本项目拒绝这类测试。
---

# Test-Hardened Dev（测试护栏开发法）

每一处改动都被测试夹住：新功能同提交测试，修 bug 先修根因再补回归，任何一次全量回归数字只能涨不能跌。测试不依赖真实网络、真实简历、真实 API key。

## 前提与红线

- **基线数字**：当前 `pytest tests/ --collect-only` 为 **175** 项。改动前先跑一遍全量并记录通过数，**只许涨不许跌**；跌了就是退化，先修再继续。
- **只读约束**：本 skill 只在 `tests/` 与本目录内活动；不改业务逻辑"为了好测"。若为可测性不得不调整源码接缝（依赖注入点），在 PR/说明里单独标出。
- **隔离优先**：任何用例不得读真实 `RESUME_DIR`、真实 `.env`、真实云端模型 key、真实网络。假客户端与临时 SQLite 是默认而非例外。

## 工作流（按序执行）

### 1. 改动即配测试（同提交，按批次编号）
- 新功能与它的测试**同一个提交**；不写"事后补测试"。
- 用例归入既有批次编号：`test_p0_regression.py` 放硬回归（评分三态、年限、路由边界），主题批次 T1–T15 对应恢复边界/身份/模型抽取/评测/API/可观测/MCP/复核一致性/前端契约/任务store+SSE/注入/DOCX/上传+dashboard/PII/认证。**文件名以 `tests/` 目录实际为准**，新主题开新文件时沿用 `test_t<序号>_<slug>.py` 命名。
- 动手前先跑 `pytest tests/ -q` 记录基线（当前 175）；改动后再跑，对比数字。

### 2. 隔离（不碰真实世界）
- 复用 `tests/conftest.py` 的 `_isolated_repository` autouse fixture：临时 SQLite + 逐模块 patch `DB_PATH`（config/repository/workflow/task_store 四处都要 patch，因为 `from .config import DB_PATH` 在导入时复制了常量）+ `CHECKPOINT_DB_PATH`，并重置进程内单例 `_default_repo` / `_default_store` / `_checkpointer_cache`。
- 模型调用一律用 `ScriptedModelClient` 假客户端（脚本化返回 `ModelResponse` 或抛 `ModelTransientError`/`ModelPermanentError`），`sleep_fn=lambda s: None` 跳过等待。
- 日历/通知/云端等外部协作用**可注入 `fail_times` 的 mock**：第一次/前 N 次抛错，之后走真实实现，用于测重试与恢复。
- API 层用 `TestClient(app)` + autouse fixture 把 `api_module.RESUME_DIR` 指到 `tmp_path` 下的合成简历，并清空 `api_module._REVIEW_EXTRACT_CACHE` 防跨用例串味。

### 3. 故障注入测恢复边界
- **审批消费时序**：验证"先落库后恢复"——用 `FlakyGraph` 让第一次 `resume_run` 在 `invoke` 中途抛错，断言审批仍 `get_unconsumed_review() is not None`、第二次恢复才消费一次并置 `finished`。
- **崩溃后 resume**：用 `FlakyRepo.mark_review_consumed` 让收尾时崩溃，断言第二次恢复走重放保护、`invoke` 恰好只执行一次、不重复消费。
- **通知/外部副作用重试幂等**：mock 让前 N 次失败，断言重试有限次、耗尽后转人工不崩溃、副作用（写库/发通知）不重复生效。
- 关键断言写在**状态账本**上：审批态（未消费→已消费）与 run 态（pending→resumed→finished）两次查询比对一致。

### 4. 契约测试（HTTP 层断言，不靠浏览器）
- 断言响应头：`Content-Type`（PDF 必须 `application/pdf`）、`Content-Disposition`（**PDF 必须 `inline;` 而非 `attachment`**，否则 `<iframe>` 空白）、状态码（400 坏抽取器/坏决策/坏扩展名、401 未认证、404 不存在、409 重复排期、413 超限）。
- 断言文件魔数：PDF 响应体 `startswith(b"%PDF-")`；上传拒绝 PNG 字节改名 `.pdf`、裸 zip 改名 `.docx`。
- SSE：`client.stream(...)` + `iter_lines()` 逐行解析，断言事件序列首事件 `connected`、中间含 `progress`、末事件 `done`（且 `done==total`、`rows` 非空）、错误任务首事件 `error`；忽略 `:` 心跳注释行。
- 响应 JSON 形状 **byte 级稳定**：轮询接口的全部前端字段（`task_id/batch_id/status/total/done/current_file/rows/error`）逐键断言存在，dashboard 顶层键集合 `{kpis, score_distribution, funnel}` 固定、漏斗六段顺序固定、空库全 0 不 404。

### 5. 边界值与幂等
- 阈值点必须参数化覆盖：路由 **59→hold / 60→needs_review / 74→needs_review / 75→recommend**；评分永不超 100；缺维度即不完整→绝不 recommend。
- 幂等：同候选重复排期第二次返回 **409**；已完成 run 再次 `resume_run` 返回同一结果不重复消费；同文档跨批次 runs 翻倍但 dashboard `extracted` 按 `document_id` 去重不回跳。
- 空库 dashboard 返回全 0/null 结构（`avg_score is None`、桶计数全 0），**不得 404**。

### 6. 修 bug：先定位根因，不修现象
按序执行，缺一不可：
1. **复现**：先写一个失败测试把 bug 钉住（可临时 xfail）。
2. **读调用链**：从报错/异常现象沿调用栈读到真正的状态读写点，确认为什么发生。
3. **改根因**：改产生错误的那一层，不在上层打补丁掩盖。
4. **补回归测试**：把复现用例转成永久断言，注释里写清根因。
5. **全量回归**：`pytest tests/ -q` 必须回到基线以上全绿。
6. **独立复验**：由"不同于生成路径"的方式复验——子代理/测试断言通过后，主控另起真实 `uvicorn`、喂合成数据冒烟一次关键路径，确认不是测试自己骗自己。

### 7. 安全测试
- 注入用例**先 xfail 固化漏洞再修**：把"被注入后仍 recommend/满分"的攻击样例写成失败测试（见 `references/root-cause-playbook` 与 `tests/test_t11_prompt_injection.py` 的 A/B/C 三段：规则抽取器无指令通道、模型捏造引文定位失败、真实无关子串贴满档位必须降级 unknown），修完转正向断言。
- **开关两态都测**：加密（`PII_ENCRYPT_KEY` 空 vs 合法 Fernet key）、认证（`AUTH_ENABLED` 关 vs 开）——默认关全绿、开启态独立用例集；开启但缺凭据必须 fail-fast 抛 `RuntimeError`。
- 加密读层：无 key 进程读到 `gAAAA` 前缀密文必须显式 `RuntimeError`，绝不静默返密文或 `None`；导出失败不得写半截文件。

## 三条取舍原则

- **隔离是默认**：临时 SQLite + 假客户端是每一个新用例的起点，不是可选优化。
- **根因高于补丁**：现象被压住而根因仍在的修法，等于欠一笔必然复现的债。
- **数字即真相**：基线 175 只能涨；漏斗单调性、状态账本、响应头契约任何一条被破坏，就是红灯。

## 参考文件（需要时再读，勿一次性全加载）

- `references/test-patterns.md` — 模式→本项目测试文件映射表（隔离 fixture / 假客户端 / 故障注入 / TestClient+cookie / SSE 解析 / multipart 伪造 / DOCX 现场合成 / 漏斗单调性 / 加密与认证 monkeypatch），含文件、用例名与关键断言片段。
- `references/root-cause-playbook.md` — 六个真实 bug 的根因复盘（现象→错误修法诱惑→真实根因→修法→回归测试→可迁移教训），每条标注对应测试文件。

## 互链

- 规划分解 → `spec-driven-planning`
- 可信实现模式 → `trustworthy-agent-patterns`
- 评测 harness → `agent-eval-harness`
- 安全发布 → `pii-safe-release`
- 收束发布 → `project-closeout`
