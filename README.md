# 简历筛选 Agent（海外项目负责人 · 钢结构/MiC）

基于 LangGraph 的简历筛选辅助工作流，面向"海外项目负责人（钢结构/MiC 方向）"岗位。
P0：规则版（无 LLM）——严格数据契约 + 8 维岗位评分 + 三态硬条件 + 合成回归测试。
P1：分析闭环——分页解析 + Mock 抽取（带精确引用）+ 证据校验 + 规则评分，每个得分可定位原文。
P2：流程闭环——SQLite 持久化 + 审批 interrupt/进程恢复 + 幂等排期/建会/通知。
P3：作品展示——CLI 闭环（analyze/review/resume/schedule/export/clean）+ 三个展示页 + 评测报告 +
一键演示脚本（合成数据、离线可复现）。
对应 `DEVELOPMENT_SPEC.md` P0-P3 阶段。

## 环境搭建（uv 独立环境，不依赖系统解释器）

```powershell
cd F:\ResumeScreening
uv venv --python 3.12          # 或使用已有 .venv
.\.venv\Scripts\Activate.ps1
uv pip install -r requirements.txt
```

> 本项目 `.venv` 为独立 Python 3.12 环境（由 uv 管理），不要使用系统/沙箱解释器。

## 项目结构

```
ResumeScreening/
├── Agent.md                     # 任务说明（原题）
├── DEVELOPMENT_SPEC.md          # 开发规格与 P0-P3 完成定义
├── README.md                    # 本文件
├── requirements.txt             # 依赖（锁定版本，含 pytest）
├── main.py                      # CLI 入口（analyze/batch/review/resume/schedule/export/clean/graph）
├── demo/
│   ├── run_demo.py              # 一键演示脚本（按规格 §12 顺序，合成数据）[P3]
│   ├── make_samples.py          # 合成样例生成器（最小 PDF + 坏文件）[P3]
│   ├── samples/                 # 生成的合成样例（可离线复现）
│   └── output/                  # 演示产物：index + 3 展示页 + 评测报告（不入版本控制）[P3]
├── src/
│   ├── __init__.py
│   ├── config.py                # 岗位画像、8 维评分表 v1、三态硬条件、相对路径
│   ├── schemas.py               # Pydantic 数据契约（文档/页/证据/事实/评估/审批/排期/外部动作）
│   ├── state.py                 # LangGraph 共享状态
│   ├── parser.py                # 分页解析（PDF / OCR / 文件名元数据）
│   ├── extraction.py            # 抽取服务层入口（extract_facts）
│   ├── evidence.py              # 证据引用校验（validate_evidence）
│   ├── scorer.py                # 三态硬条件 + 8 维规则评分 + 建议路由
│   ├── workflow.py              # LangGraph 工作流（parse→…→score→review(interrupt)→apply→report）
│   ├── repository.py            # SQLite 仓库（WAL、唯一约束、幂等 API、document 级清理）[P2/P3]
│   ├── review.py                # 审批服务（版本校验、request_id 幂等、恢复任务）[P2]
│   ├── scheduler.py             # 全局排期器（冻结名单排序、原子占槽、外部动作）[P2]
│   ├── providers/               # 抽取器 + mock_calendar / mock_notifier（幂等）[P2]
│   └── batch.py                 # 批量处理与导出（JSON/Excel/Markdown）
├── tests/
│   ├── conftest.py              # 临时 SQLite 隔离（逐测试独立库）
│   ├── test_p0_regression.py    # P0 合成回归用例（18 个）
│   ├── test_p1_extraction.py    # P1 抽取/校验/端到端用例（12 个）
│   └── test_p2_flow.py          # P2 流程闭环用例（9 个）[P2]
├── 海外项目负责招聘简历/        # 数据源（仅本地使用，不入版本控制）
├── data/                        # SQLite 运行数据（不入版本控制）[P2]
└── output/                      # 结果输出目录（不入版本控制）
```

## 系统架构

```mermaid
flowchart LR
    F[简历文件<br/>PDF / JPG / 坏文件] --> P[parse 分页解析<br/>pdfplumber / rapidocr]

    subgraph 候选人工作流（LangGraph，每简历独立 thread）
        P --> E[extract Mock 抽取<br/>事实+任职区间+引用]
        E --> V[validate 证据校验]
        V --> S[screen 三态硬条件]
        S --> SC[score 8 维评分<br/>落库 SQLite]
        SC --> R[review interrupt<br/>人工审批]
        R --> A[apply 消费审批]
        A --> RP[report 汇总]
        P -.解析失败.-> R
        V -.校验失败.-> R
    end

    R -->|批次审批截止| B[create_round 建轮<br/>4×45min]
    B --> C[close_round 冻结批次]
    C --> SD[schedule_round<br/>排序+原子占槽]
    SD --> X[run_external<br/>建会→通知]
    X -.通知失败→只重试通知.-> X

    RP --> OUT[分析报告<br/>证据定位/缺失项]
    subgraph 存储（SQLite WAL）
        DB[(resume.db<br/>业务数据)]
        CK[(checkpoints.db<br/>LangGraph 检查点)]
    end
    SC --> DB
    SD --> DB
    X --> DB
    P --> CK
    R --> CK
    A -.进程崩溃→新进程 resume_run 恢复.-> CK
```

## 工作流节点

1. **parse** — 分页解析 PDF/图片；正文为空/解析失败 → `needs_review`，绝不进入评分
2. **extract** — 事实抽取：默认 Mock 抽取器（规则）；启用 MODEL_* 后走模型抽取
   （见 T3 变更说明），均绑定精确引用（quote=原文切片）
3. **validate** — 证据校验：引用与原文一致、页码存在、范围合法；校验失败 → `needs_review`
4. **screen** — 三态硬条件：相关领域经验 AND（总经验≥8年 OR 直接相关≥5年）AND 项目管理经历
5. **score** — 8 维评分（每维取最高档），输出 known_score / missing_items / 得分证据定位；结果落库
6. **review** — 复核节点：interrupt 等待人工审批（审批前不建会、不通知）；审批记录先以 request_id
   幂等入库再恢复任务（进程在两步之间退出时，恢复程序读取未消费审批继续执行）
7. **apply** — 消费审批结果，仅更新审批状态（排期由独立 scheduler 完成）
8. **report** — 汇总报告（含评分明细与证据原文、审批状态）

图外闭环（P2）：批次审批截止 → `scheduler.create_round/close_round/schedule_round`
（4 个 45 分钟时段、冻结名单全局排序、原子占槽、无槽候补）→
`run_external`（建会成功后才通知，均幂等，通知失败只重试通知不重复建会）。

路由优先级（规格 §6.4）：解析失败/证据校验未通过/硬条件 unknown/评分不完整 → `needs_review`；
否则硬条件 fail → `hold`；否则 ≥75 → `recommend`，60–74 → `needs_review`，<60 → `hold`。

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

未提及维度 score=null、status=unknown；有明确反向事实（如"英语不熟练"）计 0；
known_score 仅累加已确认项；score_complete=false 时禁止显示完整适配率。

## 运行方式

```powershell
# 单份简历（旧入口，兼容）
python main.py --file "海外项目负责招聘简历\xxx.pdf"

# 批量（限制数量快速测试）
python main.py --batch --limit 10

# 全量
python main.py --batch

# 工作流图
python main.py --graph
```

## CLI 命令（P3，规格 §9/§10）

```powershell
# 单份分析（停在审批节点；可用 --batch-id 归批、--thread-id 固定检查点线程）
python main.py analyze "海外项目负责招聘简历\xxx.pdf" --batch-id b1

# 批量分析（合成样例演示）→ 证据校验/评分/路由落库
python main.py batch --dir demo\samples

# 提交审批（approve/hold/reject；--request-id 相同则幂等；版本不匹配报 VersionConflictError）
python main.py review <assessment_id> approve --version v1 --request-id req001

# 恢复被中断任务（审批已入库但恢复前崩溃时：读取未消费审批继续执行）
python main.py resume <run_id>

# 批次排期：建轮 → 冻结 → 6 人争 4 时段 → 建会 → 通知
#   --fail-notify 1 --retry 演示"通知失败→只重试通知，不重复建会"
python main.py schedule <batch_id> --fail-notify 1 --retry

# 导出批次状态（JSON/Markdown/Excel）
python main.py export <batch_id>

# 本地清理：按 document_id 删除文件/页/事实/评估及关联检查点（规格 §12）
python main.py clean <document_id> [--delete-file]
```

## 一键演示与展示页（P3）

```powershell
# 生成合成样例（4 份 PDF：3 份可解析 + 1 份坏文件）
python demo\make_samples.py demo\samples

# 一键演示：按规格 §12 顺序跑通全流程，产出三个展示页 + 评测报告 + 日志
python demo\run_demo.py
# 产物：demo\output\ 下 index.html / 01_import.html / 02_approval.html /
#       03_scheduling.html / evaluation.md / demo.log（浏览器直接打开）
```

演示顺序覆盖：批量导入含坏文件 → 证据与缺失项 → 重启进程（子进程模拟，从
SQLite 检查点恢复）→ 批准并恢复 → 6 人竞争 4 时段 → 重复点击幂等 → 通知失败重试 →
对照评测。全部合成数据、离线可复现、不调用外部模型。

## P3 变更说明（相对 P2）

- **CLI 闭环**：`main.py` 新增 analyze/review/resume/schedule/export/clean 子命令，
  保留旧 --file/--batch/--graph 入口；schedule 支持 --availability/--fail-notify/--fail-meeting/--retry
  注入故障演示幂等与重试。
- **document 级清理**：`repository.delete_document`（规格 §12 数据边界入口）按 document_id
  级联删除 reviews→external_actions→bookings→runs→assessments→pages→documents，
  返回关联 thread_id；`delete_checkpoint_threads` 用 SqliteSaver.delete_thread 清理 LangGraph 检查点。
  `main.py clean <document_id> [--delete-file]` 暴露为命令。
- **路径可覆盖**：`config.DB_PATH / CHECKPOINT_DB_PATH` 支持环境变量
  `RESUME_DB_PATH / RESUME_CHECKPOINT_PATH`（演示脚本与测试隔离用），默认仍是 data/ 下。
- **一键演示**：`demo/make_samples.py`（合成最小 PDF + 坏文件，Helvetica 免嵌入）、
  `demo/run_demo.py`（§12 演示顺序全流程，含子进程模拟"重启进程"恢复，产物见 demo/output/）。
- **评测报告**：`demo/output/evaluation.md` 记录合成集上 7 个场景的规则行为；
  明确声明不构成真实准确率/提效倍数/生产规模（未人工标注，按 §12 不虚报）。
- **展示页**：index + 01_import + 02_approval + 03_scheduling，静态 HTML 离线可打开。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -v
```

P0（18 个）：规格 §10 的 7 个合成样例（空正文绝不推荐、仅表态、6年+5年年限、重叠日期、
英语否定、重复行业词、解析器异常）与 §11 的 59/60/74/75 路由边界、三态年限等。
P1（12 个）：分页解析、Mock 抽取（事实/任职/英语否定）、引用校验（伪造引用/错页/越界拒绝）、
端到端"得分可定位原文"。
P2（9 个）：审批前不排期、重复批准幂等、评估版本冲突、崩溃后恢复（审批已入库未恢复）、
6 人争 4 时段（4 confirmed + 2 waitlist + 无重复占位）、通知失败重试不重复建会、
重复排期幂等、批量单项失败其余完成。

## P2 变更说明（相对 P1）

- **SQLite 持久化**：`repository.py`（WAL + 外键 + 事务），唯一约束
  documents(sha256)/reviews(request_id)/runs(thread_id)/slots(round,start)/bookings(round,candidate)/
  external_actions(booking,type)；`get_repository()` 进程单例。
- **审批恢复**：`workflow` 用 `SqliteSaver`（独立连接 + 进程内缓存编译图）持久化检查点；
  review 节点 `interrupt` 等人工审批；`submit_review` 先以 request_id 幂等入库再
  `Command(resume=...)` 恢复——进程在两步之间退出时，`resume_run` 读取未消费审批继续（§7）。
  注意：`SqliteSaver.from_conn_string()` 返回 context manager，不能直接作 checkpointer，
  须 `sqlite3.connect + SqliteSaver(conn)`。
- **审批消费时序（T1 修复）**：`resume_run` 在恢复成功后才标记审批已消费；恢复中途崩溃时
  审批保持未消费，下次重放可继续；线程已恢复完成时重放只补收尾、不重复执行。
  故障注入与重放测试见 `tests/test_t1_recovery_boundary.py`。
- **任务身份三分离（T2 修复）**：执行身份 = thread 身份——同文件重跑复用同一 run 行
  （批次归属更新为最新、状态重置为新一轮）；评估版本键改为 `{document_id}_{job_version}`，
  岗位版本升级自动产生新评估，旧版本审批对新版本无效（`VersionConflictError`）；
  批处理弃用 `resume_{idx}` 位置编号，thread 由稳定 document 派生（跨批次稳定、不串线程）。
  测试见 `tests/test_t2_identity.py`。
- **全局排期**：批次审批截止后由 `scheduler` 独立排期（不在候选人图内建会）；
  冻结名单 = 已批准且审批已消费且评估版本匹配；排序 `(-known_score, -mic, -overseas, -management, id)`；
  `claim_slot` 事务内原子占槽（先插 pending 预约，占用成功才置 confirmed）；无槽转候补。
- **幂等外部动作**：`MockCalendar.create_meeting / MockNotifier.send` 以幂等键返回稳定 provider_id；
  会议成功后才允许通知；通知失败重试只重试通知，不重复建会（§8）。
- **测试隔离**：`conftest.py` 为每个测试创建临时 SQLite，并同步 patch
  config/repository/workflow 三处路径常量（`from .config import DB_PATH` 是导入时复制，只 patch
  config 无效）并重置单例/checkpointer 缓存；真实 `data/` 不再被测试污染。
- **数据契约新增**：Review/ReviewDecision、Batch、Slot/Booking/BookingStatus、
  ExternalAction/ExternalActionStatus、SchedulingRound（`schemas.py`）。

## P1 变更说明（相对 P0）

- 分页解析：`parse_document -> list[Page]`（页文本哈希 + 页级质量标记）。
- Mock 抽取器：`providers/mock_extractor.py` 从文本抽取 8 维事实与任职区间，每条事实绑定
  Evidence（start/end 精确字符偏移，quote=原文切片）；否定句（"英语不熟练"）产出 CONTRADICTED。
- 证据校验：`evidence.py::validate_evidence` 拒绝伪造引用、错页引用、越界偏移。
- 评分明细：每个已确认维度可回溯到证据原文（matched_facts → fact → evidence.quote）。
- Model 抽取器：P1 为占位（不调用外部模型）；T3 已实现本地逻辑并预留云端调用点
  （见 T3 变更说明）。

## OCR 支持（图片简历）

- 图片简历 OCR：优先 tesseract（系统安装时）；未安装时自动回退 rapidocr-onnxruntime
  （纯 pip 依赖、模型随包内置、无需外部下载）。
- 超长简历图（如 720×20000 的网页截图）自动切块 OCR（2000px 高、150px 重叠），
  单页合并为整页文本。
- 验证：3 张真实 JPG 简历均可参与筛选（熊世峰 5046 字、曲良峰 1639 字、贺兴龙 2375 字），
  证据校验通过、每维得分可定位原文。
- 已知限制：OCR 文本质量取决于图片分辨率/扫描质量；rapidocr CPU 推理约 30s/张（切块）。

## P0 变更说明（相对原实现）

- 文件名/目录名不再参与评分；文件名年限仅作为"待核实元数据"展示。
- 正文为空/解析失败 → 直接人工复核，不再产出空评分。
- 评分由 6 维关键词叠加改为 8 维档位评分（每维取最高档，不叠加次数）。
- 硬条件改为三态（pass/fail/unknown），支持年月并集去重、月份未知的区间不确定性。
- 修复依赖：去掉未声明的 pandas（导出改用 openpyxl）、补 pydantic/pytest。
- 去除硬编码路径 `f:/ResumeScreening`，改由项目根相对定位。

## T3 变更说明（相对 P3）：云端大模型抽取（增量落地）

- `ModelConfig`（schemas.py）：云端配置契约，密钥用 `SecretStr` 防日志泄露；
  由 `.env` 装配（字段见 `.env.example` 的 `MODEL_*`）。
- `ModelExtractor`（providers/model_extractor.py）：提示词 v1 + 结构化解析 +
  引用定位（**模型只回页码+原文引用，程序计算偏移**；定位失败 → 明确歧义并转人工）
  + 有限重试（规格 §7，含首次最多 3 次）+ 格式纠正一次 + 成本/耗时/token 统计（`stats`）。
- 云端作用域预留：`CloudModelClient.complete()` 是唯一待补充的云端调用点
  （OpenAI 兼容 /chat/completions 约定已注释）；在 `.env` 填好
  `MODEL_API_KEY / MODEL_BASE_URL / MODEL_NAME` 后启用。
- 未配置/未启用时，模型抽取返回空报告 + 警告并路由人工复核，不影响规则版流程与演示。
- 测试：`tests/test_t3_model_extractor.py`（13 项，全部确定性、不依赖真实 API）。

## 注意事项

- 图片简历 OCR：优先 tesseract（系统安装时）；未安装时自动回退 rapidocr-onnxruntime
  （纯 pip 依赖、模型随包内置、无需外部下载）。
- 本仓库 P0–P3 为规则实现；Mock 抽取数据不代表真实准确率，真实评测需人工标注（规格 §11/§12）。
- 真实简历、SQLite 运行数据与输出不入版本控制，公开仓库仅放合成样例（demo/samples）。
- 已知缺口：LangGraph 检查点反序列化自定义 Pydantic 类型时打印
  "Deserializing unregistered type" 警告（默认不阻塞，未来版本可能收紧，届时需注册允许模块）；
  演示默认 Mock，真实排期/会议/通知需替换 providers 实现。
- 中文简历纯年份区间（"2016至2019年"）与带月至至今（"2025年8月至今"）均已覆盖回归测试。

## 简历上的项目描述（据实填写模板，规格 §12）

> 基于 LangGraph 实现带证据引用和人工审批的简历筛选辅助工作流，支持持久化恢复、
> 批量处理及幂等模拟排期；在合成样本上验证了解析容错、跨进程恢复、6 人竞争 4 时段的
> 排期闭环与外部动作幂等（评测记录见 demo/output/evaluation.md）。

未测量真实标注准确率，故描述中不填写准确率、提效倍数或生产规模。
