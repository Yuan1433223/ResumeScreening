# HITL + 崩溃恢复 + 幂等三件套

人工介入（HITL）在 Agent 系统里最容易出两类事故：进程在"审批入库"与"恢复执行"之间崩溃，任务悬空；或恢复时重跑图，导致重复建会/重复通知。本项目用 interrupt + checkpointer + 幂等审批 + 重放保护四件套解决。

## 目录

1. [LangGraph interrupt / checkpointer 装配](#1-langgraph-interrupt--checkpointer-装配)
2. [审批提交时序：先落库，后恢复](#2-审批提交时序先落库后恢复)
3. [resume_run 的崩溃点分析与重放保护](#3-resume_run-的崩溃点分析与重放保护)
4. [T1 故障注入测试怎么写](#4-t1-故障注入测试怎么写)
5. [排期幂等四保证与通知重试边界](#5-排期幂等四保证与通知重试边界)
6. [精简骨架](#6-精简骨架)

## 1. LangGraph interrupt / checkpointer 装配

图结构：`parse → extract → validate → screen → score → review(interrupt) → apply → report`。解析失败/证据校验失败条件边直接路由到 `review`。

```python
workflow.compile(checkpointer=_get_checkpointer())   # SqliteSaver 持久检查点
# invoke 时带 thread_id：
graph.invoke(state, config={"configurable": {"thread_id": tid}})
```

- `_get_checkpointer()` 进程内复用同一个 `SqliteSaver(sqlite3.connect(CHECKPOINT_DB_PATH, check_same_thread=False))`。
- 后台批次线程与 FastAPI 线程共用同一连接，故图调用全程加 `_GRAPH_LOCK = threading.RLock()`，防并发游标竞争。
- `thread_id` 缺省用 `document_id`：同一文件重复分析复用同一检查点。

## 2. 审批提交时序：先落库，后恢复

`review.submit_review(assessment_id, expected_version, decision, request_id)`：

1. 校验 assessment 存在；`assessment.job_version != expected_version` → `VersionConflictError`（旧审批不能自动批准新版本）。
2. 以 `request_id` 幂等写 `reviews` 表（`request_id TEXT UNIQUE`）：重复提交返回已有记录，不重复创建。
3. **入库成功后**才 `_resume_assessment` → `workflow.resume_run(run_id)`。

关键：第 2 步与第 3 步之间进程退出也不丢审批——`resume_run` 启动时会读 `get_unconsumed_review(assessment_id)`（`consumed=0` 的最早一条）继续执行。

## 3. resume_run 的崩溃点分析与重放保护

`workflow.resume_run(run_id, decision=None)` 三个分支：

| 崩溃点 | 恢复行为 |
|---|---|
| invoke 前退出（审批已入库未恢复） | 审批保持 `consumed=0`，下次 `resume_run` 读到后正常 `Command(resume=decision)` 恢复 |
| invoke 成功后、`mark_review_consumed` 前退出 | 检查点已到 END（`snapshot.next` 为空）→ **重放保护**：不重复 invoke，只补 `mark_review_consumed` + 置 run=finished |
| 正常恢复 | invoke 成功 → `mark_review_consumed` → run=finished |

即：**只在 invoke 成功后才消费审批**；线程已跑完时重放只补收尾。重复调用已完成的 run 返回同一报告，不抛错、不重复消费。

## 4. T1 故障注入测试怎么写

`tests/test_t1_recovery_boundary.py` 用 monkeypatch 造两个"崩溃点"：

- **中途崩溃**：包一层 `FlakyGraph`，第一次 `invoke` 抛 `RuntimeError("模拟进程在恢复中途退出")`，第二次走真图。断言：第一次恢复后审批仍未消费；第二次恢复后审批消费一次、run=finished、`invoke` 恰好两次。
- **收尾前崩溃**：包一层 `FlakyRepo.mark_review_consumed`，第一次抛异常。断言：第二次恢复走重放保护，`invoke` 只执行一次（不重跑图），只补消费与收尾。
- **正常台账**：恢复前审批未消费/run=pending；恢复后审批已消费/run=finished/finished_at 非空；再调一次幂等返回同一结果。

种子方法 `seed_paused_run`：`run_single(..., facts=合成高分事实)` 跑到 review 节点 interrupt 暂停，再手工 `repo.create_review` 一条未消费审批，模拟"崩溃前已审批"。

## 5. 排期幂等四保证与通知重试边界

`src/scheduler.py`：

1. **轮冻结**：`close_round(round_id, expected_job_version)` 校验版本匹配且状态 open，才 `freeze_round` + `freeze_batch`；冻结名单 = 批次内 `decision='approve' AND consumed=1` 且 `job_version` 匹配的候选人。
2. **候选唯一**：`bookings UNIQUE(round_id, candidate_id)`；`create_booking` 重复返回已有预约，不重复分配。
3. **槽位唯一**：`claim_slot(booking_id, slot_id)` 在事务内先查该 slot 是否已有 `status='confirmed'` 占用、再确认本 booking 是 pending，才置 confirmed；争不到 → `WAITLIST`。
4. **外部动作唯一**：`external_actions UNIQUE(booking_id, type)`；`run_external` 先建会（`idempotency_key=meet_{booking}`），**会议 succeeded 后才发通知**（`ntf_{booking}`）；`retry_action` 只重试失败动作——通知失败重试通知时复用会议的 `provider_id`，绝不重建会议。

排序键 `_rank_key`：`known_score DESC → steel_mic_experience DESC → overseas DESC → management DESC → candidate_id ASC`。`schedule_round` 对已 `scheduled` 的轮幂等返回既有 bookings。

测试 `tests/test_p2_flow.py`：6 个获批候选人争 4 时段 → 2 个 WAITLIST；`run_external` 注入会失败的 notifier → `retry_action` 只补通知；重复 `schedule_round` 返回同一结果。

## 6. 精简骨架

```python
# review.py —— 先入库后恢复
def submit_review(assessment_id, expected_version, decision, request_id):
    assessment = repo.get_assessment(assessment_id)
    if assessment["job_version"] != expected_version:
        raise VersionConflictError(...)
    review = Review(request_id=request_id, ...)
    created = repo.create_review(review)          # request_id UNIQUE，幂等
    if created.request_id != review.request_id:
        return created                            # 重复提交：直接返回
    resume_run(run_row["id"])                     # 入库后才恢复
    return created

# workflow.py —— 恢复成功才消费
def resume_run(run_id, decision=None):
    review = repo.get_unconsumed_review(assessment_id)   # consumed=0
    if review: decision = review.decision.value
    snapshot = graph.get_state(cfg)
    if not snapshot.next:                        # 已到 END：重放保护
        if review: repo.mark_review_consumed(review.id)
        repo.update_run(run_id, status="finished", ...)
        return report
    repo.update_run(run_id, status="resumed")
    graph.invoke(Command(resume=decision), cfg)  # 成功后才消费
    if review: repo.mark_review_consumed(review.id)
    repo.update_run(run_id, status="finished", ...)
```

红线：审批消费必须晚于 invoke 成功；外部动作按 `(booking, type)` 唯一、按阶段顺序（会→通知）；重试只补失败阶段，不回滚已成功阶段。
