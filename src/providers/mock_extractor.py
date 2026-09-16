# -*- coding: utf-8 -*-
"""Mock 抽取器：基于规则的岗位事实抽取（无 LLM）

对应 DEVELOPMENT_SPEC.md §6.1：
- 从 Page.text 中按关键词/正则抽取岗位事实，绑定 Evidence（quote 精确等于原文切片）
- 工作区间抽取为 Employment（含 is_current / relevant 判定）
- 否定句（"不熟练""未负责"等）产出 CONTRADICTED 事实，不加分
- 同一维度可产出多个档位事实，评分器取最高档（§6.3）

Mock 数据不代表真实抽取准确率；真实评测需人工标注（§11）。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from ..schemas import (
    Employment,
    Evidence,
    EvidenceSource,
    ExtractionReport,
    Fact,
    FactStatus,
    Page,
)


class MockExtractor:
    """基于规则的抽取器。version 用于缓存与审批绑定。"""

    version = "mock-v1"

    # ------------------------------------------------------------------
    # 关键词表：维度 -> [(档位值, [关键词...]), ...]
    # ------------------------------------------------------------------
    STEEL_MIC_LEVELS = [
        ("led", ["主导", "带领", "统筹", "负责人", "牵头", "项目经理", "项目总监", "技术负责人"]),
        ("responsible", ["负责", "承担", "主管", "主持", "交付责任", "独立负责"]),
        ("participated", ["参与", "参加", "协助", "配合"]),
    ]
    STEEL_MIC_KEYWORDS = [
        "钢结构", "钢构", "MiC", "MIC", "模块化建筑", "模块化", "预制装配",
        "装配式", "箱式房", "箱房", "钢结构工程", "钢构工程", "heavy steel",
        "steel structure", "modular", "prefab", "MiC 模块化",
    ]

    FLOW_KEYWORDS = ["设计", "采购", "生产", "施工", "交付", "深化设计", "安装", "制造"]
    FLOW_LEVELS = [
        ("full_flow", ["深化设计", "设计", "采购", "生产", "施工", "交付", "制造", "安装"]),
        ("two_phases", []),  # 由计数逻辑判定
        ("one_phase", []),
    ]

    OVERSEAS_LEVELS = [
        ("delivered", ["海外项目交付", "海外交付", "海外项目负责人", "海外项目经理",
                       "海外现场项目经理", "负责海外", "负责境外"]),
        ("responsible", ["海外项目", "海外工程", "国际工程", "国外项目", "海外业务",
                         "境外项目", "境外工程", "驻外", "海外工作", "海外拓展", "海外市场"]),
        ("collab", ["跨国协作", "跨国合作", "国际协作", "海外协作", "与海外", "外方", "海外客户"]),
    ]
    OVERSEAS_KEYWORDS = ["海外", "国外", "境外", "国际", "跨国", "出口", "外派", "abroad", "overseas", "international"]

    MANAGEMENT_LEVELS = [
        ("scope_and_results", ["管理范围", "管理对象", "带领团队", "管理团队", "团队负责人", "分管"]),
        ("clear_scope", ["项目经理", "项目总监", "部门经理", "管理职责", "管理职能", "负责管理"]),
        ("managed", ["管理", "带领", "领导", "主管", "负责"]),
    ]

    QUALITY_LEVELS = [
        ("two_plus", []),  # 计数逻辑
        ("one_duty", ["质量", "安全", "体系", "ISO", "QC", "QA", "EHS", "质检", "安全生产"]),
    ]

    ENGLISH_POSITIVE = ["英语流利", "英文流利", "商务英语", "英语六级", "英语八级", "雅思",
                        "托福", "英语作为工作语言", "英文作为工作语言", "英语沟通", "英文沟通",
                        "fluent english", "business english", "IELTS", "TOEFL"]
    ENGLISH_INFO = ["英语", "英文", "英语能力", "英语水平", "English", "CET-4", "CET-6"]
    ENGLISH_NEGATION = ["不熟练", "不流利", "一般", "较差", "有限", "basic", "poor",
                        "水平一般", "不太好"]

    SCALE_KEYWORDS = ["万", "亿", "平方米", "m²", "㎡", "吨", "t", "亿元", "万平方米", "亿元",
                      "5000万", "合同额", "产值", "营业额", "年产值"]
    # 数字必须至少含一个数字字符，逗号仅作为分隔，避免把"，人"误判为数值
    SCALE_UNITS = r"(?<!\d)\d[\d,，]*\.?\d*\s*(?:万|亿|平方米|㎡|m²|吨|t|人|项|个|栋|层)"

    MOBILITY_LOCATION = ["东莞", "接受东莞", "工作地点", "base 东莞", "base东莞", "可base"]
    MOBILITY_TRAVEL = ["接受出差", "愿意出差", "可出差", "接受驻场", "愿意驻场", "可驻场",
                       "常驻项目", "出差频繁", "接受外派"]

    # 工作区间正则（支持多种年月分隔）
    DATE_RANGE_PATTERNS = [
        re.compile(
            r"(?P<sy>\d{4})\s*[.．\-/年]\s*(?P<sm>\d{1,2})?\s*[月]?\s*"
            r"[-–—~至到—]+\s*"
            r"(?:(?P<ey>\d{4})\s*[.．\-/年]\s*(?P<em>\d{1,2})?\s*[月]?|"
            r"(?:至今|现在|今|目前|current|present|now|现在))"
        ),
        re.compile(
            r"(?P<sy>\d{4})\s*年\s*(?P<sm>\d{1,2})\s*月\s*[-–—~至到—]+\s*"
            r"(?P<ey>\d{4})\s*年\s*(?P<em>\d{1,2})\s*月"
        ),
        re.compile(
            r"(?P<sy>\d{4})\.(?P<sm>\d{2})\s*[-–—~]+\s*(?P<ey>\d{4})\.(?P<em>\d{2})"
        ),
        # 纯年份区间：中文简历常见 "2016至2019年" / "2020-2024年期间"
        re.compile(
            r"(?P<sy>\d{4})\s*年?\s*[-–—~至到—]+\s*(?P<ey>\d{4})\s*年?"
        ),
    ]

    # 相关领域判定：出现 STEEL_MIC_KEYWORDS 或明确的工程总包/建筑类
    DOMAIN_KEYWORDS = STEEL_MIC_KEYWORDS + ["工程总包", "总承包", "建筑工程", "工程公司",
                                            "建筑公司", "厂房", "工业建筑", "大型项目"]

    def extract(self, document_id: str, pages: List[Page]) -> ExtractionReport:
        facts: List[Fact] = []
        employments: List[Employment] = []
        warnings: List[str] = []

        all_text = "\n".join(p.text for p in pages)
        if not all_text.strip():
            warnings.append("正文为空，无可抽取事实")

        # 1) 工作区间
        for page in pages:
            employments.extend(self._extract_employments(document_id, page, all_text))

        # 2) 相关领域硬条件
        self._extract_domain(document_id, pages, facts)

        # 3) 维度事实
        self._extract_steel_mic(document_id, pages, facts)
        self._extract_flow(document_id, pages, facts)
        self._extract_overseas(document_id, pages, facts)
        self._extract_management(document_id, pages, facts)
        self._extract_quality(document_id, pages, facts)
        self._extract_english(document_id, pages, facts)
        self._extract_scale(document_id, pages, facts)
        self._extract_mobility(document_id, pages, facts)

        # 相关年限判定：有相关领域事实且任职区间与之相关时置 relevant
        self._mark_relevant(facts, employments, all_text)

        return ExtractionReport(
            facts=facts,
            employments=employments,
            warnings=warnings,
            extractor_version=self.version,
        )

    # ------------------------------------------------------------------
    def _find_evidence(self, page: Page, keyword: str) -> Optional[Evidence]:
        """在页文本中定位关键词，构造精确引用。"""
        start = page.text.find(keyword)
        if start < 0:
            return None
        return Evidence(
            id=f"ev_{page.page_number}_{start}_{len(keyword)}",
            document_id=page.document_id,
            page_number=page.page_number,
            start=start,
            end=start + len(keyword),
            quote=page.text[start:start + len(keyword)],
            source_type=EvidenceSource.PAGE_TEXT,
        )

    def _add_fact(self, facts: List[Fact], document_id: str, kind: str, value,
                  evidence: Evidence, status: FactStatus = FactStatus.SUPPORTED) -> None:
        facts.append(Fact(
            id=f"f_{kind}_{value}_{len(facts)}",
            document_id=document_id,
            kind=kind,
            value=value,
            status=status,
            evidence_ids=[evidence.id],
            evidences=[evidence],
        ))

    # ------------------------------------------------------------------
    def _extract_employments(self, document_id: str, page: Page,
                             all_text: str) -> List[Employment]:
        emps: List[Employment] = []
        for pat in self.DATE_RANGE_PATTERNS:
            for m in pat.finditer(page.text):
                sy = int(m.group("sy"))
                sm = m.groupdict().get("sm")
                ey = m.groupdict().get("ey")
                em = m.groupdict().get("em")
                is_current = not ey
                end_month = None if is_current else f"{int(ey)}-{int(em):02d}" if em else f"{int(ey)}-00"
                start_month = f"{sy}-{int(sm):02d}" if sm else f"{sy}-00"
                # 段落上下文判定相关性
                seg_start = max(0, m.start() - 80)
                seg_end = min(len(page.text), m.end() + 120)
                seg = page.text[seg_start:seg_end]
                relevant = any(kw.lower() in seg.lower() for kw in self.DOMAIN_KEYWORDS)
                ev = Evidence(
                    id=f"ev_emp_{page.page_number}_{m.start()}_{m.end()}",
                    document_id=document_id,
                    page_number=page.page_number,
                    start=m.start(),
                    end=m.end(),
                    quote=page.text[m.start():m.end()],
                    source_type=EvidenceSource.PAGE_TEXT,
                )
                emps.append(Employment(
                    document_id=document_id,
                    start_month=start_month,
                    end_month=end_month,
                    is_current=is_current,
                    relevant=relevant if relevant else None,
                    responsibilities=[],
                    evidence_ids=[ev.id],
                ))
        # 去重：相同 (start_month, end_month) 只保留一条
        seen = set()
        dedup: List[Employment] = []
        for e in emps:
            key = (e.start_month, e.end_month, e.is_current)
            if key in seen:
                continue
            seen.add(key)
            dedup.append(e)
        return dedup

    def _extract_domain(self, document_id: str, pages: List[Page], facts: List[Fact]) -> None:
        for page in pages:
            text = page.text
            for kw in self.DOMAIN_KEYWORDS:
                ev = self._find_evidence(page, kw)
                if ev:
                    self._add_fact(facts, document_id, "relevant_domain", True, ev)
                    return

    def _extract_steel_mic(self, document_id: str, pages: List[Page], facts: List[Fact]) -> None:
        for page in pages:
            # 先判定档位关键词
            for level, kws in self.STEEL_MIC_LEVELS:
                for kw in kws:
                    ev = self._find_evidence(page, kw)
                    if ev:
                        # 该词附近必须有 MiC/钢结构上下文，否则不算该档
                        seg_start = max(0, ev.start - 60)
                        seg_end = min(len(page.text), ev.end + 60)
                        seg = page.text[seg_start:seg_end]
                        if any(k.lower() in seg.lower() for k in self.STEEL_MIC_KEYWORDS):
                            self._add_fact(facts, document_id, "steel_mic_experience", level, ev)

    def _extract_flow(self, document_id: str, pages: List[Page], facts: List[Fact]) -> None:
        for page in pages:
            found: List[Tuple[str, Evidence]] = []
            for kw in self.FLOW_KEYWORDS:
                ev = self._find_evidence(page, kw)
                if ev:
                    found.append((kw, ev))
            # 去重（同一关键词多处出现只取首个）
            uniq = {}
            for kw, ev in found:
                if kw not in uniq:
                    uniq[kw] = ev
            n = len(uniq)
            if n == 0:
                continue
            if n >= 3 and any(k in uniq for k in ["交付", "施工", "生产"]):
                val, ev = "full_flow", list(uniq.values())[0]
            elif n >= 2:
                val, ev = "two_phases", list(uniq.values())[0]
            else:
                val, ev = "one_phase", list(uniq.values())[0]
            self._add_fact(facts, document_id, "steel_full_flow", val, ev)

    def _extract_overseas(self, document_id: str, pages: List[Page], facts: List[Fact]) -> None:
        for page in pages:
            for level, kws in self.OVERSEAS_LEVELS:
                for kw in kws:
                    ev = self._find_evidence(page, kw)
                    if ev:
                        self._add_fact(facts, document_id, "overseas", level, ev)

    def _extract_management(self, document_id: str, pages: List[Page], facts: List[Fact]) -> None:
        for page in pages:
            for level, kws in self.MANAGEMENT_LEVELS:
                for kw in kws:
                    ev = self._find_evidence(page, kw)
                    if ev:
                        self._add_fact(facts, document_id, "management", level, ev)

    def _extract_quality(self, document_id: str, pages: List[Page], facts: List[Fact]) -> None:
        for page in pages:
            found = []
            for kw in self.QUALITY_LEVELS[1][1]:  # one_duty 关键词
                ev = self._find_evidence(page, kw)
                if ev:
                    found.append(ev)
            uniq = {}
            for ev in found:
                if ev.quote not in uniq:
                    uniq[ev.quote] = ev
            if len(uniq) >= 2:
                self._add_fact(facts, document_id, "quality_safety", "two_plus", list(uniq.values())[0])
            elif len(uniq) == 1:
                self._add_fact(facts, document_id, "quality_safety", "one_duty", list(uniq.values())[0])

    def _extract_english(self, document_id: str, pages: List[Page], facts: List[Fact]) -> None:
        for page in pages:
            # 否定优先：附近有否定词 → CONTRADICTED
            for kw in self.ENGLISH_POSITIVE + self.ENGLISH_INFO:
                ev = self._find_evidence(page, kw)
                if not ev:
                    continue
                seg_start = max(0, ev.start - 30)
                seg_end = min(len(page.text), ev.end + 30)
                seg = page.text[seg_start:seg_end]
                if any(neg in seg for neg in self.ENGLISH_NEGATION):
                    self._add_fact(facts, document_id, "english", "not_fluent",
                                   ev, status=FactStatus.CONTRADICTED)
                    return
            # 正向
            for kw in self.ENGLISH_POSITIVE:
                ev = self._find_evidence(page, kw)
                if ev:
                    self._add_fact(facts, document_id, "english", "usage", ev)
                    return
            for kw in self.ENGLISH_INFO:
                ev = self._find_evidence(page, kw)
                if ev:
                    self._add_fact(facts, document_id, "english", "info", ev)
                    return

    def _extract_scale(self, document_id: str, pages: List[Page], facts: List[Fact]) -> None:
        for page in pages:
            m = re.search(self.SCALE_UNITS, page.text)
            if m:
                ev = Evidence(
                    id=f"ev_scale_{page.page_number}_{m.start()}_{m.end()}",
                    document_id=document_id,
                    page_number=page.page_number,
                    start=m.start(),
                    end=m.end(),
                    quote=page.text[m.start():m.end()],
                    source_type=EvidenceSource.PAGE_TEXT,
                )
                self._add_fact(facts, document_id, "project_scale", "quantified", ev)
                return

    def _extract_mobility(self, document_id: str, pages: List[Page], facts: List[Fact]) -> None:
        for page in pages:
            for kw in self.MOBILITY_TRAVEL:
                ev = self._find_evidence(page, kw)
                if ev:
                    self._add_fact(facts, document_id, "mobility", "travel_ok", ev)
                    return
            for kw in self.MOBILITY_LOCATION:
                ev = self._find_evidence(page, kw)
                if ev:
                    self._add_fact(facts, document_id, "mobility", "location_ok", ev)
                    return

    def _mark_relevant(self, facts: List[Fact], employments: List[Employment], all_text: str) -> None:
        """已有相关领域事实 → 任职区间相关性按段落上下文判定结果补齐。"""
        has_domain = any(f.kind == "relevant_domain" and f.status == FactStatus.SUPPORTED for f in facts)
        if not has_domain:
            return
        for emp in employments:
            if emp.relevant is None:
                emp.relevant = True  # 有相关领域事实且无反向证据，视为相关（待人工抽样评测）


# 模块级入口（规格 §9：extract_facts(document_id, extractor_version)）
def extract_facts(document_id: str, pages: List[Page], extractor: str = "mock") -> ExtractionReport:
    if extractor == "mock":
        return MockExtractor().extract(document_id, pages)
    from .model_extractor import ModelExtractor
    return ModelExtractor().extract(document_id, pages)
