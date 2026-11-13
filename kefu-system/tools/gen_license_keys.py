# -*- coding: utf-8 -*-
"""授权签名密钥对生成工具（Ed25519）。

为什么要非对称签名：客户端只内置**公钥**，私钥只存在于授权服务器。这样客户
即使把程序目录翻个底朝天，也拿不到能伪造许可证的东西——用对称密钥（HMAC）
的话，密钥必须随客户端分发，等于把「自己给自己发授权」的能力一起发出去了。

用法（在 kefu-system 目录下）::

    python tools/gen_license_keys.py --out tools/license_keys
    python tools/gen_license_keys.py --out tools/license_keys --key-id prod-2026

输出：
- ``<out>/<key_id>_private.pem``：私钥，**只能放在授权服务器**，绝不能进客户端包
- ``<out>/<key_id>_public.pem``：公钥（PEM，给人看）
- 控制台打印 **base64 原始公钥**（32 字节），用于粘贴到
  ``app/license.py`` 的 ``PUBLIC_KEYS`` 表（客户端内置）。

注意：``--force`` 会覆盖同名密钥文件。已发出去的授权是用旧私钥签的，覆盖后
老许可证全部验签失败——上线后除非计划做密钥轮换（客户端需同时内置新旧两把
公钥），否则不要覆盖。
"""
from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


def generate(out_dir: Path, key_id: str, force: bool = False) -> int:
    """生成一对 Ed25519 密钥并落盘，返回退出码（0 成功）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    priv_path = out_dir / f"{key_id}_private.pem"
    pub_path = out_dir / f"{key_id}_public.pem"
    if not force:
        for path in (priv_path, pub_path):
            if path.exists():
                print(f"错误：{path} 已存在，拒绝覆盖（确认要换密钥请加 --force，"
                      f"注意已签发的许可证会全部失效）。", file=sys.stderr)
                return 2

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    priv_pem = private_key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    pub_pem = public_key.public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    priv_path.write_bytes(priv_pem)
    pub_path.write_bytes(pub_pem)
    try:                        # 私钥文件尽量收紧权限（Windows 上是 no-op）
        priv_path.chmod(0o600)
    except OSError:
        pass

    raw_pub = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    b64_pub = base64.b64encode(raw_pub).decode("ascii")

    print(f"已生成密钥对（key_id = {key_id}）")
    print(f"  私钥（仅放授权服务器）：{priv_path.resolve()}")
    print(f"  公钥（PEM）：{pub_path.resolve()}")
    print()
    print("把下面这行粘贴进 app/license.py 的 PUBLIC_KEYS（客户端内置公钥）：")
    print(f'    "{key_id}": "{b64_pub}",')
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="生成授权许可证签名用的 Ed25519 密钥对")
    parser.add_argument("--out", default="tools/license_keys",
                        help="密钥输出目录（默认 tools/license_keys）")
    parser.add_argument("--key-id", default="dev",
                        help="密钥标识（默认 dev；正式环境建议如 prod-2026）")
    parser.add_argument("--force", action="store_true",
                        help="覆盖已存在的同名密钥（危险：老许可证会失效）")
    args = parser.parse_args(argv)
    return generate(Path(args.out), args.key_id.strip() or "dev",
                    force=bool(args.force))


if __name__ == "__main__":
    raise SystemExit(main())
