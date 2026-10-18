# 测试模式映射表（本项目实读法）

> 用法：要写新用例时，先在本表找到对应模式，照抄该文件的 fixture/断言骨架，不要新造风格。
> 所有路径相对项目根 `F:\ResumeScreening`；基线 175 项，见 `tests/`。

## 目录

- [1. 隔离 fixture：临时 SQLite + 逐模块 patch + 重置单例](#1-隔离-fixture)
- [2. 模型假客户端：ScriptedModelClient](#2-模型假客户端)
- [3. 故障注入：FlakyGraph / FlakyRepo / fail_times](#3-故障注入)
- [4. API TestClient + cookie 会话](#4-api-testclient--cookie-会话)
- [5. SSE 流解析](#5-sse-流解析)
- [6. 上传 multipart 伪造与魔数](#6-上传-multipart-伪造与魔数)
- [7. DOCX / PDF 现场合成](#7-docx--pdf-现场合成)
- [8. Dashboard 漏斗单调性断言](#8-dashboard-漏斗单调性断言)
- [9. 加密 monkeypatch key（两态）](#9-加密-monkeypatch-key两态)
- [10. AUTH 开关 monkeypatch（两态 + fail-fast）](#10-auth-开关-monkeypatch两态--fail-fast)

---

## 1. 隔离 fixture

- **文件**：`tests/conftest.py` → `_isolated_repository(monkeypatch)`（autouse）。
- **关键片段**：
  - `tmp_dir = Path(tempfile.mkdtemp(prefix="resume_test_"))`；`tmp_db` / `ckpt_db` 分库。
  - 逐模块 patch：`for mod in (config, repo_mod, wf, task_store_mod): monkeypatch.setattr(mod, "DB_PATH", str(tmp_db))`；`for mod in (config, wf): monkeypatch.setattr(mod, "CHECKPOINT_DB_PATH", str(ckpt_db))`。
  - 重置单例：`repo_mod._default_repo = None`；`task_store_mod._default_store = None`；`wf._checkpointer_cache = None`。
- **要点**：`from .config import DB_PATH` 在导入时复制常量，**必须每个模块各 patch 一次**，只 patch config 无效。

## 2. 模型假客户端

- **文件**：`tests/test_t3_model_extractor.py`；注入抗性见 `tests/test_t11_prompt_injection.py`。
- **用例**：`test_transient_error_retry_then_success`、`test_format_correction_once_then_success`、`test_permanent_error_no_retry`、`test_cloud_client_raises_when_not_configured`。
- **关键片段**：
  - `ScriptedModelClient([ModelTransientError("rate limited"), resp_json(facts)])` —— 脚本化队列，按序抛错/返回。
  - `ModelExtractor(make_config(max_retries=2), client=client, sleep_fn=lambda s: None)` —— 不真睡。
  - 断言：`report.stats["calls"] == 2`（重试次数精确）、`client.prompts[1]` 含纠正提示、`ModelPermanentError` 不重试 `calls == 1`。

## 3. 故障注入

- **文件**：`tests/test_t1_recovery_boundary.py`。
- **用例**：
  - `test_resume_crash_mid_invoke_then_replay` —— `FlakyGraph.invoke` 第一次抛 `RuntimeError("模拟进程在恢复中途退出")`，断言 `repo.get_unconsumed_review(assessment_id) is not None`，第二次成功后 `calls["n"] == 2`。
  - `test_resume_crash_after_invoke_replay_without_reexecute` —— `FlakyRepo.mark_review_consumed` 第一次抛错，断言重放保护下 `calls["invoke"] == 1`。
  - `test_approval_and_run_state_ledger` —— 状态账本：恢复后 `run["status"]=="finished"`、`finished_at is not None`、审批 `is None`；再调一次幂等。
- **要点**：`monkeypatch.setattr(wf, "build_resume_graph", flaky_build)` / `monkeypatch.setattr(wf, "get_repository", lambda: FlakyRepo())`。

## 4. API TestClient + cookie 会话

- **文件**：`tests/test_t5_api.py`（业务路由）、`tests/test_t15_auth.py`（会话）。
- **业务断言片段**：
  - `client.post("/api/batches", json={...})` → 轮询 `/api/batches/tasks/{task_id}` 直到 `status=="done"`（300×0.1s）。
  - 状态码：`test_create_batch_bad_extractor` → 400；`test_get_batch_not_found` → 404；`test_approve_bad_decision` → 400；`test_scheduling_flow` 重复排期 → 409。
- **cookie 会话（t15）**：
  - `client` fixture 每用例新建 + 结束 `c.cookies.clear()`。
  - `enable_auth` fixture：`monkeypatch.setattr(config, "AUTH_ENABLED", True)` + 固定 `AUTH_USERNAME/PASSWORD/AUTH_SECRET_KEY`。
  - 登录后断言 `rs_session=` 与 `HttpOnly` 进 `client.cookies`；`test_tampered_cookie_rejected` 伪造 cookie → 401。

## 5. SSE 流解析

- **文件**：`tests/test_t10_task_store.py`。
- **用例**：`test_sse_stream_lifecycle`、`test_sse_task_not_found_emits_error`。
- **关键片段**：
  - `with client.stream("GET", f"/api/batches/tasks/{tid}/events") as resp:` → 断言 `resp.headers["content-type"].startswith("text/event-stream")`。
  - `_parse_sse_lines(lines)`：按空行分帧，`event:`/`data:` 前缀拆分，`:` 开头心跳行忽略，`data` 拼起来 `json.loads`。
  - 断言：`types[0] == "connected"`、`"progress" in types`、`types[-1] == "done"`、`done_data["done"] == done_data["total"]`；不存在 task 首事件即 `"error"`。

## 6. 上传 multipart 伪造与魔数

- **文件**：`tests/test_t13_upload_and_dashboard.py` → `TestUploadHardening`；补充 `tests/test_t5_api.py::test_upload_rejects_bad_ext`。
- **用例与断言**：
  - `test_forged_extension_rejected`：PNG 字节改名 `.pdf` → 400，且 `not (tmp_path/"evil.pdf").exists()`（失败不落盘）。
  - `test_oversize_rejected`：`monkeypatch.setattr(api_module, "MAX_UPLOAD_BYTES", 16)` → 合法 PDF 也 413，`detail` 含"上限"，不落盘。
  - `test_docx_without_document_xml_rejected`：裸 zip（`zipfile.writestr("not/a/real.docx", ...)`）→ 400。
  - multipart 调用：`client.post("/api/upload", files={"file": ("x.pdf", pdf_bytes, "application/pdf")})`。

## 7. DOCX / PDF 现场合成

- **PDF**：`_minimal_pdf(text)`（带 xref 的最小合法单页，pdfplumber 可抽文本）—— 重复出现在 `test_t5_api.py` / `test_t9_document_file_inline.py` / `test_t10_task_store.py` / `test_t13_upload_and_dashboard.py` / `test_t14_pii.py`，照抄即可，不要内联真简历。
- **DOCX**：`tests/test_t12_docx_parser.py::_write_docx(path, paragraphs, tables)` 与 `tests/test_t13_upload_and_dashboard.py::_make_docx_bytes(paragraphs)`（`io.BytesIO` + `python-docx` 内存合成，供上传/解析测试）。
- **坏数据样例**：`bad_resume.pdf`（刻意 33B 坏文件）用于漏斗回退冒烟；空正文用 `b"%PDF-1.4\n%%EOF"`。

## 8. Dashboard 漏斗单调性断言

- **文件**：`tests/test_t13_upload_and_dashboard.py` → `TestDashboard`。
- **用例与断言**：
  - `test_empty_returns_zero_structure`：`set(body.keys()) == {"kpis","score_distribution","funnel"}`；六桶 `["0-20","20-40","40-60","60-75","75-90","90-100"]` 计数全 0；漏斗六段顺序 `["uploaded","parsed","extracted","hard_passed","scored","approved"]`；`avg_score is None`。
  - `test_aggregates_match_mock_data`：经 `_insert_mock_data()` 直接插 3 份全链路数据，断言 `counts == [3,3,3,2,2,1]` 且 `counts[i-1] >= counts[i]`（单调不减）。
  - `test_extracted_dedup_across_reruns`：同 3 文档各再落一条 run（`runs` 翻倍）+ 1 份有 run 无 pages/assessment 的失败文档，断言 `counts == [4,3,3,2,2,1]`、`extracted <= parsed <= uploaded`。

## 9. 加密 monkeypatch key（两态）

- **文件**：`tests/test_t14_pii.py` → `TestEncryption` / `TestNoKeyReadsCiphertext`。
- **两态要点**：
  - 开启态：`monkeypatch.setattr(config, "PII_ENCRYPT_KEY", _KEY_A)`（`Fernet.generate_key()`）；直查 `repo._conn.execute("SELECT pii_phone ...")` 断言 `raw.startswith("gAAAAA")` 且不含明文，`repo.get_document(...)` 透明回明文。
  - 错误 key：换 `_KEY_B` 再读 → `pytest.raises(ValueError, match="解密失败")`。
  - 关闭态：`setattr(config, "PII_ENCRYPT_KEY", "")` → 写明文 + `caplog` 捕获 "PII 加密未启用"。
  - **无 key 读密文**：先 `_KEY_A` 写密文（断言 `raw.startswith("gAAAA")`），再清空 key 读 → `pytest.raises(RuntimeError, match="PII_ENCRYPT_KEY")`；`cmd_export` 无 key 跑 `== 1` 且 `list(tmp_path.iterdir()) == []`（不写半截）。

## 10. AUTH 开关 monkeypatch（两态 + fail-fast）

- **文件**：`tests/test_t15_auth.py`。
- **关闭态（`TestDisabled`）**：`/api/dashboard`、`/api/resumes`、`/api/traces` 全部 200；`/api/auth/login` 任意错误凭据也 200 且不发 `rs_session` cookie。
- **开启态（`TestEnabled`）**：未带 cookie 访问业务路由 → 401 `{"detail":"未认证"}`；错用户名/错密码各 401；正确登录 `set-cookie` 含 `rs_session=` + `HttpOnly`；登出后再访问 → 401。
- **fail-fast（`TestFailFast`）**：`AUTH_ENABLED=True` 但 `AUTH_USERNAME/PASSWORD` 空 → `pytest.raises(RuntimeError, match="AUTH_ENABLED=1")`；关闭态不校验。
- **要点**：开关在请求时从 `src.config` 常量惰性读取，故只 `monkeypatch` config 常量即可在同一 `app` 实例上切换，无需重建 app。
