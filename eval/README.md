# T4 评测：口径与使用说明（骨架）

目标：用**真实简历**固定评测集，回答"规则抽取 vs 模型抽取，谁更准、贵多少、错在哪"。
调提示词只允许看 `dev` 划分；`test` 划分留到最后验收，避免"调完再拿同一批样本证明效果"。

## 目录

```
eval/
  run_eval.py         # 评测 runner（入库）
  README.md           # 本文档
  example_label.json  # 标注格式示例（合成内容，入库）
  labels/             # 标注样本（真实简历 ground truth）—— 不入库
    eval_001.json
    ...
  results/            # 运行结果（含引文，仅本地）—— 不入库
    <tag>/
      summary.json    # 聚合（脱敏，可入库/可贴报告）
      summary.md      # 人读报告（脱敏）
      detail.jsonl    # 每份文档明细（含引文片段，仅本地）
```

## 数据边界（用户已确认）

| 用途 | 边界 |
|---|---|
| 本地评测/测试集 | ✅ 直接使用真实简历（`海外项目负责招聘简历/`） |
| 发送云端模型（DeepSeek） | ✅ 用户已确认（个人项目内部） |
| 公开产物（仓库/截图/报告） | ⚠️ 只放聚合指标与脱敏内容；不出现姓名/电话/邮箱/雇主名 |

`labels/`、`results/` 已加入 `.gitignore`；`summary.json/summary.md` 是脱敏聚合，可自行决定是否入库展示。

## 标注格式（labels/<document_id>.json）

```json
{
  "document_id": "eval_001",
  "source_file": "xxx.pdf",
  "split": "dev",
  "notes": "可选备注",
  "expected_facts": [
    {"kind": "overseas", "value": "responsible", "quote": "负责海外项目交付"},
    {"kind": "total_years", "value": 19, "quote": "19年工作经验"}
  ],
  "expected_employments": [
    {"start": "2016", "end": "2020", "relevant": true}
  ]
}
```

- `kind` / `value` 必须与 `src/config.py` 的 `FACT_KINDS` 枚举一致（年限类 value 填数字）
- `split`：`dev`（调提示词）/ `test`（验收）
- `document_id` 用匿名编号；真实文件名只写 `source_file`（本地参考，不入库）
- 标注原则：**以简历原文为准**；简历没写的维度**不要**标（ground truth 只含真实出现的断言）

## 用法

```bash
# 1) 基线（无标注）：证据定位/截断引用/转人工/成本延迟 —— 先跑这份了解现状
python eval/run_eval.py --extractors mock --limit 10

# 2) 模型基线（会消耗配额）：同上 + 成本延迟
python eval/run_eval.py --extractors model --limit 5

# 3) 有标注后的对照（dev 调提示词）
python eval/run_eval.py --extractors mock,model --split dev

# 4) 最终验收（test 划分，全量）
python eval/run_eval.py --extractors mock,model --split test
```

## 指标口径

| 指标 | 定义 |
|---|---|
| precision / recall / F1（micro） | 事实按 `kind + 归一化 value` 匹配（年限类转 int）；只统计 `supported` 事实；**只汇总含 GT 的文档**（未标注骨架不参与，避免稀释），所有已标注文档 pooled 计算 |
| 证据定位有效率 | 抽取事实中，引文与原文切片精确一致的比例（程序定位成功） |
| 截断引用率 | 引文起点/终点切断在 **ASCII 词/数字**中间的启发式占比（如 `'achieveme'`、`'期望薪资：4'` 被截断）；中文字符边界不算截断——提示词质量信号 |
| 转人工率 | 含"转人工/定位失败/抽取失败/未配置"等警告的文档占比 |
| 空报告率 | 无任何抽取事实的文档占比 |
| 任职区间匹配 | 年份级匹配（start 年 + end 年；"至今"=None） |
| 成本与延迟 | model 抽取器：总调用/均值、输入输出 token、P50/P95 延迟、总成本/单份（需在 `.env` 填 `MODEL_COST_PER_1K_*` 价目才估算成本） |

## 已知局限（骨架阶段如实记录）

- 标注需人工完成（最懂岗位的人是你）：目标 30–50 份，建议先 dev 20–30 / test 10–20
- 模型抽取为串行（每份约 60–90s）；批量有界并发属于 T9，暂未实现
- 截断引用是启发式，可能有少量误报（如引文恰好停在行首/行尾）
- 事实值完全匹配较严格：模型产出"相近但不同档位"（如 `led` vs `responsible`）计为未匹配，属预期，可看 detail.jsonl 分析
