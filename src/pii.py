# -*- coding: utf-8 -*-
"""PII 处理：提取 / 打码 / 字段级加解密（Fernet）。

设计取舍（如实说明，勿当万能）：
- 手机号：正则 ``1[3-9]\\d{9}`` 精确匹配；邮箱：标准正则。
- 姓名：**启发式**，不可靠。仅尝试 (1) 去噪后的文件名 stem 是否为 2-4 个汉字；
  (2) 正文首段连续 2-4 个汉字行。抽不到就留空，绝不瞎猜。它只是“辅助定位”字段，
  不参与任何评分/审批决策。
- 加密：Fernet（AES-128-CBC + PKCS7 + HMAC）。密钥来自环境变量 ``PII_ENCRYPT_KEY``
  （base64 urlsafe 编码的 32 字节）。未配置 → 明文模式，导入即显式警告（绝不静默）。
- 透明加解密在 Repository 层完成：写库前 encrypt_value，读库后 decrypt_value；
  解密失败（密钥错误/密文损坏）显式抛错，不返回乱码。
- **不加密**：pages.text 原文、assessments.payload、storage_path 文件名——复核页需要
  原文证据，故这些仍明文。本模块只守护 documents 表新增的 pii_* 三列。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict

from . import config

logger = logging.getLogger(__name__)

# 手机号：1 开头，第二位 3-9，共 11 位；前后不接数字（避免误吃长串数字）
PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
# 邮箱：足够宽松的标准匹配
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# 文件名去噪词（简历/resume/CV/求职 等）
_NAME_NOISE_RE = re.compile(r"(个人简历|简历|求职|resume|cv|的|本人)", re.IGNORECASE)
_CJK_NAME_RE = re.compile(r"[\u4e00-\u9fa5]{2,4}")

# 明文模式只警告一次，避免日志刷屏
_plaintext_warned = False


# ---------------------------------------------------------------------------
# 加解密
# ---------------------------------------------------------------------------
def encryption_enabled() -> bool:
    """是否配置了合法密钥（由 config.PII_ENCRYPT_KEY 动态决定，便于测试 monkeypatch）。"""
    return bool((getattr(config, "PII_ENCRYPT_KEY", "") or "").strip())


def _fernet():
    key = (getattr(config, "PII_ENCRYPT_KEY", "") or "").strip()
    if not key:
        return None
    from cryptography.fernet import Fernet

    return Fernet(key.encode("utf-8"))


def encrypt_value(plain: object) -> str:
    """写库前调用。空值原样返回空串；未配置密钥 → 明文 + 一次性警告。"""
    if plain is None:
        return ""
    text = str(plain)
    if not text:
        return ""
    f = _fernet()
    if f is None:
        global _plaintext_warned
        if not _plaintext_warned:
            logger.warning("PII 加密未启用（PII_ENCRYPT_KEY 未配置）：PII 字段将以明文落库")
            _plaintext_warned = True
        return text
    return f.encrypt(text.encode("utf-8")).decode("ascii")


def decrypt_value(token: object) -> str:
    """读库后调用。空值返回空串；明文模式原样返回；解密失败显式抛错。"""
    if token is None:
        return ""
    text = str(token)
    if not text:
        return ""
    f = _fernet()
    if f is None:
        return text  # 明文模式
    try:
        return f.decrypt(text.encode("ascii")).decode("utf-8")
    except Exception as e:  # Fernet.InvalidToken 等
        raise ValueError(f"PII 字段解密失败（密钥错误或密文损坏）: {e}") from e


# ---------------------------------------------------------------------------
# 提取
# ---------------------------------------------------------------------------
def extract_pii(text: str, file_name: str = "") -> Dict[str, str]:
    """从解析正文（或文件头字节）提取姓名/手机号/邮箱。

    姓名为启发式，可能为空；手机号/邮箱为正则命中即取第一个。
    任何异常都不应阻断主流程——调用方已包 try/except。
    """
    result = {"name": "", "phone": "", "email": ""}
    text = text or ""

    m = PHONE_RE.search(text)
    if m:
        result["phone"] = m.group(0)
    m = EMAIL_RE.search(text)
    if m:
        result["email"] = m.group(0)

    result["name"] = _extract_name(text, file_name)
    return result


def _extract_name(text: str, file_name: str) -> str:
    # 1) 文件名 stem 去噪后，若恰为 2-4 个汉字 → 当作姓名
    stem = Path(file_name).stem if file_name else ""
    cand = _NAME_NOISE_RE.sub("", stem).strip()
    if cand and _CJK_NAME_RE.fullmatch(cand):
        return cand
    # 2) 正文首个 2-4 汉字短行（常见于简历顶部标题）
    for line in (text or "").splitlines():
        line = line.strip()
        if _CJK_NAME_RE.fullmatch(line):
            return line
    return ""


# ---------------------------------------------------------------------------
# 打码
# ---------------------------------------------------------------------------
def mask_pii(value: object) -> str:
    """打码展示：138****1234 / z***@x.com / 张**。空值返回空串。"""
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    if PHONE_RE.fullmatch(text):
        return f"{text[:3]}****{text[-4:]}"
    if "@" in text:
        local, _, domain = text.partition("@")
        masked_local = (local[0] + "***") if local else "***"
        return f"{masked_local}@{domain}"
    # 姓名 / 通用：保留首字符，其余打码
    if len(text) <= 1:
        return "*"
    return text[0] + "*" * (len(text) - 1)
