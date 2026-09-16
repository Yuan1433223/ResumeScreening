# -*- coding: utf-8 -*-
import os
import sys
from pathlib import Path

ROOT = Path(r"F:\ResumeScreening")
sys.path.insert(0, str(ROOT))
os.environ["RESUME_DB_PATH"] = str(ROOT / "data" / "probe.db")
os.environ["RESUME_CHECKPOINT_PATH"] = str(ROOT / "data" / "probe.checkpoints.db")

from src.workflow import run_single

rep = run_single(str(ROOT / "demo" / "samples" / "li_na.pdf"))
print("KEYS:", list(rep.keys()))
print("评分明细" in rep, rep.get("评分明细"))
for f in Path(ROOT / "data").glob("probe*"):
    f.unlink(missing_ok=True)
