# -*- coding: utf-8 -*-
"""批次 6 B 组（图片链路）回归测试。

覆盖两部分：

1. 待办 1 · 图片花屏（``wechatauto/media.py``）
   - ``is_complete_image``：完整 JPEG/PNG/GIF 为 True，半截 JPEG 为 False；
   - ``image_looks_broken``：解码失败/纯色占位/「顶部一条真图 + 下面是整片
     纯绿」判坏，正常多色照片判好；
   - ``MediaDownloader.load_image``：原图不合格自动回退缩略图、两者都不合格
     返回 None（不返回花图）、坏缓存就地删除后重建（自愈）。

2. 需求 5 · 图片内容识别（``app/vision.py`` + 落库/管线/监听/接口）
   - 坏图抛 ``VisionError``（不消耗 token）；
   - 请求结构：``messages[].content`` 含 ``text`` + ``image_url`` 的 base64
     data URI，``response_format=json_object``，长边已缩到 ≤1024；
   - 正常 JSON 解析 / 非 JSON 报错 / 429 中文映射 / 非法 VIN 丢弃；
   - ``image_analysis`` 表存取与覆盖；
   - 管线：``has_image_sell=True`` 时 direction 强制 sell（AI 原返回 buy 也覆盖）；
   - 监听/异步队列：``_on_message`` 只入队（视觉调用睡 0.5s 时仍 <0.1s 返回）；
     worker 消费后车辆图 → 聚合器收到 ``image_sell=True`` 的文本消息（行时间
     取原始消息时间）；非车辆 → skipped 不进聚合；队列满 → skipped「队列已满」
     且不抛异常；worker 内非预期异常不终止线程；开关关闭 → 原同步 skipped
     文案逐字不变；命中缓存不调模型；start/stop 生命周期带起停 worker；
   - 接口：``GET /api/messages/{u}/images/{id}/analysis`` 200 / 404；
     ``/api/status`` 的 ``queue.vision_pending``（无该属性时默认 0）。

运行（在 kefu-system 目录下）::

    python -m pytest tests/test_batch6_media.py -v --basetemp=D:/tmp/kefu-pytest-b6c
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import queue
import random
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app import vision
from app.aggregator import Aggregator
from app.api import ALL_ROUTERS, deps
from app.config import KEY_IMAGE_VISION_ENABLED, VISION_WORKERS
from app.db import Database
from app.notifier import Notifier
from app.pipeline import Pipeline
from app.wechat_service import WeChatService
from tests.test_core import FakeChatClient, make_unit
from tests.test_api import FakePipeline, FakeWechatService
from wechatauto.media import (
    MediaDownloader,
    image_looks_broken,
    is_complete_image,
    is_usable_image,
    reset_verified_cache,
)

USER = "21264829018@chatroom"


# ---------------------------------------------------------------------------
# 图片样本（全部用 Pillow 合成，不读真实微信库）
# ---------------------------------------------------------------------------
SIZE = (240, 180)


def _noise_image(size=SIZE, seed: int = 7) -> Image.Image:
    """噪声图：颜色足够多，代表「正常照片」。"""
    rnd = random.Random(seed)
    img = Image.new("RGB", size)
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                 for _ in range(size[0] * size[1])])
    return img


def _jpeg_bytes(img: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _complete_jpeg() -> bytes:
    """正常多色 JPEG（完整、不花屏）。"""
    return _jpeg_bytes(_noise_image())


def _green_block_jpeg() -> bytes:
    """复现花图：顶部 1/3 是真实图像，下面整片纯绿。"""
    width, height = SIZE
    img = _noise_image()
    green = Image.new("RGB", (width, height - height // 3), (0, 200, 0))
    img.paste(green, (0, height // 3))
    return _jpeg_bytes(img)


def _truncated_jpeg() -> bytes:
    """半截文件：砍掉尾部 100 字节（没有 FF D9 结束符）。"""
    return _complete_jpeg()[:-100]


def _solid_jpeg() -> bytes:
    """纯色占位图（颜色数 < 16）。"""
    img = Image.new("RGB", SIZE, (12, 34, 56))
    return _jpeg_bytes(img)


def _white_backdrop_jpeg() -> bytes:
    """白底实拍商品图：上面 55% 是配件实物（多色），下面 45% 纯白。

    底部 1/3 完全落在白底区域内（边界在 55%，远离底部三分之一），所以
    「底部单色占比」天然接近 1.0 —— 这正是修 P2 前会被误判成花图的形态。
    """
    width, height = SIZE
    img = _noise_image()
    white = Image.new("RGB", (width, height - int(height * 0.55)), (255, 255, 255))
    img.paste(white, (0, int(height * 0.55)))
    return _jpeg_bytes(img)


def _green_block_jpeg_exact() -> bytes:
    """真实花图主色：底部整片 ``(0,135,0)``（min 通道 0，不享受白底豁免）。"""
    width, height = SIZE
    img = _noise_image()
    green = Image.new("RGB", (width, height - height // 3), (0, 135, 0))
    img.paste(green, (0, height // 3))
    return _jpeg_bytes(img)


def _light_gray_backdrop_jpeg() -> bytes:
    """近中性浅灰底（235,235,238）：不算花图（白底/浅灰底豁免）。"""
    width, height = SIZE
    img = _noise_image()
    gray = Image.new("RGB", (width, height - int(height * 0.55)), (235, 235, 238))
    img.paste(gray, (0, int(height * 0.55)))
    return _jpeg_bytes(img)


# ---------------------------------------------------------------------------
# 待办 1：完整性 / 花屏校验
# ---------------------------------------------------------------------------
class TestCompleteImage:
    def test_complete_jpeg_true(self):
        assert is_complete_image(_complete_jpeg()) is True

    def test_truncated_jpeg_false(self):
        assert is_complete_image(_truncated_jpeg()) is False

    def test_png_complete_true(self):
        buf = io.BytesIO()
        _noise_image().save(buf, format="PNG")
        assert is_complete_image(buf.getvalue()) is True

    def test_png_truncated_false(self):
        buf = io.BytesIO()
        _noise_image().save(buf, format="PNG")
        assert is_complete_image(buf.getvalue()[:-40]) is False

    def test_gif_complete_true(self):
        buf = io.BytesIO()
        _noise_image().save(buf, format="GIF")
        assert is_complete_image(buf.getvalue()) is True

    def test_empty_and_unknown_false(self):
        assert is_complete_image(b"") is False
        assert is_complete_image(b"wxgf" + b"\x00" * 64) is False


class TestImageLooksBroken:
    def test_green_block_detected(self):
        """实测花图形态：顶部真图 + 下面整片纯绿 → 判坏。"""
        data = _green_block_jpeg()
        assert is_complete_image(data) is True     # 结构其实是完整的
        assert image_looks_broken(data) is True

    def test_normal_photo_ok(self):
        assert image_looks_broken(_complete_jpeg()) is False

    def test_solid_color_detected(self):
        assert image_looks_broken(_solid_jpeg()) is True

    def test_undecodable_detected(self):
        assert image_looks_broken(b"\xff\xd8\xff\xe0" + b"\x00" * 100) is True

    def test_usable_helper(self):
        assert is_usable_image(_complete_jpeg()) is True
        assert is_usable_image(_green_block_jpeg()) is False

    # --- P2：白底/浅灰底商品图不能被判坏（否则缓存会被清、图永久降级）---
    def test_white_backdrop_not_broken(self):
        """白底实拍（配件在上 55%、底部 45% 纯白）→ 不是花图。"""
        data = _white_backdrop_jpeg()
        assert is_complete_image(data) is True
        assert image_looks_broken(data) is False
        assert is_usable_image(data) is True

    def test_light_gray_backdrop_not_broken(self):
        """近中性浅灰底（235,235,238）→ 不是花图。"""
        data = _light_gray_backdrop_jpeg()
        assert image_looks_broken(data) is False

    def test_exact_green_bottom_still_broken(self):
        """真绿块主色 (0,135,0)：min 通道 0，不享受白底豁免，仍判坏。"""
        assert image_looks_broken(_green_block_jpeg_exact()) is True

    def test_dark_uniform_bottom_still_broken(self):
        """深色纯色底（12,34,56）不属白/浅灰豁免，仍判坏。"""
        width, height = SIZE
        img = _noise_image()
        dark = Image.new("RGB", (width, height - int(height * 0.55)), (12, 34, 56))
        img.paste(dark, (0, int(height * 0.55)))
        assert image_looks_broken(_jpeg_bytes(img)) is True


# ---------------------------------------------------------------------------
# 待办 1：下载选图 + 缓存自愈
# ---------------------------------------------------------------------------
class FakeImgDb:
    """MediaDownloader 需要的 WeChatDB 最小替身（不触碰真实微信库）。"""

    def __init__(self, packed_info: bytes, local_type: int = 3) -> None:
        self.row = {
            "local_id": 1,
            "local_type": local_type,
            "packed_info": packed_info,
            "content": b"",
            "create_time": int(time.time()),
        }

    def get_message_row(self, user: str, local_id: int):
        return dict(self.row)


class TestDownloadImageSelection:
    def _downloader(self, tmp_path, **kwargs) -> MediaDownloader:
        reset_verified_cache()
        db = FakeImgDb(b"<img md5='0123456789abcdef0123456789abcdef'/>")
        return MediaDownloader(db, save_dir=str(tmp_path), **kwargs)

    def test_falls_back_to_thumbnail_when_original_broken(self, tmp_path):
        """原图「完整但花屏」→ 自动回退缩略图，返回好图而不是花图。"""
        dl = self._downloader(tmp_path)
        orig = tmp_path / "orig.dat"
        thumb = tmp_path / "thumb_t.dat"
        orig.write_bytes(b"x")
        thumb.write_bytes(b"y")
        good = _complete_jpeg()
        broken = _green_block_jpeg()

        dl._find_dat = lambda user, md5, ts, thumbnail=False: (
            str(thumb) if thumbnail else str(orig))
        dl._find_h_dat = lambda user, md5: None
        dl.decrypt_image = lambda path, aes_key=None, xor_key=None: (
            broken if path == str(orig) else good)

        data, path, reason = dl.load_image(USER, 1)
        assert reason == ""
        assert data == good
        # 落盘文件名带 _thumb 后缀，且内容就是缩略图
        assert path and path.endswith("_thumb.jpg")
        assert (tmp_path / path.split("\\")[-1].split("/")[-1]).read_bytes() == good

    def test_returns_none_when_all_candidates_broken(self, tmp_path):
        """原图与缩略图都不合格 → 返回 None（宁可占位也不要花图）。"""
        dl = self._downloader(tmp_path)
        dat = tmp_path / "bad.dat"
        dat.write_bytes(b"x")
        dl._find_dat = lambda user, md5, ts, thumbnail=False: str(dat)
        dl._find_h_dat = lambda user, md5: None
        dl.decrypt_image = lambda path, aes_key=None, xor_key=None: _green_block_jpeg()

        data, path, reason = dl.load_image(USER, 1)
        assert data is None and path is None
        assert reason == "原图与缩略图均不完整，返回占位"
        # 不合格结果绝不落盘
        assert list(tmp_path.glob("*.jpg")) == []

    def test_bad_cache_renamed_to_bak_and_not_returned(self, tmp_path):
        """坏缓存（旧版本写进去的花图）命中后改名 .bak，绝不返回给前端。

        改名而不是删除：判坏规则对白底商品图有理论误杀风险，保留原件便于
        人工找回（P2 修复要求）。
        """
        dl = self._downloader(tmp_path)
        bad = tmp_path / f"{USER}_1.jpg"
        broken = _green_block_jpeg()
        bad.write_bytes(broken)
        assert bad.exists()

        # 解不出好图（没有 .dat）→ 返回 None，坏缓存被改名备份
        dl._find_dat = lambda user, md5, ts, thumbnail=False: None
        dl._find_h_dat = lambda user, md5: None

        data, path, _reason = dl.load_image(USER, 1)
        assert data is None
        assert not bad.exists()                       # 原文件已让位
        backup = tmp_path / f"{USER}_1.jpg.bak"
        assert backup.is_file()                       # 备份在，可人工找回
        assert backup.read_bytes() == broken

    def test_white_backdrop_cache_kept(self, tmp_path):
        """白底商品图缓存（曾经被判坏）现在应正常命中，不被改名/删除。"""
        dl = self._downloader(tmp_path)
        good = _white_backdrop_jpeg()
        cached = tmp_path / f"{USER}_1.jpg"
        cached.write_bytes(good)

        dl._find_dat = lambda user, md5, ts, thumbnail=False: None
        dl._find_h_dat = lambda user, md5: None

        data, path, reason = dl.load_image(USER, 1)
        assert data == good and reason == ""
        assert cached.is_file()
        assert not (tmp_path / f"{USER}_1.jpg.bak").exists()

    def test_good_cache_reused_without_redecrypt(self, tmp_path):
        """好缓存直接命中（已验证集合），不必重新解密。"""
        dl = self._downloader(tmp_path)
        good = _complete_jpeg()
        cached = tmp_path / f"{USER}_1.jpg"
        cached.write_bytes(good)
        called = {"n": 0}

        def _boom(path, aes_key=None, xor_key=None):
            called["n"] += 1
            raise AssertionError("不应重新解密")

        dl.decrypt_image = _boom
        dl._find_dat = lambda user, md5, ts, thumbnail=False: None
        dl._find_h_dat = lambda user, md5: None

        data, path, reason = dl.load_image(USER, 1)
        assert data == good and reason == ""
        assert path.endswith(".jpg")
        assert called["n"] == 0

    def test_download_image_returns_path_or_none(self, tmp_path):
        dl = self._downloader(tmp_path)
        dat = tmp_path / "one.dat"
        dat.write_bytes(b"x")
        dl._find_dat = lambda user, md5, ts, thumbnail=False: str(dat)
        dl._find_h_dat = lambda user, md5: None
        dl.decrypt_image = lambda path, aes_key=None, xor_key=None: _complete_jpeg()
        assert dl.download_image(USER, 1)

        # 换一个空目录模拟「没有好缓存」的场景：只有花图 → None
        empty = tmp_path / "second"
        empty.mkdir()
        reset_verified_cache()
        dl.save_dir = str(empty)
        dl.decrypt_image = lambda path, aes_key=None, xor_key=None: _green_block_jpeg()
        assert dl.download_image(USER, 1) is None


# ---------------------------------------------------------------------------
# 需求 5：vision 模块
# ---------------------------------------------------------------------------
class _Completions:
    def __init__(self, outer: "FakeVisionClient") -> None:
        self._outer = outer

    def create(self, **kwargs):
        return self._outer.create(**kwargs)


class FakeVisionClient:
    """openai 客户端替身：记录请求参数并返回构造好的 JSON 文本。"""

    def __init__(self, content: str | None = None,
                 error: BaseException | None = None) -> None:
        self.content = content if content is not None else json.dumps(
            {"is_vehicle_part": True, "part_name": "发动机总成",
             # 校验位合法的 VIN（批次 8 起 analyze_image 会校验第 9 位）
             "vin": "lfv3a24f2a3102185", "reason": "发动机实物照片"},
            ensure_ascii=False)
        self.error = error
        self.kwargs: dict = {}
        self.calls = 0
        self.chat = SimpleNamespace(completions=_Completions(self))

    def create(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(prompt_tokens=1200, completion_tokens=48),
        )


class TestVisionAnalyze:
    def test_broken_image_raises_without_calling_model(self):
        client = FakeVisionClient()
        with pytest.raises(vision.VisionError) as exc:
            vision.analyze_image(_truncated_jpeg(), client=client)
        assert str(exc.value) == vision.MSG_INCOMPLETE
        assert client.calls == 0

    def test_green_block_raises(self):
        client = FakeVisionClient()
        with pytest.raises(vision.VisionError):
            vision.analyze_image(_green_block_jpeg(), client=client)
        assert client.calls == 0

    def test_request_structure_and_result(self):
        client = FakeVisionClient()
        # 用 900x700 的大图：批次 8 起长边 < 600 的图会拒收模型给的 VIN
        result = vision.analyze_image(
            _jpeg_bytes(_noise_image(size=(900, 700))),
            base_url="https://mock.local/v1",
            api_key="sk-x", model="GLM-5.3-Flash", client=client)
        # 请求结构：text + image_url(base64 data URI)，json_object，无 thinking
        content = client.kwargs["messages"][0]["content"]
        assert content[0]["type"] == "text" and "车辆" in content[0]["text"]
        assert content[1]["type"] == "image_url"
        url = content[1]["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        assert client.kwargs["response_format"] == {"type": "json_object"}
        assert "extra_body" not in client.kwargs
        # 图片已等比缩放：长边 ≤ 1024 且仍能解码
        sent = base64.b64decode(url.split(",", 1)[1])
        with Image.open(io.BytesIO(sent)) as im:
            assert max(im.size) <= 1024
        # 结果规整：VIN 转大写
        assert result["is_vehicle_part"] is True
        assert result["part_name"] == "发动机总成"
        assert result["vin"] == "LFV3A24F2A3102185"
        assert result["vin_rejected"] == ""
        assert result["prompt_tokens"] == 1200
        assert result["completion_tokens"] == 48

    def test_large_image_downscaled(self):
        client = FakeVisionClient()
        big = _jpeg_bytes(_noise_image(size=(1600, 1200)))
        vision.analyze_image(big, client=client, max_side=512)
        url = client.kwargs["messages"][0]["content"][1]["image_url"]["url"]
        sent = base64.b64decode(url.split(",", 1)[1])
        with Image.open(io.BytesIO(sent)) as im:
            assert max(im.size) <= 512

    def test_invalid_json_raises(self):
        client = FakeVisionClient(content="我看不清这张图")
        with pytest.raises(vision.VisionError) as exc:
            vision.analyze_image(_complete_jpeg(), client=client)
        assert "无法解析" in str(exc.value)

    def test_code_block_wrapped_json_ok(self):
        client = FakeVisionClient(
            content='```json\n{"is_vehicle_part": false, "part_name": "",'
                    ' "vin": "", "reason": "聊天截图"}\n```')
        result = vision.analyze_image(_complete_jpeg(), client=client)
        assert result["is_vehicle_part"] is False
        assert result["reason"] == "聊天截图"

    def test_429_mapped_to_chinese(self):
        client = FakeVisionClient(error=RuntimeError("Error code: 429 quota"))
        with pytest.raises(vision.VisionError) as exc:
            vision.analyze_image(_complete_jpeg(), client=client)
        assert "限流" in str(exc.value) or "额度" in str(exc.value)

    def test_401_mapped_to_chinese(self):
        client = FakeVisionClient(error=RuntimeError("Error code: 401 invalid key"))
        with pytest.raises(vision.VisionError) as exc:
            vision.analyze_image(_complete_jpeg(), client=client)
        assert "Key" in str(exc.value)

    def test_not_configured(self):
        with pytest.raises(vision.VisionError) as exc:
            vision.analyze_image(_complete_jpeg())
        assert "未配置" in str(exc.value)

    @pytest.mark.parametrize("vin", [
        "LSGMJ5P578V01615",      # 16 位
        "LSGMJ5P578V0161544",    # 18 位
        "LSGMJ5P578V0I6154",     # 含 I（非法字符）
        "LSGMJ5P578V0O6154",     # 含 O
    ])
    def test_invalid_vin_dropped(self, vin):
        client = FakeVisionClient(content=json.dumps(
            {"is_vehicle_part": True, "part_name": "车壳", "vin": vin,
             "reason": "x"}, ensure_ascii=False))
        result = vision.analyze_image(_complete_jpeg(), client=client)
        assert result["vin"] == ""

    def test_vin_only_when_vehicle_part(self):
        client = FakeVisionClient(content=json.dumps(
            {"is_vehicle_part": False, "part_name": "发动机",
             "vin": "LSGMJ5P578V016154", "reason": "截图"},
            ensure_ascii=False))
        result = vision.analyze_image(_complete_jpeg(), client=client)
        assert result["is_vehicle_part"] is False
        assert result["part_name"] == "" and result["vin"] == ""


# ---------------------------------------------------------------------------
# 需求 5：image_analysis 落库
# ---------------------------------------------------------------------------
class TestImageAnalysisDb:
    def test_roundtrip_and_overwrite(self):
        db = Database(":memory:")
        assert db.get_image_analysis(USER, 2467) is None
        db.save_image_analysis(USER, 2467, is_vehicle_part=True,
                               part_name="轮毂", vin="LSGMJ5P578V016154",
                               model="vm")
        row = db.get_image_analysis(USER, 2467)
        assert row["is_vehicle_part"] == 1
        assert row["part_name"] == "轮毂"
        assert row["vin"] == "LSGMJ5P578V016154"
        assert row["model"] == "vm"
        assert row["error"] == ""
        # 同 local_id 二次写覆盖（不新增历史行）
        db.save_image_analysis(USER, 2467, is_vehicle_part=False,
                               error="图片非车辆/汽车配件")
        row = db.get_image_analysis(USER, 2467)
        assert row["is_vehicle_part"] == 0
        assert "非车辆" in row["error"]
        total = db._conn.execute(
            "SELECT COUNT(*) FROM image_analysis").fetchone()[0]
        assert total == 1

    def test_illegal_local_id(self):
        db = Database(":memory:")
        assert db.get_image_analysis(USER, "abc") is None


# ---------------------------------------------------------------------------
# 需求 5：管线 direction 覆盖
# ---------------------------------------------------------------------------
class TestPipelineImageDirection:
    def _db(self) -> tuple[Database, Notifier]:
        db = Database(":memory:")
        db.insert_type("配件采购", "询价/求购汽车配件")
        db.set_setting("ai.base_url", "https://mock.local/v1")
        db.set_setting("ai.model", "mock-model")
        db.set_setting("compliance.confirmed", "1")
        return db, Notifier()

    def test_image_sell_overrides_ai_direction(self, monkeypatch):
        monkeypatch.setattr("app.pipeline.decrypt_api_key", lambda raw: "sk-test")
        db, notifier = self._db()
        client = FakeChatClient(result={
            "hit": True, "type": "配件采购", "points": ["发动机总成"],
            "confidence": 88, "quote": "[图片] 识别：车辆/汽车配件", "direction": "buy"})
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        unit = make_unit()
        unit.has_image_sell = True
        pipeline._process(unit)
        total, rows = db.list_opportunities()
        assert total == 1
        # AI 说 buy，但含配件图片 → 强制 sell
        assert rows[0]["direction"] == "sell"

    def test_without_image_keeps_ai_direction(self, monkeypatch):
        monkeypatch.setattr("app.pipeline.decrypt_api_key", lambda raw: "sk-test")
        db, notifier = self._db()
        client = FakeChatClient(result={
            "hit": True, "type": "配件采购", "points": ["求购"],
            "confidence": 88, "quote": "求购", "direction": "buy"})
        pipeline = Pipeline(db, notifier, client_factory=lambda cfg: client)
        pipeline._process(make_unit())
        assert db.list_opportunities()[1][0]["direction"] == "buy"

    def test_aggregator_carries_flag(self):
        units: list = []
        agg = Aggregator(units.append, window_sec_provider=lambda: 5)
        msg = {
            "local_id": 1, "type": "图片", "sender_id": 54,
            "sender_username": "wxid_a", "sender_display_name": "张三",
            "create_time": time.time(), "content": "wxid_a:\n[图片] 识别：车辆/汽车配件",
            "sort_seq": 1, "username": USER, "image_sell": True,
        }
        assert agg.add(msg, "配件群", "group") is True
        agg.stop(flush_all=True)
        assert units[0].has_image_sell is True


# ---------------------------------------------------------------------------
# 需求 5：监听分支（wechat_service）—— 异步队列
# ---------------------------------------------------------------------------
class FakeWx:
    def __init__(self) -> None:
        self.self_wxid = "wxid_self"

    def get_group_members(self, chatroom: str) -> list[dict]:
        return [{"username": "wxid_other", "nick_name": "李四", "remark": ""}]

    def get_nickname(self, username: str) -> str:
        return ""

    def get_session_row(self, username: str):
        return None

    def get_self_info(self) -> dict:
        return {"username": self.self_wxid, "nick_name": "我", "remark": ""}


def make_svc(units: list | None = None) -> tuple[WeChatService, Database]:
    db = Database(":memory:")
    svc = WeChatService(db, Aggregator(units.append if units is not None
                                       else (lambda u: None)))
    svc._wxdb = FakeWx()
    return svc, db


def image_msg(local_id: int = 2467, create_time: float | None = None) -> dict:
    """图片消息样本（原始消息时间可指定，用于校验聚合行时间）。"""
    return {
        "local_id": local_id, "type": "图片", "sender_id": 54,
        "sender_username": "",
        "create_time": (create_time if create_time is not None else time.time()),
        "content": "<msg><img md5='0123456789abcdef'/></msg>",
        "sort_seq": local_id, "username": USER,
    }


def skipped_logs(db: Database) -> list[dict]:
    return db.list_analysis_logs(status="skipped")[1]


def wait_processed(svc: WeChatService, expect: int = 1,
                   timeout: float = 5.0) -> bool:
    """等 worker 处理完 ``expect`` 条任务（轮询计数，避免 join 死等）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if svc.vision_processed_total >= expect:
            return True
        time.sleep(0.01)
    return svc.vision_processed_total >= expect


@contextlib.contextmanager
def vision_workers(svc: WeChatService):
    """启动图片识别 worker，退出时停掉（保证测试不残留线程）。"""
    svc._start_vision_workers()
    try:
        yield svc
    finally:
        svc._stop_vision_workers()


def vehicle_result(part: str = "发动机总成",
                   vin: str = "LSGMJ5P578V016154",
                   reason: str = "发动机实拍") -> dict:
    return {"is_vehicle_part": True, "part_name": part, "vin": vin,
            "reason": reason}


class TestVisionQueueAsync:
    """监听线程只入队（不阻塞）；识别由 worker 异步完成。"""

    def test_listener_does_not_block_on_vision(self, monkeypatch):
        """视觉调用睡 0.5s，``_on_message`` 必须在 0.1s 内返回且队列 +1。"""
        svc, _db = make_svc()
        monkeypatch.setattr(svc, "get_image_bytes",
                            lambda u, l: _complete_jpeg())

        def _slow(data, **kw):
            time.sleep(0.5)
            return vehicle_result()

        monkeypatch.setattr(vision, "analyze_image", _slow)
        started = time.monotonic()
        svc._on_message(image_msg())
        elapsed = time.monotonic() - started
        assert elapsed < 0.1, f"监听线程被阻塞了 {elapsed:.2f}s"
        assert svc.vision_queue_size == 1
        assert svc.vision_processed_total == 0     # 还没有 worker 消费
        assert svc._aggregator.pending_count() == 0

    def test_worker_emits_vehicle_sell_message(self, monkeypatch):
        units: list = []
        svc, db = make_svc(units)
        original_ts = time.time() - 3600           # 1 小时前：便于校验行时间
        monkeypatch.setattr(svc, "get_image_bytes",
                            lambda u, l: _complete_jpeg())
        monkeypatch.setattr(vision, "analyze_image",
                            lambda data, **kw: vehicle_result())
        with vision_workers(svc):
            svc._on_message(image_msg(create_time=original_ts))
            assert wait_processed(svc)
        assert svc._aggregator.pending_count() == 1
        svc._aggregator.stop(flush_all=True)
        unit = units[0]
        assert unit.has_image_sell is True
        assert unit.local_ids == [2467]
        hm, text = unit.lines[0]
        assert "[图片] 识别：车辆/汽车配件" in text
        assert "发动机总成" in text and "LSGMJ5P578V016154" in text
        # 行时间取**原始消息时间**，不是识别完成时刻
        assert hm == datetime.fromtimestamp(original_ts).strftime("%H:%M")
        # 识别结果落库（同一张图后续不再烧钱）
        row = db.get_image_analysis(USER, 2467)
        assert row["is_vehicle_part"] == 1 and row["vin"] == "LSGMJ5P578V016154"
        assert svc.vision_failed_total == 0

    def test_worker_non_vehicle_skipped(self, monkeypatch):
        units: list = []
        svc, db = make_svc(units)
        monkeypatch.setattr(svc, "get_image_bytes",
                            lambda u, l: _complete_jpeg())
        monkeypatch.setattr(vision, "analyze_image", lambda data, **kw: {
            "is_vehicle_part": False, "part_name": "", "vin": "",
            "reason": "聊天截图"})
        msg = image_msg()
        # sender_username 作为最终回退（假库无群成员前缀）：验证 worker 写日志
        # 时用的是监听线程解析出的权威发送人，而不是原始消息里的映射值
        msg["sender_username"] = "wxid_other"
        with vision_workers(svc):
            svc._on_message(msg)
            assert wait_processed(svc)
        assert svc._aggregator.pending_count() == 0
        logs = skipped_logs(db)
        assert len(logs) == 1
        assert logs[0]["sender_wxid"] == "wxid_other"
        assert "非车辆/汽车配件" in logs[0]["error"]
        assert "聊天截图" in logs[0]["error"]
        # 结论落库（含原因），下次命中缓存时原因不丢；正常结论不算失败
        row = db.get_image_analysis(USER, 2467)
        assert row["is_vehicle_part"] == 0 and row["error"] == "聊天截图"
        assert svc.vision_failed_total == 0

    def test_queue_full_skips_without_raising(self, monkeypatch):
        """队列满：落 skipped「队列已满」并返回，不抛异常、不阻塞。"""
        svc, db = make_svc()
        svc._vision_queue = queue.Queue(maxsize=1)     # 不启动 worker
        svc._vision_queue.put_nowait({"username": "占位", "local_id": 1})

        def _never(data, **kw):
            raise AssertionError("队列已满时不应调用视觉模型")

        monkeypatch.setattr(vision, "analyze_image", _never)
        svc._on_message(image_msg())                   # 不抛异常
        assert svc._vision_queue.qsize() == 1
        logs = skipped_logs(db)
        assert len(logs) == 1
        assert "图片识别队列已满" in logs[0]["error"]
        assert "2467" in logs[0]["error"]

    def test_worker_survives_unexpected_exception(self, monkeypatch):
        """worker 内非预期异常：线程存活、落 skipped，后续图片仍能处理。"""
        units: list = []
        svc, db = make_svc(units)
        calls = {"n": 0}

        def _flaky(data, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("网关返回了意料之外的东西")
            return vehicle_result(part="前大灯", vin="", reason="大灯实拍")

        monkeypatch.setattr(svc, "get_image_bytes",
                            lambda u, l: _complete_jpeg())
        monkeypatch.setattr(vision, "analyze_image", _flaky)
        with vision_workers(svc):
            svc._on_message(image_msg(2467))
            svc._on_message(image_msg(2468))
            assert wait_processed(svc, expect=2)
            assert all(t.is_alive() for t in svc._vision_threads)
        assert svc.vision_processed_total == 2
        assert svc.vision_failed_total == 1
        assert svc._aggregator.pending_count() == 1    # 第二张成功进聚合
        errors = [log["error"] for log in skipped_logs(db)]
        assert any("图片识别异常" in e for e in errors)

    def test_switch_off_keeps_original_behavior(self, monkeypatch):
        """开关关闭：仍走同步 skipped（文案主体不变，末尾带会话+local_id）。"""
        svc, db = make_svc()
        db.set_setting(KEY_IMAGE_VISION_ENABLED, "0")
        called = {"n": 0}

        def _fake(data, **kw):
            called["n"] += 1
            return vehicle_result()

        monkeypatch.setattr(vision, "analyze_image", _fake)
        svc._on_message(image_msg())
        logs = skipped_logs(db)
        assert len(logs) == 1
        error = logs[0]["error"]
        assert error.startswith("非文本消息（图片），不分析")
        assert "local_id=2467" in error
        assert called["n"] == 0
        assert svc.vision_queue_size == 0
        assert svc.vision_processed_total == 0
        assert svc._aggregator.pending_count() == 0

    def test_cache_hit_does_not_call_vision(self, monkeypatch):
        svc, db = make_svc()
        db.save_image_analysis(USER, 2467, is_vehicle_part=True,
                               part_name="轮毂", vin="")
        called = {"n": 0}

        def _fake(data, **kw):
            called["n"] += 1
            return vehicle_result(part="不该用到")

        monkeypatch.setattr(vision, "analyze_image", _fake)
        with vision_workers(svc):
            svc._on_message(image_msg())
            assert wait_processed(svc)
        assert called["n"] == 0
        assert svc._aggregator.pending_count() == 1

    def test_cached_non_vehicle_reuses_reason(self, monkeypatch):
        """同一张非车辆图片第二次不再调模型，且仍显示原因为「聊天截图」。"""
        svc, db = make_svc()
        db.save_image_analysis(USER, 2467, is_vehicle_part=False,
                               error="聊天截图")
        called = {"n": 0}

        def _fake(data, **kw):
            called["n"] += 1
            return vehicle_result(part="不该用到")

        monkeypatch.setattr(vision, "analyze_image", _fake)
        with vision_workers(svc):
            svc._on_message(image_msg())
            assert wait_processed(svc)
        assert called["n"] == 0
        assert "聊天截图" in skipped_logs(db)[0]["error"]

    def test_image_read_failure_skipped(self, monkeypatch):
        svc, db = make_svc()
        monkeypatch.setattr(svc, "get_image_bytes", lambda u, l: None)
        with vision_workers(svc):
            svc._on_message(image_msg())
            assert wait_processed(svc)
        logs = skipped_logs(db)
        assert logs[0]["error"].startswith("图片读取失败或不完整，未识别")
        assert "local_id=2467" in logs[0]["error"]
        assert svc._aggregator.pending_count() == 0
        assert svc.vision_failed_total == 1

    def test_incomplete_error_is_retryable_no_cache(self, monkeypatch):
        """「图片不完整」不落库（等微信下载完再试），但要留 skipped 日志。"""
        svc, db = make_svc()

        def _raise(data, **kw):
            raise vision.VisionError(vision.MSG_INCOMPLETE)

        monkeypatch.setattr(svc, "get_image_bytes",
                            lambda u, l: _complete_jpeg())
        monkeypatch.setattr(vision, "analyze_image", _raise)
        with vision_workers(svc):
            svc._on_message(image_msg())
            assert wait_processed(svc)
        assert db.get_image_analysis(USER, 2467) is None
        assert "不完整" in skipped_logs(db)[0]["error"]

    def test_quota_error_not_cached_then_retried(self, monkeypatch):
        """P1 回归：额度不足**不能**落库成「非配件」，额度恢复后重投能识别。

        旧实现会把 ``is_vehicle_part=0`` 写进缓存，之后永远不再识别（假阴性）。
        """
        units: list = []
        svc, db = make_svc(units)
        calls = {"n": 0}

        def _flaky(data, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise vision.VisionError("图片识别失败：额度不足或触发限流")
            return vehicle_result(part="车壳", vin="")

        monkeypatch.setattr(svc, "get_image_bytes",
                            lambda u, l: _complete_jpeg())
        monkeypatch.setattr(vision, "analyze_image", _flaky)
        with vision_workers(svc):
            # 第 1 次投递：额度不足 → 不落库
            svc._on_message(image_msg())
            assert wait_processed(svc)
            assert db.get_image_analysis(USER, 2467) is None
            errors = [log["error"] for log in skipped_logs(db)]
            assert any(e.startswith("图片识别失败：额度不足") for e in errors)
            assert all("非车辆/汽车配件" not in e for e in errors)
            assert svc._aggregator.pending_count() == 0
            # 第 2 次投递（重新识别）：成功 → 进聚合 + 落库
            svc._on_message(image_msg())
            assert wait_processed(svc, expect=2)
        assert calls["n"] == 2
        assert svc._aggregator.pending_count() == 1
        row = db.get_image_analysis(USER, 2467)
        assert row is not None and row["is_vehicle_part"] == 1
        assert row["part_name"] == "车壳"

    def test_vision_error_not_cached(self, monkeypatch):
        """其它临时失败（Key/超时/未配置）同样不落库，日志前缀为「图片识别失败」。"""
        svc, db = make_svc()

        def _raise(data, **kw):
            raise vision.VisionError("图片识别失败：Key 无效或无权限")

        monkeypatch.setattr(svc, "get_image_bytes",
                            lambda u, l: _complete_jpeg())
        monkeypatch.setattr(vision, "analyze_image", _raise)
        with vision_workers(svc):
            svc._on_message(image_msg())
            assert wait_processed(svc)
        assert db.get_image_analysis(USER, 2467) is None
        error = skipped_logs(db)[0]["error"]
        assert error.startswith("图片识别失败：Key 无效")
        assert "local_id=2467" in error
        assert svc.vision_failed_total == 1

    def test_unprefixed_error_gets_failure_prefix(self, monkeypatch):
        """未带前缀的失败原因（如预处理/SDK 缺失）统一补「图片识别失败：」。"""
        svc, db = make_svc()

        def _raise(data, **kw):
            raise vision.VisionError("图片预处理失败：cannot identify image")

        monkeypatch.setattr(svc, "get_image_bytes",
                            lambda u, l: _complete_jpeg())
        monkeypatch.setattr(vision, "analyze_image", _raise)
        with vision_workers(svc):
            svc._on_message(image_msg())
            assert wait_processed(svc)
        assert skipped_logs(db)[0]["error"].startswith("图片预处理失败：")

    def test_cached_non_vehicle_without_reason(self, monkeypatch):
        """缓存命中且没有原因 → 「图片已识别，非车辆/汽车配件」，不说成失败。"""
        svc, db = make_svc()
        db.save_image_analysis(USER, 2467, is_vehicle_part=False)
        with vision_workers(svc):
            svc._on_message(image_msg())
            assert wait_processed(svc)
        error = skipped_logs(db)[0]["error"]
        assert error.startswith("图片已识别，非车辆/汽车配件")
        assert "图片识别失败" not in error
        assert "local_id=2467" in error

    def test_missing_local_id_skipped(self):
        svc, db = make_svc()
        msg = image_msg()
        msg["local_id"] = 0
        with vision_workers(svc):
            svc._on_message(msg)
            assert wait_processed(svc)
        assert "缺少 local_id" in skipped_logs(db)[0]["error"]

    def test_worker_threads_named_and_restartable(self):
        svc, _db = make_svc()
        try:
            svc._start_vision_workers()
            names = sorted(t.name for t in svc._vision_threads)
            assert names == [f"kefu-vision-{i}" for i in range(VISION_WORKERS)]
            assert all(t.is_alive() for t in svc._vision_threads)
            threads = list(svc._vision_threads)
            svc._start_vision_workers()          # 幂等：不重复起线程
            assert len(svc._vision_threads) == len(threads)
        finally:
            svc._stop_vision_workers()
        assert svc._vision_threads == []
        assert not any(t.is_alive() for t in threads)
        # stop → start 仍可用（重启进程/服务场景）
        svc._start_vision_workers()
        assert len(svc._vision_threads) == VISION_WORKERS
        svc._stop_vision_workers()

    def test_workers_start_and_stop_with_service_lifecycle(self, monkeypatch,
                                                          tmp_path):
        """start()/stop() 生命周期里 worker 也跟着起停（不依赖微信可用）。"""
        svc, _db = make_svc()
        monkeypatch.setattr(svc, "configured_cache_dir", lambda: tmp_path)
        svc.start()          # 微信库是替身，Listener 起不来，但 worker 已起
        try:
            assert len(svc._vision_threads) == VISION_WORKERS
        finally:
            svc.stop()
        assert svc._vision_threads == []


# ---------------------------------------------------------------------------
# 需求 5：接口契约（图片识别结果 + 状态队列字段）
# ---------------------------------------------------------------------------
@pytest.fixture()
def env(tmp_path):
    db = Database(str(tmp_path / "b6media.sqlite3"))
    deps.configure(db=db, wechat=FakeWechatService(), pipeline=None,
                   sender=None, notifier=Notifier())
    app = FastAPI()
    for router in ALL_ROUTERS:
        app.include_router(router)
    client = TestClient(app)
    yield SimpleNamespace(db=db, client=client)
    deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                   notifier=None)
    db.close()


class TestImageAnalysisApi:
    def test_200(self, env):
        env.db.save_image_analysis(USER, 2467, is_vehicle_part=True,
                                   part_name="轮毂", vin="LSGMJ5P578V016154",
                                   model="GLM-5.3-Flash")
        resp = env.client.get(f"/api/messages/{USER}/images/2467/analysis")
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_vehicle_part"] is True
        assert body["part_name"] == "轮毂"
        assert body["vin"] == "LSGMJ5P578V016154"
        assert body["model"] == "GLM-5.3-Flash"

    def test_404_when_not_analyzed(self, env):
        resp = env.client.get(f"/api/messages/{USER}/images/999/analysis")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "该图片尚未识别"

    def test_422_when_local_id_illegal(self, env):
        resp = env.client.get(f"/api/messages/{USER}/images/0/analysis")
        assert resp.status_code == 422

    def test_non_vehicle_row_reports_false(self, env):
        env.db.save_image_analysis(USER, 2467, is_vehicle_part=False,
                                   error="聊天截图")
        body = env.client.get(
            f"/api/messages/{USER}/images/2467/analysis").json()
        assert body["is_vehicle_part"] is False
        assert body["error"] == "聊天截图"


@pytest.fixture()
def status_env(tmp_path):
    """带 pipeline 替身的状态接口环境（/api/status 需要 queue 字段）。"""
    db = Database(str(tmp_path / "b6status.sqlite3"))
    svc = FakeWechatService()
    deps.configure(db=db, wechat=svc, pipeline=FakePipeline(), sender=None,
                   notifier=Notifier())
    app = FastAPI()
    for router in ALL_ROUTERS:
        app.include_router(router)
    client = TestClient(app)
    yield SimpleNamespace(db=db, client=client, svc=svc)
    deps.configure(db=None, wechat=None, pipeline=None, sender=None,
                   notifier=None)
    db.close()


class TestStatusVisionPending:
    def test_defaults_to_zero_when_service_has_no_queue(self, status_env):
        """兼容：服务实现没有该属性时按 0 返回，字段始终存在。"""
        body = status_env.client.get("/api/status").json()
        assert body["queue"]["vision_pending"] == 0

    def test_reports_service_queue_size(self, status_env):
        status_env.svc.vision_queue_size = 7
        body = status_env.client.get("/api/status").json()
        assert body["queue"]["vision_pending"] == 7
