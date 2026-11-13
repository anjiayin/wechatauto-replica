# -*- coding: utf-8 -*-
"""批次 13 需求 7 续：账号级 XOR 密钥兜底（图片「未就绪」真凶之一）。

现场：分析日志反复刷

    ［media］图片不完整或疑似花屏，跳过：xxx_t.dat（25191 字节）
    第 4 次重试：图片仍未就绪（原图与缩略图均不完整）…

用户在微信里**从没点开**过的图却大多能正常识别。实测该群 62 张图片消息：

- 42 张靠 ``_t.dat`` 直接识别成功（缩略图本身是完整小 JPEG）；
- 18 张被判「半截/花屏」，报「未就绪」。

对这 18 张逐张排查：文件大小、mtime 都对（消息到达那一刻就写完了，之后
再没变过），**并不是没下载完**。真正原因是单字节 XOR 密钥反推失手：

- 密钥按账号固定（本机实测 0xF9，批次 10 已记录）；
- 但 ``_key_from_tail`` 依赖「明文以 FF D9 结尾」，尾部带填充的文件会推出
  假密钥（0x11 / 0x2A / 0xDB …）；
- 用假密钥解密：AES 首块（含 JPEG 头）正常，之后熵编码数据全乱码 → 被
  ``is_complete_image`` / ``image_looks_broken`` 判成半截花屏。

实测同一账号 40 张缩略图：35 张反推出 0xF9 且可用，5 张反推出假密钥且不可用。
换用账号密钥 0xF9 重解那 18 张：**18/18 全部可用**（尺寸 210x467 等，非占位）。

本文件锁定：账号密钥投票 + 实测校验、失败短缓存、兜底路径、显式密钥不走兜底。
"""
import io
import os
import struct
import sys

import pytest
from PIL import Image

# 仓库根（wechatauto 包父级）
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wechatauto.media import (V2_MAGIC, MediaDownloader, is_usable_image,
                              reset_verified_cache, xor_bytes)

CHAT = "111@chatroom"
MD5 = "87ffb0b0765e5e9fa05d978a25155348"
KEY = 0xF9                      # 本机真实账号密钥（批次 10 实测）
AES_KEY = "0123456789abcdef"    # 造数据用；测试里显式注入，不碰内存扫描


# --------------------------------------------------------------------------
# 造图 / 造 .dat
# --------------------------------------------------------------------------
def _jpeg(size=(200, 150)) -> bytes:
    """噪声 JPEG（不会被「花屏」规则误杀，且体积 > 1024 字节）。"""
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = ((x * 9) % 256, (y * 5 + x) % 256, (x + y * 3) % 256)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _v2_dat(plain: bytes, key: int = KEY) -> bytes:
    """按微信 V2 格式加密 ``plain``：前 1024B 走 AES(ECB)，其余走单字节 XOR。"""
    aes_size = 1024
    head, rest = plain[:aes_size], plain[aes_size:]
    pad = 16 - len(head) % 16
    padded = head + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(AES_KEY.encode()), modes.ECB()).encryptor()
    cipher = enc.update(padded) + enc.finalize()
    return (V2_MAGIC + struct.pack("<LL", aes_size, len(rest)) + b"\x00"
            + cipher + xor_bytes(rest, key))


def _write_dat(account_dir, name: str, data: bytes) -> str:
    """按微信目录结构落地：attach/<会话md5>/<YYYY-MM>/Img/<name>。"""
    import hashlib
    chat_md5 = hashlib.md5(CHAT.encode()).hexdigest()
    folder = os.path.join(account_dir, "msg", "attach", chat_md5,
                          "2026-11", "Img")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


class FakeDb:
    """够用就好：load_image 只要消息行，密钥学习只要 account_dir/workdir。"""

    def __init__(self, account_dir: str, workdir: str):
        self.account_dir = account_dir
        self.workdir = workdir
        self.row = {
            "local_id": 1,
            "local_type": 3,
            "packed_info": b"",
            "content": b"<img md5='" + MD5.encode() + b"'/>",
            "create_time": 1794562783,
        }

    def get_message_row(self, user: str, local_id: int):
        return dict(self.row)


def _downloader(tmp_path, chat: str = CHAT) -> MediaDownloader:
    reset_verified_cache()
    acct = str(tmp_path / "account")
    os.makedirs(acct, exist_ok=True)
    db = FakeDb(acct, str(tmp_path / "work"))
    return MediaDownloader(db, save_dir=str(tmp_path), image_key=AES_KEY)


def _clean_thumb(account_dir, md5: str) -> str:
    """一张「尾部无填充」的缩略图：能自己反推出 0xF9。"""
    return _write_dat(account_dir, md5 + "_t.dat", _v2_dat(_jpeg()))


def _padded_thumb(account_dir, md5: str, noise: bytes = b"\x9a\x3c") -> str:
    """一张「尾部带噪声」的缩略图：反推不出 0xF9（模拟假密钥/推不出）。"""
    plain = _jpeg() + noise
    return _write_dat(account_dir, md5 + "_t.dat", _v2_dat(plain))


def _decode(dl, xor_key=None):
    """统一入口（测试里反复出现，单独抽一下免得行太长）。"""
    return dl._decode_usable_image(CHAT, MD5, 1794562783, None, xor_key)


def _counter(calls):
    """替身：统计 ``_learn_account_xor_key`` 被调了几次。"""
    def fake():
        calls["n"] = calls.get("n", 0) + 1
        return KEY
    return fake


# --------------------------------------------------------------------------
# 1. 账号密钥：投票 + 实测
# --------------------------------------------------------------------------
class TestLearnAccountKey:
    def test_majority_vote_finds_key(self, tmp_path):
        dl = _downloader(tmp_path)
        acct = dl.db.account_dir
        for i in range(5):
            _clean_thumb(acct, "%032x" % (i + 1))
        _padded_thumb(acct, "%032x" % 99)          # 一张反推不出，不参与投票
        assert dl._learn_account_xor_key() == KEY

    def test_vote_runs_through_verify(self, tmp_path):
        """票数最高但实测不可用时必须换下一个候选，不能盲信投票。"""
        dl = _downloader(tmp_path)
        acct = dl.db.account_dir
        # 3 张假密钥文件（尾部噪声恰好互相一致 → 票数最高）
        plain = _jpeg() + b"\x9a\x3c"
        for i in range(3):
            _write_dat(acct, "%032x" % (i + 1) + "_t.dat", _v2_dat(plain))
        for i in range(2):
            _clean_thumb(acct, "%032x" % (i + 100))
        # 假密钥（0x25）票数 3 > 真密钥 2，但实测解不出可用图
        assert dl._learn_account_xor_key() == KEY

    def test_empty_account_returns_none(self, tmp_path):
        dl = _downloader(tmp_path)
        assert dl._learn_account_xor_key() is None

    def test_no_account_dir_returns_none(self, tmp_path):
        """老测试里的 FakeDb 没有 account_dir —— 必须安静返回 None，不能抛。"""
        reset_verified_cache()
        db = type("D", (), {"get_message_row": lambda s, u, l: None})()
        dl = MediaDownloader(db, save_dir=str(tmp_path), image_key=AES_KEY)
        assert dl._learn_account_xor_key() is None

    def test_success_is_cached(self, tmp_path):
        dl = _downloader(tmp_path)
        acct = dl.db.account_dir
        for i in range(3):
            _clean_thumb(acct, "%032x" % (i + 1))
        calls = {"n": 0}
        real = dl._detect_account_xor_key

        def counting():
            calls["n"] += 1
            return real()

        dl._detect_account_xor_key = counting
        assert dl._learn_account_xor_key() == KEY
        assert dl._learn_account_xor_key() == KEY
        assert calls["n"] == 1          # 第二次命中缓存

    def test_negative_result_uses_short_ttl(self, tmp_path):
        dl = _downloader(tmp_path)
        calls = {"n": 0}

        def fake():
            calls["n"] += 1
            return None

        dl._detect_account_xor_key = fake
        assert dl._learn_account_xor_key() is None
        assert dl._learn_account_xor_key() is None
        assert calls["n"] == 1          # 失败也缓存（短 TTL），不反复扫盘
        assert dl._ACCT_KEY_NEG_TTL < dl._ACCT_KEY_TTL


# --------------------------------------------------------------------------
# 2. 兜底路径：逐文件反推失手时，账号密钥必须救回来
# --------------------------------------------------------------------------
class TestAccountKeyFallback:
    def test_padded_file_rescued_by_account_key(self, tmp_path):
        dl = _downloader(tmp_path)
        acct = dl.db.account_dir
        for i in range(3):                     # 账号里有干净样本可供学习
            _clean_thumb(acct, "%032x" % (i + 1))
        _padded_thumb(acct, MD5)               # 目标图：自身反推失手

        got = _decode(dl)
        assert got is not None, "账号密钥兜底应救回这张图"
        data, thumbnail = got
        assert is_usable_image(data)
        assert thumbnail is True                # 只有缩略图，标成缩略图

    def test_still_none_when_account_key_also_wrong(self, tmp_path):
        """账号里没有可学样本时，坏文件仍是 None（不硬凑一张花图）。"""
        dl = _downloader(tmp_path)
        _padded_thumb(dl.db.account_dir, MD5)
        assert _decode(dl) is None

    def test_explicit_key_skips_learning(self, tmp_path):
        """显式传密钥的调用方要的是「只认这一把」，不许偷偷换账号密钥。"""
        dl = _downloader(tmp_path)
        acct = dl.db.account_dir
        for i in range(3):
            _clean_thumb(acct, "%032x" % (i + 1))
        _padded_thumb(acct, MD5)
        calls = {"n": 0}
        dl._learn_account_xor_key = _counter(calls)
        assert _decode(dl, xor_key=0x88) is None
        assert calls["n"] == 0

    def test_learned_key_already_tried_is_skipped(self, tmp_path):
        """账号密钥若已在候选里试过，不重复解密同一把（省一次解码）。"""
        dl = _downloader(tmp_path)
        acct = dl.db.account_dir
        for i in range(3):
            _clean_thumb(acct, "%032x" % (i + 1))
        # 目标图用**别的**密钥加密：候选里已有 KEY 也解不出
        plain = _jpeg() + b"\x9a\x3c"
        path = _write_dat(acct, MD5 + "_t.dat", _v2_dat(plain, 0x25))
        dl._learn_account_xor_key = lambda: KEY
        # 人为把账号密钥塞进该文件的候选，模拟「第一轮已经试过」
        dl._xor_key_candidates = lambda p: [KEY] if p == path else [0x88]
        calls = []
        real = dl._decode_file

        def counting(p, keys, aes):
            calls.append(keys)
            return real(p, keys, aes)

        dl._decode_file = counting
        assert _decode(dl) is None
        assert len(calls) == 1          # 第二轮被跳过，没有用同一把密钥再解一次

    def test_clean_file_unaffected(self, tmp_path):
        """本来就能解的图不受影响（第一轮就命中，不进兜底）。"""
        dl = _downloader(tmp_path)
        _clean_thumb(dl.db.account_dir, MD5)
        calls = {"n": 0}
        dl._learn_account_xor_key = _counter(calls)
        got = _decode(dl)
        assert got is not None and is_usable_image(got[0])
        assert calls["n"] == 0


@pytest.mark.parametrize("key", [0x00, 0xF9, 0x25])
def test_v2_helper_roundtrip(tmp_path, key):
    """自造 .dat 的辅助函数本身要可信（否则上面的用例都是假的）。"""
    dl = _downloader(tmp_path)
    plain = _jpeg()
    path = _write_dat(dl.db.account_dir, "x_t.dat", _v2_dat(plain, key))
    assert dl.decrypt_image(path, AES_KEY, xor_key=key) == plain
