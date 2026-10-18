---
name: agent-eval-harness
description: Agent 评测度量体系（eval harness）。当用户要为一个带"规则抽取器 vs 模型抽取器"双路径的 Agent 建可复跑、可对账、可脱敏对外发布的评测时使用；典型触发：建标注集、跑 mock vs model 对照算 micro P/R/F1、算转人工率/截断引用率/成本/P50-P95、验证评测路径与工作台路径一致性、给 prompt-injection 攻击写 xfail 固化用例、把聚合结论脱敏发布到 eval/published。执行：dev/test 冻结划分 + 事实级 GT 写死、双路径同集对照、运营指标与转人工率红线、三层一致性验证（pages 冻结/mock 确定性/model 波动留痕）、注入抗性先 xfail 固化漏洞再修复转真、诚实报告口径争议项。不适用于：没有标注集就想报准确率（那是自嗨）、把简历原文/PII 贴进公开报告、调 prompt 时偷看 test 划分、或跑分后不复跑（指标不随代码变化更新即作废）。
---

# Agent Eval Harness（Agent 评测度量体系）

把"规则 vs 模型谁更准、贵多少、错在哪、被注入会怎样"变成**可复跑、可对账、可脱敏公开**的度量。**只度量，不粉饰**：指标数字必须指到实跑产物，口径争议单列，转人工率是体验与成本的硬约束。

## 前提

系统已有两条可切换的抽取路径（规则 mock / 模型 model），且有真实样本可解析。若连单路径都跑不通，先回去开发，不要进评测。

## 工作流（按序执行）

### 1. 建标注集（GT 口径先写死，再动手标）
- 划分 dev / test：**dev 可调 prompt，test 锁死留作最终验收**，防止"调完再拿同一批样本证明效果"的自证循环。
- GT 是**事实级标注**：只写 `kind + 归一化 value + 原文佐证 quote`；简历没写的维度**不要标**（ground truth 只含真实出现的断言，不脑补）。
- 标注集落 `eval/labels/<document_id>.json`，`document_id` 用匿名编号，真实文件名只写 `source_file`。
- 本项目实例：7 份真实简历人工标注、41 条 GT 事实（`eval_001`–`eval_007`，split=dev）；`eval_008`–`eval_010` 为空壳候选。**真实 PII 永不入库**，`labels/` 已进 `.gitignore`。
- 文件格式、supported/contradicted/unknown 判定与脱敏发布流程见 `references/labeling-consistency.md`。

### 2. 跑双路径对照（同一标注集，两条路径都跑）
- 命令：`python eval/run_eval.py --extractors mock,model --split dev --tag <run名>`。
- 算 **micro P/R/F1**：所有含 GT 的文档 pooled 后统计，事实按 `kind + 归一化 value` 匹配（年限类转 int），只数 `supported` 事实。
- 本项目实例（2026-09-18，tag=dev7_20260918）：**mock F1=0.364 vs model F1=0.727**——证明模型带来增量价值，但远不完美（71% 转人工），如实两边都报。
- 公式口径与 TP/FP/FN 计数法见 `references/metrics.md`。

### 3. 算运营指标（准确率之外的生死线）
- **转人工率**：含"转人工/定位失败/抽取失败/未配置/无法解析"等警告的文档占比。本项目 model=71.4%——这是体验与成本的关键约束：**任何加固/优化都不得显著推高它**，每次改动后复跑对比。
- **引用质量**：证据定位有效率（引文切片与原文逐字符一致）、截断引用率（启发式：起点/终点切在 ASCII 词中间）、平均引文长度。
- **成本与延迟**：单份 input/output token × 单价、总成本、延迟 P50/P95。价目未配置时成本记 0 并注明"价目未配置"，**不得谎称便宜**。
- 本项目实例：model P50=50.0s / P95=66.7s、单份约 3.7k input / 12.4k output token；公式见 `references/metrics.md`。

### 4. 三层一致性验证（读 `eval/verify_consistency.py`，不凭感觉）
- **层1 pages 输入冻结**：工作台路径（SQLite 缓存）与评测路径（重 parse_document）对同一文档逐页文本全等。本项目 3/3 全等。
- **层2 mock 确定性一致**：规则抽取器是确定性的，前后两次 matched/spurious/missing 集合必须全等。本项目 3/3 全等。
- **层3 model 跨运行波动留痕**：即便 temperature=0，云端模型两次输出也可能漂移——本项目 3/3 `identical=false`。**波动必须记录运行日期 + 模型版本 + prompt 版本**（`ModelExtractor.version` = `model-<prompt_version>`），跨 run 对比时先对齐这三样再谈"变好/变坏"。

### 5. 注入抗性（先证明漏洞存在，再修）
- 简历正文是**不可信数据**，按 `references/prompt-injection.md` 的方法设计攻击用例。
- 工作流：**先用 `xfail(strict=False)` 把真实漏洞写成"预期失败"的攻击用例**（固化并证明漏洞存在）→ 修复后摘掉 xfail 转真实断言 → 补一组"正常输入不误伤"的正向用例作为转人工率护栏。
- 本项目 4+1 用例（`tests/test_t11_prompt_injection.py`）：规则 M1/M2 无指令通道、模型 L1 捏造引文定位失败、L2 真实子串错贴档位（xfail→已修复转真）、正向用例真实海外钢结构经理全部保持 supported。

### 6. 诚实报告（不虚报，不藏争议）
- 聚合脱敏结论写 `eval/published/`（summary.json/md）：只放指标数字、档位枚举、token/延迟/成本聚合值。
- **口径争议项单列不藏**：例如"相近但不同档位"（led vs responsible）按精确匹配计未命中——是预期严格口径，不是模型错；mock 任职区间 30/31 反而高于 model 22/31——规则在年份上更稳。这些都写进报告。
- 原文标注、姓名、电话、邮箱、雇主名、detail.jsonl 引文**永不入库**。
- 指标随代码/prompt/模型变化**必须复跑**，旧数字不沿用；复跑产物在 `eval/results/`，人工核对无 PII 后再复制进 `published/`。

## 三条取舍原则

- **同一标注集对照**：mock 与 model 吃完全相同的 GT，否则 0.36 vs 0.73 的对比无意义。
- **转人工率是红线**：提准确率不能以推高转人工率为代价——那是把成本转嫁给人。
- **波动留痕优先于单次漂亮**：temperature=0 仍漂移，报数字必带运行日期+模型版本+prompt 版本，否则数字不可复现。

## 参考文件（需要时再读，勿一次性全加载）

- `references/metrics.md` — 指标定义与公式口径表（micro P/R/F1、转人工率、定位/截断率、成本、P50/P95、跨运行一致性），附本项目 dev7 已核实真实值与叙事权重。
- `references/labeling-consistency.md` — 标注集工程：labels 文件格式、GT 口径、dev/test 划分、三层一致性自检、脱敏聚合发布流程。
- `references/prompt-injection.md` — 注入用例设计：M1/M2/L1/L2 真实案例、quote↔value 一致性修法、xfail 固化→修复转真工作流、正向不误伤用例。

## 生命周期导航

- **上游**：← `spec-driven-planning`（评测口径与指标在规格期就定好，不事后补）。
- **下游**：→ `trustworthy-agent-patterns`（度量结论沉淀为可信设计模式）；→ `test-hardened-dev`（注入/一致性用例并入测试护栏）；→ `pii-safe-release`（published 脱敏聚合随安全上线发布）。
