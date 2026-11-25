# -*- coding: utf-8 -*-
"""重发 manifest：只改序号/升级来源，复用线上已验证的安装包（发布机运行）。

适用场景（现场 1.3.4 事故）：
  包内容本身是对的（app-1.3.4.zip 已含微信窗口修复），但发包时漏配了
  ``--from-version``，manifest 里 app 包只有 ``from_versions=['1.2.9']``。
  在野的 1.3.1/1.3.2/1.3.3 客户端选不中增量包，被赶到从未成功过的全量包。

  此时**不能用 build_release.py 重打包**：本地 bundle 的版本层/构建时间未必
  与线上一致，重打会覆盖掉那份已验证的好包。本工具只重签 manifest：
    - 包条目（url / size / sha256）原样复用线上 manifest，绝不重打、不重传；
    - 抬高 sequence 让客户端重新看到更新；
    - 给 app 包补上 from_versions，让在野版本走回增量链路；
    - 用发布机私钥对新的 manifest 原始字节重签（与客户端验签严格同源）。

用法
----
    # 本地生成，先看清楚改了什么（不上传）
    python packaging/reissue_manifest.py \
        --base-url https://chehche-statics1.oss-cn-beijing.aliyuncs.com/wechat-kefu \
        --out D:/tmp/kefu-reissue \
        --sequence 1341 --from-version 1.3.3 --from-version 1.3.2 \
        --from-version 1.3.1 --from-version 1.2.9 \
        --notes "修复微信窗口识别；补齐增量包升级路径" \
        --key-id prod-2026 --private-key .../prod-2026_private.pem --dry-run

    # 确认无误后上传（只传 manifest.json + manifest.json.sig，包不重传）
    python packaging/reissue_manifest.py ... --oss-base oss://chehche-statics1/wechat-kefu

fail-closed 底线：
  - 线上 manifest 拉不到 / 验签不过 → 直接退出，绝不基于不可信输入重签；
  - sequence 必须 > 线上现值（防重放，否则客户端仍判「已是最新」）；
  - latest 版本必须与线上包版本一致，且 app 包必须存在；
  - 包条目的 url/size/sha256 一个字节都不改（只动 from_versions/sequence/notes）；
  - 私钥必须是 Ed25519 且 key_id 在客户端 PUBLIC_KEYS 内；
  - 签名先于上传；--dry-run 绝不触发 ossutil。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "kefu-system"))

from build_release import (  # noqa: E402
    _oss_upload, load_private_key, manifest_bytes, require_ossutil, sign_manifest,
)
from app.license import PUBLIC_KEYS  # noqa: E402
from app.update_core import verify_manifest  # noqa: E402


def fetch_online(base: str, name: str, timeout: float = 30.0) -> bytes:
    """带时间戳破 CDN 缓存地拉取线上文件。"""
    url = f"{base.rstrip('/')}/{name}?t={int(time.time())}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def reissue(args: argparse.Namespace) -> dict[str, Any]:
    base = args.base_url.strip().rstrip("/")
    if not base:
        raise SystemExit("[FATAL] 需要 --base-url（manifest.json 所在公网目录）")

    # 1) 拉线上 manifest + sig 并验签：绝不基于不可信输入重签
    payload = fetch_online(base, "manifest.json")
    sig_doc = json.loads(fetch_online(base, "manifest.json.sig").decode("utf-8"))
    ok, err = verify_manifest(payload, sig_doc, PUBLIC_KEYS)
    if not ok:
        raise SystemExit(f"[FATAL] 线上 manifest 验签失败：{err}")
    m = json.loads(payload.decode("utf-8"))
    print(f"[SRC ] 线上 manifest 验签通过：latest={m.get('latest')} "
          f"sequence={m.get('sequence')}")

    # 2) 前置校验：sequence 单调、版本一致、app 包存在
    old_seq = int(m.get("sequence", 0) or 0)
    if args.sequence <= old_seq:
        raise SystemExit(f"[FATAL] sequence {args.sequence} 必须 > 线上 {old_seq}")
    latest = str(m.get("latest", ""))
    packages = m.get("packages") or []
    app_pkg = next((p for p in packages if p.get("type") == "app"), None)
    if app_pkg is None:
        raise SystemExit("[FATAL] 线上 manifest 没有 app 增量包，无法重发")
    if str(app_pkg.get("version", "")) != latest:
        raise SystemExit("[FATAL] app 包版本与 latest 不一致，拒绝重签")
    # 包版本合法性：避免把坏包通过抬高 sequence 推给客户
    from app.update_core import VERSION_RE  # noqa: E402
    if not VERSION_RE.match(latest):
        raise SystemExit(f"[FATAL] latest 版本号非法：{latest!r}")

    # 3) 只改三个字段：sequence / app.from_versions / notes + released_at
    before_versions = list(app_pkg.get("from_versions") or [])
    new_versions = list(dict.fromkeys(before_versions + list(args.from_version)))
    app_pkg["from_versions"] = new_versions
    m["sequence"] = int(args.sequence)
    if args.notes:
        m["notes"] = args.notes
    m["released_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime())

    # 4) 包条目字节不动：对比除允许字段外，package dict 其余值不变
    new_payload = manifest_bytes(m)
    new_m = json.loads(new_payload.decode("utf-8"))
    for old, new in zip(packages, new_m.get("packages") or []):
        for key in ("type", "version", "url", "size", "sha256"):
            if old.get(key) != new.get(key):
                raise SystemExit(f"[FATAL] 包字段 {key} 被意外修改，拒绝发布")
    print(f"[CHG ] sequence {old_seq} -> {args.sequence}")
    print(f"[CHG ] app from_versions {before_versions} -> {new_versions}")

    # 5) 签名（必须在任何上传之前），并本地回验
    priv = load_private_key(Path(args.private_key))
    sig = sign_manifest(new_payload, priv, args.key_id)
    ok2, err2 = verify_manifest(new_payload, sig, PUBLIC_KEYS)
    if not ok2:
        raise SystemExit(f"[FATAL] 新 manifest 本地验签失败：{err2}")
    print(f"[SIG ] {args.key_id} 本地回验通过（{len(new_payload)} 字节）")

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_bytes(new_payload)
    (out / "manifest.json.sig").write_text(
        json.dumps(sig, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OUT ] {out / 'manifest.json'}")

    if args.oss_base:
        if args.dry_run:
            print("[DRY ] 跳过上传")
        else:
            require_ossutil()
            for name in ("manifest.json", "manifest.json.sig"):
                if not _oss_upload(args.oss_base, out / name, name, True):
                    raise SystemExit(f"[FATAL] 上传失败：{name}")
            print("[OSS ] manifest + sig 已上传（包未重传）")

    return {"old_sequence": old_seq, "new_sequence": args.sequence,
            "latest": latest, "from_versions": new_versions}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", required=True,
                   help="manifest.json 所在公网目录")
    p.add_argument("--out", type=Path, required=True, help="产物输出目录")
    p.add_argument("--sequence", type=int, required=True, help="新的递增序号")
    p.add_argument("--from-version", action="append", default=[],
                   help="追加到 app 包 from_versions，可多次")
    p.add_argument("--notes", default="", help="更新日志（覆盖线上）")
    p.add_argument("--key-id", default="prod-2026")
    p.add_argument("--private-key", required=True, help="Ed25519 私钥 PEM 路径")
    p.add_argument("--oss-base", default="", help="ossutil 上传目标")
    p.add_argument("--dry-run", action="store_true")
    reissue(p.parse_args())


if __name__ == "__main__":
    main()
