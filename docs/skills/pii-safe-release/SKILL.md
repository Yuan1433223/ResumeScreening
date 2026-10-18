---
name: pii-safe-release
description: 数据安全与上线基线（PII safe release）。当项目要把含真实候选人简历/手机号/邮箱的本地工作台推进到可入库、可加密、可交付、可运行状态时使用；典型触发：第一次 git 提交或开源化、装配 PII_ENCRYPT_KEY 启用字段加密、开 AUTH_ENABLED 上线部署、purge 清理过期简历、导出批次给 HR、排查"无 key 读到 gAAAA 密文"类故障。执行数据分级与 .gitignore 验证、入库前 PII 扫描、Fernet 透明加解密与幂等迁移、上传魔数/大小校验、导出 --redact 打码、purge 级联删除、签名 Cookie 认证三端点与 13 路由依赖、org_id 四表预留。不适用于从零设计企业级 KMS/多租户 RBAC/服务端会话（本 skill 只做 P1 单用户本地工作台的最小基线，生产增强项在 references 列待治理）；不做收束门面（README/LICENSE/CI/Docker 归 project-closeout）。
---

# PII Safe Release（数据安全与上线基线）

把"含真实候选人简历的本地工作台"推到**可入库、可加密、可交付、可运行**的最小安全基线。**只补数据安全与认证红线，不做业务功能、不做收束门面**。

## 前提

业务功能已跑通（`tests/test_t14_pii.py` 20 用例 / `tests/test_t15_auth.py` 22 用例全绿）。功能未闭环先回开发，不进入安全基线装配。

## 工作流（按数据生命周期，按序执行）

### 1. 数据分级与入库红线
- 永不入库：`.env` 与密钥（`*.key`/`*.pem`）、`海外项目负责招聘简历/`（真实用户数据目录）、`eval/labels/`（人工标注原文）、`eval/results/`（逐人运行结果）、`data/`（SQLite 运行库）、`output/`、`*.xlsx`、`*.log`。
- `.gitignore` 改动后逐条 `git check-ignore -v <路径>` 验证被忽略；通配规则不得误伤 `docs/` 报告与 `eval/published/` 聚合结论。
- 拟入库文本文件先脚本扫 PII：手机号 `1[3-9]\d{9}`、邮箱正则、身份证 `\d{17}[\dXx]`；姓名黑名单从真实简历文件名提取（本项目曾提取约 190 个）；再人工抽查，**0 命中才入库**。
- 可入库：`eval/published/`、`docs/` 下的**脱敏聚合**结论（只有聚合数字，无逐人原文）。

### 2. 字段级加密（documents.pii_*）
- 敏感标量只有三列：`documents.pii_name/pii_phone/pii_email`；密钥 `PII_ENCRYPT_KEY`（Fernet，base64 urlsafe 32 字节）经 env 装配，**不入库**。生成：`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`。
- Repository 层透明加解密：写前 `encrypt_value`、读后 `decrypt_value`（`src/pii.py` + `src/repository.py`）。
- **无 key 却读到 `gAAAA` 密文必须显式 `RuntimeError`**（`_decode_pii_field`），绝不静默返密文/丢行/返乱码——真实 bug 演进后的硬约束。
- 未配 key = 明文模式，import 时显式告警一次（`config.py` + `pii.py`），不静默。
- 表列变更走 `PRAGMA table_info` 探测 + `ALTER TABLE ADD COLUMN` 幂等迁移（`_ensure_pii_columns`/`_ensure_org_columns`），老行 DEFAULT 回填。
- 刻意不加密：`pages.text`（复核证据链）、`assessments.payload`、`storage_path`/文件名（dashboard 聚合与文件下载依赖明文）。

### 3. 上传接入校验（POST /api/upload）
- 顺序：扩展名白名单 → 空文件 → 大小上限 20MB（超限 413）→ 魔数，任一失败不落盘。
- 魔数：PDF `%PDF-`、JPEG `FFD8FF`、PNG 8 字节签名、BMP `BM`、DOCX `PK\x03\x04` + `zipfile.is_zipfile` + 含 `word/document.xml`。
- 内容级 PII 扫描**仅提示不阻断**：返回 `pii_warnings`，提示文本本身先打码（`138****8000`）。
- 已知局限：字节级 latin-1 粗扫对压缩 PDF/DOCX 可能漏扫，文档中如实注明。

### 4. 导出脱敏（main.py export）
- 默认导出**不含任何 PII 字段**；`--redact` 显式开启才附带打码列：手机 `138****1234`、姓名 `张*`、邮箱 `t***@example.com`。
- 加密库无 key 导出：友好报错、退出码 1，**在写文件前退出，不写半截 JSON/MD/XLSX**。
- JSON / Markdown / Excel 三格式 redact 开/关行为一致（两套表头）。

### 5. 保留期清理（purge）
- `documents.created_at`（UTC ISO 字符串，字典序即时序）是保留期依据。
- `purge --days N --dry-run` 先预览匹配清单；实删走级联：业务行（reviews → external_actions → bookings → runs → assessments → pages → documents）+ LangGraph 检查点线程（`delete_checkpoint_threads`）。
- 保留天数 env 可配：`PII_RETENTION_DAYS` 默认 90；0/空 = 不启用（purge 仍可显式 `--days`）。

### 6. 最小认证（开关式，默认关闭）
- `AUTH_ENABLED=0/1`；关闭态不破坏本地免登录与既有测试，`/api/auth/*` 三端点仍在但空操作。
- 开启态：`pbkdf2_hmac(SHA-256, 20 万轮)` 哈希 + `hmac.compare_digest` 恒定时间比对（密码不落库）；itsdangerous 签名 cookie `rs_session`（HttpOnly/SameSite=lax/max-age 12h）。
- 三端点 login/logout/me；**me 未认证也 200，仅 `authenticated:false`**。
- 13 个业务路由统一 `Depends(get_current_user)`；`/api/health` 与 `/api/auth/*` 公开；未认证 401。
- 开启态缺凭据 → `ensure_auth_configured()` 导入即 fail-fast 抛 RuntimeError。
- 前端统一 401 拦截跳 `#/login`，豁免 `/api/auth/login` 与 `/api/auth/me`。

### 7. 多租户预留（org_id，只留缝不实现）
- `documents/assessments/runs/batches` 四表已加 `org_id` 列（默认 `'local'`），`src/schemas.py` 模型字段先就位。
- P2a **不实现隔离逻辑**：查询不加 `WHERE org_id=?`；数据访问保持 Repository 单点边界，为 Postgres 迁移与 org 过滤留缝。

## 三条取舍

- **默认安全、显式放行**：加密/认证/redact/purge 全部默认关闭或最小权限，开启靠 env，不破坏本地体验。
- **失败要响**：无 key 读密文、开启态缺凭据、导出密文——全部显式报错或非零退出，不静默降级。
- **证据链优先**：复核原文、dashboard 聚合、文件下载依赖明文，故只加密"联系标量"，不加密证据文本。

## 参考文件（需要时再读，勿一次性全加载）

- `references/pii-controls.md` — 加密/脱敏/保留期控制清单：Fernet 装配、加密列取舍、正则与打码表、幂等迁移模板、purge 级联顺序、入库前扫描思路、两模式测试矩阵、无 key 读密文 bug 复盘。
- `references/auth-baseline.md` — 开关式最小认证基线：env 键清单与模板、惰性读取、pbkdf2/compare_digest、cookie 属性、13 路由挂载清单、三态测试组织、前端 401 拦截、org_id 预留与 P2 接续点、生产待治理项。

## 互链

上游（开发期产物）：`trustworthy-agent-patterns`、`agent-eval-harness`、`test-hardened-dev`；收束 → `project-closeout`（LICENSE/CI/Docker/门面在彼处）。
