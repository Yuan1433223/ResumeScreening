# -*- coding: utf-8 -*-
"""证据引用校验（对应 DEVELOPMENT_SPEC.md §6.1/§9）

校验内容：
- quote 必须等于 pages[page_number-1].text[start:end]（左闭右开）
- 页码必须存在、范围合法
- 事实必须绑定至少一条证据（证据为空 → 无效，需人工核实）

校验失败的任何一条 → ValidationResult.passed=False → 路由到 needs_review（§6.4）。
人工补充证据（MANUAL）不受文本切片约束，视为有效待核。
"""
from __future__ import annotations

from typing import List, Sequence

from .schemas import (
    EvidenceSource,
    EvidenceValidation,
    Fact,
    Page,
    ValidationResult,
)


def validate_evidence(facts: Sequence[Fact], pages: Sequence[Page]) -> ValidationResult:
    """校验全部事实绑定证据的引用一致性。"""
    page_map = {p.page_number: p for p in pages}
    invalid: List[EvidenceValidation] = []
    total = 0
    valid_count = 0

    for fact in facts:
        if not fact.evidences:
            total += 1
            invalid.append(EvidenceValidation(
                evidence_id="",
                valid=False,
                reason=f"事实 {fact.kind} 无证据绑定",
            ))
            continue
        for ev in fact.evidences:
            total += 1
            result = _validate_one(ev, page_map)
            if result.valid:
                valid_count += 1
            else:
                invalid.append(result)

    return ValidationResult(total=total, valid_count=valid_count, invalid=invalid)


def _validate_one(ev, page_map: dict) -> EvidenceValidation:
    if ev.source_type == EvidenceSource.MANUAL:
        return EvidenceValidation(evidence_id=ev.id, valid=True, reason="manual")

    page = page_map.get(ev.page_number)
    if page is None:
        return EvidenceValidation(evidence_id=ev.id, valid=False, reason="页码不存在")

    text = page.text
    if ev.start < 0 or ev.end > len(text) or ev.end <= ev.start:
        return EvidenceValidation(evidence_id=ev.id, valid=False, reason="字符偏移越界")

    if text[ev.start:ev.end] != ev.quote:
        return EvidenceValidation(evidence_id=ev.id, valid=False, reason="引用与原文不一致")

    return EvidenceValidation(evidence_id=ev.id, valid=True, reason="ok")
