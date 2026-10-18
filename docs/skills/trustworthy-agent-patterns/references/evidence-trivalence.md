# 证据三角（Evidence Trivalence）完整机制

模型输出 / 原文切片 / UI 高亮三者必须指向**同一段原文**。任何一环错位，复核页就会"左边事实、右边原文"对不上。

## 目录

1. [三段契约](#1-三段契约)
2. [定位：程序算偏移，不让模型算](#2-定位程序算偏移不让模型算)
3. [校验：逐字比对与四种失败态](#3-校验逐字比对与四种失败态)
4. [真 bug 一：复核端点 extractor_version 硬编码](#4-真-bug-一复核端点-extractor_version-硬编码)
5. [真 bug 二：payload 字段空默认值](#5-真-bug-二payload-字段空默认值)
6. [精简骨架](#6-精简骨架)

## 1. 三段契约

| 段 | 责任方 | 产物 |
|---|---|---|
| 模型输出 | LLM（schema 约束） | `kind / value / status / page / quote`，quote 为 10–80 字原文片段，**不输出 start/end** |
| 程序定位 | `model_extractor._localize_quote` | 在 `pages[page-1].text` 里找到 quote，回填 `Evidence(start,end,quote=原文真实切片)` |
| 校验 | `evidence.validate_evidence` | 逐字复核 `page.text[start:end] == quote`，产出 `ValidationResult` |
| UI | ReviewPage | 用 `start/end` 在原文面板高亮 `text[start:end]` |

为什么不让模型直接给偏移：模型断行/换行/空格与程序读到的页文本永远有出入，直接信任偏移必然大面积"引用与原文不一致"；让模型只挑句子、程序负责定位，是把不可靠的自由文本匹配压缩成一次子串查找。

## 2. 定位：程序算偏移，不让模型算

`_localize_quote(page, quote)` 两级匹配：

1. **精确子串**：`page.text.find(q)`，命中即用。
2. **空白归一**：模型把换行/多空格压成单空格时，用 `re.compile(r"\s+".join(tokens))` 跨空白匹配，`m.start()/m.end()` 取**原文真实切片**回填（不是模型给的那句，而是原文里那一段）。
3. 两级都失败 → 返回 `None`，调用方记 warning、该事实转人工，不硬塞一个偏移。

任职区间引用走 `_locate_on_any_page`（全页扫描），因为任职经历常跨页引用。

## 3. 校验：逐字比对与四种失败态

`validate_evidence(facts, pages)` 对每条证据跑 `_validate_one`：

- `source_type == MANUAL`（人工补证）→ 直接 valid，不受切片约束。
- 页码不存在 → invalid，reason=`页码不存在`。
- `start<0 or end>len(text) or end<=start` → invalid，reason=`字符偏移越界`。
- `text[start:end] != quote` → invalid，reason=`引用与原文不一致`。
- 事实**没有任何证据** → 记一条 invalid（`事实 {kind} 无证据绑定`），空证据不算通过。

任一 invalid → `ValidationResult.passed=False` → 工作流路由 `review`（人工复核），不进评分。

## 4. 真 bug 一：复核端点 extractor_version 硬编码

**现象**：`GET /api/documents/{id}/review` 左侧事实抽取曾硬编码 `extract_facts(..., extractor_version="mock")`，右侧评估明细却来自 `repo.get_assessment_by_document()`。当某文档最近一次批次是 model 抽取时，左右两套结果对不上，演示 model 批次直接露馅。

**修法**（`app/api.py get_review_detail`）：

1. 以 assessment 行的 `extractor_version` 为最权威来源（无 assessment 回退 mock，保持旧行为）。
2. **版本归一**：库里存的是抽取器自报版本（`mock-v1` / `model-p1`），而 `extract_facts` 分派只认 `"mock"` 或 `startswith("model")`——
   - `startswith("mock")` → 分派 `"mock"`；
   - `startswith("model")` → 原样透传（`model-p1`）；
   - 未知版本（如未来抽取器）→ 安全回退 `"mock"`，不抛 ValueError。
3. **展示缓存**：`_REVIEW_EXTRACT_CACHE` 键为 `(document_id, 抽取器自报版本, assessment_id, assessment_created_at, page_count)`，重复打开复核页不重复调云端模型；批次重跑产生新 assessment → created_at 变 → 键变化 → 旧缓存自然失效。

回归：`tests/test_t8_review_extractor_consistency.py`——mock 批次仍走 mock；把 assessment 改成 model 后第一次 GET 必须传 `"model"`、第二次 GET 缓存命中不重复抽取；参数化 `mock-v1/model-p1/unknown-future` 三种归一。

## 5. 真 bug 二：payload 字段空默认值

**现象**：`assessments` 表只有 `known_score/score_complete/recommendation/hard_summary` 等独立列，`hard_results / dimensions / missing_items` 三字段是 `save_assessment` 把整个 `assessment.model_dump(mode="json")` 写进 **payload JSON 列**的。端点曾直接 `assessment.get("hard_results", {})` 读列——列不存在，永远拿到默认空值，前端"评估明细"卡片永远空白。

**修法**：防御性 `json.loads(assessment["payload"])`，从解析后的 dict 取 `hard_results/dimensions/missing_items`；独立列（known_score/recommendation/extractor_version/job_version）仍以列值为准（列有 NOT NULL 默认值，payload 损坏也能返回骨架）。坏 JSON 跳过、返回空 dict，不 500。

回归：`tests/test_t8` 的 `test_review_detail_exposes_payload_score_fields`——`hard_results` 必须是含 `relevant_domain/experience/management/summary` 的 dict，`missing_items` 必须是 list（即使空），`dimensions` 必须是 dict。

## 6. 精简骨架

```python
# model_extractor.py —— 模型只给 quote，程序算偏移
def _localize_quote(page, quote):
    q = quote.strip()
    idx = page.text.find(q)
    if idx >= 0:
        start, end = idx, idx + len(q)
    else:  # 空白归一：模型断行/多空格
        pat = re.compile(r"\s+".join(re.escape(t) for t in q.split()), re.DOTALL)
        m = pat.search(page.text)
        if m is None:
            return None          # 调用方记 warning、事实转人工
        start, end = m.start(), m.end()
    return Evidence(page_number=page.page_number, start=start, end=end,
                    quote=page.text[start:end], source_type=EvidenceSource.PAGE_TEXT)

# evidence.py —— 逐字复核
def _validate_one(ev, page_map):
    if ev.source_type == EvidenceSource.MANUAL:
        return EvidenceValidation(valid=True, reason="manual")
    page = page_map.get(ev.page_number)
    if page is None:                       return invalid("页码不存在")
    if not (0 <= ev.start < ev.end <= len(page.text)):
        return invalid("字符偏移越界")
    if page.text[ev.start:ev.end] != ev.quote:
        return invalid("引用与原文不一致")
    return EvidenceValidation(valid=True, reason="ok")
```

要点：定位失败宁可转人工也不伪造偏移；校验失败宁可路由复核也不静默通过；UI 高亮必须用程序回填的 `start/end`，不能用模型自报位置。
