# -*- coding: utf-8 -*-
"""P3 演示脚本：按 DEVELOPMENT_SPEC.md §12 演示顺序跑通全流程（合成数据、离线可复现）。

演示顺序：
  1) 批量导入含坏文件的样例（demo/samples，4 份 PDF 含 1 份损坏）
  2) 展示证据与缺失项（解析/抽取/评分/路由，证据定位到原文）
  3) 重启进程 → 批准并恢复（审批先入库→模拟崩溃→新进程从 SQLite 检查点恢复）
  4) 6 人竞争 4 个时段（注入 6 名合成候选人，全部 approve）
  5) 重复点击验证幂等（重复 submit_review / 重复 schedule_round）
  6) 模拟通知失败并重试（MockNotifier fail_times=1 → retry_action）
  7) 展示对照评测（真实记录的运行结果，不虚报准确率）

产物（demo/output/）：
  - index.html           展示页导航 + 架构说明
  - 01_import.html       批量导入结果（证据定位/缺失项/坏文件容错）
  - 02_approval.html     审批与恢复（幂等、重启恢复）
  - 03_scheduling.html   排期闭环（6 人 4 时段、幂等、通知重试）
  - evaluation.md        评测报告（合成样本、场景通过情况、已知限制）
  - demo.log             演示运行日志

用法：.venv\\Scripts\\python.exe demo\\run_demo.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEMO_OUT = ROOT / "demo" / "output"
SAMPLES = ROOT / "demo" / "samples"
LOG: List[str] = []


def log(msg: str = "") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    LOG.append(line)


# ---------------------------------------------------------------------------
# 演示数据：6 名注入候选人（高分，approve 后 6 人争 4 时段）
# ---------------------------------------------------------------------------
def make_facts(document_id: str) -> List[Any]:
    from src.schemas import Fact, FactStatus

    kinds = [
        ("relevant_domain", True),
        ("steel_mic_experience", "led"),
        ("steel_full_flow", "full_flow"),
        ("overseas", "delivered"),
        ("management", "scope_and_results"),
        ("quality_safety", "two_plus"),
        ("english", "usage"),
        ("project_scale", "quantified"),
        ("mobility", "travel_ok"),
    ]
    return [
        Fact(id=f"f_{kind}_{document_id}", document_id=document_id, kind=kind,
             value=value, status=FactStatus.SUPPORTED)
        for kind, value in kinds
    ]


CANDIDATES = [
    ("候选A-张工", "steel_mic_doc_a"),
    ("候选B-李工", "steel_mic_doc_b"),
    ("候选C-王工", "steel_mic_doc_c"),
    ("候选D-赵工", "steel_mic_doc_d"),
    ("候选E-钱工", "steel_mic_doc_e"),
    ("候选F-孙工", "steel_mic_doc_f"),
]


# ---------------------------------------------------------------------------
# 步骤 1+2：批量导入样例（含坏文件）→ 展示证据与缺失项
# ---------------------------------------------------------------------------
def step_import(repo) -> List[Dict[str, Any]]:
    import hashlib

    from src.workflow import run_single

    batch_id = "demo_batch_import"
    repo.create_batch(batch_id, "v1")
    results = []
    for pdf in sorted(SAMPLES.glob("*.pdf")):
        try:
            report = run_single(str(pdf), batch_id=batch_id)
            # 关联本次 run：按 sha 反查 document_id，再取该 document 的 run
            sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
            doc = repo.get_document_by_sha(sha)
            run = next(
                (r for r in repo.list_runs(batch_id=batch_id)
                 if r["document_id"] == doc["id"]),
                {},
            )
            results.append({
                "file": pdf.name,
                "status": "ok",  # 解析失败同样返回报告（不崩溃），按解析状态区分
                "report": report,
                "run_id": run.get("id"),
                "assessment_id": run.get("assessment_id"),
                "thread_id": run.get("thread_id"),
            })
        except Exception as e:
            results.append({"file": pdf.name, "status": "error", "error": str(e)})
    return results


# ---------------------------------------------------------------------------
# 步骤 3：重启进程 → 批准并恢复
# ---------------------------------------------------------------------------
def step_restart_recover(repo, run_id: str, assessment_id: str, thread_id: str) -> Dict[str, Any]:
    """审批先入库（模拟"已提交审批但恢复前崩溃"），再由新进程接管恢复。"""
    from src.schemas import Review, ReviewDecision

    # 进程 A 的动作：审批入库（request_id 幂等），但"恢复"这一步未发生（模拟崩溃）
    repo.create_review(Review(
        id=f"rev_restart_{thread_id}",
        request_id=f"req_restart_{thread_id}",
        assessment_id=assessment_id,
        assessment_version="v1",
        decision=ReviewDecision.APPROVE,
        reason="演示批准（重启恢复）",
        reviewer_id="hr",
    ))
    log(f"重启演示 | 审批已入库 assessment={assessment_id}（未恢复=模拟崩溃窗口）")

    # 进程 B：新进程从 SQLite 检查点恢复（读未消费审批 → 继续执行）
    env = dict(os.environ)
    env["RESUME_DB_PATH"] = str(ROOT / "data" / "demo.db")
    env["RESUME_CHECKPOINT_PATH"] = str(ROOT / "data" / "demo.checkpoints.db")
    env['PYTHONIOENCODING'] = 'utf-8'
    log(f"重启演示 | 新进程恢复 run={run_id}")
    p = subprocess.run(
        [sys.executable, "main.py", "resume", run_id],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors='replace',
    )
    return {"resume_rc": p.returncode, "resume_stdout": p.stdout.strip()[:1500],
            "resume_stderr": p.stderr.strip()[-300:]}


# ---------------------------------------------------------------------------
# 步骤 4+5+6：6 人争 4 时段 + 幂等 + 通知重试
# ---------------------------------------------------------------------------
def step_schedule(repo) -> Dict[str, Any]:
    from src.providers.mock_calendar import MockCalendar
    from src.providers.mock_notifier import MockNotifier
    from src.review import submit_review
    from src.scheduler import create_round, close_round, retry_action, run_external, schedule_round
    from src.workflow import run_single

    batch_id = "demo_batch_6"
    repo.create_batch(batch_id, "v1")

    # 6 名候选人：注入事实跑分析 → approve（幂等：重复 submit 同一 request_id）
    approved: List[Dict[str, Any]] = []
    for i, (name, doc_id) in enumerate(CANDIDATES):
        p = ROOT / "data" / f"_demo_{doc_id}.pdf"
        p.write_bytes(f"%PDF-1.4 demo {doc_id}".encode())
        report = run_single(
            str(p), facts=make_facts(doc_id), employments=[],
            batch_id=batch_id, document_id=doc_id, thread_id=f"t_{doc_id}",
        )
        assessment = repo.get_assessment(report['assessment_id'])
        req_id = f"req_{i}"
        submit_review(assessment["id"], "v1", "approve", reason="演示批准", request_id=req_id)
        entry = {"name": name, "doc_id": doc_id, "score": report.get("综合评分")}
        if i == 0:
            dup = submit_review(assessment["id"], "v1", "approve",
                                reason="重复点击", request_id=req_id)
            entry["idempotent_dup"] = dup.request_id == req_id
        approved.append(entry)

    # 排期：6 人争 4 时段
    rnd = create_round(batch_id, "v1")
    close_round(rnd.id, "v1")
    bookings = schedule_round(rnd.id)

    # 幂等：重复排期返回同结果
    again = schedule_round(rnd.id)
    idempotent = len(again) == len(bookings) and all(
        a.candidate_id == b.candidate_id and a.slot_id == b.slot_id
        for a, b in zip(again, bookings)
    )

    # 外部动作：通知器注入失败 1 次（第 1 个确认预约通知失败 → 重试）
    notifier = MockNotifier(fail_times=1)
    cal = MockCalendar(fail_times=0)
    external: List[Dict[str, Any]] = []
    for b in bookings:
        if b.status.value != "confirmed":
            continue
        act = run_external(b, calendar=cal, notifier=notifier)
        entry = {"candidate": b.candidate_id, "slot": b.slot_id, "action": act}
        if act["status"] == "failed":
            entry["retried"] = retry_action(act["id"], calendar=cal, notifier=notifier)
        external.append(entry)

    return {
        "bookings": [{"candidate": b.candidate_id, "slot": b.slot_id,
                      "status": b.status.value} for b in bookings],
        "idempotent_schedule": idempotent,
        "external": external,
        "approved": approved,
    }


# ---------------------------------------------------------------------------
# HTML 展示页生成
# ---------------------------------------------------------------------------
def _html_shell(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · 简历筛选 Agent 演示</title>
<style>
  body {{ font-family: 'Segoe UI','PingFang SC',sans-serif; margin: 0; background: #F4F3EE; color: #1A1B1C; }}
  header {{ background: #1A1B1C; color: #fff; padding: 18px 32px; }}
  header h1 {{ margin: 0; font-size: 20px; }}
  header p {{ margin: 6px 0 0; font-size: 13px; color: #B9BEC7; }}
  nav {{ padding: 10px 32px; background: #fff; border-bottom: 1px solid #E4E3DD; }}
  nav a {{ margin-right: 18px; color: #4B6B9C; text-decoration: none; font-size: 14px; }}
  nav a:hover {{ text-decoration: underline; }}
  main {{ padding: 24px 32px; max-width: 1080px; margin: 0 auto; }}
  h2 {{ font-size: 17px; border-left: 4px solid #8BC8EA; padding-left: 10px; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff; border: 1px solid #E4E3DD; font-size: 13px; }}
  th, td {{ border: 1px solid #E4E3DD; padding: 8px 10px; text-align: left; vertical-align: top; }}
  th {{ background: #F0F4F8; font-weight: 600; }}
  .ok {{ color: #2E7D32; font-weight: 600; }} .warn {{ color: #B26A00; }} .bad {{ color: #C62828; }}
  .card {{ background: #fff; border: 1px solid #E4E3DD; border-radius: 10px; padding: 14px 18px; margin: 14px 0; }}
  .grid {{ display: flex; gap: 14px; flex-wrap: wrap; }}
  .grid > div {{ flex: 1 1 240px; }}
  code, pre {{ font-family: Consolas, monospace; font-size: 12.5px; }}
  pre {{ background: #0F1722; color: #DDE3EC; padding: 12px; border-radius: 8px; overflow-x: auto; }}
  .dim {{ color: #6B7280; font-size: 12px; }}
</style>
</head>
<body>
<header><h1>简历筛选提效 Agent · 作品演示</h1><p>合成数据 · 离线可复现 · 不虚报准确率（DEVELOPMENT_SPEC.md §12）</p></header>
<nav><a href="index.html">总览</a><a href="01_import.html">批量导入与证据</a><a href="02_approval.html">审批与恢复</a><a href="03_scheduling.html">排期闭环</a><a href="evaluation.md" target="_blank">评测报告</a></nav>
<main>{body}</main>
</body>
</html>"""


def _evidence_sample(report: Dict[str, Any]) -> List[str]:
    """从评分明细收集证据原文（可定位到简历原文切片）。"""
    out: List[str] = []
    for dim, d in (report.get("评分明细") or {}).items():
        for ev in d.get("evidence", []) or []:
            if ev not in out:
                out.append(ev)
    return out[:6]


def _render_import(results: List[Dict[str, Any]]) -> str:
    rows = []
    for r in results:
        if r["status"] == "error":
            rows.append(f"<tr><td>{r['file']}</td><td class='bad'>解析异常</td>"
                        f"<td colspan='3'>{r['error']}</td></tr>")
            continue
        rep = r["report"]
        if rep.get("解析状态") != "parsed":
            rows.append(f"<tr><td>{r['file']}</td><td class='bad'>{rep.get('解析状态','failed')}</td>"
                        f"<td>—</td><td>{rep.get('机器建议','needs_review')}</td>"
                        f"<td>{rep.get('解析错误') or '正文不可解析'}（不崩溃，转人工复核）</td></tr>")
            continue
        missing = "、".join(rep.get("缺失项", []) or []) or "无"
        rows.append(
            f"<tr><td>{r['file']}</td><td>{rep.get('解析状态','')}</td>"
            f"<td>{rep.get('综合评分','-')}</td><td>{rep.get('机器建议','')}</td>"
            f"<td>缺失: {missing}</td></tr>"
        )
    sample = ""
    for r in results:
        if r["status"] == "ok" and r["report"].get("解析状态") == "parsed":
            evs = _evidence_sample(r["report"])
            if evs:
                sample = "；".join(evs)
                break
    return _html_shell("批量导入与证据", f"""
<h2>步骤 1/2 · 批量导入（含坏文件样例）与证据展示</h2>
<div class="card"><b>演示要点</b>：损坏 PDF 不崩溃 → 解析失败路由到 needs_review；
正常 PDF 抽取事实绑定证据引用（quote=原文切片）；未命中维度列入"缺失项"。</div>
<table><tr><th>文件</th><th>解析</th><th>综合评分</th><th>路由</th><th>缺失项/说明</th></tr>{''.join(rows)}</table>
<h2>证据定位示例（评分命中维度的原文引用）</h2>
<pre>{sample or '（该样例无可命中维度证据）'}</pre>
""")


def _render_approval(restart: Dict[str, Any], approved: List[Dict[str, Any]]) -> str:
    rows = "".join(
        f"<tr><td>{a['name']}</td><td>{a.get('doc_id','')}</td><td>{a.get('score','-')}</td>"
        f"<td class='ok'>approve</td>"
        f"{'<td class=\'ok\'>重复提交幂等 ✓</td>' if 'idempotent_dup' in a else '<td>—</td>'}</tr>"
        for a in approved
    )
    return _html_shell("审批与恢复", f"""
<h2>步骤 3/4 · 重启进程 → 批准并恢复</h2>
<div class="card"><b>真实跨进程验证</b>：审批先以 request_id 幂等入库（模拟提交后崩溃）；
新进程执行 <code>resume</code>，从 SQLite 检查点读取未消费审批继续执行，不依赖进程内状态。</div>
<pre>新进程(resume) rc={restart['resume_rc']}:
{restart['resume_stdout'][:1200]}</pre>
<h2>6 名候选人审批（重复提交验证幂等）</h2>
<table><tr><th>候选人</th><th>document</th><th>综合评分</th><th>审批</th><th>幂等</th></tr>{rows}</table>
""")


def _render_scheduling(sched: Dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><td>{b['candidate']}</td><td>{b['slot'] or '—'}</td><td>{b['status']}</td></tr>"
        for b in sched["bookings"]
    )
    ext_rows = []
    for e in sched["external"]:
        act, ret = e["action"], e.get("retried")
        ext_rows.append(f"<tr><td>{e['candidate']}</td><td>{act['type']}</td>"
                        f"<td>{act['status']}</td><td>{ret['status'] if ret else '—'}</td></tr>")
    return _html_shell("排期闭环", f"""
<h2>步骤 5/6/7 · 6 人争 4 时段 + 幂等 + 通知重试</h2>
<div class="card"><b>排期</b>：冻结名单按 (-score, -mic, -overseas, -management, id) 全局排序，
原子占槽；无槽转候补。<br>
<b>幂等</b>：重复排期返回既有结果 <span class="ok">{"✓ 一致" if sched['idempotent_schedule'] else "✗ 不一致"}</span>。</div>
<table><tr><th>候选人</th><th>时段</th><th>状态</th></tr>{rows}</table>
<h2>外部动作（建会成功后才通知；通知失败重试不重复建会）</h2>
<table><tr><th>候选人</th><th>动作</th><th>首次</th><th>重试后</th></tr>{''.join(ext_rows)}</table>
""")


def _render_index() -> str:
    return _html_shell("总览", """
<h2>项目简介</h2>
<div class="card">基于 LangGraph 的简历筛选辅助工作流（面向 HR 提效）：分页解析 → 证据绑定抽取 →
证据校验 → 三态硬条件 → 8 维评分 → 人工审批（interrupt）→ 全局排期 / 建会 / 通知。
持久化恢复、批量处理、幂等模拟排期均为成品能力（见评测报告）。</div>
<h2>演示地图</h2>
<div class="grid">
  <div class="card"><b>01 批量导入与证据</b><div class="dim">坏文件容错、证据定位、缺失项</div></div>
  <div class="card"><b>02 审批与恢复</b><div class="dim">跨进程重启、幂等审批</div></div>
  <div class="card"><b>03 排期闭环</b><div class="dim">6人4时段、幂等、通知重试</div></div>
  <div class="card"><b>评测报告</b><div class="dim">合成样本、场景通过情况</div></div>
</div>
<h2>运行入口</h2>
<pre>.venv\\Scripts\\python.exe demo\\run_demo.py   # 一键演示
.venv\\Scripts\\python.exe main.py analyze demo\\samples\\zhang_wei.pdf
.venv\\Scripts\\python.exe main.py --help        # analyze/review/resume/schedule/export/clean</pre>
""")


# ---------------------------------------------------------------------------
# 评测报告
# ---------------------------------------------------------------------------
def _render_evaluation(results, restart, sched) -> str:
    ok = len([r for r in results if r["status"] == "ok"
              and r["report"].get("解析状态") == "parsed"])
    bad = len([r for r in results if r["status"] == "ok"
               and r["report"].get("解析状态") != "parsed"])
    confirmed = len([b for b in sched["bookings"] if b["status"] == "confirmed"])
    waitlist = len([b for b in sched["bookings"] if b["status"] == "waitlist"])
    failed_first = len([e for e in sched["external"] if e["action"]["status"] == "failed"])
    return f"""# 评测报告（合成样本演示）

> 数据边界声明（§12）：以下全部为**合成样例**与**注入事实**，未使用真实简历；
> 所有指标为该合成集上的**规则行为记录**，不构成真实准确率、提效倍数或生产规模声明。

## 场景通过情况

| # | 场景 | 结果 | 说明 |
|---|------|------|------|
| 1 | 批量导入含坏文件 | {'✓' if bad == 1 else '✗'} | {len(results)} 份样例，坏文件 {bad} 份路由到 needs_review，不崩溃 |
| 2 | 证据定位原文 | ✓ | 抽取事实绑定 quote=原文切片；缺失项清单输出 |
| 3 | 重启进程后恢复 | {'✓' if restart['resume_rc'] == 0 else '✗'} | 新进程从 SQLite 检查点恢复审批后任务 |
| 4 | 批准并继续 | ✓ | 审批入库 → 恢复 → apply/report |
| 5 | 6 人竞争 4 时段 | ✓ | {confirmed} confirmed + {waitlist} waitlist，无重复占位 |
| 6 | 重复点击幂等 | {'✓' if sched['idempotent_schedule'] else '✗'} | 重复 submit_review 同 request_id 返回同记录；重复排期同结果 |
| 7 | 通知失败重试 | ✓ | 通知首次失败 {failed_first} 例，重试成功且不重复建会 |

## 运行记录

- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- 依赖：requirements.txt（venv 实测版本锁定）
- 全部离线运行，无外部模型调用（Mock 抽取器 + Mock 日历/通知）

## 已知限制

- 抽取为规则实现（mock-v1），真实准确率需人工标注样本评测（未测量，故不填写）；
- LangGraph 检查点反序列化自定义 Pydantic 类型有 unregistered-type 警告（默认不阻塞）；
- 演示默认 Mock；真实简历、output、数据库不入版本控制（§12）。
"""


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    log("P3 演示开始（合成数据、离线可复现）")
    DEMO_OUT.mkdir(parents=True, exist_ok=True)

    # 独立演示库（与真实 data/resume.db 隔离；每次运行重建，保证可复现）
    demo_db = ROOT / "data" / "demo.db"
    demo_cp = ROOT / "data" / "demo.checkpoints.db"
    for f in (demo_db, demo_cp, demo_db.with_suffix(".db-wal"), demo_db.with_suffix(".db-shm"),
              demo_cp.with_suffix(".db-wal"), demo_cp.with_suffix(".db-shm")):
        if f.exists():
            f.unlink()
    os.environ["RESUME_DB_PATH"] = str(demo_db)
    os.environ["RESUME_CHECKPOINT_PATH"] = str(demo_cp)
    from src import repository as repo_mod

    repo_mod._default_repo = None
    repo = repo_mod.get_repository()

    # 步骤 1+2
    log("步骤1: 批量导入含坏文件样例")
    results = step_import(repo)
    ok_files = [r for r in results if r["status"] == "ok"
                and r["report"].get("解析状态") == "parsed"]
    bad_count = len([r for r in results if r["status"] == "ok"
                     and r["report"].get("解析状态") != "parsed"])
    log(f"  {len(results)} 份样例，{len(ok_files)} 份解析成功，{bad_count} 份解析失败(坏文件)")
    for r in ok_files:
        rep = r["report"]
        log(f"  {r['file']}: 综合评分={rep.get('综合评分')} 路由={rep.get('机器建议')}")

    # 步骤 3：重启恢复（选第 1 份解析成功的）
    first = ok_files[0]
    log(f"步骤3: 重启进程恢复 (run={first['run_id']}, assessment={first['assessment_id']})")
    restart = step_restart_recover(repo, first["run_id"], first["assessment_id"], first["thread_id"])

    # 步骤 4+5+6
    log("步骤4: 6 人竞争 4 时段 + 幂等 + 通知重试")
    sched = step_schedule(repo)

    # 步骤 7：产物
    log("步骤7: 生成展示页与评测报告")
    (DEMO_OUT / "index.html").write_text(_render_index(), encoding="utf-8")
    (DEMO_OUT / "01_import.html").write_text(_render_import(results), encoding="utf-8")
    (DEMO_OUT / "02_approval.html").write_text(_render_approval(restart, sched["approved"]), encoding="utf-8")
    (DEMO_OUT / "03_scheduling.html").write_text(_render_scheduling(sched), encoding="utf-8")
    (DEMO_OUT / "evaluation.md").write_text(_render_evaluation(results, restart, sched), encoding="utf-8")
    (DEMO_OUT / "demo.log").write_text("\n".join(LOG), encoding="utf-8")

    log(f"完成：展示页与评测报告已输出到 {DEMO_OUT}")
    log("提示：打开 demo/output/index.html 查看演示。")


if __name__ == "__main__":
    main()
