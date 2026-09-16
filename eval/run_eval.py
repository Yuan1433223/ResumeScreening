# -*- coding: utf-8 -*-
"""T4 评测 runner：真实简历 vs 标注样本的抽取质量对照（骨架）。

用法示例：
  python eval/run_eval.py --limit 5                  # 前 5 份真实简历：无标注基线（证据/路由/成本质量）
  python eval/run_eval.py --extractors mock          # 只跑规则版（无需云端、不花钱）
  python eval/run_eval.py --labels-dir eval/labels   # 有标注 → 事实准确率对照
  python eval/run_eval.py --split test               # 只看 test 划分（dev 调提示词，test 验收）

输出（eval/results/<tag>/）：
  summary.json   # 聚合结果（机器可读；脱敏，可入库）
  summary.md     # 人读报告（脱敏，无真实姓名/引文）
  detail.jsonl   # 每份文档明细（含引文片段，仅本地，不入库）

口径约定（详见 eval/README.md）：
  - dev/test 由标注文件 split 字段决定；调提示词只允许看 dev
  - 事实匹配：kind + 归一化 value（年限类转 int）；只统计 supported 事实
  - 指标：micro precision/recall/F1、证据定位有效率、引用完整度（启发式）、
    转人工率、空报告率、成本与延迟 P50/P95
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, Field

from src.config import MODEL_CONFIG, RESUME_DIR
from src.evidence import validate_evidence
from src.extraction import extract_facts
from src.parser import parse_document
from src.schemas import ExtractionReport, Fact, Page

# ---------------------------------------------------------------------------
# 标注格式（eval/labels/<document_id>.json）
# ---------------------------------------------------------------------------


class ExpectedFact(BaseModel):
    """ground truth 事实：kind + value（与 FACT_KINDS 枚举一致）。"""

    kind: str
    value: object
    quote: str = ""          # 原文佐证（可选，标注时尽量填）
    comment: str = ""


class ExpectedEmployment(BaseModel):
    start: str = ""          # "2016" 或 "2016-03"
    end: str = ""            # "2020" / "2020-06" / "至今"
    relevant: Optional[bool] = None
    comment: str = ""


class EvalLabel(BaseModel):
    document_id: str         # 匿名编号（如 eval_001），报告与明细只用它
    source_file: str = ""    # 真实文件名（本地标注参考，不入库）
    split: str = "dev"       # dev / test
    notes: str = ""
    expected_facts: List[ExpectedFact] = Field(default_factory=list)
    expected_employments: List[ExpectedEmployment] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------
YEAR_KINDS = {"total_years", "relevant_years"}


def normalize_fact_value(kind: str, value: object) -> object:
    """事实值归一化：年限类转 int，其余转 str，保证两抽取器可比。"""
    if kind in YEAR_KINDS:
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return value
    return str(value)


def fact_key(fact: Fact) -> Tuple[str, object]:
    return (fact.kind, normalize_fact_value(fact.kind, fact.value))


def percentile(sorted_values: Sequence[float], p: float) -> Optional[float]:
    if not sorted_values:
        return None
    idx = int((len(sorted_values) - 1) * p)
    return float(sorted_values[idx])


@dataclass
class DocResult:
    doc_id: str
    extractor_version: str
    report: Optional[ExtractionReport] = None
    validation: Optional[object] = None
    pages: List[Page] = field(default_factory=list)
    parse_status: str = ""
    parse_reason: str = ""
    gt_facts: Set[Tuple[str, object]] = field(default_factory=set)
    extracted_keys: Set[Tuple[str, object]] = field(default_factory=set)
    matched: Set[Tuple[str, object]] = field(default_factory=set)


REVIEW_MARKERS = (
    "转人工", "人工复核", "定位失败", "抽取失败",
    "未启用", "未配置", "无法解析", "正文为空",
)


def review_triggered(report: ExtractionReport) -> bool:
    return any(m in w for w in report.warnings for m in REVIEW_MARKERS)


def truncated_quote(quote_start: int, quote_end: int, page_text: str) -> bool:
    """启发式：引文起点/终点切断在 ASCII 词/数字中间（如 'achieveme'、'薪资：4'）。

    中文字符边界不算截断（CJK 无词边界，避免对中文文本饱和）。
    """
    def ascii_word_char(ch: str) -> bool:
        return ch.isascii() and (ch.isalnum() or ch in "_")

    before = page_text[quote_start - 1:quote_start] if quote_start > 0 else ""
    after = page_text[quote_end:quote_end + 1] if quote_end < len(page_text) else ""
    return bool(
        (after and ascii_word_char(after))
        or (before and ascii_word_char(before))
    )


def evidence_quality(report: ExtractionReport, pages: List[Page]) -> Dict[str, float]:
    """引用定位有效率 + 完整度（启发式）+ 平均引文长度（按事实计数加权）。"""
    page_map = {p.page_number: p for p in pages}
    ok, truncated, quote_lens = 0, 0, []
    for f in report.facts:
        if not f.evidences:
            continue
        ev = f.evidences[0]
        p = page_map.get(ev.page_number)
        if p is not None and 0 <= ev.start < ev.end <= len(p.text) \
                and p.text[ev.start:ev.end] == ev.quote:
            ok += 1
            quote_lens.append(ev.end - ev.start)
            if truncated_quote(ev.start, ev.end, p.text):
                truncated += 1
    total = len(report.facts)
    return {
        "fact_count": float(total),
        "evidence_ok": float(ok),
        "evidence_ok_rate": ok / total if total else 0.0,
        "truncated_quotes": float(truncated),
        "truncated_rate": truncated / ok if ok else 0.0,
        "avg_quote_len": (sum(quote_lens) / len(quote_lens)) if quote_lens else 0.0,
    }


def employment_metrics(report: ExtractionReport,
                       label: Optional[EvalLabel]) -> Dict[str, float]:
    """任职区间年份级匹配（start 年 + end 年；'至今' 记 None）。"""
    if not label or not label.expected_employments:
        return {"expected": 0.0, "matched": 0.0}
    matched = 0
    for exp in label.expected_employments:
        esy, eey = year_of(exp.start), year_of(exp.end)
        for g in report.employments:
            if esy is not None and esy == year_of(g.start_month) \
                    and eey == year_of(g.end_month):
                matched += 1
                break
    return {"expected": float(len(label.expected_employments)),
            "matched": float(matched)}


def year_of(month_str: Optional[str]) -> Optional[int]:
    if not month_str:
        return None
    try:
        return int(str(month_str)[:4])
    except ValueError:
        return None


def compute_fact_metrics(results: Sequence[DocResult]) -> Dict[str, float]:
    """micro 汇总：所有文档 pooled 后算 precision/recall/F1。"""
    gt_total = sum(len(r.gt_facts) for r in results)
    ext_total = sum(len(r.extracted_keys) for r in results)
    matched_total = sum(len(r.matched) for r in results)
    precision = matched_total / ext_total if ext_total else 0.0
    recall = matched_total / gt_total if gt_total else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "gt_facts": float(gt_total),
        "extracted_facts": float(ext_total),
        "matched": float(matched_total),
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def aggregate_stats(results: Sequence[DocResult]) -> Dict[str, float]:
    """模型调用统计：延迟 P50/P95、token、成本（只有 model 抽取器有 stats）。"""
    durs = sorted(r.report.stats.get("duration_s", 0.0) for r in results
                  if r.report and r.report.stats.get("duration_s", 0.0) > 0)
    calls = [int(r.report.stats.get("calls", 0)) for r in results if r.report]
    tokens_in = sum(int(r.report.stats.get("input_tokens", 0)) for r in results if r.report)
    tokens_out = sum(int(r.report.stats.get("output_tokens", 0)) for r in results if r.report)
    cost = sum(float(r.report.stats.get("cost_cents", 0)) for r in results if r.report)
    n = len(results)
    return {
        "docs": float(n),
        "calls_total": float(sum(calls)),
        "calls_avg": sum(calls) / n if n else 0.0,
        "input_tokens_total": float(tokens_in),
        "output_tokens_total": float(tokens_out),
        "duration_p50_s": percentile(durs, 0.50) or 0.0,
        "duration_p95_s": percentile(durs, 0.95) or 0.0,
        "cost_cents_total": cost,
        "cost_cents_avg": cost / n if n else 0.0,
    }


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------


def load_labels(labels_dir: Path) -> Dict[str, EvalLabel]:
    labels: Dict[str, EvalLabel] = {}
    if not labels_dir.is_dir():
        return labels
    for fp in sorted(labels_dir.glob("*.json")):
        try:
            label = EvalLabel.model_validate_json(fp.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 单个标注文件损坏不阻断整体
            print(f"[warn] 标注文件解析失败，跳过：{fp.name}（{e}）")
            continue
        if label.document_id in labels:
            print(f"[warn] document_id 重复，后者覆盖：{label.document_id}")
        labels[label.document_id] = label
    return labels


def iter_docs(labels: Dict[str, EvalLabel], labels_dir: Path, split: Optional[str],
              limit: Optional[int]) -> List[Tuple[str, Path, Optional[EvalLabel]]]:
    """标注模式：labels 指定 source_file；无标注：真实简历目录兜底（基线模式）。"""
    if labels:
        items = []
        for label in labels.values():
            if split and label.split != split:
                continue
            src = label.source_file
            fp = RESUME_DIR / src if src else None
            if fp is None or not fp.exists():
                print(f"[warn] 找不到 source_file：{src or label.document_id}，跳过")
                continue
            items.append((label.document_id, fp, label))
        return items[:limit] if limit else items
    if labels_dir.is_dir() and any(labels_dir.glob("*.json")):
        print("[warn] labels 目录存在但无有效标注，退回真实简历基线模式")
    files = sorted(list(RESUME_DIR.glob("*.pdf")) + list(RESUME_DIR.glob("*.jpg")))
    items = [(f"eval_{i:03d}", fp, None) for i, fp in enumerate(files)]
    return items[:limit] if limit else items


def process_doc(doc_id: str, file_path: Path, extractor_version: str,
                label: Optional[EvalLabel]) -> DocResult:
    pages, status, reason = parse_document(doc_id, str(file_path))
    gt: Set[Tuple[str, object]] = set()
    if label:
        gt = {(e.kind, normalize_fact_value(e.kind, e.value)) for e in label.expected_facts}
    result = DocResult(
        doc_id=doc_id,
        extractor_version=extractor_version,
        pages=pages,
        parse_status=status.value,
        parse_reason=reason,
        gt_facts=gt,
    )
    if not pages:
        result.report = ExtractionReport(extractor_version=extractor_version,
                                         warnings=["解析无文本"])
        return result
    report = extract_facts(doc_id, pages, extractor_version=extractor_version)
    result.report = report
    result.validation = validate_evidence(report.facts, pages)
    result.extracted_keys = {fact_key(f) for f in report.facts
                             if f.status.value == "supported"}
    if label:
        result.matched = result.extracted_keys & gt
    return result


def aggregate_extractor(results: Sequence[DocResult],
                        docs: Sequence[Tuple[str, Path, Optional[EvalLabel]]]) -> dict:
    n = len(results)
    m: dict = {"evidence": {}, "cost": None, "fact_metrics": None,
               "employment": None}

    # 证据与路由（不依赖标注）
    ev_total = ev_ok = ev_trunc = 0
    quote_len_sum = 0.0
    reviews = empties = 0
    for r in results:
        if not r.report:
            empties += 1
            continue
        if not r.report.facts:
            empties += 1
        if review_triggered(r.report):
            reviews += 1
        eq = evidence_quality(r.report, r.pages)
        ev_total += int(eq["fact_count"])
        ev_ok += int(eq["evidence_ok"])
        ev_trunc += int(eq["truncated_quotes"])
        quote_len_sum += eq["avg_quote_len"] * eq["fact_count"]
    m["evidence"] = {
        "evidence_ok_rate": ev_ok / ev_total if ev_total else 0.0,
        "truncated_rate": ev_trunc / ev_ok if ev_ok else 0.0,
        "avg_quote_len": quote_len_sum / ev_total if ev_total else 0.0,
        "review_rate": reviews / n if n else 0.0,
        "empty_rate": empties / n if n else 0.0,
    }

    # 事实准确率（有标注才计算）
    if any(r.gt_facts for r in results):
        m["fact_metrics"] = compute_fact_metrics(results)

    # 任职区间匹配（标注含 expected_employments 才计算）
    emp_exp = emp_match = 0
    for (doc_id, fp, label), r in zip(docs, results):
        if label and r.report:
            em = employment_metrics(r.report, label)
            emp_exp += int(em["expected"])
            emp_match += int(em["matched"])
    if emp_exp:
        m["employment"] = {"expected": float(emp_exp), "matched": float(emp_match)}

    # 成本与延迟（model 抽取器才有 stats）
    if any(r.extractor_version.startswith("model") for r in results):
        m["cost"] = aggregate_stats(results)
    return m


def render_md(summary: dict) -> str:
    lines = [
        "# T4 评测报告（脱敏）", "",
        f"- 时间：{summary['run']['created_at']}",
        f"- 抽取器：{', '.join(summary['run']['extractors'])}",
        f"- 文档数：{summary['run']['docs']}",
        f"- 数据范围：{summary['run']['scope']}",
        "",
    ]
    for name, m in summary["per_extractor"].items():
        lines.append(f"## {name}")
        if m.get("fact_metrics"):
            fm = m["fact_metrics"]
            lines.append(
                "### 事实准确率（micro）\n\n"
                "| precision | recall | F1 | GT事实 | 抽取事实 | 匹配 |\n"
                "|---|---|---|---|---|---|\n"
                f"| {fm['precision']:.3f} | {fm['recall']:.3f} | {fm['f1']:.3f} "
                f"| {fm['gt_facts']:.0f} | {fm['extracted_facts']:.0f} | {fm['matched']:.0f} |\n"
            )
        ev = m["evidence"]
        lines.append(
            "### 证据与路由质量\n\n"
            "| 证据定位有效率 | 截断引用率 | 平均引文长度 | 转人工率 | 空报告率 |\n"
            "|---|---|---|---|---|\n"
            f"| {ev['evidence_ok_rate']:.3f} | {ev['truncated_rate']:.3f} "
            f"| {ev['avg_quote_len']:.1f} | {ev['review_rate']:.3f} | {ev['empty_rate']:.3f} |\n"
        )
        if m.get("employment"):
            em = m["employment"]
            lines.append(
                "### 任职区间匹配\n\n"
                f"匹配 {em['matched']:.0f}/{em['expected']:.0f}\n"
            )
        if m.get("cost"):
            c = m["cost"]
            lines.append(
                "### 成本与延迟\n\n"
                "| 总调用 | 均值调用 | 输入token | 输出token | P50(s) | P95(s) | 总成本(分) | 单份(分) |\n"
                "|---|---|---|---|---|---|---|---|\n"
                f"| {c['calls_total']:.0f} | {c['calls_avg']:.1f} | {c['input_tokens_total']:.0f} "
                f"| {c['output_tokens_total']:.0f} | {c['duration_p50_s']:.1f} "
                f"| {c['duration_p95_s']:.1f} | {c['cost_cents_total']:.1f} | {c['cost_cents_avg']:.2f} |\n"
            )
        lines.append("")
    return "\n".join(lines)


def run(labels_dir: Path, results_dir: Path, extractors: Sequence[str],
        split: Optional[str], limit: Optional[int], tag: str) -> dict:
    labels = load_labels(labels_dir)
    docs = iter_docs(labels, labels_dir, split, limit)
    if not docs:
        raise SystemExit("没有可处理的文档（检查 labels 或 RESUME_DIR）")

    summary = {
        "run": {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "extractors": list(extractors),
            "docs": len(docs),
            "scope": "真实简历(本地评测)" if not labels else f"标注样本 {len(labels)} 份",
            "split": split or "all",
        },
        "per_extractor": {},
    }

    out_dir = results_dir / tag
    out_dir.mkdir(parents=True, exist_ok=True)

    detail_path = out_dir / "detail.jsonl"
    with detail_path.open("w", encoding="utf-8") as fh:
        for ex in extractors:
            results = []
            for doc_id, fp, label in docs:
                r = process_doc(doc_id, fp, ex, label)
                results.append(r)
                fh.write(json.dumps({
                    "document_id": r.doc_id,
                    "extractor": r.extractor_version,
                    "parse_status": r.parse_status,
                    "parse_reason": r.parse_reason,
                    "facts": len(r.report.facts) if r.report else 0,
                    "employments": len(r.report.employments) if r.report else 0,
                    "warnings": r.report.warnings[:5] if r.report else [],
                    "matched": sorted(f"{k}={v}" for k, v in r.matched),
                    "missing": sorted(f"{k}={v}" for k, v in (r.gt_facts - r.matched)),
                    "spurious": sorted(f"{k}={v}" for k, v in (r.extracted_keys - r.matched)),
                    "stats": r.report.stats if r.report else {},
                    "quotes": [f.evidences[0].quote[:40] for f in (r.report.facts if r.report else [])
                               if f.evidences],
                }, ensure_ascii=False) + "\n")
            summary["per_extractor"][ex] = aggregate_extractor(results, docs)

    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary.md").write_text(render_md(summary), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="T4 评测 runner")
    parser.add_argument("--labels-dir", type=Path,
                        default=ROOT / "eval" / "labels",
                        help="标注样本目录（默认 eval/labels，无标注=基线模式）")
    parser.add_argument("--results-dir", type=Path,
                        default=ROOT / "eval" / "results",
                        help="输出目录（默认 eval/results）")
    parser.add_argument("--extractors", default="mock,model",
                        help="逗号分隔的抽取器版本（mock,model）")
    parser.add_argument("--split", default=None, choices=["dev", "test"],
                        help="只看 dev 或 test 划分")
    parser.add_argument("--limit", type=int, default=None,
                        help="最多处理 N 份文档（冒烟用）")
    parser.add_argument("--tag", default=None,
                        help="结果子目录名（默认时间戳）")
    args = parser.parse_args()

    extractors = [e.strip() for e in args.extractors.split(",") if e.strip()]
    if "model" in extractors and not MODEL_CONFIG.enabled:
        print("[warn] MODEL_CONFIG 未启用：model 抽取器将全部转人工（检查 .env 的 MODEL_*）")
    tag = args.tag or datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    summary = run(args.labels_dir, args.results_dir, extractors, args.split,
                  args.limit, tag)
    print(f"\n结果已写入：{args.results_dir / tag}")
    for ex, m in summary["per_extractor"].items():
        ev = m["evidence"]
        fm = m.get("fact_metrics")
        cost = m.get("cost")
        print(f"[{ex}] 证据定位 {ev['evidence_ok_rate']:.1%} "
              f"| 截断引用 {ev['truncated_rate']:.1%} | 转人工 {ev['review_rate']:.1%}"
              + (f" | P/R/F1 {fm['precision']:.2f}/{fm['recall']:.2f}/{fm['f1']:.2f}"
                 if fm else " | 无标注（基线）")
              + (f" | 成本 {cost['cost_cents_total']:.1f}分 | P95 {cost['duration_p95_s']:.0f}s"
                 if cost else ""))


if __name__ == "__main__":
    main()
