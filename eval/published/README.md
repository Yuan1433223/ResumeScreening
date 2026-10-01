# eval/published — 随仓库公开的脱敏聚合评测结论

本目录只放**纯聚合、无原文引文、无个人身份信息（PII）**的评测结果，
用于向外部说明系统精度、成本与延迟量级，可随仓库公开。

## 目录内容

| 文件 | 来源 | 说明 |
|---|---|---|
| `dev7_20260918_summary.md` | `eval/results/dev7_20260918/summary.md` | T4 主评测：mock vs model 抽取器的 micro P/R/F1、证据定位有效率、转人工率、token/延迟/成本聚合。 |
| `dev7_20260918_summary.json` | `eval/results/dev7_20260918/summary.json` | 同上，结构化数据。 |
| `consistency_free_summary.json` | `eval/results/consistency_free/summary.json` | 无模型层（L1 缓存 + L2 mock）前后端一致性：页面对齐、档位命中完全一致。 |
| `consistency_model2_summary.json` | `eval/results/consistency_model2/summary.json` | 含模型层（L3）的前后端一致性：逐样本档位命中对比、调用次数、token 与耗时聚合，以及 workbench 侧 F1。 |

以上文件均只包含**指标数字、档位枚举、token/延迟/成本聚合值**，不含任何简历正文片段、姓名、手机号、邮箱或其他可识别信息。

## 不入库的内容（含 PII，仅本地）

以下目录/文件**不会**出现在版本控制中，本地需要时自行生成：

- `eval/labels/` — 人工标注原文（来自真实简历）。
- `eval/results/` — 逐简历 `detail.jsonl`、`workbench_facts.jsonl`、`preview/` 等中间产物，可能含真实引文。
- `candidates/`、`output/`、`data/` — 真实简历与运行产物。
- 仓库根目录的 `海外项目负责招聘简历/` 原始简历文件（PDF/JPG）。

## 如何本地复跑

```powershell
# 在项目根目录，先激活 .venv
.\.venv\Scripts\Activate.ps1
# 复跑 T4 主评测（标注集需本地 eval/labels/，否则无样本）
python eval/run_eval.py
```

复跑产物会写入 `eval/results/<时间戳或run名>/`，与本目录的 `eval/published/` 相互独立；
若要把新一轮聚合结论补进本目录，请人工核对无 PII 后再复制。
