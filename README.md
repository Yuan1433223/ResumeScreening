# 简历筛选 Agent（海外项目负责人 · 钢结构/MiC）

基于 **LangGraph** 的简历筛选辅助工作流：解析 → 事实抽取（规则 / 云端大模型）→
证据校验 → 8 维岗位评分 → **人工审批（interrupt / 进程恢复）** → 幂等排期建会通知，
并配有一个 **FastAPI + React/TypeScript 的可交互工作台** 与 **真实简历评测骨架**。

面向"海外项目负责人（钢结构/MiC 方向）"岗位；`DEVELOPMENT_SPEC.md` 定义了 P0–P3
阶段规格，本仓库在其上增量完成 T1–T5c（见下）。

---

## 能力亮点（技术视角）

| 能力 | 落点 | 佐证 |
| --- | --- | --- |
| 人工介入的 Agent 工作流 | review 节点 `interrupt` 等待审批；审批以 request_id 幂等入库后恢复 | `src/workflow.py`、`src/review.py` |
| 崩溃恢复与重放 | LangGraph `SqliteSaver` 检查点；恢复成功后才消费审批（T1 故障注入测试） | `tests/test_t1_recovery_boundary.py` |
| 任务身份三分离 | 文档身份 / 评估版本键 / 执行线程身份分离，跨批次稳定、换抽取器可重跑（T2） | `tests/test_t2_identity.py` |
| 云端大模型抽取 | OpenAI 兼容客户端 + 结构化输出 + 页码/原文引用定位（程序算偏移）+ 限流重试 + 成本统计（T3） | `src/providers/model_extractor.py` |
| 真实简历评测 | 标注样本对照 mock/model 的事实准确率、证据支持率、成本与延迟（T4） | `eval/run_eval.py` |
| 可交互工作台 | FastAPI 后台任务化批次 + React/TS 三页（批次/复核/排期）（T5a/T5b/T5c） | `app/api.py`、`frontend/` |
| 并发与缓存工程 | Repository 连接级 RLock、图执行锁、页面缓存复用（重跑跳过 250s OCR） | `src/repository.py`、`src/workflow.py` |

**测试：94 项全部通过**（规则回归 + 恢复边界 + 模型抽取器 + 评测 runner + API，均不依赖真实网络）。

---

## 快速开始（工作台）

```powershell
# 终端 1：后端（FastAPI，端口 8017）
.\.venv\Scripts\python.exe -m uvicorn app.api:app --port 8017

# 终端 2：前端（Vite dev，端口 5173，/api 代理到 8017）
cd frontend; npm install; npm run dev
```

浏览器打开 **http://localhost:5173**：批次页选抽取器（规则 / 大模型）创建批次 →
复核页左原文右事实、点击证据高亮定位、提交审批 → 排期页生成预约与通知。

> 模型抽取需在 `.env` 配置 `MODEL_ENABLED=1 / MODEL_API_KEY / MODEL_BASE_URL / MODEL_NAME`
> （模板见 `.env.example`；`.env` 不入版本控制）。未配置时自动降级为人工复核，不影响其他功能。

---

## 功能总览

- **CLI 闭环**：`analyze / batch / review / resume / schedule / export / clean`，合成数据离线可复现
- **工作台**：批次（上传/后台任务/进度轮询）、复核（原文↔事实双向定位、审批）、排期（幂等生成）
- **评测**：`eval/run_eval.py` 在真实简历上对照 mock 与 model 抽取（详见"评测"节）
- **一键演示**：`demo/run_demo.py` 跑通导入→审批→重启恢复→排期→通知重试全流程（合成数据）

## 系统架构

```mermaid
flowchart LR
    F[简历文件<br/>PDF / JPG] --> P[parse 分页解析<br/>pdfplumber / tesseract / rapidocr]
    P -->|pages 缓存<br/>重跑免 OCR| DB2[(pages 表)]

    subgraph 候选人工作流（LangGraph，每简历独立 thread）
        P --> E[extract 事实抽取<br/>mock 规则 / model 云端大模型]
        E --> V[validate 证据校验<br/>引用=原文切片]
        V --> S[screen 三态硬条件]
        S --> SC[score 8 维评分<br/>幂等落库]
        SC --> R[review interrupt<br/>人工审批]
        R --> A[apply 消费审批]
        A --> RP[report 汇总]
        P -.解析失败.-> R
        V -.校验失败.-> R
    end

    R -->|批次审批截止| B[create_round 建轮]
    B --> C[close_round 冻结]
    C --> SD[schedule_round<br/>排序+原子占槽]
    SD --> X[run_external<br/>建会→通知]
    X -.通知失败→只重试通知.-> X

    subgraph 存储（SQLite WAL）
        DB[(resume.db<br/>业务数据)]
        CK[(checkpoints.db<br/>LangGraph 检查点)]
    end
    SC --> DB
    SD --> DB
    X --> DB
    A -.进程崩溃→新进程 resume_run 恢复.-> CK
```

工作台侧：`app/api.py` 以 SQLite 为唯一事实来源；批次在**后台线程**执行
（任务表 + `GET /api/batches/tasks/{id}` 轮询），前端 React/TS 通过 `/api` 代理访问。

## 工作流节点

1. **parse** — 分页解析 PDF/图片；失败 → `needs_review`，绝不进入评分；正文落 `pages` 缓存
2. **extract** — 事实抽取：默认 Mock 规则；启用 MODEL_* 后走模型（模型只回页码+原文引用，
   程序计算偏移）；均绑定精确引用（quote=原文切片）
3. **validate** — 证据校验：引用与原文一致、页码存在、范围合法；失败 → `needs_review`
4. **screen** — 三态硬条件：相关领域经验 AND（总经验≥8年 OR 直接相关≥5年）AND 项目管理经历
5. **score** — 8 维评分（每维取最高档），输出 known_score / missing_items / 得分证据定位；幂等落库
6. **review** — interrupt 等待人工审批；审批先以 request_id 幂等入库再恢复任务
7. **apply** — 消费审批结果，仅更新审批状态（排期由独立 scheduler 完成）
8. **report** — 汇总报告（评分明细、证据原文、审批状态）

路由：解析失败/校验未通过/硬条件 unknown/评分不完整 → `needs_review`；
硬条件 fail → `hold`；≥75 → `recommend`，60–74 → `needs_review`，<60 → `hold`。

## 评分维度（8 维，满分 100）

| 维度 | 满分 | 分档 |
| --- | ---: | --- |
| 直接钢结构/MiC/模块化经验 | 30 | 参与 10 / 承担交付责任 20 / 主导并有成果 30 |
| 钢结构项目全流程 | 15 | 1 阶段 5 / 2 阶段 10 / ≥3 且含交付 15 |
| 海外项目/跨国协作 | 15 | 跨国协作 5 / 海外项目职责 10 / 海外交付 15 |
| 项目/团队管理 | 15 | 管理职责 5 / 职责对象明确 10 / 另有范围或结果 15 |
| 生产/质量/安全/体系 | 10 | 一项 5 / 两项且有举措 10 |
| 英语/跨文化沟通 | 5 | 能力信息 2 / 业务使用实例 5 |
| 项目规模/量化成果 | 5 | 有单位有所属项目的数值 5 |
| 岗位适配/机动性 | 5 | 接受工作地点 2 / 另接受出差驻场 5 |

未提及维度 score=null、status=unknown；有明确反向事实计 0；known_score 仅累加已确认项。

---

## 模型接入（T3 + T5c）

- `ModelConfig`（`schemas.py`）：云端配置契约，密钥用 `SecretStr` 防日志泄露，由 `.env` 装配
- `ModelExtractor`：提示词 v1 + 结构化解析 + 引用定位（模型回页码+原文引用，程序算偏移；
  定位失败→明确歧义转人工）+ 有限重试（首次最多 3 次）+ 格式纠正一次 +
  token/耗时/成本统计（`ExtractionReport.stats`）
- `CloudModelClient`：OpenAI 兼容 `/chat/completions`（DeepSeek 等通用）；
  429/5xx/网络超时自动重试，4xx 不重试
- 未配置/未启用 → 空报告 + 警告 + 人工复核，不影响规则版流程
- 工作台接入：抽取器可选 mock/model；`node_extract` 由 state 驱动；批次后台任务化 +
  页面缓存复用（同文档重跑跳过 OCR，实测长图 rapidocr 约 250s/张 → 缓存命中 0s）

## 评测（T4）

- `eval/run_eval.py`：标注样本（`labels/`）+ runner（dev/test 划分、micro P/R/F1、
  转人工率、截断引用率、成本与延迟 P50/P95）；`results/` 输出脱敏聚合
- **真实简历对照**（已标注 3 份 dev 样本，小样本供参考）：

| 抽取器 | F1 | P / R | 备注 |
| --- | ---: | ---: | --- |
| mock 规则 | 0.46 | 0.45 / 0.47 | 不抽年限、关键词误报（如把"安全"专业名当质量事实） |
| model 大模型 | 0.67 | 0.71 / 0.63 | 转人工率 33%（1/3，第 3 页噪声字符引用定位失败） |

- 模型错误主要是档位粒度与 GT 口径争议（如推断 relevant_domain=True 但 GT 未含→记 spurious，
  会低估模型）——口径本身是有价值的讨论点
- 数据边界：真实简历本地评测与云端调用已获确认；`labels/`、`results/` 不入版本控制

## 工作台（T5a/T5b/T5c）

- **T5a 服务层**：FastAPI（`app/api.py`）复用现有 workflow/repository，SQLite 为唯一事实来源；
  复核接口按需解析并缓存 `pages`（同一文档二次复核免重解析）
- **T5b 前端**：React 18 + TypeScript 严格模式 + Vite，无 UI 库；
  三页：**批次**（上传/创建/结果表）、**复核**（左原文右事实，点击事实高亮定位，提交审批）、
  **排期**（时段表 + 预约/通知状态）；视觉：系统字体、白/浅灰、单一墨青强调色，去 AI 味
- **T5c 后台任务 + 模型接入**：`POST /api/batches` 后台线程执行（单份失败不阻断）；
  `GET /api/batches/tasks/{id}` 轮询（done/total/current_file/rows）；
  Repository 公开方法 `threading.RLock` 统一包裹 + `_GRAPH_LOCK` 保护图执行；
  批次每次运行独立线程（`{batch_id}__{file}`），换抽取器重跑得到全新分析；
  `node_parse` 优先读 pages 缓存（有测试保证 parse_document 被禁用时重跑仍成功）
- **真实冒烟**（熊世峰 JPG，5046 字）：缓存命中后模型批次 39s 完成
  （模型调用 37.5s，9 事实 4 任职，in=3606/out=8227 token）；复核页 12 事实、证据 12/12

## CLI 命令

```powershell
# 单份分析（停在审批节点；--batch-id 归批、--thread-id 固定检查点线程）
python main.py analyze "海外项目负责招聘简历\xxx.pdf" --batch-id b1

# 批量分析（合成样例演示）→ 证据校验/评分/路由落库
python main.py batch --dir demo\samples

# 提交审批（approve/hold/reject；--request-id 相同则幂等；版本不匹配报 VersionConflictError）
python main.py review <assessment_id> approve --version v1 --request-id req001

# 恢复被中断任务（审批已入库但恢复前崩溃时：读取未消费审批继续执行）
python main.py resume <run_id>

# 批次排期：建轮 → 冻结 → 6 人争 4 时段 → 建会 → 通知（--fail-notify/--retry 演示幂等重试）
python main.py schedule <batch_id> --fail-notify 1 --retry

# 导出批次状态（JSON/Markdown/Excel）
python main.py export <batch_id>

# 本地清理：按 document_id 级联删除数据与检查点（规格 §12）
python main.py clean <document_id> [--delete-file]
```

一键演示：`python demo\make_samples.py demo\samples` → `python demo\run_demo.py`，
产物 `demo\output\` 下三个静态展示页 + 评测报告（离线可打开）。

---

## 项目结构

```
ResumeScreening/
├── Agent.md                     # 任务说明（原题）
├── DEVELOPMENT_SPEC.md          # 开发规格与 P0-P3 完成定义
├── README.md                    # 本文件
├── requirements.txt             # 依赖（锁定版本，含 fastapi/uvicorn/httpx）
├── .env.example                 # 云端模型配置模板（MODEL_* 字段，密钥不入库）
├── main.py                      # CLI 入口（analyze/batch/review/resume/schedule/export/clean/graph）
├── app/
│   └── api.py                   # FastAPI 工作台后端（批次后台任务/复核/审批/排期）[T5a/T5c]
├── frontend/                    # React+TS+Vite 工作台（批次/复核/排期三页）[T5b]
├── eval/                        # 评测：标注格式 + runner + 结果（labels/results 不入库）[T4]
├── demo/
│   ├── run_demo.py              # 一键演示脚本（按规格 §12 顺序，合成数据）[P3]
│   ├── make_samples.py          # 合成样例生成器（最小 PDF + 坏文件）[P3]
│   ├── samples/                 # 生成的合成样例（可离线复现）
│   └── output/                  # 演示产物（不入版本控制）[P3]
├── src/
│   ├── config.py                # 岗位画像、8 维评分表 v1、三态硬条件、路径
│   ├── schemas.py               # Pydantic 数据契约（含 ModelConfig / ExtractionReport）
│   ├── state.py                 # LangGraph 共享状态
│   ├── parser.py                # 分页解析（PDF / OCR / 文件名元数据）
│   ├── extraction.py            # 抽取服务层入口（extract_facts，mock/model 分派）
│   ├── evidence.py              # 证据引用校验（validate_evidence）
│   ├── scorer.py                # 三态硬条件 + 8 维规则评分 + 建议路由
│   ├── workflow.py              # LangGraph 工作流（parse→…→score→review(interrupt)→apply→report）
│   ├── repository.py            # SQLite 仓库（WAL、唯一约束、幂等 API、RLock、页面缓存）
│   ├── review.py                # 审批服务（版本校验、request_id 幂等、恢复任务）
│   ├── scheduler.py             # 全局排期器（冻结名单排序、原子占槽、外部动作）
│   ├── providers/               # mock_extractor / model_extractor / mock_calendar / mock_notifier
│   └── batch.py                 # 批量处理与导出（JSON/Excel/Markdown）
├── tests/                       # 94 项（conftest 临时 SQLite 隔离，不依赖真实网络）
└── 海外项目负责招聘简历/          # 真实数据源（仅本地，不入版本控制）
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q   # 94 passed
```

| 批次 | 数量 | 覆盖 |
| --- | ---: | --- |
| P0 规则回归 | 18 | 合成样例（空正文绝不推荐、年限并集、路由边界 59/60/74/75 等） |
| P1 抽取/校验 | 12 | 分页解析、Mock 抽取、引用校验（伪造/错页/越界拒绝）、端到端可定位 |
| P2 流程闭环 | 9 | 审批前不排期、重复批准幂等、崩溃恢复、6 争 4 排期、通知失败重试、批量容错 |
| T1 恢复边界 | 3 | 审批消费时序、END 重放保护（故障注入） |
| T2 身份三分离 | 4 | 同文件重跑复用、评估版本键、跨批次线程稳定 |
| T3 模型抽取器 | 13 | 结构化解析、引用定位、重试/纠正、成本统计（mock HTTP，不依赖真实 API） |
| T3 云端客户端 | 6 | 429/5xx 重试、4xx 不重试、响应结构异常（mock HTTP） |
| T4 评测 runner | 10 | 指标计算、dev/test 划分、无标注基线、截断引用率 |
| T5 API | 19 | 批次/复核/审批、后台任务轮询、抽取器版本透出、页面缓存复用、model 未启用降级 |

---

## 变更说明（历史）

- **P3**：CLI 闭环（analyze/review/resume/schedule/export/clean）、document 级级联清理、
  路径环境变量覆盖、一键演示与静态展示页、合成评测报告（不虚报准确率）
- **P2**：SQLite 持久化（WAL+唯一约束）、审批 interrupt/恢复（SqliteSaver）、
  全局幂等排期、外部动作幂等（通知失败只重试通知）、测试隔离（conftest）
- **P1**：分页解析、Mock 抽取（精确字符偏移引用）、证据校验、评分明细可回溯原文
- **P0**：8 维档位评分（每维取最高档）、三态硬条件、文件名不参与评分、
  空正文/解析失败直接人工复核

## 注意事项 / 已知边界

- OCR：优先 tesseract（系统安装时），未安装自动回退 rapidocr-onnxruntime；
  实测本机长图 rapidocr 约 250s/张（切片推理），普通图片约 30s；工作台靠 pages 缓存跳过重跑
- 批次行展示该文档**最近一次**审批结果；换抽取器重跑覆盖同文档评估，旧审批仍显示属预期
- LangGraph 检查点反序列化自定义 Pydantic 类型时打印 "Deserializing unregistered type"
  警告（默认不阻塞，未来版本可能收紧，届时需注册允许模块）
- 排期/会议/通知默认 mock 实现（`providers/`），真实集成需替换
- Mock 抽取不代表真实准确率；真实评测见"评测"节（小样本标注对照）

## 简历上的项目描述（据实填写模板）

> 基于 LangGraph 实现带证据引用和人工审批的简历筛选辅助工作流：支持持久化恢复、
> 批次处理、幂等模拟排期，并接入云端大模型抽取与真实简历评测（94 项测试全绿；
> 小样本标注对照 model F1 0.67 vs 规则 0.46）；配套 FastAPI + React/TS 可交互工作台。
> 在合成样本上验证了解析容错、跨进程恢复、6 人竞争 4 时段的排期闭环与外部动作幂等。
