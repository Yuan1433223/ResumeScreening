# -*- coding: utf-8 -*-
"""检查 run_single 返回报告里评分明细 evidence 是否为空。"""
import os
import sys
from pathlib import Path

ROOT = Path(r"F:\ResumeScreening")
sys.path.insert(0, str(ROOT))
os.environ["RESUME_DB_PATH"] = str(ROOT / "data" / "probe_ev.db")
os.environ["RESUME_CHECKPOINT_PATH"] = str(ROOT / "data" / "probe_ev.checkpoints.db")

from src.workflow import run_single

rep = run_single(str(ROOT / "demo" / "samples" / "li_na.pdf"))
print("english detail:", rep["评分明细"]["english"])
print("evidence list:", rep["评分明细"]["english"].get("evidence"))
# 清理
for f in Path(ROOT / "data").glob("probe_ev*"):
    f.unlink(missing_ok=True)
