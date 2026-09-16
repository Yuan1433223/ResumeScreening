# -*- coding: utf-8 -*-
"""岗位筛选规则配置 - 海外项目负责人（钢结构/MiC 方向）

对应 DEVELOPMENT_SPEC.md §6.2 / §6.3：
- 硬条件：相关领域经验 AND（总工作经验>=8年 OR 直接相关经验>=5年）AND 项目或团队管理经历
- 评分表 v1：8 维，每维取最高满足档位，不按关键词次数叠加
- 未提及维度 score=null、status=unknown；known_score 仅累加已确认项
"""
from __future__ import annotations

import os
from pathlib import Path

from .schemas import JobProfile

# ---------------------------------------------------------------------------
# 路径：项目相对路径，不硬编码盘符
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
RESUME_DIR = ROOT / "海外项目负责招聘简历"
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# P2 持久化：业务数据 + LangGraph 检查点
# 支持环境变量覆盖（演示脚本/测试隔离用），默认 data/ 下
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = Path(os.environ.get("RESUME_DB_PATH", str(DATA_DIR / "resume.db")))
CHECKPOINT_DB_PATH = Path(os.environ.get("RESUME_CHECKPOINT_PATH", str(DATA_DIR / "resume.checkpoints.db")))

# ---------------------------------------------------------------------------
# 岗位画像
# ---------------------------------------------------------------------------
JOB_ID = "overseas_mic"
JOB_VERSION = "v1"
JOB_TITLE = "海外项目负责人（钢结构/MiC方向）"
ANALYSIS_DATE = "2026-09-14"  # 当前任职截止日期；随使用日期更新

# 硬条件（三态：pass/fail/unknown）
HARD_RULES = {
    # 相关领域经验：无证据=unknown
    "relevant_domain": {
        "description": "相关领域（钢结构/MiC/模块化建筑/工程总包）经验",
        "fact_kind": "relevant_domain",
    },
    # 总工作经验 >= 8 年 OR 直接相关经验 >= 5 年
    "experience": {
        "description": "总工作经验>=8年 或 直接相关经验>=5年",
        "total_years_ge": 8,
        "relevant_years_ge": 5,
    },
    # 项目或团队管理经历
    "management": {
        "description": "项目或团队管理经历",
        "fact_kind": "management",
    },
}

# 8 维评分表 v1（初始分档；后续由领域人员校准后发布新版本）
# 每维: {"max": 满分, "levels": [(档位分, 档位说明, 命中条件), ...]}
# 命中条件用事实 kind 匹配，见 FACT_KINDS 说明。
SCORING = {
    "steel_mic_experience": {
        "name": "直接钢结构/MiC/模块化经验",
        "max": 30,
        "levels": [
            (10, "明确参与相关项目", "participated"),
            (20, "承担交付责任", "responsible"),
            (30, "主导相关项目并有成果", "led"),
        ],
    },
    "steel_full_flow": {
        "name": "钢结构项目全流程",
        "max": 15,
        "levels": [
            (5, "明确负责一个阶段（设计/采购/生产/施工/交付）", "one_phase"),
            (10, "负责两个阶段", "two_phases"),
            (15, "至少三个阶段且含交付", "full_flow"),
        ],
    },
    "overseas": {
        "name": "海外项目/跨国协作",
        "max": 15,
        "levels": [
            (5, "明确跨国协作", "collab"),
            (10, "承担海外项目职责", "responsible"),
            (15, "负责海外项目交付", "delivered"),
        ],
    },
    "management": {
        "name": "项目/团队管理",
        "max": 15,
        "levels": [
            (5, "明确管理职责", "managed"),
            (10, "职责与管理对象明确", "clear_scope"),
            (15, "另有范围或结果证据", "scope_and_results"),
        ],
    },
    "quality_safety": {
        "name": "生产/质量/安全/体系",
        "max": 10,
        "levels": [
            (5, "承担其中一项职责", "one_duty"),
            (10, "至少两项且有落实举措", "two_plus"),
        ],
    },
    "english": {
        "name": "英语/跨文化沟通",
        "max": 5,
        "levels": [
            (2, "明确英语能力信息", "info"),
            (5, "英文业务使用或跨文化沟通实例", "usage"),
        ],
    },
    "project_scale": {
        "name": "项目规模/量化成果",
        "max": 5,
        "levels": [
            (5, "有单位、有所属项目的规模或成果数值", "quantified"),
        ],
    },
    "mobility": {
        "name": "岗位适配/机动性",
        "max": 5,
        "levels": [
            (2, "明确接受该岗位所需工作地点", "location_ok"),
            (5, "另明确接受岗位要求的出差/驻场", "travel_ok"),
        ],
    },
}

# 事实 kind 枚举（抽取器与评分器共用）
FACT_KINDS = {
    # 硬条件相关
    "relevant_domain": "相关领域经验（true/false）",
    "total_years": "总工作年限（年，数值）",
    "relevant_years": "直接相关年限（年，数值）",
    "management": "项目/团队管理经历（managed/clear_scope/scope_and_results）",
    # 评分维度
    "steel_mic_experience": "participated/responsible/led",
    "steel_full_flow": "one_phase/two_phases/full_flow",
    "overseas": "collab/responsible/delivered",
    "quality_safety": "one_duty/two_plus",
    "english": "info/usage（另有 negated 表示明确不熟练）",
    "project_scale": "quantified",
    "mobility": "location_ok/travel_ok",
}

# 阈值
THRESHOLDS = {"recommend": 75, "needs_review": 60}

# 文件名年限模式（仅作“待核实元数据”展示，不参与评分）
YEAR_PATTERNS = [
    r"(\d{1,2})\s*年以上",
    r"(\d{1,2})\s*years?\s*\+?",
    r"经验\s*(\d{1,2})\s*年",
]

JOB_PROFILE = JobProfile(
    id=JOB_ID,
    version=JOB_VERSION,
    title=JOB_TITLE,
    analysis_date=ANALYSIS_DATE,
    hard_rules=HARD_RULES,
    rubric=SCORING,
    thresholds=THRESHOLDS,
)
