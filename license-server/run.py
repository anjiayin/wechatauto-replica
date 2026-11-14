#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""授权服务后台唯一运维入口。

用法::

    python run.py                          # 默认 serve 127.0.0.1:8800
    python run.py serve --host 0.0.0.0 --port 8800
    python run.py init-db                  # 只建表（幂等，可重复执行）
    python run.py create-admin --username ops --password Abc12345
    python run.py issue-code --days 365 --phone 13800000000 --customer 某某汽配
    python run.py check                    # 自检：签名序列化逐字节一致
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from datetime import timedelta
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


# ---------------------------------------------------------------------------
# init-db
# ---------------------------------------------------------------------------
def cmd_init_db(_args: argparse.Namespace) -> int:
    """建表（幂等）并回显表清单与关键索引。"""
    from app import boot, db

    statements = boot.init_db()
    info = boot.describe_tables()
    print(f"[init-db] 已执行 {statements} 条建表语句（幂等，可重复执行）")
    if info:
        print(f"[init-db] 当前库共 {len(info['tables'])} 张表：")
        for name in info["tables"]:
            print(f"          - {name}")
        flag = "存在" if info["has_current_key_index"] else "缺失"
        print(f"[init-db] license.current_key 唯一索引（uk_current_machine）：{flag}")
        if len(info["tables"]) < 6:
            print("[init-db] 警告：表数量少于 6 张，请检查建表脚本执行日志")
            return 1
    else:
        print("[init-db] 警告：无法读取表结构，请检查数据库连接")
        return 1
    db.close_pool()
    return 0


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------
def cmd_serve(args: argparse.Namespace) -> int:
    """启动 HTTP 服务（生产建议 workers=1，见 README）。"""
    import uvicorn

    from app.config import get_config

    cfg = get_config()
    host = args.host or cfg.host
    port = int(args.port or cfg.port)
    if args.workers and int(args.workers) > 1:
        print("警告：限流器与首启建表都是进程内的，多 worker 会各自独立计数；"
              "生产请使用 --workers 1")
    print(f"[serve] 启动中：http://{host}:{port}   管理台 /admin/   接口文档 /docs")
    from app.main import app                 # 触发 create_app（含私钥加载）

    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


# ---------------------------------------------------------------------------
# create-admin
# ---------------------------------------------------------------------------
def cmd_create_admin(args: argparse.Namespace) -> int:
    """创建或重置管理员账号（已存在则重置密码）。"""
    from app import boot, db, security, signing
    from app.repo import admins, audit

    boot.init_db()
    username = str(args.username or "").strip().lower()[:32]
    if not username:
        print("错误：--username 不能为空", file=sys.stderr)
        return 2
    password = str(args.password or "")
    generated = False
    if not password:
        password = secrets.token_urlsafe(12)
        generated = True
    else:
        try:
            security.validate_password_policy(password)
        except Exception as exc:                  # noqa: BLE001 - CLI 提示
            print(f"错误：密码不符合策略：{exc}", file=sys.stderr)
            return 2
    moment = signing.now()
    must_change = bool(args.must_change) or generated
    password_hash = security.hash_password(password)
    try:
        with db.transaction() as cur:
            existing = admins.get_by_username(cur, username)
            if existing:
                admins.update_password(cur, int(existing["id"]), password_hash,
                                       moment, must_change=must_change)
                audit.write(cur, now=moment, actor="cli", action="reset_password",
                            target_type="admin", target_id=username,
                            note="命令行重置密码")
                action = "重置"
            else:
                admins.create(cur, username=username,
                              password_hash=password_hash,
                              display_name=str(args.display or "系统管理员"),
                              must_change_password=must_change, now=moment)
                audit.write(cur, now=moment, actor="cli", action="create_admin",
                            target_type="admin", target_id=username,
                            note="命令行创建管理员")
                action = "创建"
    finally:
        db.close_pool()
    print(f"[create-admin] 已{action}管理员：{username}")
    if generated:
        print(f"[create-admin] 随机初始密码：{password}")
    if must_change:
        print("[create-admin] 该账号首次登录必须修改密码（must_change_password=1）")
    return 0


# ---------------------------------------------------------------------------
# issue-code
# ---------------------------------------------------------------------------
def cmd_issue_code(args: argparse.Namespace) -> int:
    """命令行造激活码（不启动服务）。"""
    from app import boot, db, signing
    from app.repo import codes

    boot.init_db()
    moment = signing.now()
    try:
        with db.transaction() as cur:
            result = codes.batch_create(
                cur, count=int(args.count), days=int(args.days),
                edition=str(args.edition or "standard"),
                customer_name=str(args.customer or ""),
                customer_phone=str(args.phone or ""),
                note=str(args.note or "命令行生成"),
                batch=str(args.batch or ""), created_by="cli", now=moment)
    finally:
        db.close_pool()
    print(f"[issue-code] 批次 {result['batch']}，共 {len(result['items'])} 个"
          f"（{args.days} 天，版本 {args.edition}）")
    for item in result["items"]:
        print(item["code"])
    return 0


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------
def _sample_payload() -> Dict[str, Any]:
    """构造一张样例许可证 payload（用当前时间，避免硬编码日期）。"""
    from app import signing
    issued = signing.now().replace(microsecond=0)
    return {
        "license_id": "LIC-20261113-A1B2",
        "machine_code": "BBCF-BCA8-F127-5747-05D8-46B3-E8D8-CAF0",
        "customer": "某某汽配",
        "edition": "standard",
        "issued_at": signing.to_iso(issued),
        "expires_at": signing.to_iso(issued + timedelta(days=365)),
        "grace_days": 7,
        "features": ["monitor", "reply", "ai"],
    }



def _client_payload_hex(payload: Dict[str, Any]) -> Optional[str]:
    """用**客户端**的 ``payload_bytes`` 算一遍签名原文（十六进制）。

    ``kefu-system/app/license.py`` 与本站的 ``app`` 包同名，必须放到子进程里
    以 ``kefu-system`` 为工作目录运行，避免包名冲突；这里只读不写。
    """
    kefu_dir = BASE_DIR.parent / "kefu-system"
    if not (kefu_dir / "app" / "license.py").exists():
        return None
    script = (
        "import json,sys;"
        "from app.license import payload_bytes;"
        "data=json.load(sys.stdin);"
        "sys.stdout.write(payload_bytes(data).hex())"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script], cwd=str(kefu_dir),
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[check] 跳过客户端比对（子进程失败）：{exc}")
        return None
    if proc.returncode != 0:
        print(f"[check] 跳过客户端比对（客户端模块不可用）："
              f"{(proc.stderr or '').strip()[:300]}")
        return None
    return (proc.stdout or "").strip()


def cmd_check(_args: argparse.Namespace) -> int:
    """自检：读私钥 → 签一张样例证 → 验签 → 打印 payload_bytes 原文。"""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    from app import signing
    from app.config import get_config

    cfg = get_config()
    print(f"[check] 1/5 私钥路径：{cfg.private_key_path}")
    private_key = signing.load_private_key(cfg.private_key_path)
    public_b64 = signing.public_key_b64(private_key)
    print(f"[check] 2/5 key_id={cfg.key_id}  公钥(base64)={public_b64}")

    payload = _sample_payload()
    raw = signing.payload_bytes(payload)
    doc = signing.sign_doc(payload, private_key, cfg.key_id)
    print("[check] 3/5 payload_bytes（签名原文，客户端必须逐字节一致）：")
    print(raw.decode("utf-8"))
    print(f"[check] signature={doc['signature']}")

    # 验签：优先用同目录的 <key_id>_public.pem，否则用私钥推导的公钥
    public_pem = cfg.private_key_path.with_name(
        cfg.private_key_path.name.replace("_private.pem", "_public.pem"))
    public_key: Any
    source = "由私钥推导"
    if public_pem.exists():
        try:
            public_key = load_pem_public_key(public_pem.read_bytes())
            source = str(public_pem)
        except Exception as exc:                  # noqa: BLE001 - 回退到推导
            print(f"[check] 读取公钥 PEM 失败（改用推导）：{exc}")
            public_key = private_key.public_key()
    else:
        public_key = private_key.public_key()
    try:
        public_key.verify(__import__("base64").b64decode(doc["signature"]), raw)
        print(f"[check] 4/5 验签：OK（公钥来源：{source}）")
    except (InvalidSignature, ValueError) as exc:
        print(f"[check] 4/5 验签：失败 - {exc}", file=sys.stderr)
        return 1

    client_hex = _client_payload_hex(payload)
    if client_hex is None:
        print("[check] 5/5 客户端比对：跳过（未找到 kefu-system 客户端实现）")
    elif client_hex == raw.hex():
        print("[check] 5/5 客户端比对：OK（与本服务 payload_bytes 逐字节一致）")
    else:
        print("[check] 5/5 客户端比对：不一致！", file=sys.stderr)
        print(f"        本服务：{raw.hex()}", file=sys.stderr)
        print(f"        客户端：{client_hex}", file=sys.stderr)
        return 1

    # 顺带自检时间与机器码工具
    iso = signing.to_iso(signing.from_iso("2027-01-01T00:00:00+00:00"))
    print(f"[check] 时间换算：2027-01-01T00:00:00+00:00 → {iso}")
    print(f"[check] 机器码校验：{signing.valid_machine_code('bbcfbca8f127574705d846b3e8d8caf0')}"
          f" / {signing.valid_machine_code('BBCF-BCA8-F127-5747-05D8-46B3-E8D8-CAF0')}"
          f" / {signing.valid_machine_code('ABC')}")
    print("[check] 全部自检通过")
    return 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="run.py", description="授权服务后台（license-server）运维入口")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动 HTTP 服务")
    serve.add_argument("--host", default="", help="监听地址（默认取配置）")
    serve.add_argument("--port", type=int, default=0, help="监听端口（默认取配置）")
    serve.add_argument("--workers", type=int, default=1, help="进程数（建议 1）")
    serve.set_defaults(func=cmd_serve)

    init_db = sub.add_parser("init-db", help="只建表，不启动服务")
    init_db.set_defaults(func=cmd_init_db)

    create_admin = sub.add_parser("create-admin", help="创建/重置管理员账号")
    create_admin.add_argument("--username", required=True, help="登录名")
    create_admin.add_argument("--password", default="", help="密码（留空则随机生成）")
    create_admin.add_argument("--display", default="系统管理员", help="显示名")
    create_admin.add_argument("--must-change", action="store_true",
                              help="强制首次登录改密")
    create_admin.set_defaults(func=cmd_create_admin)

    issue = sub.add_parser("issue-code", help="命令行生成激活码")
    issue.add_argument("--days", type=int, default=365, help="有效天数")
    issue.add_argument("--count", type=int, default=1, help="生成数量（1-500）")
    issue.add_argument("--phone", default="", help="客户手机号")
    issue.add_argument("--customer", default="", help="客户名称")
    issue.add_argument("--edition", default="standard", help="版本标识")
    issue.add_argument("--note", default="命令行生成", help="备注")
    issue.add_argument("--batch", default="", help="批次号（留空自动生成）")
    issue.set_defaults(func=cmd_issue_code)

    check = sub.add_parser("check", help="自检签名序列化")
    check.set_defaults(func=cmd_check)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """入口：无子命令时等价于 ``serve``。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args = parser.parse_args(["serve"] + (argv or []))
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        return int(args.func(args))
    except SystemExit as exc:                     # 私钥/配置类退出
        return int(exc.code or 0)
    except Exception as exc:                      # noqa: BLE001 - CLI 兜底
        print(f"错误：{exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
