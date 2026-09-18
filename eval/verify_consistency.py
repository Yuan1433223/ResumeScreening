# -*- coding: utf-8 -*-
"""T5e 一致性验证：工作台路径 vs 评测路径。

背景：README 中 F1（mock 0.46 vs model 0.67）来自评测路径 run_eval.py
（每次重新 parse_document + extract_facts）；工作台路径 src.workflow.run_single
复用 SQLite pages 缓存 + 同一 extract_facts。本脚本验证两条路径对同一批文档
产出是否一致，并顺带产出 eval_004-010 的 model 候选事实（供人工标注）。

层次：
  层1（免费、确定性）：工作台 pages（缓存 or 重解析并写缓存）vs 评测路径重解析
      → 对已标注 dev 样本（eval_001-003）重 parse_document 逐页对比。
  层2（免费、确定性）：mock 抽取对比（规则确定性，评测已有结果 vs 工作台重跑）。
  层3（花钱、统计性）：eval_001-010 工作台路径 model 抽取，与评测已有 model
      结果（matched/missing/spurious + F1）对比；temperature=0 下应基本一致。
  层4（副产品）：eval_004-010 model 候选事实（标注模板，供人工审改 GT）。

用法：
  python eval/verify_consistency.py            # 全部
  python eval/verify_consistency.py --skip-model  # 只跑免费层（层1/2）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import RESUME_DIR  # noqa: E402
from src.extraction import extract_facts  # noqa: E402
from src.parser import parse_document  # noqa: E402
from src.repository import get_repository  # noqa: E402
from src.workflow import _cached_pages, _doc_id  # noqa: E402

from eval.run_eval import (  # noqa: E402
    EvalLabel,
    compute_fact_metrics,
    fact_key,
    load_labels,
    normalize_fact_value,
)

# 评测路径已有结果（9/16 跑，README 数字来源）
PRIOR_EVAL_DIR = ROOT / "eval" / "results" / "20260916_095847"


def _label_map(labels: Dict[str, EvalLabel]) -> Dict[str, EvalLabel]:
    return {k: v for k, v in labels.items() if v.expected_facts}


def workbench_pages(file_path: str, document_id: str):
    """工作台路径的 pages 获取逻辑（与 src.workflow.run_single 对齐）。

    - sha256 去重：文件已入库 → 复用库内 document_id；否则用传入 id 并入库
    - pages 缓存命中优先；否则解析并存库（pages 表外键引用 documents）
    """
    import hashlib

    repo = get_repository()
    sha = hashlib.sha256(Path(file_path).read_bytes()).hexdigest()
    existing = repo.get_document_by_sha(sha)
    final_id = existing["id"] if existing else document_id
    cached = _cached_pages(repo, final_id)
    if cached is not None:
        return cached, "cache", final_id
    pages, status, reason = parse_document(final_id, file_path)
    repo.upsert_document(
        final_id, sha, file_path,
        mime=Path(file_path).suffix.lower().lstrip(".") or "unknown",
        parser_version="pdfplumber/rapidocr",
    )
    repo.save_pages(final_id, pages)
    return pages, "parse+save", final_id


def load_prior_detail(path: Path) -> Dict[str, Dict[str, list]]:
    """读取评测路径 detail.jsonl → {doc_id: {matched/missing/spurious}}。"""
    out: Dict[str, Dict[str, list]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        out.setdefault(d["document_id"], {})[d["extractor"]] = {
            "matched": set(d.get("matched", [])),
            "missing": set(d.get("missing", [])),
            "spurious": set(d.get("spurious", [])),
        }
    return out


def set_of(keys) -> str:
    def fmt(k):
        return f"{k[0]}={k[1]}" if isinstance(k, tuple) else str(k)
    return ", ".join(sorted(fmt(k) for k in keys))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-model", action="store_true", help="只跑免费层（层1/2）")
    parser.add_argument("--tag", default=None)
    args = parser.parse_args()

    labels = load_labels(ROOT / "eval" / "labels")
    labeled = _label_map(labels)
    out_dir = ROOT / "eval" / "results" / (
        args.tag or "consistency_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    report: Dict[str, object] = {
        "run": {"created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "labeled_dev": sorted(labeled)},
        "layer1_pages": {},
        "layer2_mock": {},
        "layer3_model": {},
        "layer4_candidates": {},
    }

    # ---- 层1：pages 输入一致性（免费） -----------------------------------
    print("== 层1：pages 输入一致性 ==")
    for doc_id in sorted(labeled):
        fp = RESUME_DIR / labeled[doc_id].source_file
        if not fp.exists():
            print(f"[skip] {doc_id} 源文件缺失 {labeled[doc_id].source_file}")
            continue
        wb_pages, src, _ = workbench_pages(str(fp), _doc_id(str(fp)))
        # 评测路径：重新解析（与 run_eval 相同调用）
        ev_pages, status, reason = parse_document(doc_id, str(fp))
        same_pages = len(wb_pages) == len(ev_pages) and all(
            a.text == b.text for a, b in zip(wb_pages, ev_pages)
        )
        detail = {
            "workbench_source": src,
            "workbench_pages": len(wb_pages),
            "eval_pages": len(ev_pages),
            "pages_identical": same_pages,
            "chars": sum(len(p.text) for p in wb_pages),
        }
        if not same_pages:
            for i, (a, b) in enumerate(zip(wb_pages, ev_pages)):
                if a.text != b.text:
                    detail["first_diff_page"] = i + 1
                    detail["diff_preview"] = (a.text[:60], b.text[:60])
                    break
        report["layer1_pages"][doc_id] = detail
        print(f"  {doc_id}: pages {'一致' if same_pages else '不一致'} "
              f"(工作台 {detail['workbench_pages']} 页/{src} vs 评测 {detail['eval_pages']} 页)")

    # ---- 层2：mock 抽取一致性（免费，规则确定性） --------------------------
    print("== 层2：mock 抽取一致性 ==")
    prior = load_prior_detail(PRIOR_EVAL_DIR / "detail.jsonl")
    for doc_id in sorted(labeled):
        fp = RESUME_DIR / labeled[doc_id].source_file
        if not fp.exists():
            continue
        wb_pages, _, _ = workbench_pages(str(fp), _doc_id(str(fp)))
        if not wb_pages:
            report["layer2_mock"][doc_id] = {"skip": "pages 为空"}
            continue
        report_row = extract_facts(doc_id, wb_pages, extractor_version="mock")
        keys = {fact_key(f) for f in report_row.facts if f.status.value == "supported"}
        gt = {(e.kind, normalize_fact_value(e.kind, e.value))
              for e in labeled[doc_id].expected_facts}
        matched = keys & gt
        prior_row = prior.get(doc_id, {}).get("mock")
        same = prior_row is not None and (
            set_of(matched) == set_of(prior_row["matched"])
            and set_of(keys - gt) == set_of(prior_row["spurious"])
            and set_of(gt - keys) == set_of(prior_row["missing"])
        )
        report["layer2_mock"][doc_id] = {
            "workbench_matched": set_of(matched),
            "prior_matched": set_of(prior_row["matched"]) if prior_row else None,
            "identical": same,
        }
        print(f"  {doc_id}: {'一致' if same else '不一致'} "
              f"(匹配 {set_of(matched) or '∅'})")

    # ---- 层3：model 抽取一致性（花钱；工作台路径 10 份） -------------------
    print("== 层3：model 抽取（工作台路径，10 份）==")
    if not args.skip_model:
        workbench_facts: List[dict] = []
        for doc_id in sorted(labels):
            fp = RESUME_DIR / labels[doc_id].source_file
            if not fp.exists():
                print(f"[skip] {doc_id} 源文件缺失")
                continue
            wb_pages, src, _ = workbench_pages(str(fp), _doc_id(str(fp)))
            if not wb_pages:
                print(f"[skip] {doc_id} pages 为空")
                continue
            report_row = extract_facts(doc_id, wb_pages, extractor_version="model")
            keys = {fact_key(f) for f in report_row.facts if f.status.value == "supported"}
            gt = ({(e.kind, normalize_fact_value(e.kind, e.value))
                   for e in labeled[doc_id].expected_facts} if doc_id in labeled else set())
            matched = keys & gt
            record = {
                "document_id": doc_id,
                "extractor": report_row.extractor_version,
                "pages_source": src,
                "facts": [{"kind": f.kind, "value": f.value, "status": f.status.value,
                           "page": f.evidences[0].page_number if f.evidences else None,
                           "quote": f.evidences[0].quote if f.evidences else ""}
                          for f in report_row.facts],
                "employments": [{"start_month": e.start_month, "end_month": e.end_month,
                                 "is_current": e.is_current, "domain": e.domain,
                                 "relevant": e.relevant} for e in report_row.employments],
                "warnings": report_row.warnings[:5],
                "stats": report_row.stats,
                "matched": set_of(matched),
                "missing": set_of(gt - keys),
                "spurious": set_of(keys - gt),
            }
            workbench_facts.append(record)
            if doc_id in labeled:
                prior_row = prior.get(doc_id, {}).get("model")
                same = prior_row is not None and (
                    set_of(matched) == set_of(prior_row["matched"])
                    and set_of(keys - gt) == set_of(prior_row["spurious"])
                    and set_of(gt - keys) == set_of(prior_row["missing"])
                )
                report["layer3_model"][doc_id] = {
                    "workbench_matched": set_of(matched),
                    "prior_matched": set_of(prior_row["matched"]) if prior_row else None,
                    "identical": same,
                    "calls": report_row.stats.get("calls"),
                    "duration_s": round(report_row.stats.get("duration_s", 0.0), 1),
                    "input_tokens": report_row.stats.get("input_tokens"),
                    "output_tokens": report_row.stats.get("output_tokens"),
                }
                print(f"  {doc_id}: {'一致' if same else '不一致'} "
                      f"(匹配 {set_of(matched) or '∅'} | {report_row.stats.get('calls')} 调用 "
                      f"{round(report_row.stats.get('duration_s', 0.0), 0)}s)")
            else:
                print(f"  {doc_id}: 候选生成（facts={len(report_row.facts)} "
                      f"| {report_row.stats.get('calls')} 调用）")

        (out_dir / "workbench_facts.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in workbench_facts),
            encoding="utf-8",
        )

        # 已标注 dev：工作台 model 的 F1（与评测 0.67 对照）
        labeled_results = []
        for r in workbench_facts:
            if r["document_id"] not in labeled:
                continue
            gt = {(e.kind, normalize_fact_value(e.kind, e.value))
                  for e in labeled[r["document_id"]].expected_facts}
            keys = {(f["kind"], normalize_fact_value(f["kind"], f["value"]))
                    for f in r["facts"] if f["status"] == "supported"}
            labeled_results.append((r["document_id"], gt, keys))
        if labeled_results:
            from eval.run_eval import DocResult
            results = []
            for doc_id, gt, keys in labeled_results:
                dr = DocResult(doc_id=doc_id, extractor_version="model")
                dr.gt_facts = gt
                dr.extracted_keys = keys
                dr.matched = keys & gt
                results.append(dr)
            fm = compute_fact_metrics(results)
            report["layer3_model"]["workbench_f1"] = fm
            print(f"  工作台 model F1: P={fm['precision']:.3f} R={fm['recall']:.3f} "
                  f"F1={fm['f1']:.3f}（评测 0.667）")

    # ---- 层4：004-010 model 候选（标注模板） ------------------------------
    cand_dir = ROOT / "eval" / "labels" / "candidates"
    cand_dir.mkdir(exist_ok=True)
    made = []
    for doc_id in sorted(labels):
        if labels[doc_id].expected_facts:
            continue  # 已标注不生成
        jsonl = out_dir / "workbench_facts.jsonl"
        if not jsonl.exists():
            continue
        rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()
                if json.loads(line)["document_id"] == doc_id]
        if not rows:
            continue
        r = rows[0]
        candidate = {
            "document_id": doc_id,
            "split": labels[doc_id].split,
            "source_file": labels[doc_id].source_file,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "extractor": r["extractor"],
            "candidate_facts": [
                {"kind": f["kind"], "value": f["value"], "status": f["status"],
                 "page": f["page"], "quote": f["quote"]} for f in r["facts"]
            ],
            "candidate_employments": r["employments"],
            "warnings": r["warnings"],
        }
        fp = cand_dir / f"{doc_id}.json"
        fp.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        made.append(doc_id)
    report["layer4_candidates"] = {"generated": made}
    if made:
        print(f"== 层4：已生成候选标注 {len(made)} 份 → eval/labels/candidates/ ==")

    (out_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{out_dir / 'summary.json'}")


if __name__ == "__main__":
    main()
