# -*- coding: utf-8 -*-
"""把用户审核通过的候选标注转正为 GT（eval/labels/eval_00N.json）。

候选字段（candidates/）→ GT 字段（EvalLabel）：
  candidate_facts        → expected_facts    （仅 supported；kind/value/quote）
  candidate_employments  → expected_employments（start/end/relevant）
只转正未标注文档（004–010）；已标注的 001–003 不动。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from eval.run_eval import EvalLabel  # noqa: E402

CAND = ROOT / "labels" / "candidates"
LABELS = ROOT / "labels"


def main() -> None:
    made, skipped = [], []
    for fp in sorted(CAND.glob("eval_*.json")):
        c = json.loads(fp.read_text(encoding="utf-8"))
        doc_id = c["document_id"]
        target = LABELS / f"{doc_id}.json"
        if target.exists() and json.loads(target.read_text(encoding="utf-8")).get("expected_facts"):
            skipped.append(f"{doc_id}（已有 GT，跳过）")
            continue
        expected_facts = [
            {"kind": f["kind"], "value": f["value"], "quote": f.get("quote", ""), "comment": ""}
            for f in c["candidate_facts"]
            if f.get("status") == "supported"
        ]
        expected_employments = []
        for e in c.get("candidate_employments", []):
            end = e.get("end_month") or ("至今" if e.get("is_current") else "")
            expected_employments.append({
                "start": e.get("start_month") or "",
                "end": end,
                "relevant": e.get("relevant"),
                "comment": "",
            })
        label = EvalLabel(
            document_id=doc_id,
            source_file=c.get("source_file", ""),
            split=c.get("split", "dev"),
            notes="GT 由模型候选人工审核转正（T5e）",
            expected_facts=expected_facts,
            expected_employments=expected_employments,
        )
        target.write_text(
            json.dumps(json.loads(label.model_dump_json()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        made.append(f"{doc_id}（{len(expected_facts)} 事实 / {len(expected_employments)} 任职）")
    print("转正完成：" if made else "无新转正。")
    for m in made:
        print("  ", m)
    for s in skipped:
        print(" 跳过：", s)


if __name__ == "__main__":
    main()
