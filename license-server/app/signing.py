# -*- coding: utf-8 -*-
"""Ed25519 签名与时间工具。

**本文件是 ``payload_bytes`` 的唯一实现处** —— 任何其它模块都不许自己
``json.dumps`` 许可证 payload，否则序列化差一个字节客户端就验签失败。

时间口径：
    * 数据库内统一存 **东八区 naive DATETIME**（``now()``）；
    * 下发到客户端 payload 时才加 ``+08:00``（``to_iso``）；
    * ``from_iso`` 把带时区的 ISO 转回东八区 naive。
"""
from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_private_key,
)

logger = logging.getLogger(__name__)

#: 服务端统一东八区（客户端只比对绝对时间，时区必须显式）
TZ = timezone(timedelta(hours=8))

#: 机器码去掉横杠后的十六进制长度
_MACHINE_HEX_LEN = 32
#: 机器码分组长度（每 4 位一组）
_GROUP_SIZE = 4
_HEX_RE = re.compile(r"^[0-9A-F]+$")


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------
def now() -> datetime:
    """当前时间（东八区 naive，写库口径）。"""
    return datetime.now(TZ).replace(tzinfo=None)


def to_iso(dt: Optional[datetime]) -> str:
    """DB naive datetime → 客户端 payload 用的带时区 ISO（秒级）。

    >>> to_iso(datetime(2027, 11, 13, 21, 2, 53))
    '2027-11-13T21:02:53+08:00'
    """
    if dt is None:
        return ""
    return dt.replace(microsecond=0, tzinfo=TZ).isoformat()


def from_iso(text: Any) -> Optional[datetime]:
    """ISO8601（可带时区，兼容末尾 ``Z``）→ 东八区 naive；解析失败返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(microsecond=0)
    return dt.astimezone(TZ).replace(tzinfo=None, microsecond=0)


def parse_expiry_text(text: Any) -> Optional[datetime]:
    """解析「到期日」入参。

    规则（设计文档 §11-5）：
    * ``YYYY-MM-DD`` → 当天 **23:59:59**（东八区）；
    * 完整 ISO8601 → 精确到秒。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        try:
            day = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None
        return day.replace(hour=23, minute=59, second=59)
    parsed = from_iso(raw)
    return parsed


def fmt_cn(dt: Optional[datetime]) -> str:
    """东八区时间的人类可读格式（给客户看的中文提示用）。"""
    if dt is None:
        return "未知时间"
    return dt.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 签名
# ---------------------------------------------------------------------------
def payload_bytes(payload: Dict[str, Any]) -> bytes:
    """签名原文的规范化序列化（**全项目唯一实现**）。

    三项任一不同，客户端验签必失败：``sort_keys=True``（递归键排序）、
    紧凑分隔符（无空格）、``ensure_ascii=False``（中文不转义）。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def load_private_key(path: Path) -> Ed25519PrivateKey:
    """读取 Ed25519 PEM 私钥；失败打印中文原因并 ``SystemExit(2)``。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        print(f"错误：读取私钥失败（{path}）：{exc}\n"
              f"请确认 LS_PRIVATE_KEY_PATH 指向存在的 PEM 文件",
              file=sys.stderr)
        raise SystemExit(2)
    try:
        key = load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        print(f"错误：私钥格式不正确（{path}）：{exc}", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(key, Ed25519PrivateKey):
        print(f"错误：私钥不是 Ed25519 类型（{path}）", file=sys.stderr)
        raise SystemExit(2)
    return key


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    """推导 base64 原始公钥（与客户端 ``PUBLIC_KEYS`` 的值比对用）。"""
    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


_PRIVATE_KEY: Optional[Ed25519PrivateKey] = None


def get_private_key() -> Ed25519PrivateKey:
    """进程级私钥单例（只在内存，任何日志/响应都不输出内容）。"""
    global _PRIVATE_KEY
    if _PRIVATE_KEY is None:
        from app.config import get_config          # 局部导入避免循环依赖
        _PRIVATE_KEY = load_private_key(get_config().private_key_path)
    return _PRIVATE_KEY


def reset_private_key() -> None:
    """清空私钥缓存（测试 / 密钥轮换用）。"""
    global _PRIVATE_KEY
    _PRIVATE_KEY = None


def sign_doc(payload: Dict[str, Any], private_key: Ed25519PrivateKey,
             key_id: str) -> Dict[str, Any]:
    """给 payload 签名，返回客户端可直接保存的许可证文档。"""
    signature = private_key.sign(payload_bytes(payload))
    return {
        "key_id": key_id,
        "payload": payload,
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def build_payload(rec: Dict[str, Any], grace_days: int = 7,
                  features_default: str = "monitor,reply,ai") -> Dict[str, Any]:
    """由许可证记录生成 payload。

    * ``issued_at`` 取记录首次签发时间（心跳重签**不改**，INV-4）；
    * ``expires_at`` 用最新值（管理端延期后下次心跳即生效）；
    * ``features`` 由逗号串还原成**数组**（落库是 VARCHAR，5.6 无 JSON）。
    """
    features = [x for x in str(rec.get("features") or features_default).split(",")
                if x]
    return {
        "license_id": str(rec["license_id"]),
        "machine_code": str(rec["machine_code"]),
        "customer": str(rec.get("customer_name") or ""),
        "edition": str(rec.get("edition") or "standard"),
        "issued_at": to_iso(rec.get("issued_at")),
        "expires_at": to_iso(rec.get("expires_at")),
        "grace_days": int(rec.get("grace_days") or grace_days),
        "features": features,
    }


# ---------------------------------------------------------------------------
# 标识符工具
# ---------------------------------------------------------------------------
def normalize_machine(value: Any) -> str:
    """机器码归一化：转大写、去非法分隔、按 4 位分组。

    同时接受 ``bbcfbca8...``（紧凑）与 ``BBCF-BCA8-...``（分组），
    统一成客户端使用的 39 字符分组形式（32 hex + 7 横杠），
    这样同一个机器无论用哪种写法提交，落库与唯一约束都是同一行。
    """
    text = str(value or "").strip().upper()
    compact = text.replace("-", "").replace(" ", "")
    if len(compact) != _MACHINE_HEX_LEN or not _HEX_RE.match(compact):
        return text
    return "-".join(compact[i:i + _GROUP_SIZE]
                    for i in range(0, len(compact), _GROUP_SIZE))


def valid_machine_code(value: Any) -> bool:
    """机器码格式校验：去掉横杠后必须是 32 位十六进制。"""
    compact = str(value or "").replace("-", "").replace(" ", "").upper()
    return len(compact) == _MACHINE_HEX_LEN and bool(_HEX_RE.match(compact))


def normalize_code(value: Any) -> str:
    """激活码归一化（去空格、转大写）。"""
    return str(value or "").strip().upper()


def new_license_id(at: Optional[datetime] = None) -> str:
    """许可证号：``LIC-yyyymmdd-XXXX``（4 位十六进制随机）。"""
    moment = at or now()
    return f"LIC-{moment:%Y%m%d}-{secrets.token_hex(2).upper()}"


def new_batch_no(at: Optional[datetime] = None) -> str:
    """批次号：``Byyyymmdd-XXXX``。"""
    moment = at or now()
    return f"B{moment:%Y%m%d}-{secrets.token_hex(2).upper()}"


def new_activation_code() -> str:
    """激活码：``KF-XXXX-XXXX-XXXX-XXXX``。"""
    return "KF-" + "-".join(secrets.token_hex(2).upper() for _ in range(4))
