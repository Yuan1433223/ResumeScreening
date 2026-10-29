"""项目入口：简历筛选多智能体系统（P0-P3）

CLI 子命令（规格 §9/§10）：
- analyze <file> [--batch-id B] [--thread-id T]   单份分析，停在审批节点
- batch  [--dir D] [--limit N]                    批量分析（P1 入口，兼容）
- graph                                           导出工作流图（PNG/Mermaid）
- review <assessment_id> <approve|hold|reject> [--version V] [--request-id R]
- resume <run_id> [--decision approve|hold|reject]
- schedule <batch_id> [--availability '{"c1":[1,2]}'] [--fail-notify N] [--fail-meeting N]
- export <batch_id> [--dir D]                     导出批次状态（JSON/Excel/Markdown）
- clean <document_id>                             本地清理（规格 §12）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import JOB_PROFILE, JOB_TITLE, RESUME_DIR, OUTPUT_DIR
from src.repository import get_repository
from src.review import submit_review
from src.schemas import BookingStatus, ReviewDecision
from src.workflow import run_single, build_resume_graph, resume_run


def _load_repo():
    return get_repository()


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------
def cmd_analyze(args) -> int:
    repo = _load_repo()
    batch_id = getattr(args, "batch_id", None)
    thread_id = getattr(args, "thread_id", None)
    if batch_id and repo.get_batch(batch_id) is None:
        repo.create_batch(batch_id, JOB_PROFILE.version)
    report = run_single(
        args.file, JOB_TITLE,
        thread_id=thread_id,
        batch_id=batch_id,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if batch_id:
        runs = repo.list_runs(batch_id=batch_id)
        if runs:
            print(f"\nrun_id: {runs[0]['id']}  assessment_id: {runs[0]['assessment_id']}")
    return 0


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------
def cmd_review(args) -> int:
    repo = _load_repo()
    decision = ReviewDecision(args.decision)
    review = submit_review(
        args.assessment_id,
        args.version or JOB_PROFILE.version,
        decision,
        reason=args.reason,
        request_id=args.request_id or "",
    )
    print(json.dumps({
        "id": review.id,
        "assessment_id": review.assessment_id,
        "decision": review.decision.value,
        "request_id": review.request_id,
    }, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------
def cmd_resume(args) -> int:
    report = resume_run(args.run_id, decision=args.decision)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# schedule（建轮 → 冻结 → 排期 → 外部动作，支持注入故障演示幂等）
# ---------------------------------------------------------------------------
def cmd_schedule(args) -> int:
    from src.scheduler import create_round, close_round, schedule_round, run_external, retry_action
    from src.providers.mock_calendar import MockCalendar
    from src.providers.mock_notifier import MockNotifier

    repo = _load_repo()
    version = JOB_PROFILE.version

    rnd = create_round(args.batch_id, version, repo)
    close_round(rnd.id, version, repo)
    print(f"round: {rnd.id} (轮次 {rnd.round_no}) 已冻结，4 个 45 分钟时段")

    availability = None
    if args.availability:
        availability = json.loads(args.availability)
    bookings = schedule_round(rnd.id, availability=availability, repo=repo)

    confirmed = [b for b in bookings if b.status == BookingStatus.CONFIRMED]
    waitlist = [b for b in bookings if b.status == BookingStatus.WAITLIST]
    print(f"排期完成：{len(confirmed)} 确认 / {len(waitlist)} 候补")

    cal = MockCalendar(fail_times=args.fail_meeting)
    ntf = MockNotifier(fail_times=args.fail_notify)
    for b in bookings:
        if b.status != BookingStatus.CONFIRMED:
            continue
        act = run_external(b, repo=repo, calendar=cal, notifier=ntf)
        print(f"  {b.candidate_id} -> {b.slot_id} | 外部动作 {act['type']}: {act['status']} {act.get('provider_id') or ''}")
        if act["status"] == "failed" and args.retry:
            time.sleep(0.05)
            act = retry_action(act["id"], repo=repo, calendar=cal, notifier=ntf)
            print(f"    重试后: {act['status']} {act.get('provider_id') or ''}")

    # 幂等展示：重复排期返回既有结果
    again = schedule_round(rnd.id, repo=repo)
    print(f"重复排期（幂等）返回 {len(again)} 条，与首次一致: {len(again) == len(bookings)}")
    return 0


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def cmd_export(args) -> int:
    repo = _load_repo()
    batch = repo.get_batch(args.batch_id)
    if batch is None:
        print(f"batch 不存在: {args.batch_id}", file=sys.stderr)
        return 1

    rows: List[Dict[str, Any]] = []
    redact = bool(getattr(args, "redact", False))
    from src.pii import mask_pii
    try:
        for run in repo.list_runs(batch_id=args.batch_id):
            assessment = repo.get_assessment(run["assessment_id"]) if run["assessment_id"] else None
            reviews = repo.get_reviews(run["assessment_id"]) if run["assessment_id"] else []
            row: Dict[str, Any] = {
                "run_id": run["id"],
                "document_id": run["document_id"],
                "status": run["status"],
                "known_score": assessment["known_score"] if assessment else None,
                "recommendation": assessment["recommendation"] if assessment else None,
                "审批": reviews[-1].decision.value if reviews else "pending",
            }
            # --redact：仅在显式要求导出候选人联系方式时附带，且一律打码。
            # 默认（无 --redact）行为与此前完全一致：不带任何 PII 字段。
            if redact:
                doc = repo.get_document(run["document_id"]) if run.get("document_id") else None
                row["pii_name"] = mask_pii((doc or {}).get("pii_name", ""))
                row["pii_phone"] = mask_pii((doc or {}).get("pii_phone", ""))
                row["pii_email"] = mask_pii((doc or {}).get("pii_email", ""))
            rows.append(row)

        # 批次内的排期轮与预约
        rounds = repo._conn.execute(
            "SELECT * FROM rounds WHERE batch_id=? ORDER BY round_no", (args.batch_id,)
        ).fetchall()
        bookings: List[Dict[str, Any]] = []
        for r in rounds:
            for b in repo.get_bookings(r["id"]):
                bookings.append({
                    "round": r["round_no"], "candidate_id": b["candidate_id"],
                    "slot_id": b["slot_id"], "status": b["status"],
                })
    except RuntimeError as e:
        # 密文数据在无密钥进程里被读：不写任何半截文件，友好报错并退出非 0
        print(f"导出失败：{e}。请配置 PII_ENCRYPT_KEY 后重跑本命令。", file=sys.stderr)
        return 1

    out_dir = Path(args.dir) if args.dir else OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    base = out_dir / f"batch_{args.batch_id}_{ts}"

    json_path = base.with_suffix(".json")
    json_path.write_text(json.dumps({
        "batch_id": args.batch_id, "job_version": batch["job_version"],
        "candidates": rows, "bookings": bookings,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path = base.with_suffix(".md")
    if redact:
        md_lines = [f"# 批次 {args.batch_id}（已脱敏）", "",
                    "| 候选人 | 姓名 | 手机 | 邮箱 | 评分 | 机器建议 | 审批 | 状态 |",
                    "| --- | --- | --- | --- | ---: | --- | --- | --- |"]
        for r in rows:
            md_lines.append(
                f"| {r['document_id']} | {r.get('pii_name') or '-'} | {r.get('pii_phone') or '-'} "
                f"| {r.get('pii_email') or '-'} | {r['known_score'] or '-'} | {r['recommendation'] or '-'} "
                f"| {r['审批']} | {r['status']} |")
    else:
        md_lines = [f"# 批次 {args.batch_id}", "", "| 候选人 | 评分 | 机器建议 | 审批 | 状态 |",
                    "| --- | ---: | --- | --- | --- |"]
        for r in rows:
            md_lines.append(f"| {r['document_id']} | {r['known_score'] or '-'} | {r['recommendation'] or '-'} | {r['审批']} | {r['status']} |")
    if bookings:
        md_lines += ["", "## 排期", "", "| 轮次 | 候选人 | 时段 | 状态 |", "| --- | --- | --- | --- |"]
        for b in bookings:
            md_lines.append(f"| {b['round']} | {b['candidate_id']} | {b['slot_id'] or '-'} | {b['status']} |")
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    xlsx_path = None
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "候选人"
        if redact:
            ws.append(["run_id", "document_id", "姓名", "手机", "邮箱", "状态", "评分", "机器建议", "审批"])
            for r in rows:
                ws.append([r["run_id"], r["document_id"], r.get("pii_name", ""),
                           r.get("pii_phone", ""), r.get("pii_email", ""), r["status"],
                           r["known_score"], r["recommendation"], r["审批"]])
        else:
            ws.append(["run_id", "document_id", "状态", "评分", "机器建议", "审批"])
            for r in rows:
                ws.append([r["run_id"], r["document_id"], r["status"], r["known_score"],
                           r["recommendation"], r["审批"]])
        if bookings:
            ws2 = wb.create_sheet("排期")
            ws2.append(["轮次", "候选人", "时段", "状态"])
            for b in bookings:
                ws2.append([b["round"], b["candidate_id"], b["slot_id"], b["status"]])
        xlsx_path = base.with_suffix(".xlsx")
        wb.save(xlsx_path)
    except Exception as e:
        print(f"Excel 导出跳过: {e}", file=sys.stderr)

    print(json.dumps({
        "json": str(json_path), "markdown": str(md_path),
        "xlsx": str(xlsx_path) if xlsx_path else None,
        "candidates": len(rows), "bookings": len(bookings),
    }, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# clean（规格 §12 本地清理入口）
# ---------------------------------------------------------------------------
def cmd_clean(args) -> int:
    repo = _load_repo()
    doc = repo.get_document(args.document_id)
    if doc is None:
        print(f"document 不存在: {args.document_id}", file=sys.stderr)
        return 1
    thread_ids = repo.delete_document(args.document_id)
    if not args.keep_checkpoints:
        repo.delete_checkpoint_threads(thread_ids)
    if args.delete_file:
        fp = Path(doc["storage_path"])
        if fp.exists():
            fp.unlink()
            print(f"已删除原文件: {fp}")
    print(json.dumps({
        "document_id": args.document_id,
        "deleted_business_rows": True,
        "checkpoint_threads": thread_ids,
        "checkpoint_cleaned": not args.keep_checkpoints,
        "file_deleted": bool(args.delete_file),
    }, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# purge（PII 保留期清理）：按 created_at 早于保留期批量级联删除
# ---------------------------------------------------------------------------
def cmd_purge(args) -> int:
    repo = _load_repo()
    # --days 优先；否则回退 config.PII_RETENTION_DAYS；<=0/空 = 不启用
    days = args.days if args.days is not None else None
    if days is None:
        from src import config
        days = config.PII_RETENTION_DAYS
    if not days or days <= 0:
        print("保留期未启用（days<=0）：用 --days N 指定，或设置 PII_RETENTION_DAYS。",
              file=sys.stderr)
        return 1

    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    docs = repo.list_documents_older_than(cutoff)
    doc_ids = [d["id"] for d in docs]
    result: Dict[str, Any] = {
        "dry_run": bool(args.dry_run),
        "retention_days": days,
        "cutoff": cutoff,
        "matched": len(doc_ids),
        "document_ids": doc_ids,
    }
    if args.dry_run:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    deleted: List[str] = []
    for d in docs:
        thread_ids = repo.delete_document(d["id"])
        if not args.keep_checkpoints:
            repo.delete_checkpoint_threads(thread_ids)
        if args.delete_files and d.get("storage_path"):
            fp = Path(d["storage_path"])
            try:
                if fp.exists():
                    fp.unlink()
            except OSError as e:
                print(f"删除原文件失败 {fp}: {e}", file=sys.stderr)
        deleted.append(d["id"])
    result["deleted"] = deleted
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# batch / graph（P1 入口，兼容保留）
# ---------------------------------------------------------------------------
def cmd_batch(args) -> int:
    from src.batch import process_batch, save_results

    result = process_batch(limit=args.limit, resume_dir=Path(args.dir))
    paths = save_results(result)
    print(f"\n处理完成: {result['处理总数']} 份 | 推荐: {result['推荐数']} 份")
    print(f"结果文件: {json.dumps(paths, ensure_ascii=False, indent=2)}")
    return 0


def cmd_graph(args) -> int:
    graph = build_resume_graph()
    try:
        from langchain_core.runnables.graph import CurveStyle, NodeColors
        png = graph.get_graph().draw_mermaid_png(
            curve_style=CurveStyle.LINEAR,
            node_colors=NodeColors(start="#4B9CD3", end="#FF6B6B", other="#45B7D1"),
        )
        out_path = OUTPUT_DIR / "workflow.png"
        out_path.write_bytes(png)
        print(f"工作流图已保存: {out_path}")
    except Exception as e:
        print(f"生成 PNG 失败（可能需要 graphviz）: {e}")
        out_path = OUTPUT_DIR / "workflow.mmd"
        out_path.write_text(graph.get_graph().draw_mermaid(), encoding="utf-8")
        print(f"Mermaid 文本已保存: {out_path}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="基于 LangGraph 的简历筛选系统")
    # 兼容旧用法（P0/P1）：--file / --batch / --graph / --dir
    parser.add_argument("--file", "-f", help="单份简历路径（旧用法）")
    parser.add_argument("--batch", "-b", action="store_true", help="批量处理（旧用法）")
    parser.add_argument("--limit", "-l", type=int, help="批量数量限制")
    parser.add_argument("--graph", "-g", action="store_true", help="导出工作流图（旧用法）")
    parser.add_argument("--dir", "-d", default=str(RESUME_DIR), help="简历目录")

    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("analyze", help="单份分析（停在审批节点）")
    p.add_argument("file")
    p.add_argument("--thread-id", "-t", default=None)
    p.add_argument("--batch-id", "-b", default=None)

    p = sub.add_parser("batch", help="批量分析（P1 入口）")
    p.add_argument("--dir", "-d", default=str(RESUME_DIR))
    p.add_argument("--limit", "-l", type=int, default=None)

    p = sub.add_parser("review", help="提交审批")
    p.add_argument("assessment_id")
    p.add_argument("decision", choices=["approve", "hold", "needs_review"])
    p.add_argument("--version", default=None)
    p.add_argument("--request-id", default="")
    p.add_argument("--reason", default="")

    p = sub.add_parser("resume", help="恢复被中断的任务")
    p.add_argument("run_id")
    p.add_argument("--decision", choices=["approve", "hold", "needs_review"], default=None)

    p = sub.add_parser("schedule", help="批次排期 + 建会通知（可注入故障演示幂等）")
    p.add_argument("batch_id")
    p.add_argument("--availability", default=None, help='JSON，如 {"c1":[1,2]}')
    p.add_argument("--fail-notify", type=int, default=0, help="通知器注入失败次数")
    p.add_argument("--fail-meeting", type=int, default=0, help="日历注入失败次数")
    p.add_argument("--retry", action="store_true", help="失败后自动重试一次")

    p = sub.add_parser("export", help="导出批次状态（JSON/Markdown/Excel）")
    p.add_argument("batch_id")
    p.add_argument("--dir", default=None)
    p.add_argument("--redact", action="store_true",
                   help="导出候选人联系方式并打码（姓名/手机/邮箱 mask）；不传则不带 PII 字段")

    p = sub.add_parser("clean", help="本地清理：按 document_id 删除（规格 §12）")
    p.add_argument("document_id")
    p.add_argument("--delete-file", action="store_true", help="同时删除原文件")
    p.add_argument("--keep-checkpoints", action="store_true", help="保留 LangGraph 检查点")

    p = sub.add_parser("purge", help="PII 保留期清理：按 created_at 早于保留期批量删除")
    p.add_argument("--days", type=int, default=None,
                   help="保留天数（默认读 PII_RETENTION_DAYS；<=0 视为不启用）")
    p.add_argument("--dry-run", action="store_true", help="只列出匹配文档，不删除")
    p.add_argument("--delete-files", action="store_true", help="同时删除 storage_path 原文件")
    p.add_argument("--keep-checkpoints", action="store_true", help="保留 LangGraph 检查点")

    p = sub.add_parser("graph", help="导出工作流图")
    p.add_argument("--png", action="store_true", help="优先 PNG")

    args = parser.parse_args()
    handlers = {
        "analyze": cmd_analyze, "batch": cmd_batch, "review": cmd_review,
        "resume": cmd_resume, "schedule": cmd_schedule, "export": cmd_export,
        "clean": cmd_clean, "graph": cmd_graph, "purge": cmd_purge,
    }
    if args.command is None:
        # 兼容旧用法：--file / --batch / --graph 顶层参数
        _legacy_main(args)
        return
    sys.exit(handlers[args.command](args))


def _legacy_main(args: argparse.Namespace) -> None:
    """兼容 P0/P1 的顶层参数调用（--file/--batch/--graph）。"""
    if args.graph:
        sys.exit(cmd_graph(args))
    if args.file:
        sys.exit(cmd_analyze(args))
    if args.batch:
        sys.exit(cmd_batch(args))
    # 默认：跑一份样例
    d = Path(args.dir)
    files = [p for p in d.iterdir() if p.is_file()] if d.exists() else []
    if files:
        report = run_single(str(files[0]), JOB_TITLE)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"目录为空: {args.dir}")


if __name__ == "__main__":
    main()
