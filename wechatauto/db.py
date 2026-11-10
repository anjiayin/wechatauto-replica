# -*- coding: utf-8 -*-
"""wechatauto 数据库读取模块（微信 4.x）

通过读取微信本地 SQLCipher 加密数据库实现消息读取，不依赖 UI 自动化。

原理：
    1. 从微信配置文件（%APPDATA%/Tencent/xwechat/config/*.ini）定位数据目录；
    2. 从 Weixin.exe 进程内存中只读扫描 ``com.Tencent.WCDB.Config.Cipher``
       配置对象，提取每个数据库独立的 32 字节密钥（SQLCipher 4 格式，
       PBKDF2-HMAC-SHA512, 256000 迭代）；
    3. 按页解密数据库到临时目录（带缓存），再用标准 sqlite3 查询。

限制：
    - 微信必须处于登录状态（密钥存在于进程内存中，首次提取后本地缓存）；
    - 合并 -wal 时若微信正在 checkpoint，可能触发一次全量重建重试；
    - 仅支持读取，不支持发送。
"""

from __future__ import annotations

import contextlib
import ctypes
import glob
import hashlib
import hmac as hmac_mod
import json
import os
import queue
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import traceback
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE_SZ = 4096
RESERVE_SZ = 80  # IV(16) + HMAC(64)
STAMP_VERSION = 2  # 解密缓存 stamp 格式版本，改合并逻辑时递增以强制重建
# 后台重建节流：同一分片距上次重建完成不足该间隔时直接沿用旧缓存，
# 避免微信活跃期无限重建（新消息最迟在下一次重建后可见）
REBUILD_MIN_INTERVAL_SEC = 8.0
# 后台重建的全局并发上限。10 个消息分片同时重建会互相抢 I/O，
# 每个分片的重建都因此被拖慢十几倍（线上实测 50 页的 contact.db 也需 106s），
# 拿不到配额的分片本轮直接跳过、下轮再试，不阻塞轮询线程。
MAX_CONCURRENT_REBUILDS = 2
# 单次后台重建的最大尝试次数。注意：确定性错误（如 key 类型错、磁盘写满）
# 也会重试满 3 次，靠 REBUILD_MIN_INTERVAL_SEC（8s）的失败退避兜住成本；
# 若将来把退避调小，需同步评估这里，否则会变成每轮白烧 3 次。
BG_REBUILD_ATTEMPTS = 3
# 源库仍在写入时的快速重试间隔：快照注定过期，没必要按撕裂退避等待
FAST_RETRY_INTERVAL_SEC = 0.2
# 缓存陈旧告警：源库有变化但缓存超过该秒数未成功 promote 时告警，
# 同一分片最多每 CACHE_STALE_WARN_INTERVAL_SEC 秒告警一次
CACHE_STALE_WARN_SEC = 120.0
CACHE_STALE_WARN_INTERVAL_SEC = 60.0
# last_error 会进 cache_status() → /api/status，必须限长（traceback 可达数 KB）
MAX_CACHE_ERROR_LEN = 800
# 临时缓存晋升为正式缓存的重试：Windows 上只要本进程还有只读连接持有
# 目标文件，os.replace 就会 PermissionError。轮询每轮都会短暂打开缓存，
# 因此把窗口放细（60×0.2s=12s），比原来的 20×0.5s 更容易挑到空档。
PROMOTE_ATTEMPTS = 60
PROMOTE_RETRY_INTERVAL_SEC = 0.2
# 全局重建配额（进程内共享，跨分片生效）
_REBUILD_SEMAPHORE = threading.Semaphore(MAX_CONCURRENT_REBUILDS)
# 惰性状态初始化锁（兼容 __new__ 构造的实例）
_CACHE_STATE_LOCK = threading.Lock()
CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
HEX_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")

# 主密钥 cfg 提取(ReadWeixinKey-rev 同源, 每版本需重采锚点):
#   weixin.dll 特征码(sub_1803308D0 机器码前缀, 其后 4×movabs 立即数 = XOR 材料)
MASTER_DLL_PATTERN = bytes.fromhex(
    "83ec404889d64889cb0f57c00f1142100f11024c8bb1c8020000"
    "4883b9d0020000107209488b9bb8020000eb074881c3b8020000"
    "4d85f60f880a0200004983fe10736d4c89761048c746180f0000"
    "000f10030f110648b8"
)
MASTER_DLL_VERIFY = (b"488944242048b8", b"488944242848b8", b"488944243048b8")
CFG_LANDMARK = b"global_config"   # cfg 对象地标字符串(SSO 内联)
CFG_PTR_BACK = 0x138              # 地标前指针链回退偏移(版本敏感: 4.1.10.31=0x130)
CFG_OFFSET = 0x68                 # v18 → cfg 指针偏移(版本敏感)
CFG_DWORD_OFF = 0x40              # cfgDword(图片密钥派生源)
CFG_WXID_OFF = 0x48               # wxId std::string
CFG_CIPHER_OFF = 0x2B8            # dbKey 密文 std::string

MSG_TYPE_NAMES = {
    1: "文本",
    3: "图片",
    34: "语音",
    43: "视频",
    47: "动画表情",
    48: "位置",
    49: "文件/链接/卡片",
    10000: "系统消息",
}


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("__alignment1", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("__alignment2", wintypes.DWORD),
    ]


class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.VirtualQueryEx.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(_MBI), ctypes.c_size_t,
]
_k32.VirtualQueryEx.restype = ctypes.c_size_t
_k32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _ts_eq(a, b) -> bool:
    """比较两个文件时间戳是否"同一个"。

    不能直接用 ``==``：stamp 历史上用 ``%f`` 写 mtime（只保留 6 位小数），
    而 ``os.path.getmtime`` 返回的 float 有更多有效位，例如源库
    ``1794190031.4439065`` 写成 ``1794190031.443907``，读回与原值不再
    相等。结果 ``_open`` 每轮都判定"源库已变化"，对每个分片无限触发
    "增量 0 页"重建（9 个分片每 6 秒一轮，白烧 CPU/磁盘并挤占重建配额）。

    容差 1e-6 秒：大于 ``%f`` 的最大舍入误差（5e-7），远小于任何真实的
    文件修改间隔（NTFS 时间精度 100ns，实际写入间隔至少毫秒级）。
    """
    try:
        return abs(float(a) - float(b)) <= 1e-6
    except (TypeError, ValueError):
        return False


def _one_line(text, limit: int = 300) -> str:
    """把多行错误（含 traceback）压成单行并截断。

    错误串会同时进 stderr 与 ``cache_status()``（再进 ``/api/status``），
    多行 traceback 直接塞进去会把日志刷屏、把响应撑大，因此统一压行截断。
    """
    if not text:
        return ""
    flat = " | ".join(line.strip() for line in str(text).splitlines()
                      if line.strip())
    if len(flat) <= limit:
        return flat
    # traceback 的关键信息在尾部（最深帧 + 异常原始消息），只截头会把最有用
    # 的部分砍掉，导致 /api/status 里只剩 _bg_rebuild 入口帧、无法定位。
    # 因此保留"首 + 尾"，中间省略。
    tail = min(500, limit // 2)
    head = max(0, limit - tail - 3)
    return flat[:head] + "..." + flat[-tail:]


# 微信内置系统/官方账号（不属于好友，get_contacts(kind="friend") 时排除）
_SYSTEM_USERNAMES = frozenset({
    "filehelper", "fmessage", "medianote", "qqmail", "qqsync", "qmessage",
    "tmessage", "weibo", "weixin", "weixinreminder", "weixinteam",
    "floatbottle", "bulletmessage", "newsapp", "brandsessionholder",
    "notification_messages", "exmail_tool", "weixin_bonus",
})


def _is_malformed(exc) -> bool:
    """是否 SQLite "database disk image is malformed" 类库损坏（数据页损坏）。"""
    return isinstance(exc, sqlite3.DatabaseError) and "malformed" in str(exc).lower()


def _pbkdf2(passwd: bytes, salt: bytes, iters: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", passwd, salt, iters, dklen=32)


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(data) + dec.finalize()


def _split_key(key: bytes) -> Tuple[bytes, Optional[bytes]]:
    """拆分密钥形态：32 字节 = 标准裸 key（salt 用文件头前 16 字节）；
    48 字节 = SQLCipher 4 "Raw Key with Explicit Salt"（前 32B key + 后 16B salt，
    用于 cipher_plaintext_header_size 明文头模式）。返回 (enc_key, salt_or_None)。"""
    if len(key) == 48:
        return key[:32], key[32:]
    return key, None


def _verify_enc_key(enc_key: bytes, page1: bytes, salt: Optional[bytes] = None) -> bool:
    """验证 enc_key 是否为 page1 的 SQLCipher 4 密钥。

    - enc_key 为 32 字节裸 key：默认用文件头前 16 字节作 salt（标准形式）；
    - enc_key 为 48 字节 key+salt：显式 salt 优先于文件头（明文头模式）；
    - 也允许单独传 salt 参数覆盖（供提取逻辑按 96hex 拆分尝试）。
    """
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
    hmac_data = page1[16: PAGE_SZ - RESERVE_SZ + 16]
    stored_hmac = page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac


def _sqlite_text_factory(data: bytes):
    """sqlite TEXT 列解码：合法 UTF-8 返回 str，否则原样返回 bytes（图片等二进制内容）"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data


# ---------------------------------------------------------------------------
# 主密钥 cfg 提取(ReadWeixinKey-rev 同源; 锚点每版本重采)
# ---------------------------------------------------------------------------
def _find_weixin_module(pid: int) -> Optional[Tuple[int, int, str]]:
    """返回 (模块基址, 模块大小, 路径); weixin.dll 为微信 4.x 主模块"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.Module32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MODULEENTRY32W)]
    k32.Module32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MODULEENTRY32W)]
    snap = k32.CreateToolhelp32Snapshot(0x08 | 0x10, pid)
    if not snap or snap == -1 or snap == (1 << 64) - 1:
        return None
    try:
        me = _MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(me)
        ok = k32.Module32FirstW(snap, ctypes.byref(me))
        while ok:
            if me.szModule and me.szModule.lower() == "weixin.dll":
                return (me.modBaseAddr or 0), me.modBaseSize, me.szExePath
            me.dwSize = ctypes.sizeof(me)
            ok = k32.Module32NextW(snap, ctypes.byref(me))
    finally:
        k32.CloseHandle(snap)
    return None


def _extract_movabs_xor_key(dll_path: str) -> Optional[bytes]:
    """从 weixin.dll 特征码后提取 4×movabs 立即数拼接成 32 字节 XOR 材料。

    特征码(sub_1803308D0 前缀)后紧跟 4 个 '48 b8 <imm64>' movabs 指令,
    立即数即主密钥的 XOR 材料。小版本通常不改此代码段, 大版本需重采。
    """
    try:
        with open(dll_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    hit = data.find(MASTER_DLL_PATTERN)
    if hit < 0:
        return None
    take = min(200, len(data) - hit)
    hex_txt = data[hit:hit + take].hex()
    hex_txt = hex_txt[len(MASTER_DLL_PATTERN) * 2:]  # 丢弃特征码自身
    key = ""
    for vf in MASTER_DLL_VERIFY:
        if len(hex_txt) < 30 or hex_txt[16:30] != vf.decode():
            return None
        key += hex_txt[0:16]
        hex_txt = hex_txt[30:]
    if len(hex_txt) < 16:
        return None
    key += hex_txt[0:16]
    try:
        return bytes.fromhex(key)
    except ValueError:
        return None


def _read_remote_string(h, read, addr: int) -> str:
    """跨进程读 MSVC x64 std::string(SSO: size<=15 内联, 否则堆指针)"""
    sz_buf = read(addr + 16, 8)
    if not sz_buf:
        return ""
    size = struct.unpack_from("<Q", sz_buf)[0]
    if size <= 0 or size > 0x7FFFFFFF:
        return ""
    if size <= 15:
        data = read(addr, size)
    else:
        p_buf = read(addr, 8)
        if not p_buf:
            return ""
        data = read(struct.unpack_from("<Q", p_buf)[0], size)
    if not data:
        return ""
    return data[:size].decode("utf-8", "replace")


def _read_remote_bytes(h, read, addr: int) -> Optional[bytes]:
    """跨进程读字节缓冲(std::string 布局: data@+0, size@+16, cap@+24)"""
    sz_buf = read(addr + 16, 8)
    if not sz_buf:
        return None
    size = struct.unpack_from("<Q", sz_buf)[0]
    if size <= 0 or size > 0x400:
        return None
    cap_buf = read(addr + 24, 4)
    cap = struct.unpack_from("<I", cap_buf)[0] if cap_buf else 0
    if (cap | 0xF) == 0xF:
        data = read(addr, size)          # SSO 内联
    else:
        p_buf = read(addr, 8)
        if not p_buf:
            return None
        data = read(struct.unpack_from("<Q", p_buf)[0], size)
    if not data or len(data) != size:
        return None
    return data[:size]


def extract_master_key_from_cfg(pid: int) -> Optional[Tuple[str, int, str]]:
    """从 Weixin.exe 进程提取 (主密钥hex, cfgDword, wxId)。

    流程(ReadWeixinKey-rev 同源): 整块读 weixin.dll 映像 → 扫 global_config
    SSO 地标 → 指针链 cfg → 读 cfg+0x2B8 密文与 cfg+0x40 cfgDword →
    密文 XOR DLL movabs 材料得主密钥。锚点(CFG_PTR_BACK/CFG_OFFSET/特征码)
    每版本重采; 提取失败返回 None。
    """
    base, mod_size, dll_path = _find_weixin_module(pid) or (0, 0, "")
    if not base or not dll_path or mod_size <= 0 or mod_size >= 0x40000000:
        return None
    h = _k32.OpenProcess(0x0010 | 0x0400, False, pid)
    if not h:
        return None
    try:
        def read(addr: int, n: int):
            buf = ctypes.create_string_buffer(n)
            br = ctypes.c_size_t(0)
            if _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(br)) and br.value:
                return buf.raw[: br.value]
            return None

        image = read(base, mod_size)
        if not image or len(image) != mod_size:
            return None
        # 扫 global_config SSO 地标(size==13@+16, cap==15@+24, 内容内联@+0)
        pos = -1
        for i in range(len(image) - 8, 0, -8):
            if struct.unpack_from("<I", image, i)[0] == len(CFG_LANDMARK):
                cap = struct.unpack_from("<I", image, i + 8)[0]
                if cap and (cap | 0xF) == 0xF and i - 16 >= 0:
                    if image[i - 16:i - 16 + len(CFG_LANDMARK)] == CFG_LANDMARK:
                        pos = i
                        break
        if pos < 0:
            return None
        # 指针链: v18 = *(base + pos - CFG_PTR_BACK); cfg = *(v18 + CFG_OFFSET)
        v18_buf = read(base + pos - CFG_PTR_BACK, 8)
        if not v18_buf:
            return None
        v18 = struct.unpack_from("<Q", v18_buf)[0]
        cfg_buf = read(v18 + CFG_OFFSET, 8)
        if not cfg_buf:
            return None
        cfg = struct.unpack_from("<Q", cfg_buf)[0]
        if not (0x10000 <= cfg < 0x800000000000):
            return None
        # cfgDword + wxId
        dw_buf = read(cfg + CFG_DWORD_OFF, 4)
        cfg_dword = struct.unpack_from("<I", dw_buf)[0] if dw_buf else 0
        wxid = _read_remote_string(h, read, cfg + CFG_WXID_OFF)
        # dbKey 密文
        cipher = _read_remote_bytes(h, read, cfg + CFG_CIPHER_OFF)
        if not cipher:
            return None
        material = _extract_movabs_xor_key(dll_path)
        if not material or len(material) != len(cipher):
            return None
        master = bytes(a ^ b for a, b in zip(cipher, material))
        return master.hex(), cfg_dword, wxid
    finally:
        _k32.CloseHandle(h)


def _decrypt_page(enc_key: bytes, page: bytes, pgno: int) -> bytes:
    iv = page[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        # 48 字节 key（key+salt）= cipher_plaintext_header_size 明文头模式：
        # 页 1 前 16 字节是明文头（非加密 salt），加密数据从 offset 16 开始；
        # 解密后拼接明文头 + 明文数据 + reserve。
        if len(enc_key) == 48:
            enc = page[16: PAGE_SZ - RESERVE_SZ]
            return page[:16] + _aes_cbc_decrypt(enc_key[:32], iv, enc) + b"\x00" * RESERVE_SZ
        enc = page[16: PAGE_SZ - RESERVE_SZ]
        return b"SQLite format 3\x00" + _aes_cbc_decrypt(enc_key, iv, enc) + b"\x00" * RESERVE_SZ
    enc = page[: PAGE_SZ - RESERVE_SZ]
    return _aes_cbc_decrypt(enc_key[:32] if len(enc_key) == 48 else enc_key, iv, enc) + b"\x00" * RESERVE_SZ


def _extract_text_from_blob(content: bytes) -> Optional[str]:
    """从微信消息容器头中还原 UTF-8 明文文本。

    微信 4.x 部分文本消息的 message_content 为「容器头(0x28 b5 2f fd...)
    + UTF-8 明文 + 尾部填充(\x01\x00...)」，多数消息明文从第 10 字节开始；
    长消息可能加密，无法还原返回 None。
    """
    def _try_off(off: int) -> Optional[str]:
        if off >= len(content):
            return None
        chunk = content[off:]
        if b"\x01\x00" in chunk:
            chunk = chunk.split(b"\x01\x00")[0]
        try:
            t = chunk.decode("utf-8")
        except UnicodeDecodeError:
            return None
        # 保留换行符(\n和\r)，删除其他控制字符
        t = re.sub(r"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f]+", "", t).strip()
        if not t:
            return None
        if not re.search(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]", t):
            return None
        printable = sum(1 for ch in t if ch.isprintable())
        if printable / len(t) < 0.6:
            return None
        return t

    t = _try_off(10)
    if t:
        return t
    for off in range(0, min(16, len(content))):
        if off == 10:
            continue
        t = _try_off(off)
        if t:
            return t
    return None


_ZSTD_MODULE = None


def _get_zstd_module():
    """惰性加载 zstd 模块，兼容 `zstandard` / `zstd` 两种包名。

    WeChat 4.x 长文本消息的 message_content 为 zstd 压缩帧；缺少该第三方
    库时无法解压（会退化为 `[类型]` 占位符）。此函数做了延迟导入 + 双包名
    兼容，缺失库时返回 None（由调用方决定如何兜底）。
    """
    global _ZSTD_MODULE
    if _ZSTD_MODULE is not None:
        return _ZSTD_MODULE
    for mod_name in ("zstandard", "zstd"):
        try:
            _ZSTD_MODULE = __import__(mod_name)
            return _ZSTD_MODULE
        except Exception:
            continue
    _ZSTD_MODULE = False
    return None


def _zstd_decompress(zstd, content: bytes) -> Optional[str]:
    """用 zstd 解压微信消息帧并解码 UTF-8，失败返回 None。"""
    if not content:
        return None
    try:
        dctx = zstd.ZstdDecompressor()
        decompressed = dctx.decompress(content, max_output_size=200000)
        if not decompressed:
            return None
        text = decompressed.decode("utf-8", "ignore").strip()
        return text if text else None
    except Exception:
        return None



def _find_account_dirs(db_dir: str) -> List[str]:
    """列出 db_dir 下所有含 db_storage 子目录的账号目录。

    微信号目录不一定以 wxid_ 开头（如自定义微信号），这里只依赖
    db_storage 子目录的存在性判断。
    """
    out = []
    try:
        for name in os.listdir(db_dir):
            p = os.path.join(db_dir, name, "db_storage")
            if os.path.isdir(p):
                out.append(os.path.join(db_dir, name))
    except OSError:
        pass
    return sorted(out)


class WeChatDB:
    """微信 4.x 本地数据库读取器"""

    def __init__(
        self,
        db_dir: Optional[str] = None,
        keys_file: Optional[str] = None,
        workdir: Optional[str] = None,
        account: Optional[str] = None,
        master_key: Optional[str] = None,
    ):
        self.db_dir = db_dir or auto_detect_db_dir()
        if not self.db_dir:
            raise RuntimeError("未找到微信数据库目录，请通过 db_dir 参数手动指定")
        self.account = account or self._pick_account()
        self.account_dir = os.path.join(self.db_dir, self.account)
        self.workdir = workdir or os.path.join(
            tempfile.gettempdir(), "wechatauto_db", self.account
        )
        self.keys_file = keys_file or os.path.join(self.workdir, "keys.json")
        self._keys: Dict[str, bytes] = {}
        self._db_files = self._collect_db_files()
        self.master_key: Optional[str] = None
        self.cfg_dword: Optional[int] = None
        self._bad_shards: Dict[str, float] = {}  # rel -> marked-bad timestamp
        self._bad_shards_mtime: Dict[str, float] = {}  # rel -> source mtime when marked bad
        # 后台重建解密缓存：rel -> 去重集合 / 全局守卫锁 / 分片互斥锁
        self._rebuilding: set = set()
        self._rebuild_guard = threading.Lock()
        self._rebuild_locks: Dict[str, threading.Lock] = {}
        self._rebuild_done_at: Dict[str, float] = {}  # rel -> 上次重建完成时刻
        # 缓存健康度（供 cache_status()/陈旧告警使用，全部为墙钟时间）
        self._cache_ok_at: Dict[str, float] = {}        # rel -> 上次成功 promote
        self._cache_first_seen: Dict[str, float] = {}   # rel -> 首次尝试重建
        self._cache_last_error: Dict[str, str] = {}     # rel -> 最近一次失败原因
        self._cache_fail_streak: Dict[str, int] = {}    # rel -> 连续失败次数
        self._stale_warned_at: Dict[str, float] = {}    # rel -> 上次陈旧告警时刻
        self._rebuild_skipped_at: Dict[str, float] = {}  # rel -> 上次因配额跳过
        self._rebuild_sem = _REBUILD_SEMAPHORE
        self._cache_state_ready = True
        self._load_or_extract_keys(master_key=master_key)

    # ------------------------------------------------------------------
    # 账号与数据库文件
    # ------------------------------------------------------------------
    def _pick_account(self) -> str:
        candidates = []
        for d in _find_account_dirs(self.db_dir):
            if os.path.isdir(os.path.join(d, "db_storage")):
                recent = max(
                    (
                        os.path.getmtime(os.path.join(root, f))
                        for root, _, files in os.walk(os.path.join(d, "db_storage"))
                        for f in files
                        if f.endswith(".db") and not f.endswith("-wal")
                    ),
                    default=0,
                )
                candidates.append((recent, os.path.basename(d)))
        if not candidates:
            raise RuntimeError("未找到任何已登录账号的数据库")
        candidates.sort(reverse=True)
        return candidates[0][1]

    def _collect_db_files(self) -> List[Tuple[str, str, int]]:
        files = []
        base = os.path.join(self.account_dir, "db_storage")
        for root, _, names in os.walk(base):
            # migrate 目录下的 unspportmsg.db 是微信保留的未支持消息库，
            # 进程内存中不存在对应密钥、代码从不访问，排除以免误触发密钥提取
            if os.path.normcase(os.path.relpath(root, base)).startswith("migrate"):
                continue
            for name in names:
                if not name.endswith(".db") or name.endswith("-wal") or name.endswith("-shm"):
                    continue
                path = os.path.join(root, name)
                files.append((os.path.relpath(path, base), path, os.path.getsize(path)))
        return files

    @property
    def wxid(self) -> str:
        """当前账号的微信号（去掉目录名末尾的 4 位哈希后缀）"""
        return re.sub(r"_\w{4}$", "", self.account)

    def get_self_info(self) -> dict:
        """当前登录账号的昵称等信息"""
        for rel, path, _ in self._db_files:
            if os.path.basename(path) != "contact.db":
                continue
            conn = self._open(rel)
            row = conn.execute(
                "SELECT username, nick_name, remark FROM contact WHERE username=? LIMIT 1",
                (self.wxid,),
            ).fetchone()
            if row:
                return {"username": row[0], "nick_name": row[1], "remark": row[2]}
        return {"username": self.wxid, "nick_name": "", "remark": ""}

    # ------------------------------------------------------------------
    # 密钥提取
    # ------------------------------------------------------------------
    KDF_ITER = 256000  # 主密钥→库密钥 PBKDF2 迭代(微信魔改 WCDB, 实测确认)

    def _load_or_extract_keys(self, master_key: Optional[str] = None) -> None:
        """加载/提取密钥, 五层优先级:

        1. 显式 master_key(构造参数) → 主密钥派生;
        2. 本地缓存 keys.json(已验证);
        3. Config.Cipher 内存扫描(4.1+ 主路径);
        4. cfg 自动提取(老版本回退);
        5. 密钥提取(最终回退)。

        派生/扫描结果均经 SQLCipher4 页1 HMAC 强校验, 零误报。
        """
        if master_key:
            self._keys.update(self.derive_keys_from_master(master_key))
            self.master_key = master_key
            self.cfg_dword = None
            self._save_keys()
        else:
            self.master_key = None
            self.cfg_dword = None
            
            # 优先级1: 尝试已保存的密钥
            if os.path.exists(self.keys_file):
                try:
                    with open(self.keys_file, "r", encoding="utf-8") as f:
                        saved = json.load(f)
                    for rel, hexkey in saved.items():
                        try:
                            self._keys[rel] = bytes.fromhex(hexkey)
                        except ValueError:
                            pass
                except (json.JSONDecodeError, OSError):
                    pass
            
            missing = [
                rel for rel, path, _ in self._db_files
                if rel not in self._keys or not self._key_works(rel)
            ]
            
            # 优先级2: Config.Cipher 内存扫描(4.1+ 主路径)
            if missing:
                extracted = self.extract_keys()
                self._keys.update(extracted)
                self._save_keys()
            
            # 尝试提取cfg_dword用于图片密钥派生
            if self.cfg_dword is None:
                auto = self.extract_master_key()
                if auto:
                    _, cfg_dword, _ = auto
                    self.cfg_dword = cfg_dword
            
            missing = [
                rel for rel, path, _ in self._db_files
                if rel not in self._keys or not self._key_works(rel)
            ]
            
            # 优先级3: cfg 自动提取(老版本回退)
            if missing:
                auto = self.extract_master_key()
                if auto:
                    master, cfg_dword, _ = auto
                    self.master_key = master
                    self.cfg_dword = cfg_dword
                    self._keys.update(self.derive_keys_from_master(master))
                    self._save_keys()
        still = [
            rel for rel, _, _ in self._db_files if not self._key_works(rel)
        ]
        if still:
            import sys as _sys
            print(
                "[wechatauto] 警告: 以下库无可用密钥，无法解密: %s"
                % ", ".join(still),
                file=_sys.stderr,
            )
            print(
                "[wechatauto] 已加载 %d/%d 个密钥 (缓存: %s)。请确认微信已登录，"
                "可运行 python -m wechatauto.diagnose_keys 排查"
                % (len(self._keys) - len(still), len(self._db_files), self.keys_file),
                file=_sys.stderr,
            )
        self.unkeyed = still

    def derive_keys_from_master(self, master_hex: str) -> Dict[str, bytes]:
        """主密钥派生逐库密钥: PBKDF2-HMAC-SHA512(主密钥, 库头salt, KDF_ITER)。

        微信 4.x 为单一主密钥 + 每库随机 salt 派生独立库密钥(SQLCipher4
        passphrase 语义)。仅返回通过页1 HMAC 校验的派生密钥。
        """
        try:
            master = bytes.fromhex(master_hex)
        except ValueError:
            raise ValueError("主密钥必须为 64 位 hex 字符串")
        if len(master) != 32:
            raise ValueError("主密钥必须为 32 字节(64 位 hex)")
        keys: Dict[str, bytes] = {}
        for rel, path, _ in self._db_files:
            try:
                with open(path, "rb") as f:
                    page1 = f.read(PAGE_SZ)
            except OSError:
                continue
            if len(page1) < PAGE_SZ:
                continue
            derived = _pbkdf2(master, page1[:16], self.KDF_ITER)
            if _verify_enc_key(derived, page1):
                keys[rel] = derived
        return keys

    def extract_master_key(self) -> Optional[Tuple[str, int, str]]:
        """从 Weixin.exe 进程 cfg 自动提取 (主密钥hex, cfgDword, wxId)。

        遍历微信进程调 extract_master_key_from_cfg; 主密钥可离线派生全部库。
        微信未运行或锚点漂移(版本变更)时返回 None, 由调用方回退。
        """
        pids = self._find_weixin_pids()
        for pid in pids:
            got = extract_master_key_from_cfg(pid)
            if got:
                return got
        return None

    def _key_works(self, rel: str) -> bool:
        key = self._keys.get(rel)
        if not key:
            return False
        path = self._db_path(rel)
        try:
            with open(path, "rb") as f:
                page1 = f.read(PAGE_SZ)
        except OSError:
            return False
        return _verify_enc_key(key, page1)

    def _db_path(self, rel: str) -> str:
        for r, path, _ in self._db_files:
            if r == rel:
                return path
        raise KeyError(rel)

    def extract_keys(self) -> Dict[str, bytes]:
        """从 Weixin.exe 进程内存扫描 Config.Cipher 对象，提取各库密钥"""
        pids = self._find_weixin_pids()
        if not pids:
            raise RuntimeError("未检测到 Weixin.exe，请先登录微信再运行")
        keys: Dict[str, bytes] = {}
        tested: set = set()
        for pid in pids:
            keys.update(self._extract_keys_pid(pid, tested))
            if len(keys) >= len(self._db_files):
                break
        return keys

    def _find_weixin_pids(self) -> List[int]:
        """查找 Weixin.exe 进程 PID。

        优先使用 psutil（基于原生 Windows API，不依赖 WMI），
        若 psutil 不可用则回退到 tasklist（不带 /FI 过滤以避免 WMI 依赖）。
        """
        try:
            import psutil
            return [
                p.info["pid"]
                for p in psutil.process_iter(["pid", "name"])
                if p.info["name"] and p.info["name"].lower() == "weixin.exe"
            ]
        except Exception:
            pass
        # psutil 不可用时的兜底：tasklist 不带 /FI（避免 WMI "Invalid class" 错误）
        import subprocess
        try:
            r = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            return []
        pids = []
        for line in r.stdout.strip().splitlines():
            parts = line.strip('"').split('","')
            if len(parts) >= 2 and parts[0].lower() == "weixin.exe" and parts[1].isdigit():
                pids.append(int(parts[1]))
        return pids

    def _extract_keys_pid(self, pid: int, tested: set) -> Dict[str, bytes]:
        h = _k32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            return {}
        try:
            def read(addr: int, n: int):
                buf = ctypes.create_string_buffer(n)
                br = ctypes.c_size_t(0)
                if _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(br)) and br.value:
                    return buf.raw[: br.value]
                return None

            needles = self._find_bytes(h, read, CONFIG_CIPHER_NAME)
            pairs = [
                struct.pack("<Q", addr) + struct.pack("<Q", len(CONFIG_CIPHER_NAME))
                for addr in needles
            ]
            keys: Dict[str, bytes] = {}
            for pair in pairs:
                for qaddr in self._find_bytes(h, read, pair):
                    node = read(qaddr - 0x10, 0x50)
                    if not node or len(node) < 0x40:
                        continue
                    if struct.unpack_from("<Q", node, 0x10)[0] not in needles:
                        continue
                    if struct.unpack_from("<Q", node, 0x18)[0] != len(CONFIG_CIPHER_NAME):
                        continue
                    config_ptr = struct.unpack_from("<Q", node, 0x28)[0]
                    if not (0x10000 <= config_ptr < 0x800000000000):
                        continue
                    obj = read(config_ptr + 0x88, 0x28)
                    if not obj or len(obj) < 0x18:
                        continue
                    data_ptr = struct.unpack_from("<Q", obj, 0x8)[0]
                    data_len = struct.unpack_from("<Q", obj, 0x10)[0]
                    if not (0 < data_len <= 1024 and 0x10000 <= data_ptr < 0x800000000000):
                        continue
                    blob = read(data_ptr, int(data_len))
                    if not blob or len(blob) != data_len:
                        continue
                    decoded = bytes(
                        v ^ CONFIG_XOR_MASK[i % len(CONFIG_XOR_MASK)]
                        for i, v in enumerate(blob)
                    )
                    for m in HEX_LITERAL_RE.finditer(decoded):
                        run = m.group(1).decode().lower()
                        starts = [0]
                        if len(run) > 96:
                            starts += list(range(0, len(run) - 63, 32))
                            starts.append(len(run) - 64)
                        for s in dict.fromkeys(starts):
                            if s + 64 > len(run):
                                continue
                            cand = bytes.fromhex(run[s:s + 64])
                            if cand in tested or not self._probable_key(cand):
                                continue
                            tested.add(cand)
                            # 96hex 形式：后 32 hex 是显式 salt（Raw Key with
                            # Explicit Salt），与文件头 salt 都要尝试
                            explicit = None
                            if s + 96 <= len(run):
                                explicit = bytes.fromhex(run[s + 64: s + 96])
                            salt_choices = [None]
                            if explicit:
                                salt_choices.append(explicit)
                            for salt_opt in salt_choices:
                                for rel, path, _ in self._db_files:
                                    if rel in keys:
                                        continue
                                    with open(path, "rb") as f:
                                        page1 = f.read(PAGE_SZ)
                                    if _verify_enc_key(cand, page1, salt=salt_opt):
                                        # 显式 salt 通过 → 存 key+salt（明文头模式）；
                                        # 文件头 salt 通过 → 存裸 key
                                        keys[rel] = cand + (salt_opt or b"")
                                        break
        finally:
            _k32.CloseHandle(h)
        return keys

    @staticmethod
    def _probable_key(b: bytes) -> bool:
        return (
            len(b) == 32
            and len(set(b)) >= 15
            and b not in {b"\x00" * 32, b"\xff" * 32}
        )

    @staticmethod
    def _find_bytes(h, read, needle: bytes) -> List[int]:
        hits = []
        addr = 0
        while True:
            mbi = _MBI()
            r = _k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if r == 0:
                break
            if (
                mbi.State == 0x1000
                and (mbi.Protect & 0xFF) & 0xE6
                and not (mbi.Protect & 0x100)
                and 0 < mbi.RegionSize < 0x10000000
            ):
                buf = read(mbi.BaseAddress or 0, mbi.RegionSize)
                if buf:
                    base = mbi.BaseAddress or 0
                    pos = 0
                    while True:
                        pos = buf.find(needle, pos)
                        if pos < 0:
                            break
                        hits.append(base + pos)
                        pos += 1
            addr = (mbi.BaseAddress or 0) + mbi.RegionSize
        return hits

    def _save_keys(self) -> None:
        os.makedirs(os.path.dirname(self.keys_file), exist_ok=True)
        with open(self.keys_file, "w", encoding="utf-8") as f:
            json.dump({k: v.hex() for k, v in self._keys.items()}, f, indent=2)

    # ------------------------------------------------------------------
    # 解密与查询
    # ------------------------------------------------------------------
    WAL_HEADER_SZ = 32   # WCDB WAL 文件头
    WAL_FRAME_SZ = 4120  # 帧头 24 字节(大端 pgno + 校验等) + 4096 加密页

    def _auto_diagnose_key_failure(self, rel: str) -> None:
        """密钥缺失报错时的自动诊断，向 stderr 输出三项最常见根因：
        1) Python 位数（32 位 Python 读不了 64 位微信进程内存）；
        2) 微信进程读取权限（管理员权限不匹配 → OpenProcess 失败 → 0 密钥）；
        3) 多账号目录与所选账号对比（选错账号 → 密钥验证不过）。
        """
        print("[wechatauto] 密钥诊断: '%s' 无可用密钥" % rel, file=sys.stderr)
        bits = 64 if sys.maxsize > 2**32 else 32
        if bits != 64:
            print("[wechatauto]  >> 当前 Python 是 %d 位，而微信 4.x 是 64 位进程。"
                  "请改用 64 位 Python（python -c \"import struct; print(struct.calcsize('P')*8)\" 应输出 64）"
                  % bits, file=sys.stderr)
        pids = []
        try:
            import psutil
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    if proc.info["name"] and proc.info["name"].lower() == "weixin.exe":
                        pids.append(proc.info["pid"])
                except Exception:
                    continue
        except Exception:
            pass
        if not pids:
            import subprocess as _sp
            try:
                r = _sp.run(
                    ["tasklist", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True,
                    creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
                )
                for line in r.stdout.strip().splitlines():
                    parts = line.strip('"').split('","')
                    if len(parts) >= 2 and parts[0].lower() == "weixin.exe" and parts[1].isdigit():
                        pids.append(int(parts[1]))
            except OSError:
                pass
        if not pids:
            print("[wechatauto]  >> 未检测到 Weixin.exe 进程，请先登录微信并保持窗口打开",
                  file=sys.stderr)
            return
        perms = []
        for pid in pids:
            h = _k32.OpenProcess(0x0010 | 0x0400, False, pid)
            if not h:
                err = ctypes.get_last_error()
                perms.append((pid, False, err))
            else:
                _k32.CloseHandle(h)
                perms.append((pid, True, 0))
        blocked = [p for p, ok, err in perms if not ok]
        if blocked:
            print("[wechatauto]  >> 部分微信进程无法读取内存 (PID %s，错误码 %s)："
                  "请用管理员身份运行 Python（若微信本身以管理员运行），"
                  "或取消微信的\"以管理员身份运行\"后重新登录"
                  % (", ".join(str(p) for p, _, _ in blocked),
                     ", ".join(str(e) for _, _, e in blocked)),
                  file=sys.stderr)
        accounts = [
            os.path.basename(d)
            for d in _find_account_dirs(self.db_dir)
        ]
        if len(accounts) > 1:
            print("[wechatauto]  >> 检测到多个微信账号目录: %s；当前自动选择: %s。"
                  "若报错，请用 WeChatDB(account=\"当前登录账号\") 显式指定"
                  % (", ".join(sorted(accounts)), self.account),
                  file=sys.stderr)
        print("[wechatauto] 密钥诊断完成，以上为自动检测结果。完整排查请运行 "
              "python -m wechatauto.diagnose_keys", file=sys.stderr)

    @staticmethod
    def _read_stamp(stamp: str) -> Optional[dict]:
        """读取解密缓存 stamp（不存在/损坏返回 None）。"""
        if not os.path.exists(stamp):
            return None
        try:
            with open(stamp, "r") as f:
                parts = f.read().split(",")
            old = {
                "ver": int(parts[0]),
                "mtime": float(parts[1]),
                "size": int(parts[2]),
                "wal_mtime": float(parts[3]),
                "wal_size": int(parts[4]),
                "applied": int(parts[5]),
            }
            if old["ver"] != STAMP_VERSION:
                return None
            return old
        except (ValueError, OSError, IndexError):
            return None

    @staticmethod
    def _write_stamp(stamp: str, src_mtime: float, src_size: int,
                     wal_mtime: float, wal_size: int, applied: int) -> None:
        """写入解密缓存 stamp。"""
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "w") as f:
            # 用 repr 写浮点保证可精确往返：%f 只有 6 位小数，会把
            # 1794190031.4439065 写成 ...443907，读回与原值不等，
            # 导致 _open 每轮都误判"源库已变化"而无限重建。
            f.write("%d,%r,%d,%r,%d,%d"
                    % (STAMP_VERSION, float(src_mtime), src_size,
                       float(wal_mtime), wal_size, applied))

    @staticmethod
    def _connect_ro(dst: str) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.text_factory = _sqlite_text_factory
        return conn

    @staticmethod
    def _cache_readable(dst: str) -> bool:
        """旧缓存能否作为只读库打开（快速校验 schema 可读）。"""
        try:
            conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
            try:
                conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            finally:
                conn.close()
            return True
        except sqlite3.Error:
            return False

    @staticmethod
    def _promote_cache(tmp: str, dst: str) -> bool:
        """把重建结果原子替换为正式缓存；目标被占用时短暂重试。

        成功返回 True；彻底失败删除临时文件并返回 False（保留旧缓存，
        stamp 不更新 → 下次打开自然再次触发重建）。
        源页哈希边车（.pages）随缓存一并替换，供下次增量重建使用。
        """
        last_err = ""
        for _ in range(PROMOTE_ATTEMPTS):
            try:
                os.replace(tmp, dst)
                if os.path.exists(tmp + ".pages"):
                    try:
                        os.replace(tmp + ".pages", dst + ".pages")
                    except OSError:
                        pass  # 边车替换失败：下次重建回退全量，不影响正确性
                return True
            except OSError as exc:
                last_err = "%s: %s" % (type(exc).__name__, exc)
                time.sleep(PROMOTE_RETRY_INTERVAL_SEC)
        # 失败时留下真实原因（如 PermissionError 表示缓存文件仍被只读连接
        # 占用），否则日志只能写“被占用？”，无法定位是谁占着。
        sys.stderr.write(
            "[wechatauto] %s 晋升失败原因：%s（重试 %d 次仍未释放）\n"
            % (os.path.basename(dst), last_err or "未知", PROMOTE_ATTEMPTS))
        for p in (tmp, tmp + ".pages"):
            try:
                os.remove(p)
            except OSError:
                pass
        return False

    def _rel_lock(self, rel: str) -> threading.Lock:
        """取指定分片的重建互斥锁（避免多线程同时写同一临时文件）。"""
        with self._rebuild_guard:
            lock = self._rebuild_locks.get(rel)
            if lock is None:
                lock = threading.Lock()
                self._rebuild_locks[rel] = lock
            return lock

    # ------------------------------------------------------------------
    # 缓存健康度（失败可见 + 陈旧告警）
    # ------------------------------------------------------------------
    def _ensure_cache_state(self) -> None:
        """惰性初始化缓存健康度字段。

        兼容两种实例：正常 ``__init__`` 构造，以及测试里 ``__new__``
        构造后只注入少量字段的实例（后者不应因缺字段而抛 AttributeError）。
        """
        if getattr(self, "_cache_state_ready", False):
            return
        with _CACHE_STATE_LOCK:
            if getattr(self, "_cache_state_ready", False):
                return
            if not hasattr(self, "_cache_ok_at"):
                self._cache_ok_at = {}
            if not hasattr(self, "_cache_first_seen"):
                self._cache_first_seen = {}
            if not hasattr(self, "_cache_last_error"):
                self._cache_last_error = {}
            if not hasattr(self, "_cache_fail_streak"):
                self._cache_fail_streak = {}
            if not hasattr(self, "_stale_warned_at"):
                self._stale_warned_at = {}
            if not hasattr(self, "_rebuild_skipped_at"):
                self._rebuild_skipped_at = {}
            if not hasattr(self, "_rebuild_sem") or self._rebuild_sem is None:
                self._rebuild_sem = _REBUILD_SEMAPHORE
            self._cache_state_ready = True

    def _record_cache_ok(self, rel: str) -> None:
        """记录一次成功的缓存 promote（刷新陈旧计时、清零连续失败）。"""
        self._ensure_cache_state()
        now = time.time()
        self._cache_ok_at[rel] = now
        self._cache_fail_streak[rel] = 0
        self._cache_last_error.pop(rel, None)

    def _record_cache_error(self, rel: str, reason: str) -> None:
        """记录一次缓存重建失败：连续失败次数 +1，保存最后一次原因。

        原因串会进 ``cache_status()`` → ``/api/status``，因此在此统一压成
        单行并截断到 ``MAX_CACHE_ERROR_LEN``（traceback 可达数 KB）。
        """
        self._ensure_cache_state()
        self._cache_last_error[rel] = (
            _one_line(reason, MAX_CACHE_ERROR_LEN) or "未知错误")
        self._cache_fail_streak[rel] = int(self._cache_fail_streak.get(rel, 0)) + 1

    def _maybe_warn_stale(self, rel: str) -> None:
        """源库已变化但缓存长期未成功更新时告警（每分片最多每 60 秒一条）。

        线上故障中重建连续失败却无任何日志，轮询读陈旧缓存恒返回空，
        watermark 不推进也没有告警——这条 WARNING 是最外层的兜底信号。
        """
        self._ensure_cache_state()
        now = time.time()
        ok_at = self._cache_ok_at.get(rel)
        if not ok_at:
            # 进程刚启动尚无成功记录：从第一次尝试重建开始计时，
            # 避免刚启动就误报陈旧
            ok_at = self._cache_first_seen.setdefault(rel, now)
        stale = now - ok_at
        if stale < CACHE_STALE_WARN_SEC:
            return
        last_warn = self._stale_warned_at.get(rel, 0.0)
        if now - last_warn < CACHE_STALE_WARN_INTERVAL_SEC:
            return
        self._stale_warned_at[rel] = now
        sys.stderr.write(
            "[wechatauto] WARNING %s 解密缓存已 %.0f 秒未成功更新（源库仍在变化），"
            "轮询可能读不到新消息；连续失败 %d 次，最后错误: %s\n"
            % (rel, stale, int(self._cache_fail_streak.get(rel, 0)),
               _one_line(self._cache_last_error.get(rel), 200) or "无")
        )

    def cache_status(self) -> List[dict]:
        """返回各分片解密缓存健康度（只读，供 /api/status 展示）。

        字段：
        - ``rel``            分片相对路径（如 ``message/message_8.db``）；
        - ``stale_seconds``  距上次成功 promote 的秒数，从未成功为 None；
        - ``last_ok_at``     最近一次成功 promote 的时刻（ISO8601，空 = 从未）；
        - ``last_error``     最近一次失败原因（空 = 无失败）；
        - ``fail_streak``    连续失败次数；
        - ``rebuilding``     当前是否正在后台重建。
        """
        self._ensure_cache_state()
        now = time.time()
        with self._rebuild_guard:
            rebuilding = set(self._rebuilding)
        rels = set(self._cache_ok_at) | set(self._cache_last_error) \
            | set(self._cache_first_seen) | rebuilding
        items: List[dict] = []
        for rel in sorted(rels):
            ok_at = self._cache_ok_at.get(rel)
            items.append({
                "rel": rel,
                "stale_seconds": (max(0.0, now - ok_at)
                                  if ok_at else None),
                "last_ok_at": (time.strftime("%Y-%m-%dT%H:%M:%S",
                                             time.localtime(ok_at))
                               if ok_at else ""),
                "last_error": self._cache_last_error.get(rel, ""),
                "fail_streak": int(self._cache_fail_streak.get(rel, 0)),
                "rebuilding": rel in rebuilding,
            })
        return items

    def _source_sig(self, src: str,
                    wal_path: Optional[str]) -> Tuple[float, int, bytes, int]:
        """采集源库一致性签名 ``(mtime, size, WAL salt, WAL 帧数)``。

        仅 4 次轻量 stat/read（不读页内容），用于在每轮重建后快速判断
        "源库是否仍在被写入"：一旦签名变化，本次快照必然过期，此时再做
        ``quick_check`` 整库校验纯属浪费（线上实测单轮 130s，三次约 396s）。
        """
        try:
            src_mtime = os.path.getmtime(src)
            src_size = os.path.getsize(src)
        except OSError:
            src_mtime, src_size = 0.0, -1
        salt = b""
        frames = 0
        if wal_path:
            try:
                wal_size = os.path.getsize(wal_path)
                frames = max(0, (wal_size - self.WAL_HEADER_SZ)
                             // self.WAL_FRAME_SZ)
                with open(wal_path, "rb") as wal:
                    header = wal.read(24)
                if len(header) >= 24:
                    salt = header[16:24]
            except OSError:
                salt, frames = b"", 0
        return (src_mtime, src_size, salt, frames)

    @contextlib.contextmanager
    def _syncing_cache(self, rel: str):
        """标记"同步重建中"，让 ``/api/status.cache[].rebuilding`` 也能看到。

        同步路径（``_open`` 里旧缓存不可用时的阻塞重建）与后台重建同样
        长时间占用 I/O，运维排查"消息停摆"时需要能区分"在重建"还是
        "卡死/无变化"。这里复用 ``_rebuilding`` 集合（只在其内增删，
        不新增任何锁获取），但仅在本方真正新增标记时才 discard，
        避免与 ``_bg_rebuild`` 的标记互相踩踏。
        """
        self._ensure_cache_state()
        with self._rebuild_guard:
            owned = rel not in self._rebuilding
            if owned:
                self._rebuilding.add(rel)
        try:
            yield
        finally:
            if owned:
                with self._rebuild_guard:
                    self._rebuilding.discard(rel)

    def _open(self, rel: str) -> sqlite3.Connection:
        """打开解密(并合并 -wal 增量)后的只读库。

        解密结果缓存到 workdir；主库或 WAL 有变化时：
        - 主库被 checkpoint 改写（mtime/size 变化）或 WAL 被重置 → 全量重建；
        - 仅 WAL 追加了新帧 → 增量合并新帧（秒级）。

        全量重建较慢（大分片几十秒），为此做两层优化：
        - 重建一律先写 .rebuild 临时文件、校验通过后原子替换正式缓存，
          失败不破坏现有缓存（旧缓存仍可服务查询，也避免反复全量重建）；
        - 旧缓存有效时「先用旧缓存响应 + 后台重建」：查询零阻塞，
          新消息在后台重建完成后的下一轮轮询即可读到。
        """
        if rel not in self._keys:
            self._auto_diagnose_key_failure(rel)
            raise RuntimeError(
                "数据库无可用密钥: %s。请确认微信已登录且保持窗口打开；"
                "若仍复现，运行 python -m wechatauto.diagnose_keys 并把完整输出发给维护者。"
                "也可删除密钥缓存强制重新提取后重试: %s"
                % (rel, self.keys_file)
            )
        src = self._db_path(rel)
        dst = os.path.join(self.workdir, rel.replace(os.sep, "__"))
        key = self._keys[rel]
        src_mtime = os.path.getmtime(src)
        src_size = os.path.getsize(src)
        wal_path = self._wal_path(rel)
        wal_mtime = os.path.getmtime(wal_path) if wal_path else 0.0
        wal_size = os.path.getsize(wal_path) if wal_path else 0
        stamp = dst + ".stamp"
        old = self._read_stamp(stamp)
        build = (not old or not _ts_eq(old["mtime"], src_mtime)
                 or old["size"] != src_size
                 or not _ts_eq(old["wal_mtime"], wal_mtime)
                 or old["wal_size"] != wal_size)
        if not build and not os.path.exists(dst):
            build = True  # stamp 残留但缓存文件丢失：强制重建
        if build:
            # 源库有变化却没能在阈值内更新缓存 → 打告警（每分片 60s 一条）
            self._maybe_warn_stale(rel)
        if build and old and self._cache_readable(dst):
            # 全量重建耗时长：立即返回旧缓存（稍旧数据），后台原子重建
            self._kick_rebuild(rel, src, dst, key,
                               src_mtime, src_size, wal_mtime, wal_size)
            return self._connect_ro(dst)
        tmp = dst + ".rebuild"
        rel_lock = self._rel_lock(rel)
        # 先拿分片锁再打"同步重建中"标记：标记只覆盖真正在重建的时间，
        # 不把"排队等锁"的时间算进去
        with rel_lock, self._syncing_cache(rel):
            attempt = 0
            while build:
                attempt += 1
                full = (not old or not _ts_eq(old["mtime"], src_mtime)
                        or old["size"] != src_size
                        or wal_size < old["wal_size"] or wal_size == 0)
                if full:
                    # 先构建到临时文件（页级增量优先）：校验通过才替换正式
                    # 缓存，失败不影响旧缓存
                    stats: dict = {}
                    try:
                        page_hashes, _how = self._build_cache(src, tmp, dst, key,
                                                              stats=stats)
                        with open(tmp + ".pages", "wb") as hf:
                            hf.write(page_hashes)
                    except (OSError, sqlite3.Error) as exc:
                        # 同步路径失败同样留痕（旧实现直接抛给上层，
                        # 日志里只剩"分片不可用"，看不出是重建出了问题）
                        self._record_cache_error(
                            rel, "构建异常 %s: %s" % (type(exc).__name__, exc))
                        sys.stderr.write(
                            "[wechatauto] %s 同步重建失败（%s: %s），"
                            "缓存保持旧版本并交给上层降级\n"
                            % (rel, type(exc).__name__, exc)
                        )
                        raise
                    applied = 0
                else:
                    stats = {"full": False, "patched": -1}
                    applied = old["applied"] if old else 0
                merged_pages = 0
                if wal_path and wal_size > self.WAL_HEADER_SZ:
                    applied, merged_pages = self._merge_wal(
                        tmp if full else dst, wal_path, key, applied)
                else:
                    applied = 0
                patched = int(stats.get("patched", -1))
                # 轮询场景下 quick_check 整库校验(大分片秒级)是延迟大头，
                # 在可证明安全时跳过：仅限增量路径——页级重建未改写任何页、
                # WAL 合并也未写入任何页，缓存与上次已验证状态完全一致。
                # 全量解密绝不跳过：微信 checkpoint 写库期间 mtime 可能不
                # 更新，mtime 稳定不能证明源库页一致，实测会解出撕裂页
                # （曾导致坏缓存被盖上有效戳，反复触发 40s+ 同步重建）。
                skip_check = (attempt == 1 and merged_pages == 0 and not full
                              and patched == 0)
                check_started = time.monotonic()
                check_ok = True if skip_check else self._check_merged(
                    tmp if full else dst)
                if not skip_check and time.monotonic() - check_started > 5.0:
                    # 慢校验留痕：便于判断重建耗时的真实构成
                    sys.stderr.write(
                        "[wechatauto] %s 整库校验耗时 %.1fs（第 %d 轮）\n"
                        % (rel, time.monotonic() - check_started, attempt))
                if check_ok:
                    build = False
                    if full and not self._promote_cache(tmp, dst):
                        old = None  # 替换失败（缓存被占用等）：重试
                        continue
                    self._write_stamp(stamp, src_mtime, src_size,
                                      wal_mtime, wal_size, applied)
                    self._record_cache_ok(rel)
                elif attempt >= 3:
                    # WAL 合并反复失败，尝试仅解密主库(不含 WAL 增量)作为兜底
                    reason = "WAL 合并/整库校验连续 %d 轮失败" % attempt
                    self._record_cache_error(rel, reason)
                    sys.stderr.write(
                        "[wechatauto] WAL 合并失败(%s)，尝试仅解密主库...\n" % rel
                    )
                    self._decrypt_file(src, tmp, key)
                    if self._check_merged(tmp):
                        # 校验通过才晋升；本次不含 WAL 增量，部分最新消息可能缺失
                        self._promote_cache(tmp, dst)
                        applied = 0
                        build = False
                        self._write_stamp(stamp, src_mtime, src_size, 0, 0, 0)
                        self._record_cache_ok(rel)
                    else:
                        # 源库正被微信写库、快照始终撕裂：不晋升未校验文件
                        # （旧实现会降级晋升坏缓存并盖上有效戳，导致后续反复
                        # 40s+ 同步重建）。直接返回临时文件的降级连接，
                        # 缓存保持待重建状态，下轮打开再次尝试。
                        self._record_cache_error(
                            rel, "主库解密后 quick_check 仍失败（快照撕裂）")
                        sys.stderr.write(
                            "[wechatauto] 主库解密校验失败，返回降级连接(部分数据可能缺失): %s\n"
                            % rel
                        )
                        applied = 0
                        build = False
                        return self._connect_ro(tmp)
                else:
                    old = None  # 合并结果损坏 → 全量重建重试
                    # 多为微信正在 checkpoint 写库导致快照撕裂：稍候再试，
                    # 立即重试大概率再次读到写了一半的源库
                    time.sleep(1.5)
        return self._connect_ro(dst)

    def _kick_rebuild(self, rel: str, src: str, dst: str, key: bytes,
                      src_mtime: float, src_size: int,
                      wal_mtime: float, wal_size: int) -> None:
        """后台重建解密缓存（同一分片去重 + 完成间隔节流）。

        微信活跃期源库几乎每秒都在变，若不加节流会无限重建。
        距上次重建完成不足 REBUILD_MIN_INTERVAL_SEC 时仅返回旧缓存，
        新消息最迟在下一次重建后即可读到（秒级延迟，不再阻塞）。
        """
        with self._rebuild_guard:
            if rel in self._rebuilding:
                return
            last_done = self._rebuild_done_at.get(rel, 0.0)
            if time.monotonic() - last_done < REBUILD_MIN_INTERVAL_SEC:
                return
            self._rebuilding.add(rel)
        t = threading.Thread(
            target=self._bg_rebuild, name="kefu-db-rebuild",
            args=(rel, src, dst, key, src_mtime, src_size, wal_mtime, wal_size),
            daemon=True,
        )
        t.start()

    def _bg_rebuild(self, rel: str, src: str, dst: str, key: bytes,
                    src_mtime: float, src_size: int,
                    wal_mtime: float, wal_size: int) -> None:
        """后台线程：增量重建解密缓存并原子替换（详见 _open 的旧缓存策略）。

        相对旧实现的修复（对应线上"14:39–17:52 三小时无新商机"故障）：
        1. 三次尝试全部失败必须留痕（分片、耗时、原因、连续失败次数）；
        2. 每轮结束快速比对源库签名，源库仍在写入时缩短重试间隔并跳过
           昂贵的整库校验（生产环境实测 3×130s 全花在注定过期的快照上），
           最后一次尝试仍强制校验，保证能推进出一个可用快照；
        3. 全局 Semaphore 限制同时重建的分片数，拿不到配额本轮直接跳过，
           不阻塞轮询线程（加锁顺序：先 semaphore 后 rel_lock，释放反序）。
        """
        self._ensure_cache_state()
        if not self._rebuild_sem.acquire(blocking=False):
            # 已有 MAX_CONCURRENT_REBUILDS 个分片在重建：本轮跳过，下轮再试。
            # 必须清掉 _rebuilding 标记，否则该分片再也不会被重新调度。
            now = time.time()
            last_log = self._rebuild_skipped_at.get(rel, 0.0)
            self._rebuild_skipped_at[rel] = now
            with self._rebuild_guard:
                self._rebuilding.discard(rel)
            if now - last_log >= 60.0:
                sys.stderr.write(
                    "[wechatauto] %s 等待重建配额超时（当前已有 %d 个分片在重建），"
                    "本轮跳过，下轮重试\n" % (rel, MAX_CONCURRENT_REBUILDS)
                )
            return
        tmp = dst + ".rebuild"
        started = time.monotonic()
        applied = 0
        check_cost = 0.0
        how = "未构建"
        # 一轮重建只记一次失败（3 次尝试不该把 fail_streak 放大 3 倍）。
        # 必须在 try 之前初始化：外层 except 覆盖 rel_lock 之外的异常时，
        # 否则引用未定义变量会抛 UnboundLocalError。
        recorded = False
        try:
            with self._rel_lock(rel):
                wal_path = self._wal_path(rel)
                # 构建前记录源库签名，用于"快速失败快速重试"
                sig = self._source_sig(src, wal_path)
                verified = False
                last_error = ""
                with_stack = ""   # 本轮首次异常的原因 + traceback（进 cache_status）
                verified_how = ""
                for attempt in range(1, BG_REBUILD_ATTEMPTS + 1):
                    try:
                        stats: dict = {}
                        page_hashes, how = self._build_cache(src, tmp, dst, key,
                                                             stats=stats)
                        with open(tmp + ".pages", "wb") as hf:
                            hf.write(page_hashes)
                        applied = 0
                        merged_pages = 0
                        if wal_path and os.path.getsize(wal_path) > self.WAL_HEADER_SZ:
                            applied, merged_pages = self._merge_wal(
                                tmp, wal_path, key, 0)
                    except Exception as exc:  # noqa: BLE001 - 任何异常都必须可见
                        # 兜底捕获任何异常（不只是 OSError/sqlite3.Error）：
                        # 未知异常若逃出线程会静默杀死重建，正是本次故障的形态。
                        base = "构建异常 %s: %s" % (type(exc).__name__, exc)
                        last_error = base
                        if not recorded:
                            # 仅本轮首次异常：把完整栈折进 cache_status 的
                            # last_error（限长 MAX_CACHE_ERROR_LEN），并单独
                            # 打一份多行栈到 stderr；后两次只留短串避免刷屏
                            detail = traceback.format_exc()
                            with_stack = _one_line(
                                "%s | %s" % (base, detail), MAX_CACHE_ERROR_LEN)
                            self._record_cache_error(rel, with_stack)
                            recorded = True
                        else:
                            detail = ""
                        # 重试提示必须与真实节奏一致：最后一次尝试之后不再
                        # sleep（直接进失败退避），不能说"X 后重试"
                        retry_hint = (
                            "%.1fs 后重试" % FAST_RETRY_INTERVAL_SEC
                            if attempt < BG_REBUILD_ATTEMPTS
                            else "已达最大尝试次数，进入收尾（不再重试）"
                        )
                        sys.stderr.write(
                            "[wechatauto] %s 缓存重建第 %d/%d 次尝试异常"
                            "（%s: %s），%s\n%s"
                            % (rel, attempt, BG_REBUILD_ATTEMPTS,
                               type(exc).__name__, exc, retry_hint, detail)
                        )
                        sig = self._source_sig(src, wal_path)
                        if attempt < BG_REBUILD_ATTEMPTS:
                            time.sleep(FAST_RETRY_INTERVAL_SEC)
                        continue
                    now_sig = self._source_sig(src, wal_path)
                    patched = int(stats.get("patched", -1))
                    unchanged = (not stats.get("full", True)) and patched == 0 \
                        and merged_pages == 0
                    if now_sig != sig and attempt < BG_REBUILD_ATTEMPTS:
                        # 源库在本轮构建期间又被写入：快照已过期，
                        # 此时整库校验纯属空转（线上单轮 130s）→ 短间隔重试
                        last_error = ("源库在构建期间持续变化（签名 %s → %s）"
                                      % (sig, now_sig))
                        sig = now_sig
                        time.sleep(FAST_RETRY_INTERVAL_SEC)
                        continue
                    if unchanged:
                        # 页级重建未改写任何页、WAL 也未写入任何页：
                        # 缓存与上次已验证状态完全一致，无需重复整库校验
                        check_ok = True
                    else:
                        check_started = time.monotonic()
                        check_ok = self._check_merged(tmp)
                        check_cost += time.monotonic() - check_started
                    if check_ok:
                        verified = True
                        verified_how = how
                        break
                    last_error = ("quick_check 校验失败（快照撕裂，第 %d 轮）"
                                  % attempt)
                    sig = now_sig
                    if attempt < BG_REBUILD_ATTEMPTS:
                        time.sleep(1.0)
                if not verified:
                    # 全部分片尝试均失败：必须留痕，不再静默 return
                    for p in (tmp, tmp + ".pages"):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                    if recorded:
                        # 异常分支已记账（fail_streak 已 +1），这里只刷新原因，
                        # 避免同一轮被重复计数；保留首次异常的原因+traceback
                        # （最有定位价值），无栈时退回最近一次失败原因
                        self._cache_last_error[rel] = (
                            with_stack
                            or _one_line(last_error, MAX_CACHE_ERROR_LEN)
                            or "未知错误")
                    else:
                        self._record_cache_error(rel, last_error)
                    sys.stderr.write(
                        "[wechatauto] %s 缓存重建失败：连续 %d 次未成功 promote"
                        "（本轮尝试 %d 次，耗时 %.1fs），正式缓存保持旧版本，"
                        "%.0fs 后重试；最后错误: %s\n"
                        % (rel, int(self._cache_fail_streak.get(rel, 1)),
                           BG_REBUILD_ATTEMPTS, time.monotonic() - started,
                           REBUILD_MIN_INTERVAL_SEC,
                           _one_line(last_error, 300))
                    )
                    return
                # 源库在重建期间又被改写 → 不写 stamp（缓存内容与 stamp 描述
                # 不一致会被误判为最新），下次 _open 会再次触发重建（自愈）。
                # 页级增量重建成本已很低，连续重建不再是瓶颈。
                try:
                    snapshot_fresh = (os.path.getmtime(src) == src_mtime
                                      and os.path.getsize(src) == src_size)
                except OSError:
                    snapshot_fresh = False
                if not self._promote_cache(tmp, dst):
                    self._record_cache_error(rel, "临时文件晋升正式缓存失败")
                    sys.stderr.write(
                        "[wechatauto] %s 缓存重建完成但晋升失败（正式缓存被占用？），"
                        "耗时 %.1fs，下轮重试\n" % (rel, time.monotonic() - started)
                    )
                    return
                if snapshot_fresh:
                    self._write_stamp(stamp=dst + ".stamp", src_mtime=src_mtime,
                                      src_size=src_size, wal_mtime=wal_mtime,
                                      wal_size=wal_size, applied=applied)
                self._record_cache_ok(rel)
                sys.stderr.write(
                    "[wechatauto] %s 缓存重建完成（%s，%.1fs，校验 %.1fs%s）\n"
                    % (rel, verified_how or how, time.monotonic() - started,
                       check_cost,
                       "" if snapshot_fresh else "，源库持续写入下轮继续"))
        except Exception as exc:  # noqa: BLE001 - 外层兜底，任何逃逸异常都要可见
            # 内层 try 只覆盖"构建 + WAL 合并"。外层的 _wal_path / _source_sig /
            # _promote_cache / _write_stamp（磁盘写满）等仍可能抛异常：
            # finally 会保证配额与标记归位（不会停摆），但若不做记账，
            # cache_status() 里 fail_streak/last_error 会停在旧值，运维只能看到
            # threading 打的裸 traceback。这里补一次记账 + 结构化日志后原样抛出，
            # 保持"异常仍向上传播"的既有语义。
            if not recorded:
                # 一轮只记一次：内层已记账时不再叠加，避免 fail_streak 双计。
                self._record_cache_error(
                    rel, "重建过程异常逃出 %s: %s | %s"
                         % (type(exc).__name__, exc, traceback.format_exc()))
            sys.stderr.write(
                "[wechatauto] %s 缓存重建异常逃出（%s: %s），本轮放弃；"
                "配额与 rebuilding 标记将在收尾(finally)时归还，下轮重试\n%s"
                % (rel, type(exc).__name__, exc, traceback.format_exc())
            )
            raise
        finally:
            # 释放顺序与获取顺序相反：先放分片锁（with 已退出）再放全局配额
            self._rebuild_sem.release()
            with self._rebuild_guard:
                self._rebuilding.discard(rel)
                self._rebuild_done_at[rel] = time.monotonic()

    @staticmethod
    def _check_merged(dst: str) -> bool:
        """校验解密/合并结果可完整读取。
        
        只兜底 sqlite_master 无法发现数据页损坏：WAL 若带入过期/错位页，
        schema 树可能仍正常，但表数据页已损坏，直到 SELECT 才抛
        "database disk image is malformed"。这里用 PRAGMA quick_check
        全库校验（含数据页与索引页），损坏时返回 False，触发全量重建重试。
        
        "never used" 页面不是数据损坏，只是 SQLCipher 预分配但 SQLite 未使用的空间，
        单独过滤不计为失败。
        
        索引错误（如 "wrong # of entries in index"）不影响数据可读性，
        仅记录警告，不计为失败。
        """
        try:
            conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
            try:
                rows = conn.execute("PRAGMA quick_check").fetchall()
            finally:
                conn.close()
            if not rows:
                return False
            fatal_errors = []
            index_warnings = []
            for r in rows:
                msg = str(r[0])
                if msg == "ok":
                    continue
                if "never used" in msg:
                    continue  # trailing preallocated pages, not corruption
                # Index errors are non-fatal: data is readable, index is just stale
                if "index" in msg.lower() or "rowid" in msg.lower():
                    index_warnings.append(msg)
                else:
                    fatal_errors.append(msg)
            if index_warnings and not fatal_errors:
                # Only index issues, data is fine
                brief = [e[:160] for e in index_warnings[:3]]
                sys.stderr.write(
                    "[wechatauto] quick_check %s index warnings (non-fatal): %s\n"
                    % (os.path.basename(dst), brief)
                )
                return True  # Treat as OK, data is readable
            if fatal_errors:
                # Data corruption is fatal
                brief = [e[:160] for e in fatal_errors[:3]]
                sys.stderr.write(
                    "[wechatauto] quick_check %s FAILED: %s\n"
                    % (os.path.basename(dst), brief)
                )
                return False
            return True
        except sqlite3.Error as exc:
            sys.stderr.write(
                "[wechatauto] quick_check %s ERROR: %s\n"
                % (os.path.basename(dst), exc)
            )
            return False

    def _wal_path(self, rel: str) -> Optional[str]:
        wal = self._db_path(rel) + "-wal"
        return wal if os.path.exists(wal) else None

    def _merge_wal(self, dst: str, wal_path: str, key: bytes, from_frame: int) -> Tuple[int, int]:
        """把 -wal 中当前世代的已提交帧合并进已解密的主库文件。

        返回 (已扫描帧数, 落盘页数)。from_frame 仅作兼容保留：
        WAL 预分配复用，checkpoint 重置后新帧从 0 号开始，
        必须全量扫描再靠 salt 世代过滤，不能按帧号截断。

        WAL 头（32B 大端）：[0:4] magic, [4:8] 版本, [8:12] 页大小,
        [12:16] checkpoint 序号, [16:24] salt（当前世代）, [24:32] 头校验和。
        帧头（24B）：[0:4] 页号, [4:8] 提交后库页数（非 0 = 提交帧）,
        [8:16] salt, [16:24] 帧校验和；帧内页面与主库页同格式加密。

        合并规则（对齐 SQLite walIndexRecover 语义）：
        1. 只合并 salt 与 WAL 头一致的帧（当前世代）——checkpoint 重置 WAL 时
           salt-1 自增、salt-2 换随机值，旧世代帧被合并会用过期页覆盖新数据；
        2. 只合并到最后一个提交帧为止——提交帧之后的未提交尾部事务回滚；
        3. 允许库增长：新页帧直接写入并扩展文件，但要求新页区间连续无空洞，
           避免稀疏零字节页；
        4. 合并后按最后提交帧的 nTruncate 修正头部页数并截断文件。
        """
        if not os.path.exists(dst):
            return 0, 0
        # ---- 读 WAL 头并校验 ----
        with open(wal_path, "rb") as wal:
            wal_hdr = wal.read(self.WAL_HEADER_SZ)
            if len(wal_hdr) < self.WAL_HEADER_SZ:
                return 0, 0
            magic = struct.unpack(">I", wal_hdr[0:4])[0]
            if (magic & 0xFFFFFFFE) != 0x377F0682:
                return 0, 0
            wal_pgsize = struct.unpack(">I", wal_hdr[8:12])[0]
            if wal_pgsize != PAGE_SZ:
                return 0, 0
            wal_salt = wal_hdr[16:24]
            wal_size = os.path.getsize(wal_path)
            n = (wal_size - self.WAL_HEADER_SZ) // self.WAL_FRAME_SZ
            # ---- 收集当前世代帧 [(帧号, pgno, n_trunc, page)] ----
            cur = []
            for i in range(n):
                wal.seek(self.WAL_HEADER_SZ + i * self.WAL_FRAME_SZ)
                hdr = wal.read(24)
                page = wal.read(PAGE_SZ)
                if len(page) < PAGE_SZ:
                    break
                if hdr[8:16] == wal_salt:
                    pgno = struct.unpack(">I", hdr[:4])[0]
                    n_trunc = struct.unpack(">I", hdr[4:8])[0]
                    cur.append((i, pgno, n_trunc, page))
        if not cur:
            return n, 0
        # ---- 只保留到最后一个提交帧（n_trunc != 0），未提交尾部事务回滚 ----
        last_commit = -1
        final_pages = 0
        for j, (_, _, n_trunc, _) in enumerate(cur):
            if n_trunc:
                last_commit = j
                final_pages = n_trunc
        if last_commit < 0:
            return n, 0
        cur = cur[: last_commit + 1]
        # ---- 解密并校验页面内容 ----
        page_data = {}
        for _, pgno, _, page in cur:
            if pgno < 1:
                continue
            pt = _decrypt_page(key, page, pgno)
            if pgno == 1:
                if len(key) == 48:
                    ok = pt[16:18] == b"\x01\x01"
                else:
                    ok = pt[:16] == b"SQLite format 3\x00"
            else:
                ok = pt[0] in (0, 2, 5, 10, 13)
            if not ok:
                continue
            page_data[pgno] = pt  # 同页后帧覆盖前帧
        if not page_data:
            return n, 0
        # ---- 库增长连续性检查：新页区间 (db_pages, max_pgno] 必须全部有帧覆盖 ----
        db_pages = (os.path.getsize(dst) + PAGE_SZ - 1) // PAGE_SZ
        max_pgno = max(page_data)
        if max_pgno > db_pages:
            missing = [p for p in range(db_pages + 1, max_pgno + 1) if p not in page_data]
            if missing:
                # 新页区间有空洞，仅合并已存在页，避免稀疏零字节页
                for p in [p for p in page_data if p > db_pages]:
                    del page_data[p]
                if not page_data:
                    return n, 0
        # ---- 写入并按提交帧页数修正 ----
        out = open(dst, "r+b")
        try:
            for pgno in sorted(page_data):
                out.seek((pgno - 1) * PAGE_SZ)
                out.write(page_data[pgno])
            out.flush()
            file_pages = (os.path.getsize(dst) + PAGE_SZ - 1) // PAGE_SZ
            new_pages = final_pages if 0 < final_pages <= file_pages else file_pages
            if new_pages < file_pages:
                out.truncate(new_pages * PAGE_SZ)
            out.seek(0)
            page1 = out.read(PAGE_SZ)
            if len(page1) >= 32 and struct.unpack(">I", page1[28:32])[0] != new_pages:
                page1 = page1[:28] + struct.pack(">I", new_pages) + page1[32:]
                out.seek(0)
                out.write(page1)
            out.flush()
        finally:
            out.close()
        return n, len(page_data)

    @staticmethod
    def _truncate_preallocated(dst: str) -> None:
        """按 page1 头部逻辑页数截断 SQLCipher 预分配的尾部空页。"""
        try:
            with open(dst, "r+b") as f:
                page1 = f.read(PAGE_SZ)
                if len(page1) >= 32:
                    hdr_pages = struct.unpack(">I", page1[28:32])[0]
                    file_pages = (os.path.getsize(dst) + PAGE_SZ - 1) // PAGE_SZ
                    if 0 < hdr_pages < file_pages:
                        f.truncate(hdr_pages * PAGE_SZ)
        except OSError:
            pass

    def _decrypt_file(self, src: str, dst: str, key: bytes,
                      hashes_out: Optional[list] = None) -> None:
        """全量解密源库到 dst；hashes_out 传入列表时逐页记录源页哈希。"""
        size = os.path.getsize(src)
        pages = size // PAGE_SZ + (1 if size % PAGE_SZ else 0)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            for pgno in range(1, pages + 1):
                page = fin.read(PAGE_SZ)
                if not page:
                    break
                if len(page) < PAGE_SZ:
                    page = page + b"\x00" * (PAGE_SZ - len(page))
                if hashes_out is not None:
                    hashes_out.append(hashlib.blake2b(page, digest_size=8).digest())
                fout.write(_decrypt_page(key, page, pgno))
        self._truncate_preallocated(dst)

    def _patch_cache(self, src: str, dst_old: str, tmp: str,
                     key: bytes) -> Optional[Tuple[bytes, int]]:
        """页级增量重建：以旧缓存为底，只解密源库中内容变化的页。

        原理：上次构建时记录了源库每页的 blake2b-8 哈希（.pages 边车文件），
        本次逐页比对，哈希相同的页直接沿用旧缓存中的解密结果，
        仅对变化页执行 AES 解密。微信 checkpoint 后通常只有几十~几百页
        变化，重建耗时从全量解密的几十秒降到约 1~2 秒（主要是顺序读盘）。

        返回 (新源页哈希串, 实际改写页数)；缺少旧缓存/哈希或发生异常时
        返回 None（调用方回退全量解密）。
        """
        hashes_path = dst_old + ".pages"
        if not (os.path.exists(dst_old) and os.path.exists(hashes_path)):
            return None
        try:
            with open(hashes_path, "rb") as f:
                old_hashes = f.read()
            shutil.copyfile(dst_old, tmp)
            tmp_size = os.path.getsize(tmp)
            size = os.path.getsize(src)
            pages = size // PAGE_SZ + (1 if size % PAGE_SZ else 0)
            new_hashes = bytearray()
            patched = 0
            logical_pages = 0
            with open(src, "rb") as fin, open(tmp, "r+b") as fout:
                for pgno in range(1, pages + 1):
                    page = fin.read(PAGE_SZ)
                    if not page:
                        break
                    if len(page) < PAGE_SZ:
                        page = page + b"\x00" * (PAGE_SZ - len(page))
                    h = hashlib.blake2b(page, digest_size=8).digest()
                    new_hashes += h
                    idx = pgno - 1
                    off = idx * 8
                    old_h = (old_hashes[off:off + 8]
                             if off + 8 <= len(old_hashes) else None)
                    if pgno == 1:
                        # 旧缓存 page1 可能被 WAL 提交帧覆盖，不能因主库源页
                        # 哈希未变就信任它。始终解密主库 page1，以其逻辑页数
                        # 界定预分配尾页，并清除缓存中可能残留的 WAL overlay。
                        plain_page = _decrypt_page(key, page, pgno)
                        if len(plain_page) >= 32:
                            logical_pages = struct.unpack(">I", plain_page[28:32])[0]
                        fout.seek(0)
                        cached_page = fout.read(PAGE_SZ) if tmp_size >= PAGE_SZ else b""
                        if cached_page != plain_page:
                            fout.seek(0)
                            fout.write(plain_page)
                            patched += 1
                        continue
                    if 0 < logical_pages < pgno:
                        continue
                    # 页内容未变且旧缓存中确实存在该页 → 沿用旧解密结果
                    if old_h == h and idx * PAGE_SZ < tmp_size:
                        continue
                    fout.seek(idx * PAGE_SZ)
                    fout.write(_decrypt_page(key, page, pgno))
                    patched += 1
            self._truncate_preallocated(tmp)
            return bytes(new_hashes), patched
        except (OSError, sqlite3.Error):
            try:
                os.remove(tmp)
            except OSError:
                pass
            return None

    def _build_cache(self, src: str, tmp: str, dst: str,
                     key: bytes,
                     stats: Optional[dict] = None) -> Tuple[bytes, str]:
        """构建解密缓存到 tmp：优先页级增量，无旧缓存/哈希时全量解密。

        返回 (源页哈希串, 构建方式描述)——哈希随晋升写入 .pages 边车，
        供下一次增量重建比对。

        ``stats`` 传入 dict 时回填构建统计：``full``（是否全量解密）、
        ``patched``（页级重建实际改写的页数，全量路径为 -1）。调用方据此
        判断"缓存是否与上次已验证状态完全相同"，从而安全跳过整库校验。
        """
        patched = self._patch_cache(src, dst, tmp, key)
        if patched is not None:
            hashes, n = patched
            if stats is not None:
                stats["full"] = False
                stats["patched"] = int(n)
            return hashes, "增量 %d 页" % n
        hashes: list = []
        self._decrypt_file(src, tmp, key, hashes_out=hashes)
        if stats is not None:
            stats["full"] = True
            stats["patched"] = -1
        return b"".join(hashes), "全量"

    def _message_dbs(self) -> List[str]:
        """返回当前所有消息分片库。微信运行中可能新建分片（如 message_5.db），
        因此每次动态重扫磁盘并补齐新库密钥，而不是用 __init__ 时的静态缓存。
        """
        self._refresh_db_files()
        return sorted(
            rel for rel, path, _ in self._db_files
            if re.match(r"^message[\\/]message_\d+\.db$", rel.replace(os.sep, "/"))
        )

    def _refresh_db_files(self) -> None:
        """重扫磁盘上的 db 文件；发现新文件时补提取其密钥，避免旧缓存漏掉新分片。

        性能：仅当 message 目录下的文件清单有变化（新增 message_5.db 等）或首次
        调用时才做全量重扫，否则直接复用 __init__ 时的扫描结果。
        """
        msg_dir = os.path.join(self.account_dir, "db_storage", "message")
        try:
            cur = sorted(
                n for n in os.listdir(msg_dir)
                if n.endswith(".db") and not n.endswith("-wal") and not n.endswith("-shm")
            )
        except OSError:
            return
        prev = sorted(
            os.path.basename(path)
            for rel, path, _ in self._db_files
            if re.match(r"^message[\\/].*\.db$", rel.replace(os.sep, "/"))
        )
        if cur == prev:
            return
        current = self._collect_db_files()
        if current == self._db_files:
            return
        self._db_files = current
        new_rels = [
            rel for rel, path, _ in current
            if rel not in self._keys or not self._key_works(rel)
        ]
        if new_rels:
            try:
                extracted = self.extract_keys()
                self._keys.update(extracted)
                self._save_keys()
            except Exception:
                pass
        self.unkeyed = [
            rel for rel, _, _ in self._db_files if not self._key_works(rel)
        ]

    def _find_msg_tables(self, user: str, conns: List[sqlite3.Connection]) -> List[Tuple[sqlite3.Connection, str]]:
        """定位用户消息表。微信 4.x 把同一会话的历史消息分散在多个分片
        （如 message_0 存最近一段、message_8 存更新一段），返回全部命中分片。"""
        target = "Msg_" + _md5_hex(user.encode())
        found = []
        for conn in conns:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (target,),
            ).fetchone()
            if row:
                found.append((conn, target))
        return found

    def _invalidate_cache(self) -> None:
        """删除 workdir 中全部解密缓存(.db/.stamp)，key 缓存除外。

        下一次 _open 会对每份库全量解密重建。media 图片等副产物不受影响。
        """
        try:
            names = os.listdir(self.workdir)
        except OSError:
            return
        removed = 0
        for n in names:
            if n.endswith((".db", ".stamp", ".pages", ".rebuild")):
                try:
                    os.remove(os.path.join(self.workdir, n))
                    removed += 1
                except OSError:
                    pass
        if removed:
            sys.stderr.write("[wechatauto] 已清 %d 个缓存文件等待重建\n" % removed)


    def _invalidate_single_cache(self, rel: str) -> None:
        """只删除指定消息库的解密缓存，不影响其他库。"""
        safe_name = rel.replace(os.sep, "__")
        workdir = self.workdir
        for ext in (".db", ".stamp", ".db-shm", ".db-wal", ".pages", ".rebuild"):
            path = os.path.join(workdir, safe_name + ext)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        sys.stderr.write("[wechatauto] 已清除 %s 的缓存，等待重建\n" % rel)
    _BAD_SHARD_COOLDOWN = 60  # seconds to skip a known-bad shard

    def _is_bad_shard(self, rel: str) -> bool:
        """Check if a shard is in the bad-shard cooldown period.

        During cooldown the shard is skipped to avoid flooding errors every
        poll cycle.  If the source file mtime changes (WeChat may have done a
        checkpoint) the cooldown is cleared so the shard is retried.
        """
        if rel not in self._bad_shards:
            return False
        marked_at = self._bad_shards[rel]
        if time.time() - marked_at >= self._BAD_SHARD_COOLDOWN:
            del self._bad_shards[rel]
            self._bad_shards_mtime.pop(rel, None)
            return False
        # source mtime changed -> WeChat may have rewritten it, clear bad mark
        try:
            cur_mtime = os.path.getmtime(self._db_path(rel))
        except OSError:
            return True
        if cur_mtime != self._bad_shards_mtime.get(rel, 0):
            del self._bad_shards[rel]
            self._bad_shards_mtime.pop(rel, None)
            sys.stderr.write(
                "[wechatauto] shard %s source updated, clearing bad mark\n"
                % os.path.basename(rel)
            )
            return False
        return True

    def _mark_bad_shard(self, rel: str) -> None:
        """Mark a shard as bad so _msg_conn skips it for a cooldown period."""
        try:
            mtime = os.path.getmtime(self._db_path(rel))
        except OSError:
            mtime = 0
        self._bad_shards[rel] = time.time()
        self._bad_shards_mtime[rel] = mtime
        sys.stderr.write(
            "[wechatauto] shard %s persistently corrupt, skipping for %ds\n"
            % (os.path.basename(rel), self._BAD_SHARD_COOLDOWN)
        )

    def _msg_conn(self, user: str, _retry: bool = True) -> Optional[List[Tuple[sqlite3.Connection, str]]]:
        """Open message databases and locate user message tables.

        Returns all matching shards [(conn, table), ...] (caller is responsible
        for closing).  Persistently corrupted shards are tracked and skipped
        during a cooldown period to avoid flooding errors on every poll cycle.
        """
        conns = []
        failed_rels = []
        try:
            for rel in self._message_dbs():
                if self._is_bad_shard(rel):
                    continue
                try:
                    conn = self._open(rel)
                    # lightweight validation: catches corrupted cached files
                    # that skip_check bypassed (e.g. stale stamp + corrupt .db)
                    conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
                    conns.append(conn)
                    # shard opened successfully: clear bad mark if any
                    self._bad_shards.pop(rel, None)
                    self._bad_shards_mtime.pop(rel, None)
                except (sqlite3.DatabaseError, RuntimeError) as exc:
                    is_mal = (
                        _is_malformed(exc)
                        if isinstance(exc, sqlite3.DatabaseError)
                        else True
                    )
                    if is_mal:
                        # only clear this shard's cache, not all
                        self._invalidate_single_cache(rel)
                        failed_rels.append(rel)
                        try:
                            conn = self._open(rel)
                            conn.execute(
                                "SELECT 1 FROM sqlite_master LIMIT 1"
                            ).fetchone()
                            conns.append(conn)
                            # rebuild succeeded: clear bad mark
                            self._bad_shards.pop(rel, None)
                            self._bad_shards_mtime.pop(rel, None)
                        except Exception:
                            self._mark_bad_shard(rel)
                    else:
                        raise
            found = self._find_msg_tables(user, conns)
            # track which shard rels contributed to found connections
            # so _run_msg_query can selectively invalidate on data errors
            self._last_found_rels = [
                rel for rel in self._message_dbs()
                if not self._is_bad_shard(rel)
            ]
        except sqlite3.DatabaseError as exc:
            for c in conns:
                c.close()
            if _retry and _is_malformed(exc):
                sys.stderr.write(
                    "[wechatauto] message db corrupt (%s), skipping query\n" % exc
                )
                return None
            raise
        except Exception:
            for c in conns:
                c.close()
            raise
        if not found:
            for c in conns:
                c.close()
            return None
        # 成功路径：只返回命中分片，**未命中的连接必须在此关闭**。
        # 这些未命中的只读连接同样持有缓存文件句柄，Windows 下会让
        # _promote_cache 的 os.replace 一直 PermissionError（日志里的
        # “缓存重建完成但晋升失败”），且要等 GC 才释放——进程长期运行时
        # 9 个分片的句柄会一直挂着，正式缓存永远换不上去。
        keep = {id(c) for c, _ in found}
        for c in conns:
            if id(c) not in keep:
                try:
                    c.close()
                except Exception:
                    pass
        return found

    def _run_msg_query(self, user: str, build):
        """对消息库执行只读查询；查询到库损坏时清缓存重建并重试一次。

        build(found) -> rows，found 为全部命中分片 [(conn, table), ...]
        （同一会话可能分布在多个分片，build 需合并各分片结果）。
        _msg_conn 已处理 schema 损坏重建，本方法兜底数据页损坏。
        重试后仍失败则抛原始异常（Listener 捕获后跳过本轮，不阻断运行）。
        找不到该会话返回 None。
        """
        for attempt in (0, 1):
            found = self._msg_conn(user)
            if found is None:
                return None
            try:
                return build(found)
            except sqlite3.DatabaseError as exc:
                if attempt or not _is_malformed(exc):
                    raise
                sys.stderr.write(
                    "[wechatauto] query db corrupt (%s), invalidating involved shards\n" % exc
                )
                # selectively invalidate only the shards used in the last query
                # instead of all caches, to avoid cascading rebuilds
                for rel in getattr(self, '_last_found_rels', []):
                    self._invalidate_single_cache(rel)
                    self._mark_bad_shard(rel)
            finally:
                for conn, _ in found:
                    conn.close()
        return None

    def get_messages(self, user: str, limit: int = 20, offset: int = 0) -> List[dict]:
        """读取指定会话（微信号/群号）的最近消息。

        同一会话的历史消息可能分布在多个 message_N 分片，
        各分片各取 limit+offset 条后按 sort_seq 全局归并再分页。
        """
        def build(found):
            rows = []
            for conn, table in found:
                rows.extend(conn.execute(
                    "SELECT local_id, local_type, real_sender_id, create_time, "
                    "message_content, source, packed_info_data, compress_content, sort_seq "
                    "FROM %s ORDER BY sort_seq DESC LIMIT ?" % table,
                    (limit + offset,),
                ).fetchall())
            rows.sort(key=lambda r: r["sort_seq"], reverse=True)
            return [self._msg_row_to_dict(r) for r in rows[offset: offset + limit]]
        return self._run_msg_query(user, build) or []

    def get_message_row(self, user: str, local_id: int) -> Optional[dict]:
        """按 local_id 读取一条消息的完整原始字段（媒体下载用，含 server_id/packed_info）。

        local_id 在分片间可能重复，取各分片命中中 sort_seq 最大的一条。
        """
        def build(found):
            best = None
            for conn, table in found:
                row = conn.execute(
                    "SELECT local_id, local_type, server_id, real_sender_id, create_time, "
                    "message_content, source, packed_info_data, compress_content, sort_seq "
                    "FROM %s WHERE local_id=? LIMIT 1" % table,
                    (local_id,),
                ).fetchone()
                if row is not None and (
                    best is None or (row["sort_seq"] or 0) > (best["sort_seq"] or 0)
                ):
                    best = row
            return best
        row = self._run_msg_query(user, build)
        if not row:
            return None
        sender_id = row["real_sender_id"]
        sender_username = ""
        if sender_id and sender_id != 2:
            sender_index = self._sender_id_index()
            sender_username = sender_index.get(int(sender_id), "")
            if not sender_username:
                # fallback: 尝试从 contact.db 获取昵称
                sender_username = self.get_nickname(str(sender_id))
        return {
            "local_id": row["local_id"],
            "local_type": row["local_type"],
            "server_id": row["server_id"],
            "sender_id": sender_id,
            "sender_username": sender_username,
            "create_time": row["create_time"],
            "content": row["message_content"],
            "source": row["source"],
            "packed_info": row["packed_info_data"],
            "compress_content": row["compress_content"],
            "sort_seq": row["sort_seq"],
        }

    def _find_media_rows(self, user: str, types: set) -> List[int]:
        """按 local_type 直接查该会话全部媒体 local_id（降序），不受总消息分页限制。

        供批量下载场景使用（如一次性拉取某群全部图片）。
        跨分片命中时按 sort_seq 全局降序归并。
        """
        placeholders = ",".join("?" * len(types))

        def build(found):
            rows = []
            for conn, table in found:
                rows.extend(conn.execute(
                    "SELECT local_id, sort_seq FROM %s WHERE local_type IN (%s) "
                    "ORDER BY sort_seq DESC" % (table, placeholders),
                    tuple(sorted(types)),
                ).fetchall())
            rows.sort(key=lambda r: r["sort_seq"], reverse=True)
            return [r["local_id"] for r in rows]

        return self._run_msg_query(user, build) or []

    def get_new_messages(self, user: str, since_seq: int = 0, limit: int = 200) -> List[dict]:
        """返回 sort_seq > since_seq 的新消息（升序），供轮询监听使用。

        跨分片命中时按 sort_seq 全局升序归并后截取 limit。
        """
        def build(found):
            rows = []
            for conn, table in found:
                rows.extend(conn.execute(
                    "SELECT local_id, local_type, real_sender_id, create_time, "
                    "message_content, source, packed_info_data, compress_content, sort_seq "
                    "FROM %s WHERE sort_seq > ? ORDER BY sort_seq ASC" % table,
                    (since_seq,),
                ).fetchall())
            rows.sort(key=lambda r: r["sort_seq"])
            return rows[:limit]

        rows = self._run_msg_query(user, build)
        return [self._msg_row_to_dict(r) for r in rows] if rows else []

    def _msg_row_to_dict(self, r) -> dict:
        content = r["message_content"]
        mtype = WeChatDB._msg_type_name(r["local_type"])
        if isinstance(content, bytes):
            content = WeChatDB._friendly_content(content, mtype)
        # 如果内容是占位符且有 compress_content，尝试使用 compress_content
        placeholder = "[%s]" % mtype
        if content == placeholder:
            try:
                cc = r["compress_content"]
            except (KeyError, IndexError):
                cc = None
            if isinstance(cc, bytes) and cc:
                cc_text = WeChatDB._friendly_content(cc, mtype)
                if cc_text != placeholder:
                    content = cc_text
        sender_id = r["real_sender_id"]
        sender_username = ""
        if sender_id and sender_id != 2:
            sender_index = self._sender_id_index()
            sender_username = sender_index.get(int(sender_id), "")
            if not sender_username:
                # fallback: 尝试从 contact.db 获取昵称
                sender_username = self.get_nickname(str(sender_id))
        return {
            "local_id": r["local_id"],
            "type": mtype,
            "sender_id": sender_id,
            "sender_username": sender_username,
            "create_time": r["create_time"],
            "content": content,
            "sort_seq": r["sort_seq"],
        }

    @staticmethod
    def _friendly_content(content: bytes, mtype) -> str:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            if content[:4] == b"\x28\xb5\x2f\xfd":
                # zstd 压缩的文本消息
                zstd = _get_zstd_module()
                if zstd is not None:
                    text = _zstd_decompress(zstd, content)
                    if text:
                        return text
                # 回退到 blob 提取（无法 zstd 解压时尝试直接剥离容器头）
                text = _extract_text_from_blob(content)
                if text:
                    return text
            if mtype == "图片":
                md5 = re.search(rb'md5="([0-9a-fA-F]{32})"', content)
                if md5:
                    return "[图片 md5=%s]" % md5.group(1).decode()
            return "[%s]" % mtype
        # 去除二进制填充
        cleaned = text.strip()
        if cleaned:
            # 尝试提取文本（处理容器头+明文+填充的格式）
            if b"\x01\x00" in content:
                parts = cleaned.split("\x01")
                cleaned = parts[0].strip()
            return cleaned if cleaned else "[%s]" % mtype
        return "[%s]" % mtype

    def get_sessions(self, limit: int = 100) -> List[dict]:
        """会话列表（来自 session.db）"""
        sessions = []
        for rel, path, _ in self._db_files:
            if os.path.basename(path) != "session.db":
                continue
            conn = self._open(rel)
            try:
                rows = conn.execute(
                    "SELECT username, unread_count, summary, last_timestamp, "
                    "last_msg_sender, last_sender_display_name "
                    "FROM SessionTable WHERE is_hidden=0 "
                    "ORDER BY sort_timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            finally:
                conn.close()
            for r in rows:
                sessions.append({
                    "username": r["username"],
                    "unread": r["unread_count"],
                    "summary": r["summary"],
                    "last_time": r["last_timestamp"],
                    "last_sender": r["last_sender_display_name"] or r["last_msg_sender"],
                })
            break
        return sessions

    def search_contact(self, keyword: str) -> List[dict]:
        """按昵称/备注/微信号搜索联系人"""
        results = []
        for rel, path, _ in self._db_files:
            if os.path.basename(path) != "contact.db":
                continue
            conn = self._open(rel)
            try:
                rows = conn.execute(
                    "SELECT username, nick_name, remark FROM contact "
                    "WHERE nick_name LIKE ? OR remark LIKE ? OR username LIKE ? "
                    "OR alias LIKE ? LIMIT 50",
                    ("%" + keyword + "%",) * 4,
                ).fetchall()
            finally:
                conn.close()
            for r in rows:
                results.append({
                    "username": r["username"],
                    "nick_name": r["nick_name"],
                    "remark": r["remark"],
                })
            break
        return results

    def get_contacts(self, kind: str = "friend", limit: int = 0) -> List[dict]:
        """获取联系人列表（contact.db）。

        Args:
            kind: 筛选类别，不区分大小写：
                - "friend"  : 好友 = 通讯录好友（local_type=3，含企业微信好友）
                              + 仅聊天联系人（type=1 中的人类账号），
                              排除公众号/系统号/群聊/自己；
                - "chatroom": 群聊（username 以 @chatroom 结尾）；
                - "official": 公众号/服务号（username 以 gh_ 开头）；
                - "openim"  : 企业微信/客服联系人（@openim / @kefu.openim）；
                - "all"     : 全部（含群内非好友等，可能数万条）。
            limit: 最多返回条数，0 = 不限。

        Returns:
            List[dict]，每条含 username / alias / nick_name / remark /
            description / avatar（大头像 URL）/ local_type。
            已删除（delete_flag=1）的联系人始终排除。

        说明：local_type 实测分布（微信 4.x）：3=通讯录好友（flag=4），
        2=群聊，1=混合桶（系统号/关注的公众号/仅聊天联系人/自己），
        0=非好友交互者，5-7=企业微信侧。
        """
        kind = (kind or "friend").lower()
        if kind == "friend":
            where = ("(local_type=3 OR (local_type=1 AND "
                     "username NOT LIKE '%@chatroom' "
                     "AND username NOT LIKE 'gh\\_%' ESCAPE '\\' "
                     "AND username NOT LIKE '%@openim' "
                     "AND username NOT LIKE '%@kefu%' "
                     "AND username NOT LIKE '%@app'))")
        elif kind == "chatroom":
            where = "username LIKE '%@chatroom'"
        elif kind == "official":
            where = "username LIKE 'gh\\_%' ESCAPE '\\'"
        elif kind == "openim":
            where = "(username LIKE '%@openim' OR username LIKE '%@kefu%')"
        elif kind == "all":
            where = "1=1"
        else:
            raise ValueError(
                "kind 仅支持 friend/chatroom/official/openim/all，收到: %r" % kind)
        sql = ("SELECT username, alias, nick_name, remark, description, "
               "big_head_url, local_type FROM contact "
               "WHERE delete_flag=0 AND " + where + " ORDER BY username")
        conn = self._contact_conn()
        if not conn:
            return []
        try:
            rows = conn.execute(sql).fetchall()
        finally:
            conn.close()
        out = [{
            "username": r["username"],
            "alias": r["alias"],
            "nick_name": r["nick_name"],
            "remark": r["remark"],
            "description": r["description"],
            "avatar": r["big_head_url"],
            "local_type": r["local_type"],
        } for r in rows]
        if kind == "friend":
            # 排除系统号与自己（它们与仅聊天联系人同在 local_type=1）
            self_user = ""
            try:
                self_user = (self.get_self_info() or {}).get("username") or ""
            except Exception:
                pass
            out = [c for c in out
                   if c["username"] not in _SYSTEM_USERNAMES
                   and c["username"] != self_user]
        if limit and limit > 0:
            out = out[:limit]
        return out

    def get_nickname(self, user: str) -> str:
        """通过微信号查昵称（用于显示）"""
        for rel, path, _ in self._db_files:
            if os.path.basename(path) != "contact.db":
                continue
            conn = self._open(rel)
            try:
                row = conn.execute(
                    "SELECT nick_name, remark FROM contact WHERE username=? LIMIT 1",
                    (user,),
                ).fetchone()
            finally:
                conn.close()
            if row:
                return row["remark"] or row["nick_name"] or user
            break
        return user

    def username_by_nickname(self, nickname: str) -> Optional[str]:
        """通过昵称/备注反查微信号（contact.db）。

        返回第一个 remark 或 nick_name 与给定昵称**精确相等**的 contact
        username；找不到返回 None。
        """
        nickname = (nickname or '').strip()
        if not nickname:
            return None
        conn = self._contact_conn()
        if not conn:
            return None
        try:
            rows = conn.execute(
                "SELECT username, nick_name, remark FROM contact "
                "WHERE remark=? OR nick_name=?",
                (nickname, nickname),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            disp = row["remark"] or row["nick_name"]
            if disp == nickname:
                return row["username"]
        return None

    # ------------------------------------------------------------------
    # 群成员（contact.db 读取，无需 UI）
    # ------------------------------------------------------------------
    def _contact_conn(self) -> Optional[sqlite3.Connection]:
        """打开 contact.db（调用方负责 close）；失败返回 None。"""
        for rel, path, _ in self._db_files:
            if os.path.basename(path) != "contact.db":
                continue
            try:
                return self._open(rel)
            except Exception:
                return None
        return None

    def get_groups(self) -> List[dict]:
        """列出所有群聊。

        Returns:
            List[dict]，每条：username(群 wxid), name(群名), owner(群主 wxid),
            member_count(成员数), members(List[dict] 成员详情，见 get_group_members)。
        """
        conn = self._contact_conn()
        if not conn:
            return []
        try:
            rooms = conn.execute(
                "SELECT id, username, owner FROM chat_room"
            ).fetchall()
            room_by_id = {r["id"]: r for r in rooms}
            if not room_by_id:
                return []
            placeholders = ",".join("?" * len(room_by_id))
            members = conn.execute(
                "SELECT room_id, member_id FROM chatroom_member "
                "WHERE room_id IN (%s)" % placeholders,
                tuple(room_by_id.keys()),
            ).fetchall()
            member_ids = sorted({m["member_id"] for m in members})
            contact = {}
            if member_ids:
                mp = ",".join("?" * len(member_ids))
                rows = conn.execute(
                    "SELECT id, username, nick_name, remark FROM contact "
                    "WHERE id IN (%s)" % mp, tuple(member_ids),
                ).fetchall()
                contact = {r["id"]: r for r in rows}
            # 群 wxid -> 群名（contact 表里 @chatroom 行的 nick_name）
            room_names = {}
            for r in conn.execute(
                    "SELECT username, nick_name FROM contact "
                    "WHERE username LIKE '%@chatroom'").fetchall():
                room_names[r["username"]] = r["nick_name"] or r["username"]
        finally:
            conn.close()
        groups = []
        for rid, room in room_by_id.items():
            ms = []
            for m in members:
                if m["room_id"] != rid:
                    continue
                c = contact.get(m["member_id"])
                if c is None:
                    continue
                ms.append({
                    "username": c["username"],
                    "nick_name": c["nick_name"],
                    "remark": c["remark"],
                    "is_owner": c["username"] == room["owner"],
                })
            groups.append({
                "username": room["username"],
                "name": room_names.get(room["username"], room["username"]),
                "owner": room["owner"],
                "member_count": len(ms),
                "members": ms,
            })
        return groups

    def group_name_to_id(self, name: str) -> Optional[str]:
        """按群名查找群 wxid（形如 ``xxx@chatroom``）；找不到返回 None。

        精确匹配优先，其次做「子串包含」的宽松匹配（可能返回多个，取第一个）。
        """
        conn = self._contact_conn()
        if not conn:
            return None
        try:
            rows = conn.execute(
                "SELECT username, nick_name FROM contact "
                "WHERE username LIKE '%@chatroom'").fetchall()
        finally:
            conn.close()
        exact = None
        fuzzy = []
        for r in rows:
            nm = r["nick_name"] or r["username"]
            if nm == name:
                exact = r["username"]
            elif name and name in nm:
                fuzzy.append(r["username"])
            elif nm == r["username"] and name in nm:  # 无群名时按 username 兜底
                fuzzy.append(r["username"])
        return exact or (fuzzy[0] if fuzzy else None)

    def group_id_to_name(self, chatroom_wxid: str) -> Optional[str]:
        """按群 wxid 查群名；找不到返回 None。"""
        conn = self._contact_conn()
        if not conn:
            return None
        try:
            r = conn.execute(
                "SELECT nick_name, username FROM contact "
                "WHERE username=? LIMIT 1", (chatroom_wxid,),
            ).fetchone()
        finally:
            conn.close()
        if not r:
            return None
        return r["nick_name"] or r["username"]

    def get_group_members(self, chatroom_wxid: str) -> List[dict]:
        """枚举指定群聊的成员列表（静态读库，可轮询）。

        Args:
            chatroom_wxid: 群 wxid（形如 ``xxx@chatroom``）。

        Returns:
            List[dict]，每条：username(wxid/微信号), nick_name, remark,
            is_owner(是否群主)。按 username 排序。
        """
        conn = self._contact_conn()
        if not conn:
            return []
        try:
            room = conn.execute(
                "SELECT id, owner FROM chat_room WHERE username=? LIMIT 1",
                (chatroom_wxid,),
            ).fetchone()
            if not room:
                return []
            rows = conn.execute(
                "SELECT m.member_id, c.username, c.nick_name, c.remark "
                "FROM chatroom_member m "
                "LEFT JOIN contact c ON c.id = m.member_id "
                "WHERE m.room_id=? AND c.username IS NOT NULL",
                (room["id"],),
            ).fetchall()
        finally:
            conn.close()
        members = []
        for r in rows:
            members.append({
                "username": r["username"],
                "nick_name": r["nick_name"],
                "remark": r["remark"],
                "is_owner": r["username"] == room["owner"],
            })
        members.sort(key=lambda x: x["username"])
        return members

    def get_group_member_watcher(self, chatroom_wxid: str) -> "GroupMemberWatcher":
        """为指定群创建成员变动监测器（见 GroupMemberWatcher）。"""
        return GroupMemberWatcher(self, chatroom_wxid)

    # ------------------------------------------------------------------
    # 历史消息全量导出
    # ------------------------------------------------------------------
    def _build_md5_index(self) -> Dict[str, str]:
        """会话 md5 → 用户名 反查表（来自 contact/session）"""
        idx: Dict[str, str] = {}
        for rel, path, _ in self._db_files:
            base = os.path.basename(path)
            if base not in ("contact.db", "session.db"):
                continue
            conn = self._open(rel)
            try:
                if base == "contact.db":
                    rows = conn.execute("SELECT username FROM contact")
                else:
                    rows = conn.execute("SELECT username FROM SessionTable")
                for (u,) in rows:
                    if u:
                        idx.setdefault(_md5_hex(u.encode()), u)
            finally:
                conn.close()
        return idx

    def _nickname_index(self) -> Dict[str, str]:
        idx = {}
        for rel, path, _ in self._db_files:
            if os.path.basename(path) != "contact.db":
                continue
            conn = self._open(rel)
            try:
                for u, n, r in conn.execute(
                    "SELECT username, nick_name, remark FROM contact"
                ):
                    idx[u] = r or n or u
            finally:
                conn.close()
            break
        return idx

    def _sender_id_index(self) -> Dict[int, str]:
        """消息表 real_sender_id(数字) → 用户名，来自 message_resource.SenderName2Id"""
        if hasattr(self, '_sender_id_cache') and self._sender_id_cache is not None:
            return self._sender_id_cache
        idx: Dict[int, str] = {}
        for rel, path, _ in self._db_files:
            if os.path.basename(path) != "message_resource.db":
                continue
            conn = self._open(rel)
            try:
                for rid, u in conn.execute(
                    "SELECT rowid, user_name FROM SenderName2Id"
                ):
                    if u:
                        idx[int(rid)] = u
            finally:
                conn.close()
            break
        self._sender_id_cache = idx
        return idx

    def _resolve_sender(self, sender_id, sender_index, nicks, self_nick) -> str:
        if sender_id in (2, "2"):
            return self_nick
        if isinstance(sender_id, int):
            u = sender_index.get(sender_id)
            if u:
                return nicks.get(u, u)
        u = str(sender_id)
        return nicks.get(u, u)

    @staticmethod
    def _msg_type_name(t: int):
        """消息类型显示名；兼容微信 4.x 的资源包装类型（低字节为真实类型）"""
        if t in MSG_TYPE_NAMES:
            return MSG_TYPE_NAMES[t]
        if isinstance(t, int) and t > 0xFFFF and (t & 0xFF) in MSG_TYPE_NAMES:
            return MSG_TYPE_NAMES[t & 0xFF]
        return t

    def _export_row(self, r, mtype_names) -> dict:
        content = r["message_content"]
        mtype = mtype_names.get(r["local_type"], self._msg_type_name(r["local_type"]))
        md5 = None
        if isinstance(content, bytes):
            content = self._friendly_content(content, mtype)
        pi = r["packed_info_data"]
        if pi:
            try:
                md5 = re.search(rb"([0-9a-fA-F]{32})", pi)
                md5 = md5.group(1).decode().lower() if md5 else None
            except TypeError:
                md5 = None
        return {
            "local_id": r["local_id"],
            "type": mtype,
            "type_code": r["local_type"],
            "sender_id": r["real_sender_id"],
            "create_time": r["create_time"],
            "content": content,
            "server_id": r["server_id"],
            "md5": md5,
            "sort_seq": r["sort_seq"],
        }

    def list_message_chats(self) -> List[dict]:
        """所有含消息的会话（md5、用户名、昵称、消息数）"""
        tables: Dict[str, int] = {}
        for rel in self._message_dbs():
            if self._is_bad_shard(rel):
                continue
            try:
                conn = self._open(rel)
            except Exception:
                continue
            try:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                )
                for t in rows:
                    key = t[0][4:]
                    try:
                        cnt = conn.execute(
                            "SELECT count(*) FROM %s" % t[0]
                        ).fetchone()[0]
                        tables[key] = tables.get(key, 0) + cnt
                    except sqlite3.DatabaseError:
                        continue
            finally:
                conn.close()
        idx = self._build_md5_index()
        nicks = self._nickname_index()
        out = []
        for md5, cnt in tables.items():
            user = idx.get(md5, md5)
            out.append({
                "md5": md5,
                "username": user,
                "name": nicks.get(user, user),
                "message_count": cnt,
            })
        out.sort(key=lambda x: -x["message_count"])
        return out

    def export_history(
        self,
        out_path: str,
        fmt: str = "json",
        users: Optional[List[str]] = None,
        limit_per_chat: Optional[int] = None,
        progress: Optional[callable] = None,
    ) -> dict:
        """导出历史消息到 JSON 或 SQLite。

        :param out_path: 输出文件路径（json 或 .db/.sqlite）
        :param fmt: "json" 或 "sqlite"
        :param users: 指定会话（用户名或 md5），None 导出全部
        :param limit_per_chat: 每会话最多导出条数（按 sort_seq 升序保留最新）
        :param progress: 回调 (chat_index, total_chats, chat_name)
        :return: {"chats": n, "messages": total, "out": out_path}
        """
        if fmt not in ("json", "sqlite"):
            raise ValueError("fmt 仅支持 json/sqlite")
        idx = self._build_md5_index()
        nicks = self._nickname_index()
        self_info = self.get_self_info()
        sender_index = self._sender_id_index()
        target_md5s = None
        if users:
            target_md5s = {
                u if re.fullmatch(r"[0-9a-f]{32}", u) else _md5_hex(u.encode())
                for u in users
            }

        # md5 -> [(conn, table), ...] 按消息库聚合（会话跨分库分片）
        buckets: Dict[str, list] = {}
        all_conns: List[sqlite3.Connection] = []
        for rel in self._message_dbs():
            if self._is_bad_shard(rel):
                continue
            try:
                conn = self._open(rel)
            except Exception:
                continue
            all_conns.append(conn)
            tabs = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
            for (t,) in tabs:
                md5 = t[4:]
                if target_md5s is not None and md5 not in target_md5s:
                    continue
                buckets.setdefault(md5, []).append((conn, t))
        try:
            total = 0
            chat_info = []
            order = sorted(buckets.keys())
            for i, md5 in enumerate(order):
                user = idx.get(md5, md5)
                name = nicks.get(user, user)
                if progress:
                    progress(i, len(order), name)
                rows = []
                for conn, table in buckets[md5]:
                    try:
                        rows += conn.execute(
                            "SELECT local_id, local_type, server_id, real_sender_id, "
                            "create_time, message_content, packed_info_data, sort_seq "
                            "FROM %s" % table
                        ).fetchall()
                    except sqlite3.DatabaseError:
                        continue
                if not rows:
                    continue
                rows.sort(key=lambda r: (r["sort_seq"], r["local_id"]))
                if limit_per_chat:
                    rows = rows[-limit_per_chat:]
                msgs = [
                    dict(
                        self._export_row(r, MSG_TYPE_NAMES),
                        sender_name=self._resolve_sender(
                            r["real_sender_id"], sender_index, nicks,
                            self_info.get("nick_name", "我"),
                        ),
                    )
                    for r in rows
                ]
                total += len(msgs)
                chat_info.append({
                    "md5": md5,
                    "username": user,
                    "name": name,
                    "messages": msgs,
                })
            if fmt == "json":
                payload = {
                    "wxid": self.wxid,
                    "nick_name": self_info.get("nick_name", ""),
                    "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "chats": [],
                    "messages": [],
                }
                for c in chat_info:
                    payload["chats"].append({
                        "md5": c["md5"],
                        "username": c["username"],
                        "name": c["name"],
                        "message_count": len(c["messages"]),
                    })
                    for m in c["messages"]:
                        payload["messages"].append(
                            dict(m, chat=c["username"])
                        )
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=1)
            else:
                conn = sqlite3.connect(out_path)
                try:
                    conn.execute(
                        "CREATE TABLE chats(md5 TEXT PRIMARY KEY, username TEXT, "
                        "name TEXT, message_count INT)"
                    )
                    conn.execute(
                        "CREATE TABLE messages(username TEXT, local_id INT, "
                        "type TEXT, type_code INT, sender_id TEXT, sender_name TEXT, "
                        "create_time INT, content TEXT, server_id INT, md5 TEXT, "
                        "sort_seq INT)"
                    )
                    for c in chat_info:
                        conn.execute(
                            "INSERT INTO chats VALUES(?,?,?,?)",
                            (c["md5"], c["username"], c["name"], len(c["messages"])),
                        )
                        conn.executemany(
                            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            [(
                                c["username"], m["local_id"], m["type"], m["type_code"],
                                str(m["sender_id"]), m["sender_name"], m["create_time"],
                                m["content"], m["server_id"], m["md5"], m["sort_seq"],
                            ) for m in c["messages"]],
                        )
                    conn.commit()
                finally:
                    conn.close()
            return {"chats": len(chat_info), "messages": total, "out": out_path}
        finally:
            for conn in all_conns:
                try:
                    conn.close()
                except Exception:
                    pass


def list_accounts(db_dir: Optional[str] = None) -> List[dict]:
    """扫描数据目录下的所有微信账号目录。

    返回: [{"account": "wxid_xxx_abcd", "wxid": "wxid_xxx",
            "path": ..., "last_activity": mtime, "self_nick": 昵称或空}]
    """
    db_dir = db_dir or auto_detect_db_dir()
    if not db_dir:
        return []
    out = []
    for d in _find_account_dirs(db_dir):
        recent = max(
            (
                os.path.getmtime(os.path.join(root, f))
                for root, _, files in os.walk(os.path.join(d, "db_storage"))
                for f in files
                if f.endswith(".db") and not f.endswith("-wal")
            ),
            default=0,
        )
        account = os.path.basename(d)
        out.append({
            "account": account,
            "wxid": re.sub(r"_\w{4}$", "", account),
            "path": d,
            "last_activity": recent,
        })
    out.sort(key=lambda x: -x["last_activity"])
    return out


class GroupMemberWatcher:
    """群成员变动监测器（只读，基于 contact.db 的 chatroom_member 关联）。

    记录一次成员快照，之后每次调用 ``poll()`` 对比当前成员，输出
    「新增 / 离群」差异。适合轮询监听群成员变动。

    用法::

        w = db.get_group_member_watcher("xxx@chatroom")
        snapshot = w.capture()          # 保存基线快照
        ...
        diff = w.poll()                 # 返回 {"joined": [...], "left": [...]}
        # diff 均空 => 无变动；否则可据此处理，并用 w.capture() 更新基线
    """

    def __init__(self, db: "WeChatDB", chatroom_wxid: str):
        self.db = db
        self.chatroom_wxid = chatroom_wxid
        self._baseline = None  # username set

    def _current(self) -> set:
        return {m["username"] for m in self.db.get_group_members(self.chatroom_wxid)}

    def capture(self) -> set:
        """读取当前成员并保存为基线快照；返回成员 username 集合。"""
        self._baseline = self._current()
        return set(self._baseline)

    @property
    def members(self) -> List[dict]:
        """当前成员列表（含昵称/备注/是否群主）。"""
        return self.db.get_group_members(self.chatroom_wxid)

    def poll(self) -> dict:
        """对比上次基线返回成员变动。

        Returns:
            dict: {"joined": [...], "left": [...]}，元素为成员 username。
        - 首次调用（无基线）时先建立基线并返回空差异。
        - 返回后不自动改基线；如需推进，显式调用 ``capture()``。
        """
        cur = self._current()
        if self._baseline is None:
            self._baseline = cur
            return {"joined": [], "left": []}
        joined = sorted(cur - self._baseline)
        left = sorted(self._baseline - cur)
        return {"joined": joined, "left": left}


_LISTENER_STOP = object()


class Listener:
    """新消息轮询监听器（只读，基于合并了 -wal 的消息库视图）。

    用法::

        listener = Listener(db, interval=1.0)
        listener.add_listener("filehelper", on_new_msg)
        listener.start()
        ...
        listener.stop()

    watermark 可持久化（json），下次启动不会重复推送。

    回调在独立工作线程中执行：每个被监听对象（会话）对应一条串行工作
    线程，保证同一会话内消息按序处理、不同会话间并行。轮询线程只负责
    读取数据库并分派任务，不会被慢回调（AI 调用/图片识别等）阻塞。
    """

    def __init__(self, db: "WeChatDB", interval: float = 1.0,
                 watermark: Optional[Dict[str, int]] = None):
        self.db = db
        self.interval = interval
        self._watermark: Dict[str, int] = watermark or {}
        self._callbacks: Dict[str, List[callable]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 每会话一条串行工作线程：跨会话并行 + 会话内保序
        self._worker_queues: Dict[str, queue.Queue] = {}
        self._worker_threads: Dict[str, threading.Thread] = {}
        self._workers_lock = threading.Lock()

    def add_listener(self, user: str, callback: callable) -> None:
        """注册新消息回调：callback(msg: dict, listener)"""
        self._callbacks.setdefault(user, []).append(callback)
        if user not in self._watermark:
            msgs = self.db.get_messages(user, limit=1)
            self._watermark[user] = msgs[0]["sort_seq"] if msgs else 0

    def remove_listener(self, user: str, callback: callable) -> None:
        try:
            self._callbacks[user].remove(callback)
        except (KeyError, ValueError):
            pass

    def add_all(self, callback: callable, discover: bool = True, fast_init: bool = False) -> None:
        """注册全局回调：监听所有已知会话的新消息。

        Args:
            callback: 回调函数，签名 callback(msg: dict, listener)。
                msg 包含 local_id / type / sender_id / create_time /
                content / sort_seq / username（会话原始 username）字段。
            discover: 为 True 时，轮询过程中自动发现新出现的会话并注册
                回调（无需重复调用 add_all）。默认 True。

        与 add_listener 的区别：add_listener 只监听指定的单个会话，
        add_all 监听所有会话（含后续新建的群聊等）。
        """
        self._all_callback = callback
        self._discover_new = discover
        sessions = self.db.get_sessions(limit=100)
        
        if fast_init:
            print(f'[DEBUG] add_all: 快速注册 {len(sessions)} 个会话（跳过 watermark 查询）', flush=True)
            for s in sessions:
                username = s["username"]
                if username not in self._callbacks:
                    self._callbacks.setdefault(username, []).append(callback)
                    if username not in self._watermark:
                        self._watermark[username] = -1
            print(f'[DEBUG] add_all: 快速注册完成', flush=True)
        else:
            print(f'[DEBUG] add_all: 注册 {len(sessions)} 个会话（逐个查询 watermark）', flush=True)
            for s in sessions:
                username = s["username"]
                if username not in self._callbacks:
                    self.add_listener(username, callback)
            print(f'[DEBUG] add_all: 注册完成', flush=True)

    @property
    def watermark(self) -> Dict[str, int]:
        return dict(self._watermark)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wxdb-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        with self._workers_lock:
            queues = list(self._worker_queues.values())
            threads = list(self._worker_threads.values())
        for q in queues:
            q.put(_LISTENER_STOP)
        for t in threads:
            t.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as exc:  # 单次轮询失败不终止监听
                sys.stderr.write("listener poll error: %r\n" % exc)
            self._stop.wait(self.interval)

    _WM_BATCH = 5  # watermark init per poll to avoid blocking

    def _poll_once(self) -> None:
        error_count = getattr(self, "_poll_error_count", 0)
        poll_count = getattr(self, "_poll_count", 0) + 1
        self._poll_count = poll_count
        # 自动发现新会话（add_all 的 discover 模式）
        if getattr(self, '_discover_new', False) and getattr(self, '_all_callback', None):
            try:
                sessions = self.db.get_sessions(limit=100)
                for s in sessions:
                    username = s["username"]
                    if username not in self._callbacks:
                        self._callbacks.setdefault(username, []).append(self._all_callback)
                        if username not in self._watermark:
                            self._watermark[username] = -1
            except Exception:
                pass
        # Batch watermark initialization: process _WM_BATCH per poll
        # to avoid blocking the poll loop for minutes on first start
        if not hasattr(self, '_wm_pending'):
            self._wm_pending = {
                u for u, v in self._watermark.items() if v == -1
            }
        if not hasattr(self, '_wm_fail'):
            self._wm_fail = {}
        wm_batch = sorted(self._wm_pending)[:self._WM_BATCH]
        for user in wm_batch:
            try:
                latest = self.db.get_messages(user, limit=1)
                if latest:
                    self._watermark[user] = latest[0]['sort_seq']
                else:
                    self._watermark[user] = 0
            except Exception:
                self._wm_fail[user] = self._wm_fail.get(user, 0) + 1
                if self._wm_fail[user] >= 3:
                    sys.stderr.write(
                        "[wechatauto] watermark init failed %d times for %s, giving up\n"
                        % (self._wm_fail[user], user[:20])
                    )
                    self._watermark[user] = 0
                continue
            self._wm_pending.discard(user)
            self._wm_fail.pop(user, None)
        pending_count = len(self._wm_pending)
        # heartbeat log: first poll + every 6th (~30s at 5s interval)
        if poll_count == 1 or poll_count % 6 == 0:
            bad = len(getattr(self.db, '_bad_shards', {}))
            total = len(self._callbacks)
            wm_set = sum(1 for v in self._watermark.values() if v != -1)
            sys.stderr.write(
                "[wechatauto] poll #%d: %d sessions, %d watermarks set, "
                "%d pending init, %d bad shards\n"
                % (poll_count, total, wm_set, pending_count, bad)
            )
        for user, callbacks in list(self._callbacks.items()):
            since = self._watermark.get(user, 0)
            # Skip sessions whose watermark is still being initialized
            if since == -1:
                continue
            try:
                msgs = self.db.get_new_messages(user, since_seq=since)
            except sqlite3.DatabaseError as e:
                self._poll_error_count = error_count + 1
                if self._poll_error_count % 10 == 0:  # Log every 10 errors
                    sys.stderr.write("[wechatauto] 轮询错误(已抑制): %s\n" % e)
                continue
            except Exception:
                continue
            if msgs:
                print(f'[LISTENER] 会话 {user} 收到 {len(msgs)} 条新消息', flush=True)
            if not msgs:
                continue
            self._watermark[user] = msgs[-1]["sort_seq"]
            if not callbacks:
                continue
            self._dispatch(user, msgs)

    def _dispatch(self, user: str, msgs: List[dict]) -> None:
        """把新消息交给该会话的工作线程处理，不阻塞轮询线程。"""
        with self._workers_lock:
            q = self._worker_queues.get(user)
            if q is None:
                q = queue.Queue()
                self._worker_queues[user] = q
                t = threading.Thread(target=self._worker_run, args=(user,),
                                     name="wxmsg-%s" % user, daemon=True)
                self._worker_threads[user] = t
                t.start()
        cbs = tuple(self._callbacks.get(user, ()))
        for m in msgs:
            m["username"] = user
            q.put((m, cbs))

    def _worker_run(self, user: str) -> None:
        q = self._worker_queues.get(user)
        if q is None:
            return
        while True:
            task = q.get()
            if task is _LISTENER_STOP:
                break
            m, cbs = task
            for cb in cbs:
                try:
                    cb(m, self)
                except Exception as exc:
                    sys.stderr.write("listener callback error: %r\n" % exc)


def _extract_path_from_config(content: str) -> Optional[str]:
    """从配置内容中提取数据目录路径，兼容 JSON 字段 / 纯路径 / 任意文本。

    微信 4.x 不同版本配置文件格式不一：有的是纯路径，有的是 JSON
    （字段如 dataDir / fileSavePath）。这里统一兜底提取第一个 Windows 路径。
    """
    content = (content or "").strip().lstrip("\ufeff")
    if not content:
        return None
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            for key in ("dataDir", "data_dir", "fileSavePath", "savePath",
                        "path", "defaultFileSavePath"):
                v = obj.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, str) and re.match(r"^[A-Za-z]:[\\/]", item):
                    return item
    except Exception:
        pass
    if re.match(r"^[A-Za-z]:[\\/]", content):
        return content
    m = re.search(r"[A-Za-z]:[\\/][^\s\x00-\x1f\"']+", content)
    if m:
        return m.group(0).rstrip("\\/")
    return None


def _config_candidates() -> List[str]:
    """可能的微信 4.x 配置目录（按新旧版本与 32/64 位安装差异）。"""
    out = []
    for env in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env, "")
        if base:
            out.extend([
                os.path.join(base, "Tencent", "xwechat"),
                os.path.join(base, "Tencent", "xwechat", "config"),
                os.path.join(base, "Tencent", "WeChat"),
            ])
    return out


def _registry_data_dirs() -> List[str]:
    """从注册表读取可能指向数据目录的值（用户自定义保存位置时补充来源）。"""
    import winreg
    dirs = []
    for hive, sub in (
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\xwechat"),
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\xwechat\config"),
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat"),
    ):
        try:
            key = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    name, data, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if not isinstance(data, str) or not data.strip():
                    continue
                low = name.lower()
                if "path" in low or "dir" in low or "save" in low:
                    dirs.append(data.strip())
        finally:
            winreg.CloseKey(key)
    return dirs


def _locate_account_root(root: Optional[str]) -> Optional[str]:
    """在候选根目录下定位「包含账号目录」的目录。

    账号目录以含 db_storage 子目录为准（微信号不一定以 wxid_ 开头）。
    兼容两种布局：
      <root>/xwechat_files/<account>/db_storage
      <root>/<account>/db_storage
    返回的目录即 WeChatDB.db_dir（账号目录的父目录）。
    """
    if not root or not os.path.isdir(root):
        return None
    root = root.rstrip("\\/")
    candidates = [root]
    for name in ("xwechat_files", "WeChat Files", "xwechat_files_data"):
        candidates.append(os.path.join(root, name))
    seen = set()
    for cand in candidates:
        cand = cand.rstrip("\\/")
        if cand in seen or not os.path.isdir(cand):
            continue
        seen.add(cand)
        try:
            dirs = os.listdir(cand)
        except OSError:
            continue
        if any(
            os.path.isdir(os.path.join(cand, d, "db_storage"))
            for d in dirs
        ):
            return cand
    return None


def auto_detect_db_dir() -> Optional[str]:
    """自动定位微信 4.x 数据目录（不同电脑存储位置不同）。

    探测顺序：
      1. 微信配置文件（%APPDATA%/%LOCALAPPDATA%，支持 JSON/纯路径/任意文本）；
      2. 注册表；
      3. 常见默认目录（Documents / 用户主目录）。
    """
    # 1) 配置文件
    for cfg_dir in _config_candidates():
        if not os.path.isdir(cfg_dir):
            continue
        for fp in glob.glob(os.path.join(cfg_dir, "*")):
            if os.path.isdir(fp):
                continue
            try:
                raw = open(fp, "r", encoding="utf-8").read(8192)
            except (UnicodeDecodeError, OSError):
                try:
                    raw = open(fp, "r", encoding="gbk").read(8192)
                except (UnicodeDecodeError, OSError):
                    continue
            path = _extract_path_from_config(raw)
            if not path:
                continue
            hit = _locate_account_root(path)
            if hit:
                return hit
    # 2) 注册表
    for p in _registry_data_dirs():
        hit = _locate_account_root(p)
        if hit:
            return hit
    # 3) 常见默认目录兜底
    userprofile = os.environ.get("USERPROFILE", "")
    for base in (os.path.join(userprofile, "Documents"), userprofile):
        hit = _locate_account_root(base)
        if hit:
            return hit
    return None
