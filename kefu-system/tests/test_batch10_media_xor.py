# -*- coding: utf-8 -*-
"""批次 10：图片 XOR 密钥推导修复（复制粘贴的图片拿不到原图）。

现场：用户在文件传输助手里**复制粘贴**发图，日志报

    ［media］图片不完整或疑似花屏，跳过：xxx.dat（5902 字节）
    ［media］图片不完整或疑似花屏，跳过：xxx_h.dat（566136 字节）
    ［media］原图不可用，已回退缩略图：xxx_t.dat

而同一张图用「文件」方式发送就正常。实测（真实样本 10 张）：

- ``_h.dat`` 用生产推导出的密钥解密 → 0/10 可用；
- 用真实密钥 0xF9 解密 → **10/10 可用**（PNG 603x799 / JPEG 1702x1276 等）。

根因：``_derive_xor_key`` 用 ``dat_path[:-4] + "_t.dat"`` 拼同图兄弟名，
传 ``<md5>_h.dat`` 时得到 ``<md5>_h`` → ``<md5>_h_t.dat``（不存在），
于是**永远**落到默认 0x88 → 原图解成乱码被判「花屏」→ 回退缩略图。

本文件锁定：suffix 归一化、兄弟密钥推导、PNG 尾部反推、候选密钥回退。
"""
import io
import os
import sys
from typing import List, Optional

import pytest
from PIL import Image

# 仓库根目录（wechatauto 包的父级）——其它测试靠先 import app 顺带把路径加进去，
# 这里只依赖 wechatauto，显式加一次更稳。
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from wechatauto.media import MediaDownloader, reset_verified_cache, xor_bytes

USER = "filehelper"
MD5 = "3f202434ed9fbc03dc3ffe491c217220"
KEY = 0xF9


# --------------------------------------------------------------------------
# 造图工具
# --------------------------------------------------------------------------
def _png_bytes(size=(300, 400)) -> bytes:
    """造一张「内容正常」的 PNG（噪声，不会被花屏规则误杀）。"""
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = ((x * 7 + y * 3) % 256, (x * 5) % 256, (y * 11) % 256)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(size=(200, 150)) -> bytes:
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = ((x * 9) % 256, (y * 5 + x) % 256, (x + y * 3) % 256)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _write(path, tail: bytes, size: int = 64) -> str:
    """写字面量文件，尾部固定为 tail（模拟 .dat 的加密尾部）。"""
    path.write_bytes(b"\x00" * size + tail)
    return str(path)


class FakeDb:
    """load_image 只需要一个能返回图片消息行的 db。"""

    def __init__(self, content: bytes):
        self.row = {
            "local_id": 1,
            "local_type": 3,
            "packed_info": b"",
            "content": content,
            "create_time": 1794492000,
        }

    def get_message_row(self, user: str, local_id: int):
        return dict(self.row)


def _downloader(tmp_path) -> MediaDownloader:
    reset_verified_cache()
    return MediaDownloader(
        FakeDb(b"<img md5='%s'/>" % MD5.encode()), save_dir=str(tmp_path))


# --------------------------------------------------------------------------
# 1. 文件名归一化（根因）
# --------------------------------------------------------------------------
class TestDatStem:
    @pytest.mark.parametrize("name", [MD5 + ".dat", MD5 + "_h.dat",
                                      MD5 + "_t.dat"])
    def test_same_stem_for_three_siblings(self, name):
        """三种 .dat 必须归一化到同一个 stem —— 这是拼兄弟文件名的前提。"""
        got = MediaDownloader._dat_stem("C:/img/" + name)
        assert got == "C:/img/" + MD5

    def test_h_dat_is_not_stripped_twice(self):
        """回归：旧实现 dat_path[:-4] 会把 _h.dat 削成 <md5>_h，兄弟名全错。"""
        got = MediaDownloader._dat_stem("C:/img/" + MD5 + "_h.dat")
        assert not got.endswith("_h")
        assert got.endswith(MD5)


# --------------------------------------------------------------------------
# 2. 密钥推导
# --------------------------------------------------------------------------
class TestXorKeyDerivation:
    def test_h_dat_uses_thumbnail_sibling(self, tmp_path):
        """回归核心：从 _h.dat 出发，密钥必须取自同图 _t.dat（而不是 0x88）。

        FF D9 ^ 0xF9 = 06 20，所以尾部 06 20 就意味着真实密钥是 0xF9。
        """
        _write(tmp_path / (MD5 + "_t.dat"), b"\x06\x20")
        h_path = _write(tmp_path / (MD5 + "_h.dat"), b"\x60\x82")

        dl = _downloader(tmp_path)
        assert dl._derive_xor_key(h_path) == KEY

    def test_png_tail_yields_key(self, tmp_path):
        """PNG 尾部 IEND CRC 末字节恒为 0x82，可单独反推密钥。

        注意：磁盘上的 .dat 尾部是**密文**，所以要按 0xF9 异或后再写，
        反推出来的才是 0xF9（明文 PNG 反推得到 0，那是「没加密」）。
        """
        png = _png_bytes()
        assert png[-1] == 0x82
        tail = xor_bytes(png[-12:], KEY)
        # 只有 _h.dat（没有 _t.dat）时，仍能靠 IEND 反推出密钥
        h_path = str(tmp_path / (MD5 + "_h.dat"))
        with open(h_path, "wb") as f:
            f.write(b"\x00" * 64 + tail)

        dl = _downloader(tmp_path)
        assert dl._derive_xor_key(h_path) == KEY

    def test_candidates_prefer_thumbnail_and_dedup(self, tmp_path):
        _write(tmp_path / (MD5 + "_t.dat"), b"\x06\x20")
        h_path = _write(tmp_path / (MD5 + "_h.dat"), b"\x06\x20")

        dl = _downloader(tmp_path)
        keys = dl._xor_key_candidates(h_path)
        assert keys[0] == KEY
        assert len(keys) == len(set(keys))          # 去重
        assert len(keys) <= 4                       # 有上界，别无限试

    def test_no_sibling_no_tail_keeps_legacy_default(self, tmp_path):
        """三个候选都推不出时保留旧默认值，不抛异常（历史行为兼容）。"""
        p = str(tmp_path / "lonely.dat")
        with open(p, "wb") as f:
            f.write(b"\x11\x22\x33\x44")
        dl = _downloader(tmp_path)
        assert dl._derive_xor_key(p) == 0x88

    def test_xor_bytes_roundtrip(self):
        data = bytes(range(256))
        assert xor_bytes(xor_bytes(data, KEY), KEY) == data
        assert xor_bytes(data, 0) == data           # key=0 是恒等


# --------------------------------------------------------------------------
# 3. 解密链：密钥不对时逐个回退，最终用上原图而不是缩略图
# --------------------------------------------------------------------------
class TestKeyFallbackInLoad:
    def test_h_dat_png_beats_thumbnail(self, tmp_path):
        """复制粘贴的场景：.dat 解不出可用图，_h.dat 用 0xF9 解出 PNG 原图。

        断言两点：① 返回的是 603x799 量级的原图，不是缩略图；
        ② 缓存文件名不带 ``_thumb`` —— 带后缀意味着以后永远只给缩略图。
        """
        png = _png_bytes()
        bubble = _write(tmp_path / (MD5 + ".dat"), b"\x00\x00")
        _write(tmp_path / (MD5 + "_t.dat"), b"\x06\x20")     # 提供正确密钥
        h_path = str(tmp_path / (MD5 + "_h.dat"))
        with open(h_path, "wb") as f:
            f.write(b"placeholder")

        dl = _downloader(tmp_path)
        dl._find_dat = (lambda user, md5, ts, thumbnail=False:
                        str(tmp_path / (MD5 + "_t.dat")) if thumbnail else bubble)
        dl._find_h_dat = lambda user, md5: h_path
        seen: List[Optional[int]] = []

        def fake_decrypt(path, aes_key=None, xor_key=None):
            seen.append(xor_key)
            if path == h_path and xor_key == KEY:
                return png
            return b"\x00" * 32          # 格式不完整 → 校验不过

        dl.decrypt_image = fake_decrypt

        data, path, reason = dl.load_image(USER, 1)
        assert reason == ""
        assert data == png
        assert path and not path.endswith("_thumb.jpg")
        assert KEY in seen

    def test_all_keys_fail_still_falls_back_to_thumbnail(self, tmp_path):
        """所有密钥都解不出可用图时，仍返回缩略图（既有行为不退化）。"""
        good = _jpeg_bytes()
        dat = _write(tmp_path / (MD5 + ".dat"), b"\x00\x00")
        thumb = _write(tmp_path / (MD5 + "_t.dat"), b"\x06\x20")

        dl = _downloader(tmp_path)
        dl._find_dat = (lambda user, md5, ts, thumbnail=False:
                        str(thumb) if thumbnail else dat)
        dl._find_h_dat = lambda user, md5: None
        dl.decrypt_image = lambda path, aes_key=None, xor_key=None: (
            good if path == str(thumb) else b"\x00" * 32)

        data, path, reason = dl.load_image(USER, 1)
        assert reason == ""
        assert data == good
        assert path and path.endswith("_thumb.jpg")
