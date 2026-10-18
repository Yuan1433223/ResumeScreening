# PII 控制清单（加密 / 脱敏 / 保留期）

落点全部经源码核实：`src/pii.py`、`src/repository.py`、`main.py`、`app/api.py`、`src/workflow.py`、`tests/test_t14_pii.py`。

## 目录

1. [Fernet key 生成与装配](#1-fernet-key-生成与装配)
2. [加密列设计原则与取舍](#2-加密列设计原则与取舍)
3. [正则与打码规则表](#3-正则与打码规则表)
4. [幂等迁移模板](#4-幂等迁移模板)
5. [purge 级联删除顺序](#5-purge-级联删除顺序)
6. [入库前 PII 扫描脚本思路](#6-入库前-pii-扫描脚本思路)
7. [明文 / 加密两模式测试矩阵](#7-明文--加密两模式测试矩阵)
8. [无 key 读密文 bug 复盘](#8-无-key-读密文-bug-复盘)

---

## 1. Fernet key 生成与装配

- 生成（一次性，本机执行，输出不入库）：
  ```
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- 装配：把输出写入本地 `.env` 的 `PII_ENCRYPT_KEY=...`（`.env` 已在 `.gitignore`）。依赖 `cryptography==49.0.0`。
- 读取链：`src/config.py` `PII_ENCRYPT_KEY = os.environ.get("PII_ENCRYPT_KEY","").strip()`，未配置时 config 层 warning 一次；`src/pii.py` `encryption_enabled()` 动态读 config（不 import 时固化），便于测试 monkeypatch。
- 空值 = 明文模式：`encrypt_value` 直接返回明文并 warning 一次（`_plaintext_warned` 防刷屏）；`decrypt_value` 原样返回。**明文模式必须显式可见，绝不静默。**
- 密钥轮换：当前版本无旋转方案；换 key 需写迁移脚本（读旧 key 解密 → 用新 key 重加密三列），列入生产待治理。

## 2. 加密列设计原则与取舍

**只加密 `documents.pii_name / pii_phone / pii_email` 三个联系标量。** 透明加解密在 Repository：
- 写：`set_document_pii()` → `encrypt_value(name/phone/email)` 后 UPDATE。
- 读：`_decode_doc()` 对每行三列调 `_decode_pii_field()` → `decrypt_value()`。
- 工作流侧 best-effort：`src/workflow.py` 解析节点 `extract_pii(raw_text, file_path)` 后 `set_document_pii(...)`，整段 try/except，提取失败不阻断主流程。姓名是启发式辅助字段，不参与评分/审批决策。

**刻意不加密以下字段（复核证据链与聚合依赖明文）：**

| 字段 | 不加密原因 |
| --- | --- |
| `pages.text` | 人工复核页需要原文证据切片；加密后复核页必须先解密展示，收益≈0 且破坏只读链路 |
| `assessments.payload` | dashboard 聚合（分数分布、维度命中）直接 SQL 读取；加密后无法 SQL 聚合 |
| `documents.storage_path` / 文件名 | 文件下载路由按明文路径读盘；加密路径会破坏"点链接下载简历文件" |
| `reviews.reason` / 排期表 | 审批意见、候选人 ID、时段是业务流程数据，不是联系标量 |

原则：**加密边界 = "可直接定位到自然人的联系标量"**；复核证据、聚合统计、下载链路保持明文。

## 3. 正则与打码规则表

落点 `src/pii.py`：

| 类型 | 正则 / 规则 | 打码输出 |
| --- | --- | --- |
| 手机号 | `(?<!\d)(1[3-9]\d{9})(?!\d)`（前后不接数字，防误吃长串） | `138****8000`（前 3 + `****` + 后 4） |
| 邮箱 | `[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}` | `t***@example.com`（local 首字符 + `***` + 完整 domain） |
| 身份证 | 入库前扫描用 `\d{17}[\dXx]`（运行时不抽取、不加密） | 不运行时打码，入库扫描命中即拒 |
| 姓名 | 启发式：文件名 stem 去噪（简历/求职/resume/cv/的/本人）后恰为 2-4 汉字；或正文首个 2-4 汉字短行 | `张*`（首字符 + `*` 补足长度；单字 → `*`） |

- 姓名提取**绝不瞎猜**：抽不到留空，宁空勿错。
- 通用称谓（"经理""工程师""先生"）会被 2-4 汉字规则误命中，属已知误报；因姓名仅作辅助展示、不进决策，可接受。
- 打码函数 `mask_pii()` 单例：上传 `pii_warnings` 提示、export `--redact` 导出、前端展示共用同一规则，保证三处打码一致。

## 4. 幂等迁移模板

`CREATE TABLE IF NOT EXISTS` 不会给已存在的老表补列，故老库升级走 `PRAGMA table_info` 探测 + `ALTER TABLE`：

```python
def _ensure_pii_columns(self) -> None:
    existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(documents)")}
    for col in ("pii_name", "pii_phone", "pii_email"):
        if col not in existing:
            self._conn.execute(f"ALTER TABLE documents ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    self._conn.commit()
```

- `_ensure_org_columns()` 同模式，四表 `_ORG_TABLES = ("documents","assessments","runs","batches")` 逐表探测，缺则 `ALTER TABLE ... ADD COLUMN org_id TEXT NOT NULL DEFAULT 'local'`。
- 两个迁移在 `_init_schema()` 末尾、连接建立时自动执行；老行由 DEFAULT 回填（`''` / `'local'`），无需刷数据。
- 原则：SQLite 不支持列变更/删列，只允许 ADD COLUMN；新增敏感列一律 `NOT NULL DEFAULT ''` 以兼容老行。

## 5. purge 级联删除顺序

`Repository.delete_document()`（`src/repository.py`，`PRAGMA foreign_keys=ON`，子表先行）：

```
reviews(按 assessment_id) → external_actions(按 booking) → bookings(按 candidate_id)
→ runs(按 id) → assessments(按 document_id) → pages(按 document_id) → documents(按 id)
```

- 删除前先查出关联 `thread_id`（runs 表）与 `assessment_id` 列表，返回 `thread_ids` 给调用方。
- 检查点清理：`delete_checkpoint_threads(thread_ids)` → `workflow._get_checkpointer().delete_thread(tid)`，删 LangGraph 检查点库 `data/resume.checkpoints.db`；缓存未建立则静默跳过。
- CLI：`python main.py purge --days N [--dry-run] [--keep-checkpoints] [--delete-files]`。
  - `--dry-run` 只打印 `{matched, document_ids, cutoff}`，不删。
  - 天数优先级：`--days N` > `config.PII_RETENTION_DAYS`；<=0/空 → 友好报错退出 1。
  - `--delete-files` 才删 `storage_path` 原文件；默认只删库行。
- 保留依据：`list_documents_older_than(cutoff_iso)`，`created_at` 为 UTC ISO 字符串，字典序比较与时间序一致。

## 6. 入库前 PII 扫描脚本思路

针对"拟入库文本文件"（.md/.py/.json/.html/.txt），流程：

1. **姓名黑名单来源**：从真实简历目录文件名提取（去噪后 2-4 汉字者），本项目曾提取约 190 个，存为本地名单文件（不入库）。
2. **正则扫三类**：手机 `1[3-9]\d{9}`、邮箱、身份证 `\d{17}[\dXx]`，对每个拟入库文件全文 grep。
3. **黑名单姓名再扫一遍**：防文档叙述里残留真名。
4. **人工抽查**：自动 0 命中后仍抽 5-10 个文件肉眼看。
5. **0 命中才入库**；命中即脱敏（打码或替换为合成名）后重扫。

边界：
- PNG/JPG/PDF/DOCX 等二进制**不做文本扫描**——上传接口的 latin-1 粗扫对压缩 PDF/DOCX 会漏（属已知局限，见 `app/api.py` `_scan_upload_pii` 注释）。
- 简历原文目录 `海外项目负责招聘简历/` 整目录不入库，从源头消除。
- `eval/labels/`、`eval/results/` 整目录忽略；只有 `eval/published/` 的聚合数字可入库。

## 7. 明文 / 加密两模式测试矩阵

落点 `tests/test_t14_pii.py`（20 用例，6 个测试类）：

| 测试类 | 用例数 | 覆盖点 |
| --- | --- | --- |
| TestExtractMask | 6 | 手机/邮箱/姓名提取与打码、空值 |
| TestEncryption | 4 | 加密读写 roundtrip、错误 key 抛错、明文模式写明文+warning、老库幂等加列迁移 |
| TestUploadPiiWarnings | 2 | 上传命中返回打码 warnings、无命中空数组 |
| TestPurge | 3 | dry-run 只列旧文档、实删只删旧、days=0 不启用 |
| TestExportRedact | 2 | redact 输出打码、无 redact 输出无 PII 字段 |
| TestNoKeyReadsCiphertext | 3 | 无 key 读密文抛 RuntimeError、明文库无 key 正常读、export 无 key 友好报错 rc1 |

关键不变量（改代码时必须保持）：
- 加密模式写入的库，无 key 进程任何读路径都必须显式炸（`_decode_pii_field`），不得静默。
- 明文模式写入的库，无 key 进程正常读（兼容老库/测试库）。
- export 在 RuntimeError 捕获后 **return 1 且不写任何文件**（写文件动作在 try 之外的后续行，异常提前退出）。

## 8. 无 key 读密文 bug 复盘

**演进链路（真实事故）：**

1. **初版**：`pii.decrypt_value()` 在无 key 时直接 `return text`（把密文当明文返回）。
2. **现象**：开发机配了 `PII_ENCRYPT_KEY`，库里 `pii_name` 是 `gAAAA...` 密文；另一台机器/CI 没配 key，读出来后：
   - 调用方 `doc.get("pii_name")` 拿到一长串 `gAAAA...`（像乱码），或下游 strip 后判空 → **`get_document` 仿佛"丢了这行数据"**。
   - 用户无法区分"库里真没数据"与"我没配 key"。
3. **修复**：在 Repository 读层 `_decode_pii_field` 拦截——值以 `gAAAA` 开头（Fernet 版本字节 0x80 base64 后的固定前缀，`_FERNET_TOKEN_PREFIX`）且 `encryption_enabled()` 为 False → 直接抛 `RuntimeError("PII 字段 X 为加密存储，但当前进程未配置 PII_ENCRYPT_KEY...")`。
4. **连带修复**：`main.py export` 捕获该 RuntimeError → stderr 友好提示 + `return 1`，且**在打开写文件之前**退出，不产生半截导出文件。

教训：**对称加密系统必须区分"无 key 的明文库"与"无 key 的加密库"**；前缀探测是最便宜的判别手段。任何"降级返回"都必须显式报错，静默降级 = 数据丢失假象。
