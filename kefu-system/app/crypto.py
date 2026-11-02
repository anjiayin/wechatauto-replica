# -*- coding: utf-8 -*-
"""Fernet 密钥管理 + API Key 加解密 + 掩码。

密钥文件位于 ``data/secret.key``，首次运行自动生成（权限仅限当前用户可读
的常规文件系统保护；本机单用户场景可接受）。
"""
from __future__ import annotations

import logging
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import SECRET_KEY_PATH

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None


def _load_or_create_key(path: Path) -> bytes:
    """读取 Fernet 密钥；不存在则生成并写入。"""
    if path.exists():
        key = path.read_bytes().strip()
        if key:
            return key
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    logger.info("已生成新的 Fernet 密钥文件：%s", path)
    return key


def get_fernet() -> Fernet:
    """获取进程级 Fernet 实例（懒加载单例）。"""
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key(SECRET_KEY_PATH))
    return _fernet


def encrypt_api_key(plain: str) -> str:
    """明文 API Key → Fernet 密文字符串。"""
    if not plain:
        return ""
    return get_fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_api_key(cipher: str) -> str:
    """Fernet 密文 → 明文 API Key；解密失败返回空串（密钥文件被更换等场景）。"""
    if not cipher:
        return ""
    try:
        return get_fernet().decrypt(cipher.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("API Key 密文解密失败（secret.key 可能已更换）")
        return ""


def mask_key(plain_or_cipher: str, *, is_plain: bool = False) -> str:
    """API Key 掩码：保留前 4 位 + "…"，其余隐藏。

    Args:
        plain_or_cipher: 密文（默认）或明文（is_plain=True）。
        is_plain: 输入是否为明文。
    """
    if not plain_or_cipher:
        return ""
    plain = plain_or_cipher if is_plain else decrypt_api_key(plain_or_cipher)
    if not plain:
        return ""
    if len(plain) <= 4:
        return "****"
    return plain[:4] + "…****"


def random_token(n: int = 16) -> str:
    """生成随机 token（内部工具用途）。"""
    return secrets.token_hex(n)
