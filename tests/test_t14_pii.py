# -*- coding: utf-8 -*-
"""T14 PII 处理四件套测试：字段级加密 / 上传提示 / 保留期清理 / 导出脱敏。

不碰前端/README/docs/真实简历目录；仅覆盖后端与数据层。
- 加密模式（monkeypatch 注入合法 Fernet key）：DB 原始值为密文、repository 读出明文；
  密钥错误解密抛错。
- 明文模式（无 key）：写入明文 + 警告路径。
- extract/mask 纯函数。
- 上传端点 pii_warnings（合成 PDF 文本含手机/邮箱）。
- purge --dry-run 与实删（构造过期/未过期文档）。
- export --redact 输出不含明文 PII。
"""
import argparse
import io
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import app.api as api_module
import main as main_cli
from app.api import app
from src import config
from src import pii as pii_mod
from src.repository import Repository

client = TestClient(app)

_KEY_A = Fernet.generate_key().decode()
_KEY_B = Fernet.generate_key().decode()


def _minimal_pdf(text: str) -> bytes:
    """带 xref 的最小合法单页 PDF（文本流未压缩，字节级可扫到手机/邮箱）。"""
    text = text.encode("latin-1")
    stream = b"BT /F1 12 Tf 72 720 Td (" + text + b") Tj ET\n"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return out


# ===========================================================================
# 纯函数：extract / mask
# ===========================================================================
class TestExtractMask:
    def test_extract_phone_email_name(self):
        out = pii_mod.extract_pii("张三\n13812345678\nzhangsan@example.com", "张三简历.pdf")
        assert out["phone"] == "13812345678"
        assert out["email"] == "zhangsan@example.com"
        assert out["name"] == "张三"

    def test_extract_empty(self):
        out = pii_mod.extract_pii("", "")
        assert out == {"name": "", "phone": "", "email": ""}

    def test_mask_phone(self):
        assert pii_mod.mask_pii("13812345678") == "138****5678"

    def test_mask_email(self):
        assert pii_mod.mask_pii("zhangsan@example.com") == "z***@example.com"

    def test_mask_name(self):
        assert pii_mod.mask_pii("欧阳非凡") == "欧***"
        assert pii_mod.mask_pii("张三") == "张*"

    def test_mask_empty(self):
        assert pii_mod.mask_pii("") == ""
        assert pii_mod.mask_pii(None) == ""


# ===========================================================================
# 字段级加密
# ===========================================================================
class TestEncryption:
    def test_encrypted_write_read_roundtrip(self, monkeypatch, _isolated_repository):
        repo = _isolated_repository
        monkeypatch.setattr(config, "PII_ENCRYPT_KEY", _KEY_A)
        repo.upsert_document("d_enc", sha256="sha_enc", storage_path="/tmp/d_enc.pdf",
                             mime="pdf", parse_status="parsed", parser_version="t")
        repo.set_document_pii("d_enc", name="张三", phone="13812345678", email="zs@x.com")

        # DB 原始值必须是密文（Fernet token 以 gAAAA 开头），不是明文
        raw = repo._conn.execute(
            "SELECT pii_name, pii_phone, pii_email FROM documents WHERE id='d_enc'"
        ).fetchone()
        assert raw[0].startswith("gAAAAA")
        assert raw[1].startswith("gAAAAA")
        assert raw[2].startswith("gAAAAA")
        assert "13812345678" not in raw[1]

        # repository 读出为明文（透明解密）
        doc = repo.get_document("d_enc")
        assert doc["pii_name"] == "张三"
        assert doc["pii_phone"] == "13812345678"
        assert doc["pii_email"] == "zs@x.com"

    def test_wrong_key_raises(self, monkeypatch, _isolated_repository):
        repo = _isolated_repository
        monkeypatch.setattr(config, "PII_ENCRYPT_KEY", _KEY_A)
        repo.upsert_document("d_wrong", sha256="sha_wrong", storage_path="/tmp/d_w.pdf",
                             mime="pdf")
        repo.set_document_pii("d_wrong", phone="13812345678")

        # 换一把不同的合法密钥再读 → 必须显式抛错，不静默返回乱码
        monkeypatch.setattr(config, "PII_ENCRYPT_KEY", _KEY_B)
        with pytest.raises(ValueError, match="解密失败"):
            repo.get_document("d_wrong")

    def test_plaintext_mode_writes_plaintext(self, monkeypatch, _isolated_repository, caplog):
        repo = _isolated_repository
        monkeypatch.setattr(config, "PII_ENCRYPT_KEY", "")
        pii_mod._plaintext_warned = False  # 确定性地捕获警告
        repo.upsert_document("d_plain", sha256="sha_plain", storage_path="/tmp/d_p.pdf",
                             mime="pdf")
        repo.set_document_pii("d_plain", phone="13812345678", email="zs@x.com")

        # DB 原始值即明文
        raw = repo._conn.execute(
            "SELECT pii_phone FROM documents WHERE id='d_plain'"
        ).fetchone()[0]
        assert raw == "13812345678"
        # 读出也是明文
        doc = repo.get_document("d_plain")
        assert doc["pii_phone"] == "13812345678"
        # 警告路径
        assert any("PII 加密未启用" in r.message for r in caplog.records)

    def test_migration_adds_columns_to_old_db(self, tmp_path):
        """老库（无 pii 列）经 PRAGMA 探测后 ALTER 补列，幂等。"""
        old_db = tmp_path / "old.db"
        # 先建一张没有 pii 列的老 documents 表
        import sqlite3
        c = sqlite3.connect(old_db)
        c.execute(
            "CREATE TABLE documents (id TEXT PRIMARY KEY, sha256 TEXT UNIQUE, "
            "storage_path TEXT NOT NULL, mime TEXT NOT NULL DEFAULT '', "
            "parse_status TEXT NOT NULL DEFAULT 'pending', "
            "parser_version TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)"
        )
        c.execute("INSERT INTO documents VALUES ('old1','sha_old','/tmp/o.pdf','pdf','p','v',?)",
                  (datetime.now(UTC).isoformat(),))
        c.commit()
        c.close()

        repo = Repository(old_db)  # 触发迁移
        cols = {r["name"] for r in repo._conn.execute("PRAGMA table_info(documents)")}
        assert {"pii_name", "pii_phone", "pii_email"} <= cols
        # 老行回填空串
        assert repo.get_document("old1")["pii_phone"] == ""
        repo.close()


# ===========================================================================
# 上传端点 pii_warnings
# ===========================================================================
@pytest.fixture(autouse=True)
def _fake_resume_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "RESUME_DIR", tmp_path)
    yield tmp_path


class TestUploadPiiWarnings:
    def test_upload_with_pii_returns_warnings(self, tmp_path):
        pdf = _minimal_pdf("Resume 13812345678 zs@example.com steel")
        r = client.post("/api/upload", files={"file": ("zs.pdf", pdf, "application/pdf")})
        assert r.status_code == 200, r.text
        body = r.json()
        # 既有字段不破坏
        assert body["file_name"] == "zs.pdf"
        assert "saved" in body
        # 新增可选字段：命中手机/邮箱
        warns = body["pii_warnings"]
        assert any("138****5678" in w for w in warns), warns
        assert any("example.com" in w for w in warns), warns

    def test_upload_without_pii_empty_warnings(self, tmp_path):
        pdf = _minimal_pdf("Alice steel structure 2019-2024")
        r = client.post("/api/upload", files={"file": ("alice.pdf", pdf, "application/pdf")})
        assert r.status_code == 200, r.text
        assert r.json()["pii_warnings"] == []


# ===========================================================================
# purge 保留期清理
# ===========================================================================
def _insert_doc(repo, doc_id, days_old, pii_phone=""):
    repo.upsert_document(doc_id, sha256=f"sha_{doc_id}", storage_path=f"/tmp/{doc_id}.pdf",
                         mime="pdf", parse_status="parsed", parser_version="t")
    past = (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
    repo._conn.execute("UPDATE documents SET created_at=? WHERE id=?", (past, doc_id))
    repo._conn.commit()
    if pii_phone:
        repo.set_document_pii(doc_id, phone=pii_phone)


class TestPurge:
    def test_dry_run_lists_old_only(self, _isolated_repository):
        repo = _isolated_repository
        _insert_doc(repo, "old_doc", days_old=200)
        _insert_doc(repo, "new_doc", days_old=10)

        ns = argparse.Namespace(days=90, dry_run=True, delete_files=False,
                                keep_checkpoints=True)
        rc = main_cli.cmd_purge(ns)
        assert rc == 0
        # dry-run 不删除
        assert repo.get_document("old_doc") is not None
        assert repo.get_document("new_doc") is not None

    def test_real_delete_removes_old_only(self, _isolated_repository):
        repo = _isolated_repository
        _insert_doc(repo, "old_doc", days_old=200)
        _insert_doc(repo, "new_doc", days_old=10)

        ns = argparse.Namespace(days=90, dry_run=False, delete_files=False,
                                keep_checkpoints=True)
        rc = main_cli.cmd_purge(ns)
        assert rc == 0
        assert repo.get_document("old_doc") is None
        assert repo.get_document("new_doc") is not None

    def test_disabled_when_days_zero(self, _isolated_repository):
        repo = _isolated_repository
        _insert_doc(repo, "old_doc", days_old=200)
        ns = argparse.Namespace(days=0, dry_run=False, delete_files=False,
                                keep_checkpoints=True)
        assert main_cli.cmd_purge(ns) == 1
        # 未删除
        assert repo.get_document("old_doc") is not None


# ===========================================================================
# export --redact
# ===========================================================================
class TestExportRedact:
    def _setup_batch(self, repo):
        repo.create_batch("b_redact", "v1")
        repo.upsert_document("d_exp", sha256="sha_exp", storage_path="/tmp/d_exp.pdf",
                             mime="pdf", parse_status="parsed", parser_version="t")
        repo.set_document_pii("d_exp", name="张三", phone="13812345678", email="zs@example.com")
        repo.create_run("run_exp", thread_id="t_exp", document_id="d_exp", batch_id="b_redact")

    def test_redact_output_masks_pii(self, _isolated_repository, tmp_path):
        repo = _isolated_repository
        self._setup_batch(repo)
        ns = argparse.Namespace(batch_id="b_redact", dir=str(tmp_path), redact=True)
        assert main_cli.cmd_export(ns) == 0

        json_file = next(tmp_path.glob("batch_b_redact_*.json"))
        text = json_file.read_text(encoding="utf-8")
        # 打码后：出现掩码，绝不出现明文手机/邮箱
        assert "138****5678" in text
        assert "13812345678" not in text
        assert "zs@example.com" not in text
        assert "z***@example.com" in text

        md_file = next(tmp_path.glob("batch_b_redact_*.md"))
        md = md_file.read_text(encoding="utf-8")
        assert "13812345678" not in md
        assert "138****5678" in md

    def test_no_redact_output_has_no_pii(self, _isolated_repository, tmp_path):
        repo = _isolated_repository
        self._setup_batch(repo)
        ns = argparse.Namespace(batch_id="b_redact", dir=str(tmp_path), redact=False)
        assert main_cli.cmd_export(ns) == 0

        json_file = next(tmp_path.glob("batch_b_redact_*.json"))
        text = json_file.read_text(encoding="utf-8")
        # 默认不带任何 PII 字段，明文手机/邮箱均不出现
        assert "13812345678" not in text
        assert "138****5678" not in text
        assert "pii_phone" not in text


# ===========================================================================
# 边界：无密钥进程读加密库 → 显式 RuntimeError，绝不静默返回密文/None
# ===========================================================================
class TestNoKeyReadsCiphertext:
    def test_ciphertext_without_key_raises(self, monkeypatch, _isolated_repository):
        repo = _isolated_repository
        # 先用密钥 A 写入密文
        monkeypatch.setattr(config, "PII_ENCRYPT_KEY", _KEY_A)
        repo.upsert_document("d_nokey", sha256="sha_nokey", storage_path="/tmp/d_nk.pdf",
                             mime="pdf")
        repo.set_document_pii("d_nokey", phone="13812345678")
        raw = repo._conn.execute(
            "SELECT pii_phone FROM documents WHERE id='d_nokey'"
        ).fetchone()[0]
        assert raw.startswith("gAAAA")  # DB 里确实是密文

        # 模拟"无 key 进程"：清空密钥再读 → 必须显式 RuntimeError
        monkeypatch.setattr(config, "PII_ENCRYPT_KEY", "")
        with pytest.raises(RuntimeError, match="PII_ENCRYPT_KEY"):
            repo.get_document("d_nokey")

    def test_plaintext_library_without_key_ok(self, monkeypatch, _isolated_repository):
        """明文库（pii 列无密文值）在无 key 下读写正常，不抛 RuntimeError。"""
        repo = _isolated_repository
        monkeypatch.setattr(config, "PII_ENCRYPT_KEY", "")
        repo.upsert_document("d_pt", sha256="sha_pt", storage_path="/tmp/d_pt.pdf",
                             mime="pdf")
        repo.set_document_pii("d_pt", phone="13812345678")
        doc = repo.get_document("d_pt")
        assert doc["pii_phone"] == "13812345678"

    def test_export_redact_no_key_friendly_error(self, monkeypatch, _isolated_repository, tmp_path):
        repo = _isolated_repository
        # 用密钥建批次 + 密文文档
        monkeypatch.setattr(config, "PII_ENCRYPT_KEY", _KEY_A)
        repo.create_batch("b_nokey", "v1")
        repo.upsert_document("d_exp_nk", sha256="sha_exp_nk", storage_path="/tmp/e.pdf",
                             mime="pdf")
        repo.set_document_pii("d_exp_nk", phone="13812345678")
        repo.create_run("run_nk", thread_id="t_nk", document_id="d_exp_nk",
                        batch_id="b_nokey")

        # 无 key 进程跑 export --redact → 退出码非 0，且不写任何半截文件
        monkeypatch.setattr(config, "PII_ENCRYPT_KEY", "")
        ns = argparse.Namespace(batch_id="b_nokey", dir=str(tmp_path), redact=True)
        assert main_cli.cmd_export(ns) == 1
        assert list(tmp_path.iterdir()) == []
