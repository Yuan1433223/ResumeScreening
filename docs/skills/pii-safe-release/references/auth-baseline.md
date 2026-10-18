# 开关式最小认证基线（单用户本地工作台）

落点全部经源码核实：`src/auth.py`、`src/config.py`、`app/api.py`、`frontend/src/api.ts`、`tests/test_t15_auth.py`、`src/repository.py`、`src/schemas.py`。依赖 `itsdangerous==2.2.0`。

## 目录

1. [env 键清单与 .env.example 模板](#1-env-键清单与-envexample-模板)
2. [惰性读取原则](#2-惰性读取原则)
3. [pbkdf2 参数与 compare_digest](#3-pbkdf2-参数与-compare_digest)
4. [itsdangerous 装配与 Set-Cookie 属性](#4-itsdangerous-装配与-set-cookie-属性)
5. [本项目 13 业务路由挂载清单](#5-本项目-13-业务路由挂载清单)
6. [三态测试组织（22 用例）](#6-三态测试组织22-用例)
7. [前端 401 拦截与豁免名单](#7-前端-401-拦截与豁免名单)
8. [org_id 预留形状与 P2 多租户 WHERE 边界接续点](#8-org_id-预留形状与-p2-多租户-where-边界接续点)
9. [生产待治理项](#9-生产待治理项)

---

## 1. env 键清单与 .env.example 模板

| 键 | 用途 | 默认值 | 出处 | 必填条件 |
| --- | --- | --- | --- | --- |
| `AUTH_ENABLED` | 总开关（`1/true/yes` 开，其余关） | `0` | `src/config.py` L224 | 否 |
| `AUTH_USERNAME` | 登录用户名（明文存 env，运行时哈希比对） | 空 | `src/config.py` L225 | 开启态必填 |
| `AUTH_PASSWORD` | 登录密码（不落库，pbkdf2 比对） | 空 | `src/config.py` L226 | 开启态必填 |
| `AUTH_SECRET_KEY` | 会话 Cookie 签名密钥 | 空（由密码派生并告警） | `src/config.py` L227 | 生产必填 |

`.env.example` 现有 AUTH 段（L32-40）模板：

```bash
# 单用户认证（默认关闭）。开启后所有业务路由需登录 Cookie 才能访问。
# AUTH_ENABLED=1 时必须同时配置 AUTH_USERNAME / AUTH_PASSWORD，否则启动即报错。
# AUTH_SECRET_KEY 不填则由 AUTH_PASSWORD 确定性派生（生产应显式配置）。
AUTH_ENABLED=0
# AUTH_USERNAME=hr
# AUTH_PASSWORD=change-me
# AUTH_SECRET_KEY=please-generate-a-long-random-string
```

注意：`PII_ENCRYPT_KEY` / `PII_RETENTION_DAYS` 目前只在 `src/config.py` 注释里有装配说明（含 key 生成命令），`.env.example` 尚未收录；补模板时同步加段即可。

## 2. 惰性读取原则

- `src/auth.py` 所有开关/凭据/密钥**在请求时从 `src.config` 模块常量读**，不在本模块 import 时固化：
  - `auth_enabled()` → 读 `config.AUTH_ENABLED`
  - `_hash_password` 比对 → 用 `config.AUTH_PASSWORD`
  - `_serializer()` → 读 `config.AUTH_SECRET_KEY`，缺省派生自 `config.AUTH_PASSWORD`
- 好处：测试 `monkeypatch.setattr(config, "AUTH_ENABLED", True)` 后下一个请求即生效，无需重建 app。
- 反模式：在 auth 模块顶层 `_ENABLED = config.AUTH_ENABLED` 固化——会让 monkeypatch 失效、测试状态串味。

## 3. pbkdf2 参数与 compare_digest

落点 `src/auth.py`：

- `hashlib.pbkdf2_hmac("sha256", password, salt, iterations)`：
  - `_APP_PBKDF2_SALT = b"resumescreening/v1"`（固定 app salt，单用户演示用）
  - `_PBKDF2_ITERATIONS = 200_000`
- `verify_password(input, expected)`：对输入与期望各哈希一次，`hmac.compare_digest` 恒定时间比对；任一为空直接 False。
- **密码不落库**：env 里的明文密码只在内存比对，数据库不存任何密码哈希；这是单用户本地部署的有意取舍（生产见 §9）。
- 登录端点同时用 `req.username == config.AUTH_USERNAME` 短路用户名、`verify_password` 校验密码。

## 4. itsdangerous 装配与 Set-Cookie 属性

落点 `src/auth.py` + `app/api.py` login 端点：

- 签名器：`URLSafeTimedSerializer(secret, salt="rs-session")`；`secret` 优先 `AUTH_SECRET_KEY`，缺省 `sha256(AUTH_PASSWORD + "::session-secret")` 确定性派生（并 warning 一次 `_secret_warned`）。
- session payload：`{"u": username}`；`create_session()` dumps，`read_session(token)` loads（`max_age=SESSION_MAX_AGE`），`BadSignature`/`SignatureExpired` → None。
- Cookie：
  - 名 `rs_session`（`COOKIE_NAME`）
  - `max_age = 12 * 3600`（12h，`SESSION_MAX_AGE`）
  - `httponly=True`、`samesite="lax"`、`path="/"`
  - logout 幂等 `delete_cookie(path="/")`
- **不引 SessionMiddleware、不落库会话**：无状态签名 cookie，换实例不丢登录（生产续期见 §9）。

## 5. 本项目 13 业务路由挂载清单

落点 `app/api.py`；全部以 `dependencies=[Depends(get_current_user)]` 挂载。公开路由仅 4 个：`GET /api/health`、`POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/me`。

| # | 方法 | 路径 | 行号 |
| --- | --- | --- | --- |
| 1 | POST | `/api/batches` | 268 |
| 2 | GET | `/api/batches/tasks/{task_id}` | 297 |
| 3 | GET | `/api/batches/tasks/{task_id}/events` | 305 |
| 4 | GET | `/api/traces` | 361 |
| 5 | GET | `/api/batches/{batch_id}` | 392 |
| 6 | GET | `/api/documents/{document_id}/file` | 457 |
| 7 | GET | `/api/documents/{document_id}/review` | 485 |
| 8 | GET | `/api/resumes` | 581 |
| 9 | POST | `/api/upload` | 593 |
| 10 | GET | `/api/dashboard` | 656 |
| 11 | GET | `/api/scheduling` | 745 |
| 12 | POST | `/api/scheduling/run` | 753 |
| 13 | POST | `/api/documents/{document_id}/approve` | 777 |

`get_current_user` 语义：
- 关闭态：直接返回 `"local"`，业务路由不拦截。
- 开启态：读 `rs_session` cookie 验签，失败/缺失/过期一律 401 `{"detail":"未认证"}`。
- `me` 例外：开启态未认证也返回 200 `{authenticated:false, username:null, auth_enabled:true}`，前端据此决定跳登录页。
- fail-fast：`app/api.py` import 时（L53）调 `ensure_auth_configured()`——开启态但缺 `AUTH_USERNAME`/`AUTH_PASSWORD` 立即抛 RuntimeError，绝不带弱配置起服务。

## 6. 三态测试组织（22 用例）

落点 `tests/test_t15_auth.py`，5 个测试类：

| 测试类 | 用例数 | 覆盖 |
| --- | --- | --- |
| TestDisabled | 5 | health 公开、me 关闭态、login 关闭态不校验凭据、logout 关闭态、业务路由不拦截 |
| TestEnabled | 9 | 业务路由 401、me 未认证仍 200、错用户名/错密码 401、登录成功 Set-Cookie、带 cookie 访问业务、登录后 me、logout 后再访问被拦、篡改 cookie 被拒 |
| TestFailFast | 3 | 开启无凭据抛错、开启有凭据正常、关闭态永不抛错 |
| TestCrypto | 2 | verify_password roundtrip、session dumps/loads roundtrip |
| TestOrgIdReservation | 3 | 新库四表有 org_id 列、默认 `'local'`、老库幂等迁移补列 |

组织原则：**关闭态/开启态/fail-fast 三态物理分组**，新加路由时往 TestEnabled 补一条"未带 cookie 401、带 cookie 200"。

## 7. 前端 401 拦截与豁免名单

落点 `frontend/src/api.ts`：

```ts
const NO_REDIRECT_401 = new Set(['/api/auth/login', '/api/auth/me'])
// request() 内：if (res.status === 401 && !NO_REDIRECT_401.has(path)) → location.hash = '#/login'
```

- 业务路由 401 → 统一跳 `#/login`。
- 豁免 `/api/auth/login`：登录失败 401 是正常业务分支，登录页要展示后端 detail（"用户名或密码错误"），不能跳走。
- 豁免 `/api/auth/me`：契约规定未认证也 200，不会是 401，这里双保险防误跳。
- 路由级兜底：`App.tsx` 启动时也查 `getMe()`，未认证直接渲染 `<LoginPage/>`。

## 8. org_id 预留形状与 P2 多租户 WHERE 边界接续点

- 列：`documents/assessments/runs/batches` 四表 `org_id TEXT NOT NULL DEFAULT 'local'`（`_ORG_TABLES`，`_ensure_org_columns` 幂等迁移补老库）。
- 模型：`src/schemas.py` 三处 `org_id: str = "local"`（Document/Assessment/Run 等）。
- **P2a 不实现隔离**：所有 Repository 查询不加 `WHERE org_id=?`，写入也不取会话 org——只留列与字段形状。
- P2 接续点（真做多租户时改这里）：
  1. `get_current_user` 返回对象扩展 `org_id`（当前只返 username 字符串）。
  2. 所有 Repository 公开查询方法加 `WHERE org_id=?` 参数，写入时取会话 org。
  3. Postgres 迁移时把 SQLite 点边界整体换成连接池，org_id 进索引。
- 数据访问单点：所有 SQL 只走 `Repository` 类，业务代码不裸写 SQL（main.py 里零星直查 rounds 是导出只读，迁移时一并收编）。

## 9. 生产待治理项

当前基线是**单用户本地工作台**，以下项上线公网前必须治理：

1. **独立 `AUTH_SECRET_KEY`**：禁止用密码派生，用密钥管理（KMS/Secret Manager）注入，轮换策略。
2. **per-user 随机 salt 落库**：当前固定 app salt + 单用户；多用户需 `pbkdf2_hmac` 每用户随机 salt 存 user 表，哈希结果落库。
3. **会话续期 / 服务端会话**：当前签名 cookie 12h 到期即失效、无滑动续期、无服务端注销；公网部署换 Redis 服务端会话 + 主动登出黑名单。
4. **HTTPS / Secure cookie**：当前 cookie 未设 `secure=True`，本地 http 调试可用，公网必须上 https 并加 secure。
5. **PII key 轮换**：Fernet 无内建 key 版本号迁移，需自行写"旧 key 解密 → 新 key 重加密"脚本。
6. **上传 PII 粗扫增强**：当前 latin-1 字节粗扫对压缩 PDF/DOCX 漏扫；接入正式解析器后改对解析文本扫描。
7. **审计日志**：当前登录/导出/purge 无审计轨迹，合规要求时补结构化审计。
