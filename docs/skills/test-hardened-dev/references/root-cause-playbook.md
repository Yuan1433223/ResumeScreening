# 根因修复复盘（六个真实 bug）

> 每个案例按六段写：现象 → 错误修法诱惑 → 真实根因 → 修法 → 回归测试 → 可迁移教训。
> 读它不是为了背答案，是为了在下次想"在上层打个补丁先压住"时停下来。

## 目录

- [① PDF 在复核页 `<iframe>` 里空白](#--pdf-在复核页-iframe-里空白)
- [② 复核详情左右两侧抽取结果对不上](#-复核详情左右两侧抽取结果对不上)
- [③ Dashboard 漏斗 extracted 翻倍回跳](#-dashboard-漏斗-extracted-翻倍回跳)
- [④ 无 key 进程静默返密文 / 丢文档](#-无-key-进程静默返密文--丢文档)
- [⑤ OCR 单份简历耗时 250s](#-ocr-单份简历耗时-250s)
- [⑥ 复核评估明细卡片永远空](#-复核评估明细卡片永远空)

---

## ① PDF 在复核页 `<iframe>` 里空白

- **现象**：复核页简历面板里图片正常显示，唯独 PDF 一片空白；浏览器手动下载 URL 却能拿到文件。
- **错误修法诱惑**：在前端把 PDF 换成 `<object>`/新窗口打开、加 `"application/pdf"` 提示、怀疑 pdfplumber 抽坏了文件——都是在渲染层/解析层打转。
- **真实根因**：`GET /api/documents/{id}/file` 用 `FileResponse(..., filename=...)`，Starlette 默认据此产出 `Content-Disposition: attachment`。PDF 在复核页是 `<iframe>` 的**导航类请求**，浏览器收到 attachment 会直接触发下载而不在 frame 内渲染；图片走 `<img>` 子资源请求不受该头影响，所以表现为"图片正常、PDF 空白"。
- **修法**：对 pdf/jpg/png/bmp 等可内联类型，端点显式返回 `Content-Disposition: inline; filename=...`。
- **回归测试**：`tests/test_t9_document_file_inline.py::test_pdf_file_endpoint_is_inline_and_parseable` —— 断言 `content-type == "application/pdf"`、`content-disposition.lower().startswith("inline;")`、响应体 `startswith(b"%PDF-")`；`test_missing_file_returns_404` 钉住不存在 id 返回 404 而非 500。
- **可迁移教训**：导航类请求（iframe/顶层窗口）与子资源请求（img/script）对 `Content-Disposition` 的反应不同；"换个标签就好了"往往是错层，先抓响应头。

## ② 复核详情左右两侧抽取结果对不上

- **现象**：演示 model 批次时，复核页右侧评估明细是 model 抽取的，左侧事实区却还是 mock 规则抽取的两套结果，对不上。
- **错误修法诱惑**：在前端把左侧也按"最近选中的抽取器"重抽、给左右各加个版本标签糊弄过去、或干脆把端点写死成 `"model"`。
- **真实根因**：`GET /api/documents/{id}/review` **硬编码** `extract_facts(..., extractor_version="mock")`，而右侧明细来自 `repo.get_assessment_by_document()` 取真实落库版本。两侧版本源不一致：该文档 assessment 是 model 批次跑的，左侧却永远用 mock。
- **修法**：以 `assessment.extractor_version` 为准（无 assessment 回退 mock）；版本归一——assessment 存的是抽取器自报版本（`mock-v1` / `model-p1`），端点归一到 `extract_facts` 的分派键（`mock` / `startswith("model")`，未知版本安全回退 mock）；并加进程内展示缓存 `_REVIEW_EXTRACT_CACHE`（缓存键含 assessment id + created_at 等元组），避免每次打开复核页都重调云端模型。
- **回归测试**：`tests/test_t8_review_extractor_consistency.py` —— `test_mock_batch_review_still_uses_mock` 不回归；`test_model_assessment_review_passes_model_extractor` 用 `spy_extract` 断言第一次 GET 实际收到 `"model"`、第二次缓存命中不再调用；`test_extractor_version_normalization` 参数化 `("mock-v1"→"mock")`、`("model-p1"→"model-p1")`、`("unknown-future"→"mock")`。
- **可迁移教训**：同一资源的两个视图必须共用**同一个真相源**；硬编码的枚举键是定时炸弹，版本字符串要归一后再分派。

## ③ Dashboard 漏斗 extracted 翻倍回跳

- **现象**：第一批跑 3 份文档，漏斗 uploaded=3 → extracted=3 正常；同一批文档再跑第二批，`extracted` 跳到 6，漏斗不再单调。
- **错误修法诱惑**：在 SQL 里给 `extracted` 加个 `min(上游)` 夹逼、前端展示时截断、或禁止重复跑同一文档——都是在掩盖口径问题。
- **真实根因**：`extracted` 口径前后不一致。第一版用 `count(runs)`，而 **run 在流程一开始就建行**（`create_run` 即落库），解析失败也有行；同文档跑两批 → runs 翻倍 → extracted 翻倍。正确口径应是"完成了抽取/评分"的文档数，即按 `document_id` DISTINCT 且必须有关联 assessment。坏数据样例 `bad_resume.pdf`（刻意 33B）就是那个"有 run 无 pages 无 assessment"的钉子户。
- **修法（两轮演进）**：先 `runs DISTINCT document_id`，再演进到 `assessments DISTINCT document_id`——因为 run 生命周期起点太早，只有 assessment 完成才代表真抽完。
- **回归测试**：`tests/test_t13_upload_and_dashboard.py::TestDashboard::test_extracted_dedup_across_reruns` —— 同 3 文档各再落一条 run（runs 翻倍）+ 1 份有 run 无 pages/assessment 的失败文档，断言 `counts == [4,3,3,2,2,1]` 且 `extracted <= parsed <= uploaded`、全程单调不减。
- **可迁移教训**：漏斗每一段的"口径生命周期起点"必须对齐——统计的是"开始了"还是"完成了"？run/assessment/document 三张表各有各的生命周期，别拿开始态的行去数完成态的量。

## ④ 无 key 进程静默返密文 / 丢文档

- **现象**：配置了 `PII_ENCRYPT_KEY` 的库，换一台没配 key 的进程启动后，`get_document` 不报错——要么把 `gAAAA...` 密文当明文返给前端，要么干脆返回 `None` 像文档丢了一样。
- **错误修法诱惑**：读层发现解不开就 `try/except` 吞掉返空、导出时跳过坏行继续写、给密文列加个"看起来像密文就当空串"的兜底。
- **真实根因**：读层没有"这份数据是密文但我没有 key"的检测分支。加密列值以 Fernet token 前缀 `gAAAA` 开头；无 key 进程既解不开，也没被告知"这是密文"，于是要么原样透传密文、要么按"列不存在/空"静默丢文档。
- **修法**：读层检测到 `gAAAA` 前缀但 `PII_ENCRYPT_KEY` 为空时**显式抛 `RuntimeError("PII_ENCRYPT_KEY ...")`**；导出命令在写文件前先校验，无 key 直接以非 0 退出，**不写任何半截文件**。
- **回归测试**：`tests/test_t14_pii.py::TestNoKeyReadsCiphertext` —— `test_ciphertext_without_key_raises`（先 key A 写密文、清空 key 读 → `pytest.raises(RuntimeError, match="PII_ENCRYPT_KEY")`）；`test_plaintext_library_without_key_ok`（明文库无 key 正常读写，不误伤）；`test_export_redact_no_key_friendly_error`（无 key 跑 export → 返回码 1 且 `list(tmp_path.iterdir()) == []`）。
- **可迁移教训**：**静默失败比崩更危险**——"解不开就当空"会把数据损坏伪装成正常业务空态；读层要对"我不认识的数据形态"显式喊停，导出/写文件类操作要么写完要么不写。

## ⑤ OCR 单份简历耗时 250s

- **现象**：同一文档第二次跑批次时，仍要重新走一遍 OCR/解析，单份耗时约 250s。
- **错误修法诱惑**：把 OCR 线程池调大、给解析加超时和"看起来慢就跳过"、前端加个进度条——都不解决重复劳动。
- **真实根因**：解析结果没有持久化缓存。`parse_document`（含 OCR）每次都被重新调用；pages 一旦算出，本应落库复用。
- **修法**：pages 表缓存——首跑 `save_pages` 落库，重跑时 `get_pages(document_id)` 命中即跳过 `parse_document`，OCR 不再触发。
- **回归测试**：`tests/test_t5_api.py::test_rerun_uses_page_cache` —— 第一跑后断言 `_repo().get_pages(doc_id)` 非空；第二跑前 `monkeypatch.setattr(wf, "parse_document", boom)`（必炸），若第二跑仍 `done` 且 document_id 相同，则证明走了缓存而非真跑解析。
- **可迁移教训**：昂贵的纯计算（OCR/模型调用）必须有"输入指纹→结果"的持久层缓存；测缓存不要靠计时，靠"把计算函数换成必炸实现，看结果是否仍正确"。

## ⑥ 复核评估明细卡片永远空

- **现象**：复核页"评估明细"卡片空白，但批次列表里推荐结论是有的，说明 assessment 明明落库了。
- **错误修法诱惑**：怀疑前端没渲染、怀疑 `save_assessment` 没存进去、在端点重新算一遍维度——都在猜。
- **真实根因**：`hard_results` / `dimensions` / `missing_items` 三字段**不是 assessments 表的列**，而是序列化进 `payload` JSON 列。端点用 `dict(row).get("hard_results")` 当表列读，列不存在 → 永远空 `None`。
- **修法**：端点从 `row["payload"]`（JSON 反序列化）里取出这三键再透出，并做形状守卫：`hard_results` 是带业务键的 dict、`missing_items` 是 list（即使空）、`dimensions` 是 dict。
- **回归测试**：`tests/test_t8_review_extractor_consistency.py::test_review_detail_exposes_payload_score_fields` —— 断言 `a["hard_results"]` 为 dict 且含 `relevant_domain/experience/management/summary` 键、`a["missing_items"]` 为 list、`a["dimensions"]` 为 dict。
- **可迁移教训**：行对象上"看似有其实没有"的键（`dict(row).get` 静默返 `None`）是隐形 bug 温床；JSON 列 vs 表列的边界必须显式解析并断言形状，别用 `.get` 猜。

---

## 跨案例共性

1. **现象都在展示层，根因都在数据/契约层**：iframe、左右对不上、漏斗翻倍、卡片空——没有一个是"改前端"能根治的。
2. **每个修复都配了一个"反方向"的回归**：不是测"修好后正确"，而是测"曾经出错的那条路径如今必然失败/必然命中缓存"。
3. **错误修法的共同特征**：吞异常、硬编码、在视图层凑数、用 `.get` 猜字段——这四个动作下次再想做时，先翻本文件。
