# 标注集工程与一致性自检

> 以 `eval/labels/eval_001.json`、`eval/run_eval.py`（`EvalLabel` 模型）、`eval/verify_consistency.py` 源码为准。标注集与结果明细含真实 PII，**永不入库**；只有脱敏聚合进 `eval/published/`。

## 目录

1. [labels 文件格式](#1-labels-文件格式)
2. [GT 事实口径（supported / contradicted / unknown）](#2-gt-事实口径supported--contradicted--unknown)
3. [dev / test 划分原则](#3-dev--test-划分原则)
4. [标注一致性自检（verify_consistency 三层）](#4-标注一致性自检verify_consistency-三层)
5. [脱敏聚合发布流程](#5-脱敏聚合发布流程)

---

## 1. labels 文件格式

`eval/labels/<document_id>.json`（实例：`eval_001.json`）：

```json
{
  "document_id": "eval_001",
  "source_file": "真实文件名.pdf（本地参考，不入库）",
  "split": "dev",
  "notes": "可选备注：GT 取舍理由",
  "expected_facts": [
    {"kind": "total_years", "value": 19, "quote": "19年工作经验", "comment": ""},
    {"kind": "management", "value": "scope_and_results", "quote": "GMV3亿200人团队高绩效"}
  ],
  "expected_employments": [
    {"start": "2007", "end": "2009", "relevant": false, "comment": ""},
    {"start": "2023", "end": "至今", "relevant": false}
  ]
}
```

字段约定：

- `kind` / `value` 必须与 `src/config.py` 的 `FACT_KINDS` 枚举一致；年限类 `value` 填数字。
- `document_id` 匿名编号——报告与明细只出现它，真实文件名只进 `source_file`。
- `expected_facts[].quote` 填原文佐证（尽量填，供人工复核 GT 本身）。
- `expected_employments[]`：`start`/`end` 可写 `"2016"` 或 `"2016-03"`；`"至今"`=在职工况；`relevant` 标是否钢构相关岗位。
- 单个标注文件解析损坏只告警跳过，不阻断整批（`load_labels`）。

## 2. GT 事实口径（supported / contradicted / unknown）

- **以简历原文为准**：简历明确断言的才进 `expected_facts`；**简历没写的维度不要标**。例：`eval_001` 是战略咨询背景、非钢构行业，notes 明确写"钢构相关维度不标（简历未断言）"——宁缺毋滥，不脑补 GT。
- **supported**：原文有可定位证据支撑的正向断言（标注即视为 supported 真值；评测只统计 supported 抽取与 GT 的匹配）。
- **contradicted**：原文明确否定（如"英语不熟练"），prompt 里要求模型输出 `value="negated", status="contradicted"`；标注侧如出现此类断言，同样只写原文确有的。
- **unknown**：原文无证据——GT 里不写这条维度，让"缺失"自然成为 FN 的一部分；**绝不把 unknown 写成 supported**。
- 任职区间 GT：按简历实际时间线逐条列（年份级），与岗位相关性用 `relevant` 标注，供任职区间匹配指标用。

## 3. dev / test 划分原则

- `split` 字段决定归属：`dev`（可调 prompt / 调抽取逻辑时看）/ `test`（锁死，最终验收才用）。
- 纪律：**调 prompt 只允许看 dev**；test 在开发期一次都不跑，避免"调完再拿同批样本证明效果"。
- 本项目现状：`eval/labels/` 共 10 份，其中 **7 份含 GT（dev）**、`eval_008`–`eval_010` 为空壳（无 `expected_facts`，只走基线不参与 micro）；`eval/labels/candidates/` 是模型预生成的候选事实模板，供人工审改后转正——转正用 `eval/promote_labels.py`。
- 样本规模教训：7 份是"能讲清方向"的最小集，不是统计显著；对外报告写清"dev7、n=7"，不夸大为大规模基准。

## 4. 标注一致性自检（verify_consistency 三层）

`python eval/verify_consistency.py`（`--skip-model` 只跑免费层）。核心问题：**工作台路径（SQLite pages 缓存）与评测路径（重 parse）对同一批文档产出是否一致**——不一致则所有"评测结论"都可能只是路径差异。

| 层 | 成本 | 验证对象 | 通过标准 | 本项目实测（2026-09-18） |
|---|---|---|---|---|
| L1 pages 输入冻结 | 免费 | 工作台缓存 pages vs 评测重解析 pages，逐页 `text` 全等 | `pages_identical=true` | eval_001/002/003 全 true |
| L2 mock 确定性 | 免费 | mock 抽取 matched/spurious/missing 集合 vs 历史 run（`results/20260916_095847`） | 三集合全等 | 3/3 全等 |
| L3 model 跨运行 | 花钱 | 工作台路径 model 重跑 vs 历史 model 结果，逐样本集合对比 + 重算 F1 | 不一致**记录留痕**，不要求全等 | 3/3 `identical=false`（temperature=0 仍漂移），workbench F1=0.882 |

纪律：

- L1/L2 不过 = 输入或规则漂移，**先修再谈评测**；L3 不过 = 模型固有波动，属预期，但必须把运行日期、模型版本、prompt 版本写进报告，否则数字不可复现。
- 层4 副产品：未标注文档的 model 候选事实写入 `eval/labels/candidates/`，供人工审改扩充 GT。

## 5. 脱敏聚合发布流程

**`eval/published/` 放什么**（随仓库公开）：

- `*_summary.json` / `*_summary.md`：纯聚合数字——P/R/F1、定位/截断/转人工/空报告率、token 总量、P50/P95、成本聚合值、档位枚举（如 `led/delivered`）、逐文档档位命中集合（无正文）。
- 一致性聚合：pages 页数/字符数、matched 档位枚举集合、调用次数/token/耗时。

**永不放**（仅本地，已进 `.gitignore`）：

- `eval/labels/`（人工标注原文佐证）、`eval/results/*/detail.jsonl`（逐份引文片段）、`workbench_facts.jsonl`、`preview/`、原始简历 PDF/JPG。
- 任何姓名、电话、邮箱、雇主名、可反推个人的引文片段。

**发布动作**：新 run 产物在 `eval/results/<tag>/` → 人工通读 summary.md 确认无 PII → 复制聚合件到 `eval/published/` 并在 `published/README.md` 登记表补一行（来源 run + 说明）。复跑命令：`. .\.venv\Scripts\Activate.ps1; python eval/run_eval.py`。
