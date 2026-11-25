#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把社区工具导出的微信 4.x 密钥导入为 wechatauto 的 keys.json。

背景
----
微信 4.x 用 SQLCipher 4 加密本地库。社区工具（wechat-dump-rs / wx_key /
ReadWxKey 等）通过扫描 Weixin.exe 进程内存导出密钥，常见两种形态：

  A) 主密钥 master key：32 字节（64 hex）。微信 4.x 单一主密钥 + 每库随机
     salt 派生出各库密钥。项目内置 WeChatDB.derive_keys_from_master() 即做此事。
  B) 每库 raw key：SQLCipher "raw key with explicit salt" 语法
     x'<64hex key + 32hex salt>' 共 48 字节。
     前 32 字节 = 已派生的库密钥，后 16 字节 = salt（= 库文件头前 16 字节）。
     部分工具只导出 64hex key（无 salt）。

wechatauto 的 keys.json 只要 {"库相对路径": "<64hex 库密钥>"}（32 字节裸 key，
见 db.py:992-995）。B 形态不能直接写入：要拆出前 64 hex 当 value，用 salt
（或 HMAC 校验）反查库路径当 key。本脚本完成这个转换。

校验策略：转换阶段就用页1 HMAC 逐库确认（与 db.py:268-289 等价的内嵌实现），
只有校验通过的 key 才写入。--verify 时再用项目 wechatauto.db._verify_enc_key
权威复核一遍并打印逐库报告。

用法（务必用项目自带 python，--verify 需要 import wechatauto.db）
------------------------------------------------------------------
  # B 形态：社区工具导出的 raw key 文本（每行一条 x'<96hex>' 或裸 96hex/64hex）
  python tools/import_wechat_keys.py \
      --raw raw_keys.txt \
      --dbstorage "E:\\xwechat_files\\wxid_xxxx_xxxx\\db_storage" \
      --out "kefu-system\\data\\wechat_cache\\keys.json" --verify

  # A 形态：主密钥
  python tools/import_wechat_keys.py \
      --master <64hex 主密钥> \
      --dbstorage "E:\\xwechat_files\\wxid_xxxx_xxxx\\db_storage" \
      --out "kefu-system\\data\\wechat_cache\\keys.json" --verify

从 wx_key 导入（推荐用 --probe 自动识别）
------------------------------------------
wx_key 常导出一条 32 字节密钥（64 hex），但它可能是"主密钥"也可能是"单库
密钥"，人工难判断。--probe 会自动尝试两种路径并用页1 HMAC 判定：

  python tools/import_wechat_keys.py --probe <wx_key给出的64hex> \
      --dbstorage "E:\\xwechat_files\\wxid_xxxx_xxxx\\db_storage" \
      --out "kefu-system\\data\\wechat_cache\\keys.json" --verify

  - 若识别为主密钥：PBKDF2 派生出全部库密钥（write all）。
  - 若识别为单库密钥：只能解出它对应的那一个库（write 1），其余库仍需密钥。
  - 都失败：密钥错误或 db_storage 账号目录选错，不写入。

注意：wx_key 是在"登录瞬间"hook 拿到密钥的，拿到后微信是否还开着无关紧要
——本脚本只读 db_storage 里的 .db 文件头，不访问微信进程。

社区工具获取地址（均为开源项目，仅供处理自己的数据）
------------------------------------------------------
  wechat-dump-rs : https://github.com/0xlane/wechat-dump-rs   （v4 分支 README）
  wx_key         : https://github.com/zj1123581321/wx_key      （含图片密钥）
  ReadWxKey      : https://github.com/yinhuacha869/ReadWxKey    （4.1.8+ 内存扫描）
  密钥格式文档    : https://github.com/zhx8702/wechat-colleague/blob/main/docs/encryption.md

安全提示：keys.json 是能解开你全部消息库的敏感凭据。操作前先备份原文件，
切勿提交版本库或发给他人。
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import struct
import sys
from pathlib import Path

PAGE_SZ = 4096
RESERVE_SZ = 80
KDF_ITER = 256000
REPO_ROOT = Path(__file__).resolve().parents[2]

RAW96 = re.compile(r"x'([0-9a-fA-F]{96})'")
RAW64 = re.compile(r"x'([0-9a-fA-F]{64})'")
BARE96 = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{96})(?![0-9a-fA-F])")
BARE64 = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{64})(?![0-9a-fA-F])")


def _pbkdf2(passwd, salt, iters):
    # 与 db.py:250-251 完全一致
    return hashlib.pbkdf2_hmac("sha512", passwd, salt, iters, dklen=32)


def _verify_local(enc_key, page1, salt=None):
    # 与 db.py:268-289 的 _verify_enc_key 等价（仅依赖标准库）
    if len(page1) < PAGE_SZ:
        return False
    if len(enc_key) == 48 and salt is None:
        enc_key, salt = enc_key[:32], enc_key[32:]
    if salt is None:
        salt = page1[:16]
    elif len(salt) != 16:
        return False
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = _pbkdf2(enc_key, mac_salt, 2)
    hmac_data = page1[16:PAGE_SZ - RESERVE_SZ + 16]
    stored_hmac = page1[PAGE_SZ - 64:PAGE_SZ]
    hm = hmac.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac


def _load_project_verify():
    """加载项目权威校验函数；--verify 时用，确保与主程序逻辑一致。"""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from wechatauto.db import _verify_enc_key
        return _verify_enc_key
    except Exception as exc:
        raise RuntimeError(
            "无法导入 wechatauto.db（%s）。请用项目自带 python 运行，例如：\n"
            "  /d/my_conda_envs/wechatauto/python.exe tools/import_wechat_keys.py ...\n"
            "（该环境已装 cryptography，db.py 依赖它）" % exc
        )


def collect_dbs(dbstorage):
    """返回 [(rel, path, page1), ...]，rel 与 db.py:664 的 os.path.relpath 一致。"""
    out = []
    for root, _, names in os.walk(dbstorage):
        if os.path.normcase(os.path.relpath(root, dbstorage)).startswith("migrate"):
            continue  # 与 db.py:658 一致，排除 migrate 目录
        for name in names:
            if not name.endswith(".db") or name.endswith(("-wal", "-shm")):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "rb") as f:
                    page1 = f.read(PAGE_SZ)
            except OSError:
                continue
            if len(page1) >= PAGE_SZ:
                out.append((os.path.relpath(path, dbstorage), path, page1))
    return out


def parse_raw_lines(path):
    """解析 raw key 文本，返回 [(key_hex, salt_hex | None), ...]。"""
    items = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            for pat, has_salt in ((RAW96, True), (BARE96, True),
                                  (RAW64, False), (BARE64, False)):
                m = pat.search(line)
                if m:
                    h = m.group(1)
                    items.append((h[:64], h[64:] if has_salt else None))
                    break
    return items


def build_from_raw(items, dbs):
    """用 raw key + HMAC 反查每个 key 属于哪个库。"""
    page1_of = {rel: p1 for rel, _, p1 in dbs}
    salt_map = {}
    for rel, _, page1 in dbs:
        salt_map.setdefault(page1[:16].hex(), []).append(rel)

    result = {}
    for key_hex, salt_hex in items:
        key = bytes.fromhex(key_hex)
        # 有 salt 先按 salt 缩小到候选库；无 salt 则对所有库做 HMAC 探测
        candidates = salt_map.get(salt_hex.lower(), []) if salt_hex else [r for r in page1_of]
        for rel in candidates:
            page1 = page1_of[rel]
            ok = _verify_local(key, page1)
            if not ok and salt_hex:
                ok = _verify_local(key, page1, salt=bytes.fromhex(salt_hex))
            if ok:
                result[rel] = key_hex.lower()
    return result


def build_from_master(master_hex, dbs):
    """与 db.py:774-798 的 derive_keys_from_master 等价。"""
    master = bytes.fromhex(master_hex)
    if len(master) != 32:
        raise ValueError("主密钥必须 32 字节（64 位 hex），当前 %d 字节" % len(master))
    result = {}
    for rel, _, page1 in dbs:
        derived = _pbkdf2(master, page1[:16], KDF_ITER)
        if _verify_local(derived, page1):
            result[rel] = derived.hex()
    return result


def probe_key(key_hex, dbs):
    """自动识别一条 32B 密钥的类型：先当主密钥派生，再当单库裸密钥直接校验。

    返回 (result, kind)；kind ∈ {"master", "single", "unknown"}。
    """
    key = bytes.fromhex(key_hex)
    if len(key) != 32:
        raise ValueError("密钥必须 32 字节（64 位 hex），当前 %d 字节" % len(key))

    # 路径 A：当作主密钥 —— PBKDF2 派生每个库
    master_result = build_from_master(key_hex, dbs)
    if master_result:
        return master_result, "master"

    # 路径 B：当作单库裸密钥 —— 直接对每个库做页1 HMAC
    single_result = {}
    for rel, _, page1 in dbs:
        if _verify_local(key, page1):
            single_result[rel] = key_hex.lower()
    if single_result:
        return single_result, "single"

    return {}, "unknown"


def main():
    ap = argparse.ArgumentParser(
        description="把社区工具导出的微信 4.x 密钥导入为 wechatauto 的 keys.json"
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--raw", metavar="FILE", help="社区工具导出的 raw key 文本文件")
    src.add_argument("--master", metavar="HEX", help="32 字节主密钥（64 位 hex）")
    src.add_argument("--probe", metavar="HEX",
                     help="32 字节密钥（64 hex），自动识别主密钥/单库密钥（wx_key 用户推荐）")
    ap.add_argument("--dbstorage", required=True, metavar="DIR",
                    help="目标账号的 db_storage 绝对路径")
    ap.add_argument("--out", required=True, metavar="FILE", help="输出 keys.json 路径")
    ap.add_argument("--verify", action="store_true",
                    help="用项目 wechatauto.db._verify_enc_key 逐库权威复核并打印报告")
    args = ap.parse_args()

    dbstorage = os.path.abspath(args.dbstorage)
    if not os.path.isdir(dbstorage):
        ap.error("dbstorage 目录不存在：%s" % dbstorage)
    dbs = collect_dbs(dbstorage)
    if not dbs:
        ap.error("dbstorage 下未找到任何 .db 文件：%s" % dbstorage)
    print("扫描到 %d 个加密库（db_storage=%s）" % (len(dbs), dbstorage))

    kind = None
    try:
        if args.raw:
            items = parse_raw_lines(args.raw)
            if not items:
                ap.error("未从 %s 解析出任何 64/96 位 hex 密钥" % args.raw)
            print("解析到 %d 条 raw key" % len(items))
            result = build_from_raw(items, dbs)
            kind = "raw"
        elif args.master:
            result = build_from_master(args.master, dbs)
            kind = "master"
        else:
            result, kind = probe_key(args.probe, dbs)
            labels = {
                "master": "识别为主密钥 → 已 PBKDF2 派生全部库密钥",
                "single": "识别为单库裸密钥 → 只能解出它对应的那一个库",
                "unknown": "无法识别：密钥错误，或 db_storage 账号目录选错",
            }
            print("探测结果: %s" % labels[kind])
    except ValueError as exc:
        ap.error(str(exc))

    if not result:
        print("密钥校验未通过任何库，未写入文件（避免覆盖已有的可用 keys.json）。")
        print("请检查：①密钥是否复制完整（64 位 hex）②--dbstorage 是否为正确账号目录。")
        sys.exit(3)

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print("写入 %d/%d 个库密钥 -> %s" % (len(result), len(dbs), out_path))

    missing = [rel for rel, _, _ in dbs if rel not in result]
    if missing:
        print("未覆盖的库 (%d): %s" % (len(missing), ", ".join(missing[:10])))

    if args.verify:
        verify_fn = _load_project_verify()
        page1_of = {rel: p1 for rel, _, p1 in dbs}
        ok = 0
        for rel, key_hex in result.items():
            good = verify_fn(bytes.fromhex(key_hex), page1_of[rel])
            print("  [%s] %s" % ("OK" if good else "FAIL", rel))
            ok += bool(good)
        print("项目 HMAC 校验: %d/%d 通过" % (ok, len(result)))
        if ok != len(result):
            sys.exit(2)


if __name__ == "__main__":
    main()
