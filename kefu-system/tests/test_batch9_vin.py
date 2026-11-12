# -*- coding: utf-8 -*-
"""批次 9：VIN 识别率——「读不对」的真正原因是图太小，不是提示词。

问题现场（2026-11-12 用户反馈）：
    图：前挡风玻璃下方 VIN 铭牌
    真值：LFV3A24F2A3102185
    线上：LFV3A23P2N3782165（前 7 位对，后面全是编的）

实测定位（不是猜的）：
- 用户提供的原图 603x799，同一套提示词下模型**读得对**；
- 线上真正送进模型的是 ``filehelper_138_thumb.jpg`` = **136x180**（微信只给了
  缩略图），VIN 一行字符高 2~3 像素，人眼放大也看不清 → 模型硬编 17 位；
- 缩略图的成因：原图 ``.dat`` 是 ``wxgf``(HEVC) 容器，本机无 ffmpeg → 必然回退。

三层修复：
1. ``wechatauto.media._wxgf_to_jpg`` 增加 OpenCV 兜底（本机 cv2 5.0 实测能解
   HEVC 裸流，输出 1706x1280 原图），不再依赖用户装 ffmpeg；
2. ``vision.VIN_MIN_IMAGE_SIDE``：送模型图长边 < 600 时，代码侧丢弃模型给的
   VIN（提示词同时明说「必须返回空字符串」）；
3. ``vision.vin_check_digit_ok``：VIN 第 9 位校验（GB 16735 / ISO 3779），
   校验位不符丢弃——实测真值通过、编造的那个差 7。
"""
from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from app import vision

pytest_plugins = ["tests.test_batch6_media"]

from tests.test_batch6_media import (  # noqa: E402
    FakeVisionClient,
    _jpeg_bytes,
    _noise_image,
)

TRUTH = "LFV3A24F2A3102185"          # 用户案例真值（校验位通过）
HALLUCINATED = "LFV3A23P2N3782165"   # 线上误识别值（校验位不符）


def _big_jpeg() -> bytes:
    """长边 900 的图：足够读 VIN（高于 VIN_MIN_IMAGE_SIDE）。"""
    return _jpeg_bytes(_noise_image(size=(900, 700)))


def _small_jpeg() -> bytes:
    """240x180：模拟微信缩略图（低于 VIN_MIN_IMAGE_SIDE）。"""
    return _jpeg_bytes(_noise_image(size=(240, 180)))


class TestCheckDigit:
    @pytest.mark.parametrize("vin", [
        TRUTH,                  # 用户案例真值
        "1M8GDM9AXKP042788",    # 校验位为 X 的经典样本
    ])
    def test_valid_passes(self, vin):
        assert vision.vin_check_digit_ok(vin) is True

    @pytest.mark.parametrize("vin", [
        HALLUCINATED,           # 线上模型编造的那个（差 7）
        "LSGMJ5P578V016154",    # 前 8 位同名但校验位不对
        "LFV3A24F2A310218",     # 16 位
        "LFV3A24F2A31021855",   # 18 位
        "LFV3A24F2A3102I85",    # 含 I（非法字符）
        "",
    ])
    def test_invalid_fails(self, vin):
        assert vision.vin_check_digit_ok(vin) is False

    def test_lowercase_accepted(self):
        assert vision.vin_check_digit_ok(TRUTH.lower()) is True


class TestLowResGuard:
    def test_small_image_drops_vin(self):
        """缩略图级别（240x180）→ 模型给的 VIN 一律丢弃并标记 low_res。"""
        client = FakeVisionClient(content=json.dumps(
            {"is_vehicle_part": True, "part_name": "车架号铭牌",
             "vin": TRUTH, "reason": "x"}, ensure_ascii=False))
        result = vision.analyze_image(_small_jpeg(), client=client)
        assert result["is_vehicle_part"] is True   # 部件判断不受影响
        assert result["vin"] == ""
        assert result["vin_rejected"] == "low_res"

    def test_small_image_prompt_has_warning(self):
        """低分辨率时提示词要明确「别猜」，减少模型输出 VIN 的概率。"""
        client = FakeVisionClient(content=json.dumps(
            {"is_vehicle_part": True, "part_name": "x", "vin": "",
             "reason": "y"}, ensure_ascii=False))
        vision.analyze_image(_small_jpeg(), client=client)
        text = client.kwargs["messages"][0]["content"][0]["text"]
        assert "分辨率较低" in text and "不要猜测" in text

    def test_big_image_prompt_has_no_warning(self):
        client = FakeVisionClient()
        vision.analyze_image(_big_jpeg(), client=client)
        text = client.kwargs["messages"][0]["content"][0]["text"]
        assert "分辨率较低" not in text


class TestCheckDigitGuard:
    def test_hallucinated_vin_dropped_on_big_image(self):
        """图够大但校验位不符 → 丢弃（这是本次用户案例的防线）。"""
        client = FakeVisionClient(content=json.dumps(
            {"is_vehicle_part": True, "part_name": "整车",
             "vin": HALLUCINATED, "reason": "x"}, ensure_ascii=False))
        result = vision.analyze_image(_big_jpeg(), client=client)
        assert result["vin"] == ""
        assert result["vin_rejected"] == "check_digit"

    def test_valid_vin_kept_on_big_image(self):
        client = FakeVisionClient(content=json.dumps(
            {"is_vehicle_part": True, "part_name": "整车",
             "vin": TRUTH.lower(), "reason": "x"}, ensure_ascii=False))
        result = vision.analyze_image(_big_jpeg(), client=client)
        assert result["vin"] == TRUTH
        assert result["vin_rejected"] == ""

    def test_no_vin_no_reject_mark(self):
        client = FakeVisionClient(content=json.dumps(
            {"is_vehicle_part": True, "part_name": "保险杠", "vin": "",
             "reason": "看不到车架号"}, ensure_ascii=False))
        result = vision.analyze_image(_big_jpeg(), client=client)
        assert result["vin"] == "" and result["vin_rejected"] == ""


class TestWxgfDecode:
    """wxgf(HEVC) 解码兜底：无 ffmpeg 时用 OpenCV。"""

    def test_garbage_returns_none(self):
        from wechatauto.media import MediaDownloader
        assert MediaDownloader._hevc_to_jpg_cv2(b"\x00\x00\x00\x01garbage") is None

    def test_falls_back_to_cv2_when_no_ffmpeg(self, monkeypatch):
        from wechatauto.media import MediaDownloader
        monkeypatch.setattr(MediaDownloader, "_ffmpeg_exe",
                            staticmethod(lambda: None))
        monkeypatch.setattr(MediaDownloader, "_hevc_to_jpg_cv2",
                            staticmethod(lambda hevc: b"\xff\xd8\xff-fake"))
        out = MediaDownloader._wxgf_to_jpg(
            object.__new__(MediaDownloader),
            b"wxgf" + b"\x00" * 10 + b"\x00\x00\x00\x01" + b"payload")
        assert out == b"\xff\xd8\xff-fake"

    def test_ffmpeg_preferred_when_available(self, monkeypatch):
        from wechatauto.media import MediaDownloader
        monkeypatch.setattr(MediaDownloader, "_hevc_to_jpg_ffmpeg",
                            staticmethod(lambda hevc: b"\xff\xd8\xff-ffmpeg"))
        monkeypatch.setattr(MediaDownloader, "_hevc_to_jpg_cv2",
                            staticmethod(lambda hevc: b"\xff\xd8\xff-cv2"))
        out = MediaDownloader._wxgf_to_jpg(
            object.__new__(MediaDownloader),
            b"wxgf" + b"\x00" * 10 + b"\x00\x00\x00\x01" + b"payload")
        assert out == b"\xff\xd8\xff-ffmpeg"

    def test_real_wxgf_sample_decodes(self):
        """本机真实样本回归（无样本/cv2 不可用时跳过）。"""
        import glob
        from wechatauto.media import MediaDownloader
        files = glob.glob(
            r"D:\Git\wechatauto-replica\kefu-system\data\imgcache\*.wxgf")
        if not files:
            pytest.skip("本机没有 wxgf 样本")
        with open(files[0], "rb") as f:
            raw = f.read()
        out = MediaDownloader._wxgf_to_jpg(
            object.__new__(MediaDownloader), raw)
        if out is None:
            pytest.skip("本机 cv2 不支持 HEVC 解码")
        with Image.open(io.BytesIO(out)) as im:
            # 原图量级（缩略图只有 136x180，这个断言能区分两者）
            assert max(im.size) >= 800


class TestThumbnailUpgrade:
    """老缓存（``*_thumb.jpg``）要被重新解成原图，否则补上解码能力也没用。"""

    @staticmethod
    def _dl():
        from wechatauto.media import MediaDownloader
        dl = object.__new__(MediaDownloader)
        dl._upgrade_tried = set()   # 覆盖类级缓存，避免用例间互相污染
        return dl

    def test_non_thumb_cache_skipped(self):
        from wechatauto.media import MediaDownloader
        dl = self._dl()
        assert MediaDownloader._upgrade_thumbnail_cache(
            dl, "u", 1, "a" * 32, 0, "/c/u_1.jpg", b"x" * 10,
            None, None, None) is None

    def test_upgrade_returns_bigger_image(self, monkeypatch):
        dl = self._dl()
        monkeypatch.setattr(dl, "_decode_usable_image",
                            lambda *a, **k: (b"B" * 500, False))
        monkeypatch.setattr(dl, "_write_image_cache",
                            lambda *a, **k: "/c/u_1.jpg")
        out = dl._upgrade_thumbnail_cache(
            "u", 1, "a" * 32, 0, "/c/u_1_thumb.jpg", b"x" * 10,
            None, None, None)
        assert out == (b"B" * 500, "/c/u_1.jpg")

    def test_thumbnail_result_keeps_cache(self, monkeypatch):
        """重新解出来还是缩略图（原图确实没有）→ 不升级、不报错。"""
        dl = self._dl()
        monkeypatch.setattr(dl, "_decode_usable_image",
                            lambda *a, **k: (b"B" * 500, True))
        assert dl._upgrade_thumbnail_cache(
            "u", 1, "a" * 32, 0, "/c/u_1_thumb.jpg", b"x" * 10,
            None, None, None) is None

    def test_same_image_only_tried_once(self, monkeypatch):
        dl = self._dl()
        calls = {"n": 0}

        def fake_decode(*a, **k):
            calls["n"] += 1
            return None

        monkeypatch.setattr(dl, "_decode_usable_image", fake_decode)
        for _ in range(3):
            dl._upgrade_thumbnail_cache(
                "u", 1, "a" * 32, 0, "/c/u_1_thumb.jpg", b"x" * 10,
                None, None, None)
        assert calls["n"] == 1
