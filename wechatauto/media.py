# -*- coding: utf-8 -*-
"""微信 4.x 媒体文件读取与下载（图片解密、语音、视频、文件）。

与 :mod:`wechatauto.db` 配合使用：``db`` 提供解密后的消息行（含 local_type、
server_id、packed_info），本模块负责把媒体内容从本地取回/解密/落地。

支持的媒体（local_type 见 :data:`wechatauto.db.MSG_TYPE_NAMES`）::

    local_type 3   图片     → 会话目录 msg/attach/<会话md5>/<YYYY-MM>/Img/<md5>.dat
    local_type 34  语音     → message/media_0.db VoiceInfo.voice_data（SILK 二进制）
    local_type 43  视频     → msg/video/<YYYY-MM>/<id>.mp4（未落地时返回 None）
    local_type 49  文件     → msg/file/<YYYY-MM>/<原文件名>，原名取自
                            message_resource.db MessageResourceDetail.packed_info

图片加密（v2 格式，本库已在本机验证）::

    结构: [6B sig 07 08 56 32 08 07][4B aes_size LE][4B xor_size LE][1B pad]
          [aes 密文(ECB, PKCS7, 对齐 16B)][raw 明文][xor 密文]

    - AES 密钥: 16 字节 ASCII（字母/数字），仅在 Weixin.exe 进程内存中。
      通过 AES-ECB 解首块密文、校验 JPEG/PNG 魔数反推出（内存正则扫描）。
    - XOR 密钥: 单字节，**按账号固定**（本机实测 0xF9）。优先从同图兄弟文件
      尾部反推：JPEG（缩略图 ``<md5>_t.dat``）看 ``FF D9``
      （``key = tail[-2] ^ 0xFF``），PNG 原图（复制粘贴的图片存在
      ``<md5>_h.dat``）看 IEND 块 CRC 末字节 ``0x82``。

      注意：逐文件反推依赖「明文正好以结束标记收尾」，尾部带填充/噪声的文件
      会推出**假密钥**（实测 40 张缩略图里 5 张如此），表现为「JPEG 头正常、
      之后全乱码 → 被判半截/花屏」。因此全部候选都失败时还有第二轮：从账号
      内一批缩略图**投票 + 实测**学出账号密钥再解一遍，见
      :meth:`MediaDownloader._learn_account_xor_key`。

用法::

    from wechatauto import WeChatDB, MediaDownloader
    db = WeChatDB()
    md = MediaDownloader(db)
    md.download_media(chat_user, msg_row["local_id"])   # 按类型自动分发
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import logging
import os
import re
import struct
import tempfile
import threading
import time
from collections import Counter
from typing import Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

V1_MAGIC = b"\x07\x08\x05\x56\x02\x05"
V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"
V1_HEADER_SZ = 22  # 6B sig + 16B xor key
AES16_RE = re.compile(rb"[0-9a-zA-Z]{16,32}")
DEFAULT_SAVE_PATH = os.path.join(os.path.expanduser("~"), "Documents", "wechatauto_media")

# 缩略图模板扫描缓存：1.2.4 打包版每次 load_image 都会递归列一遍
# ``msg/attach/*/*/Img``（几十万文件），多个 vision worker 并发时线程长期
# 卡在 glob→listdir。模板只用于「账号级 XOR 密钥投票」和「AES 探针采样」，
# 这两者都是账号级、分钟级稳定的数据，完全没必要每张图重扫。
# TTL 到期后先用缓存下来的目录列表做 mtime 签名校验，签名没变就只延长 TTL；
# 变了才真正重新列目录，避免每分钟一次全量 I/O。
TEMPLATE_CACHE_TTL_SEC = 60.0
TEMPLATE_CACHE_MAX = 64
IMG_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
PROBE_CACHE_HIT_TTL_SEC = 600.0
PROBE_CACHE_MISS_TTL_SEC = 60.0
PROBE_SAMPLE_COUNT = 4


def _jpeg_like(pt: bytes) -> bool:
    return (
        (pt[:3] == b"\xff\xd8\xff")
        or pt[:4] in (b"\x89PNG", b"GIF8", b"RIFF")
        or pt[:4] == b"wxgf"  # 微信动画表情容器
    )


# ---------------------------------------------------------------------------
# 图片完整性 / 花屏校验（批次 6 待办 1）
#
# 背景（本机实测）：微信 4.x 的 .dat 经常只下载了一部分（尾部没有 JPEG
# 结束符 FF D9），AES 解出来的头部是合法 JFIF，但尾部全是乱码；有时甚至
# 头部正常、中间突然变成整片绿色。这类字节如果直接落盘缓存，用户就永远
# 看到花图。所以解密结果必须先过两道校验：
#   1) is_complete_image —— 有合法头 + 有结束标记（半截文件不算）；
#   2) image_looks_broken —— 用 Pillow 解码 + 颜色分布判断「纯色占位/绿块」。
# 只有两项都通过的图片才会被采用/落盘，宁缺勿花。
# ---------------------------------------------------------------------------
_TAIL_WINDOW = 2048              # 结束标记只在文件尾部这么多字节里找
_MAX_THUMB_WIDTH = 64            # 颜色统计先等比缩到宽 ≤ 64（快且够用）
_MIN_UNIQUE_COLORS = 16          # 整图颜色数少于此值 → 纯色占位图
_BOTTOM_COLOR_RATIO = 0.97       # 底部 1/3 单一颜色占比超过此值 → 绿块/截断
# 底部单色占比超阈值时的**豁免**判据：拆车件群大量「白底实拍」（配件在上、
# 底部一片纯白）本身是正常商品图，直接按「单色占比」判坏会把好图误杀。
_WHITE_MIN_CHANNEL = 240         # min(r,g,b) ≥ 该值 → 近白底，豁免
_GRAY_SPREAD_MAX = 8             # 三通道最大差值 ≤ 该值（近中性灰）
_GRAY_MIN_CHANNEL = 200          # 且 min(r,g,b) ≥ 该值 → 近浅灰底，豁免


def is_complete_image(data: bytes) -> bool:
    """图片字节是否完整（合法文件头 + 结束标记）。

    支持 JPEG（FF D8 FF ... FF D9）、PNG（\\x89PNG ... IEND）、
    GIF（GIF8 ... 0x3B）。其它格式（含微信 wxgf 容器）一律返回 False：
    调用方应先把 wxgf 转成 JPEG 再校验。

    Args:
        data: 图片原始字节。

    Returns:
        完整返回 True；空数据、半截文件、未知格式返回 False。
    """
    if not data or len(data) < 16:
        return False
    tail = data[-_TAIL_WINDOW:]
    if data[:3] == b"\xff\xd8\xff":                  # JPEG
        return b"\xff\xd9" in tail
    if data[:8] == b"\x89PNG\r\n\x1a\n":             # PNG
        return b"IEND" in tail
    if data[:4] == b"GIF8":                          # GIF
        return data.rstrip(b"\x00")[-1:] == b"\x3b"
    return False


def _is_light_backdrop(color) -> bool:
    """该底部主色是否属于「白底 / 近中性浅灰底」这类正常商品图背景。

    白底实拍（配件在上、底部一片纯白）与近浅灰底在拆车件群里极其常见，
    它们的底部单色占比天然接近 1.0，不能用「单色占比」判坏。真绿块
    ``(0,135,0)`` 的 min 通道为 0，不会命中任何豁免。

    Args:
        color: Pillow ``getcolors()`` 返回的颜色元组（RGB 三元组）。

    Returns:
        True = 属于豁免的白/浅灰背景；False = 按原规则判定。
    """
    try:
        r, g, b = (int(color[0]), int(color[1]), int(color[2]))
    except (TypeError, IndexError, ValueError):
        return False
    low = min(r, g, b)
    high = max(r, g, b)
    if low >= _WHITE_MIN_CHANNEL:            # 近白（如 (255,255,255)）
        return True
    # 近中性浅灰（三通道几乎相等且都不暗），如 (235,235,238) / (210,210,212)
    return (high - low) <= _GRAY_SPREAD_MAX and low >= _GRAY_MIN_CHANNEL


def image_looks_broken(data: bytes) -> bool:
    """图片是否「看起来是坏图」（解码失败 / 纯色占位 / 底部整片绿块）。

    判据（与实测花图特征对齐）：
    - Pillow 打不开或 ``load()`` 抛异常 → 坏图；
    - 等比缩到宽 ≤ 64 后，整图唯一颜色数 < 16 → 纯色占位图；
    - 底部 1/3 区域里单一颜色占比 > 0.97 → 「顶部一条真图 + 下面整片纯色」
      这类半截/绿块图；但底部主色若是**近白 / 近中性浅灰**（白底商品图
      的正常背景）则豁免，见 :func:`_is_light_backdrop`。

    Pillow 不可用时**判为不坏**（宁可显示也不要把好图误判成坏图）；此时
    仍受 :func:`is_complete_image` 的完整性约束。
    """
    try:
        from PIL import Image
    except Exception:            # 没装 Pillow：不做花屏判定
        return False
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            rgb = im.convert("RGB")
        width, height = rgb.size
        if width <= 0 or height <= 0:
            return True
        if width > _MAX_THUMB_WIDTH:
            ratio = _MAX_THUMB_WIDTH / float(width)
            rgb = rgb.resize((_MAX_THUMB_WIDTH, max(1, int(round(height * ratio)))))
        colors = rgb.getcolors(maxcolors=1 << 16)
        if not colors:
            return True
        if len(colors) < _MIN_UNIQUE_COLORS:
            return True
        width, height = rgb.size
        band = max(1, height // 3)
        top = height - band
        bottom = rgb.crop((0, top, width, height))
        total = width * band
        if total <= 0:
            return True
        bottom_colors = bottom.getcolors(maxcolors=1 << 16) or []
        if not bottom_colors:
            return True
        # Pillow 的 getcolors() 返回的是 [(count, color), ...]（count 在前）
        most_count, most_color = max(bottom_colors, key=lambda item: item[0])
        if (most_count / float(total)) <= _BOTTOM_COLOR_RATIO:
            return False
        # 单色占比很高：只有「非白、非浅灰的纯色」才判为花图/半截图。
        # 白底/近浅灰底的实拍商品图底部本来就是整片背景色，必须豁免，
        # 否则好图会被判坏并连缓存一起被清掉（真实语料里的白底配件图）。
        return not _is_light_backdrop(most_color)
    except Exception:
        return True


def is_usable_image(data: bytes) -> bool:
    """图片是否可用于展示：格式完整且不像花屏（两项都过才算可用）。"""
    return is_complete_image(data) and not image_looks_broken(data)


# 进程内「已验证合格的缓存文件」集合：命中缓存不必每次重新解码，
# 有上限防止长期运行后无限增长（超出直接清空，代价只是重新校验一次）。
_VERIFIED_CACHE: Set[str] = set()
_VERIFIED_LOCK = threading.Lock()
_VERIFIED_MAX = 512


def _remember_verified(path: str) -> None:
    """把缓存文件标记为已验证合格（进程内）。"""
    with _VERIFIED_LOCK:
        if len(_VERIFIED_CACHE) >= _VERIFIED_MAX:
            _VERIFIED_CACHE.clear()
        _VERIFIED_CACHE.add(path)


def is_path_verified(path: str) -> bool:
    """该缓存文件是否已在本进程中验证过合格。"""
    with _VERIFIED_LOCK:
        return path in _VERIFIED_CACHE


def forget_verified(path: str) -> None:
    """把缓存文件移出已验证集合（文件被删/判定为坏图时调用）。"""
    with _VERIFIED_LOCK:
        _VERIFIED_CACHE.discard(path)


def reset_verified_cache() -> None:
    """清空已验证集合（测试用）。"""
    with _VERIFIED_LOCK:
        _VERIFIED_CACHE.clear()


def aligned_aes_block_size(aes_size: int) -> int:
    return aes_size + (16 - aes_size % 16) if aes_size % 16 else aes_size + 16


# 单字节 XOR 用 256 项换表（C 层 translate），500KB 原图比逐字节生成快一个量级
_XOR_TABLES: Dict[int, bytes] = {}


def xor_bytes(data: bytes, key: int) -> bytes:
    """按单字节密钥 XOR 一段数据（key=0 直接返回，避免无谓拷贝）。"""
    key &= 0xFF
    if key == 0:
        return bytes(data)
    table = _XOR_TABLES.get(key)
    if table is None:
        table = bytes(i ^ key for i in range(256))
        if len(_XOR_TABLES) < 256:
            _XOR_TABLES[key] = table
    return data.translate(table)


class MediaDownloader:
    """微信 4.x 媒体下载器"""

    def __init__(self, db, save_dir: Optional[str] = None,
                 image_key: Optional[str] = None,
                 cfg_dword: Optional[int] = None,
                 key_ready_callback: Optional[Callable[[str], None]] = None):
        self.db = db
        self.save_dir = save_dir or DEFAULT_SAVE_PATH
        self._image_key = image_key  # 显式注入的图片 AES 密钥
        self._cfg_dword = cfg_dword  # cfg+0x40, 派生图片密钥(最佳方案)
        # 密钥一旦可用就回调上层（应用层据此立即唤醒待补识别的图片队列），
        # 避免「用户打开图片→密钥捕获成功→已有的失败图片仍等 10/30/90s」。
        self._key_ready_callback = key_ready_callback
        self._notified_keys: Set[str] = set()
        self._xor_key: Optional[int] = None
        self._img_key: Optional[Tuple[str, int]] = None
        self._key_probe: Optional[bytes] = None
        # AES 探针缓存：多张 .dat 的首块，避免只取 glob 第一个文件（可能是
        # 半截/非图片）导致正确密钥被 _validate_key 误判为无效。
        self._probe_cache: Optional[Tuple[float, List[bytes]]] = None
        self._probe_lock = threading.Lock()
        # 缩略图模板缓存：base_dir → (过期时刻, 目录签名, 目录列表, 模板路径)。
        # 所有 vision worker / image_retry 共用同一把锁，扫描期间其他线程等待
        # 结果而不是各自重复扫盘。
        self._tpl_cache: Dict[str, Tuple[float, tuple, List[str], List[str]]] = {}
        self._tpl_lock = threading.Lock()
        # 账号级 XOR 密钥缓存：account_dir → (过期时刻, 密钥)。见
        # :meth:`_learn_account_xor_key`（尾字节反推失手时的兜底）。
        self._acct_key_cache: Dict[str, Tuple[float, Optional[int]]] = {}
        self._acct_key_flight: Dict[str, threading.Event] = {}
        self._acct_key_lock = threading.Lock()

    @staticmethod
    def derive_image_keys(cfg_dword: int, wxid: str) -> Tuple[str, int]:
        """cfgDword 派生图片密钥(微信 4.x 最佳方案, 实测 3000/3000 验证)。

        imageXorKey = cfgDword & 0xFF
        imageAesKey = MD5(str(cfgDword) + wxid)[:16]   # 前 16 位即真 AES-128 密钥
        """
        xor_key = cfg_dword & 0xFF
        aes_key = hashlib.md5(
            ("%d" % cfg_dword + wxid).encode("utf-8")).hexdigest()[:16]
        return aes_key, xor_key

    def _derive_cfg_key(self) -> Optional[Tuple[str, int]]:
        """cfgDword 派生并验证; 优先显式注入, 否则用 db.cfg_dword(自动提取)。"""
        cfg_dword = self._cfg_dword
        if cfg_dword is None:
            cfg_dword = getattr(self.db, "cfg_dword", None)
        if not cfg_dword:
            return None
        aes_key, xor_key = self.derive_image_keys(cfg_dword, self.db.wxid)
        if self._validate_key(aes_key):
            return aes_key, xor_key
        return None

    # ------------------------------------------------------------------
    # 图片密钥（内存扫描 + 缩略图反推）
    # ------------------------------------------------------------------
    @staticmethod
    def _probe_from_path(dat_path: str) -> bytes:
        """从单个 .dat 读取密文首块（V2: 15:31；V1: 22:38）。"""
        try:
            with open(dat_path, "rb") as f:
                head = f.read(V1_HEADER_SZ + 16)
        except OSError:
            return b""
        if head[:6] == V2_MAGIC and len(head) >= 31:
            return head[15:31]
        if len(head) >= V1_HEADER_SZ + 16:
            return head[V1_HEADER_SZ: V1_HEADER_SZ + 16]
        return b""

    def _probe_ct(self, dat_path: Optional[str] = None) -> bytes:
        """取一张 V2 图片的密文首块，作为密钥反测试样。

        采样源改为 :meth:`_collect_templates` 的缓存结果，不再单独 glob 一遍
        全盘（只有一个探针时也复用同一份扫描结果）。
        """
        if self._key_probe is not None:
            return self._key_probe
        if dat_path is None:
            hits = self._collect_templates(keep=1)
            if not hits:
                return b""
            dat_path = hits[0]
        self._key_probe = self._probe_from_path(dat_path)
        return self._key_probe

    def _probe_cts(self) -> List[bytes]:
        """多张图片的密文首块（去重、带 TTL 缓存）。

        线上故障里 ``image_keys.json`` 已写入正确 AES key，但 ``_validate_key``
        只拿 glob 命中的第一张 .dat 做探针——那张图可能是半截/非 JPEG，于是
        正确密钥被判无效，22:33 之后仍持续报「无法获取图片 AES 密钥」。
        取 4 张模板 + 原探针，任意一个能解出图片头就算密钥有效。
        """
        now = time.monotonic()
        with self._probe_lock:
            cached = self._probe_cache
            if cached and now < cached[0] and cached[1]:
                return list(cached[1])
        probes: List[bytes] = []
        for path in self._collect_templates(keep=PROBE_SAMPLE_COUNT):
            probe = self._probe_from_path(path)
            if probe and probe not in probes:
                probes.append(probe)
        single = self._probe_ct()
        if single and single not in probes:
            probes.insert(0, single)
        ttl = PROBE_CACHE_HIT_TTL_SEC if probes else PROBE_CACHE_MISS_TTL_SEC
        with self._probe_lock:
            self._probe_cache = (now + ttl, probes)
        return list(probes)

    def _validate_key(self, aes_key: str) -> bool:
        """用多个真实密文首块反测密钥是否有效（任一命中即可）。"""
        probes = self._probe_cts()
        if not probes:
            return False
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            for probe in probes:
                dec = Cipher(algorithms.AES(aes_key.encode()),
                             modes.ECB()).decryptor()
                if _jpeg_like(dec.update(probe) + dec.finalize()):
                    return True
        except Exception:
            return False
        return False

    def _notify_key_ready(self, aes_key: str) -> None:
        """通知应用层「图片密钥已可用」（同一把密钥只通知一次）。"""
        if not aes_key or aes_key in self._notified_keys:
            return
        self._notified_keys.add(aes_key)
        callback = self._key_ready_callback
        if callback is None:
            return
        try:
            callback(aes_key)
        except Exception:
            logger.exception("图片密钥就绪回调失败（已忽略，不影响解密）")

    def _key_store(self) -> str:
        return os.path.join(self.db.workdir, "image_keys.json")

    def _load_persisted_key(self) -> Optional[str]:
        """读取本账号持久化的 AES 密钥；读写必须用同一个 ``db.account``。

        现场确认 ``image_keys.json`` 的键是 ``wxid_..._ac0e``（微信账号目录
        名），与 ``WeChatDB.account`` 完全一致，所以键名本身没问题；真正让
        正确密钥被拒的是旧版只用一张随机 .dat 做探针。这里仍以账号键为第一
        优先级，仅在它失效时尝试文件里其它账号的键（同一台机器可能切号，
        AES key 多数情况下跟微信安装而非账号绑定），全部经密文反测。
        """
        try:
            with open(self._key_store(), "r", encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(saved, dict):
            return None
        account = str(getattr(self.db, "account", "") or "")
        ordered: List[str] = []
        own = saved.get(account)
        if isinstance(own, str) and own:
            ordered.append(own)
        for key in saved.values():
            if isinstance(key, str) and key and key not in ordered:
                ordered.append(key)
        for key in ordered:
            if self._validate_key(key):
                self._notify_key_ready(key)
                return key
        return None

    def _persist_key(self, aes_key: str) -> None:
        try:
            with open(self._key_store(), "r", encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError, json.JSONDecodeError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        saved[str(getattr(self.db, "account", "") or "")] = aes_key
        try:
            os.makedirs(os.path.dirname(self._key_store()), exist_ok=True)
            with open(self._key_store(), "w", encoding="utf-8") as f:
                json.dump(saved, f, indent=2)
        except OSError:
            pass
        self._notify_key_ready(aes_key)

    @staticmethod
    def _img_dirs(base: str) -> List[str]:
        """只遍历已知目录层级 ``attach/<会话>/<YYYY-MM>/Img``。

        绝不递归未知子树（原先的 glob 会为每个会话列出每个月份目录，再进入
        其中的 Img，实测几十万次 listdir）。同时兼容 ``attach/<会话>/Img``
        的老布局，避免少采样导致账号 XOR 投票失真。
        """
        dirs: List[str] = []
        try:
            with os.scandir(base) as chats:
                chat_entries = [e for e in chats
                                if e.is_dir(follow_symlinks=False)]
        except OSError:
            return dirs
        for chat in chat_entries:
            legacy = os.path.join(chat.path, "Img")
            if os.path.isdir(legacy):
                dirs.append(legacy)
            try:
                with os.scandir(chat.path) as months:
                    month_entries = [e for e in months
                                     if e.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            for month in month_entries:
                if not IMG_MONTH_RE.match(month.name):
                    continue
                img = os.path.join(month.path, "Img")
                if os.path.isdir(img):
                    dirs.append(img)
        return dirs

    @staticmethod
    def _dirs_signature(base: str, dirs: List[str]) -> tuple:
        """已知目录及其父/祖父目录的 mtime/size 签名（不列文件，只 stat）。

        Img 目录的 mtime 只能反映「新图片写入」；新月份目录建在会话目录下、
        新会话目录建在 attach 根下，分别只改变会话目录/根的 mtime。因此把
        每个 Img 目录的父目录（月份）和祖父目录（会话）也纳入签名，否则已
        有会话下跨月新增的模板会因签名不变而永远不被重新发现。
        """
        paths = [base]
        for img_dir in dirs:
            paths.append(img_dir)
            month_dir = os.path.dirname(img_dir)
            paths.append(month_dir)
            paths.append(os.path.dirname(month_dir))
        # 去重但保持插入顺序，保证相同输入产生稳定一致的签名 tuple。
        unique_paths = list(dict.fromkeys(paths))
        parts: List[tuple] = []
        for path in unique_paths:
            try:
                st = os.stat(path)
                parts.append((st.st_mtime_ns, st.st_size))
            except OSError:
                parts.append((0, -1))
        return tuple(parts)

    def _scan_templates(self, base: str, keep: int) -> Tuple[List[str], List[str]]:
        """实际列一次目录，返回 (模板路径列表, 已发现的 Img 目录列表)。"""
        dirs = self._img_dirs(base)
        found: List[Tuple[str, int]] = []
        for img_dir in dirs:
            try:
                with os.scandir(img_dir) as entries:
                    for entry in entries:
                        if (not entry.name.endswith("_t.dat")
                                or not entry.is_file(follow_symlinks=False)):
                            continue
                        try:
                            mtime_ns = entry.stat().st_mtime_ns
                        except OSError:
                            mtime_ns = 0
                        found.append((entry.path, mtime_ns))
            except OSError:
                continue
        found.sort(key=lambda item: item[1], reverse=True)
        return [path for path, _ in found[:max(0, keep)]], dirs

    def _collect_templates(self, limit: int = 32, keep: int = 16) -> List[str]:
        """收集最新的 ``*_t.dat`` 模板（带 TTL + 目录签名失效的进程内缓存）。

        缓存键是 attach 根目录；TTL 内直接返回（零磁盘 I/O）。TTL 到期后用
        缓存下来的目录列表做 mtime 签名：新会话/新月份会改父目录 mtime，新图
        片会改 Img 目录 mtime，任一变化都会触发重新列目录；否则只延长 TTL。
        整个流程在 ``_tpl_lock`` 内完成，多 worker 并发时只有一个人扫描。

        ``limit`` 为兼容旧调用签名保留（旧实现未使用）；``keep`` 超过缓存容量
        时直接扫描，保证调用方拿到的数量语义不变。
        """
        account_dir = str(getattr(self.db, "account_dir", "") or "")
        if not account_dir:
            return []
        base = os.path.join(account_dir, "msg", "attach")
        if keep > TEMPLATE_CACHE_MAX:
            return self._scan_templates(base, keep)[0]
        now = time.monotonic()
        with self._tpl_lock:
            cached = self._tpl_cache.get(base)
            if cached is not None and now < cached[0]:
                return list(cached[3][:keep])
            if cached is not None:
                signature = self._dirs_signature(base, cached[2])
                if signature == cached[1]:
                    self._tpl_cache[base] = (
                        now + TEMPLATE_CACHE_TTL_SEC, signature,
                        cached[2], cached[3])
                    return list(cached[3][:keep])
            paths, dirs = self._scan_templates(base, TEMPLATE_CACHE_MAX)
            signature = self._dirs_signature(base, dirs)
            self._tpl_cache[base] = (
                now + TEMPLATE_CACHE_TTL_SEC, signature, dirs, paths)
            return list(paths[:keep])

    def _get_xor_key(self, templates: List[str]) -> Optional[int]:
        """文件尾统计推 XOR 密钥: 缩略图明文为 JPEG, 尾部固定 FF D9。

        读每个模板最后 2 字节 (x, y), 统计出现最多的组合;
        xorKey = x ^ 0xFF 且校验 y ^ 0xD9 == xorKey 才返回。
        """
        tails: Dict[Tuple[int, int], int] = {}
        for p in templates:
            try:
                with open(p, "rb") as f:
                    f.seek(-2, 2)
                    tail = f.read(2)
            except OSError:
                continue
            if len(tail) == 2:
                tails[(tail[0], tail[1])] = tails.get((tail[0], tail[1]), 0) + 1
        for (x, y), _ in sorted(tails.items(), key=lambda kv: -kv[1]):
            key = x ^ 0xFF
            if y ^ 0xD9 == key:
                return key
        return None

    # 同一张图的三种 .dat：<md5>.dat（聊天里那张）/ <md5>_h.dat（原图）/ <md5>_t.dat（缩略图）
    _DAT_SUFFIXES = ("_h.dat", "_t.dat", ".dat")

    @classmethod
    def _dat_stem(cls, dat_path: str) -> str:
        """把 ``<md5>.dat`` / ``<md5>_h.dat`` / ``<md5>_t.dat`` 统一成 ``<md5>``。

        旧实现直接 ``dat_path[:-4]``：传 ``_h.dat`` 会得到 ``<md5>_h``，拼出的
        兄弟名 ``<md5>_h_t.dat`` 根本不存在 → 密钥推导**必然**落到默认 0x88 →
        高清原图解成乱码被判定「花屏」→ 回退缩略图。复制粘贴的图片尤其明显
        （原图存 ``_h.dat`` 里的 PNG，缩略图只有 4KB），所以必须先剥掉
        ``_h`` / ``_t`` 再拼兄弟名。
        """
        for suffix in cls._DAT_SUFFIXES:
            if dat_path.endswith(suffix):
                return dat_path[: -len(suffix)]
        return dat_path

    @staticmethod
    def _key_from_tail(tail: bytes) -> Optional[int]:
        """由文件尾字节反推 XOR 密钥。

        - JPEG：尾部固定 ``FF D9`` → ``key = tail[-2] ^ 0xFF``，并要求
          ``tail[-1] ^ 0xD9`` 自洽；
        - PNG：IEND 块 CRC 末字节恒为 ``0x82`` → ``key = tail[-1] ^ 0x82``
          （复制粘贴的图片，原图就是 PNG）。
        """
        if len(tail) >= 2:
            key = tail[-2] ^ 0xFF
            if tail[-1] ^ 0xD9 == key:
                return key
        if tail:
            return tail[-1] ^ 0x82
        return None

    @staticmethod
    def _read_tail(path: str, size: int = 12) -> bytes:
        """读文件尾部若干字节（文件不存在/太短返回空）。"""
        try:
            with open(path, "rb") as f:
                f.seek(-size, 2)
                return f.read(size)
        except OSError:
            return b""

    def _xor_key_candidates(self, dat_path: str) -> List[int]:
        """按可信度排序的 XOR 密钥候选（去重，最多 4 个）。

        顺序：同图 ``_t.dat``（缩略图总是完整 JPEG，尾部 FF D9 最可靠）→
        同图 ``_h.dat`` → 文件自身 → 旧实现 ``_derive_xor_key`` 的结果。

        **为什么要多候选**：单字节密钥按账号固定，但推导依赖「文件已写完整」——
        微信还在下载时尾部是半截数据，推出来的就是错的。多候选交给上层
        「解出来能不能用」的校验兜底，不必等下一次重试。
        """
        stem = self._dat_stem(dat_path)
        keys: List[int] = []
        for path in (stem + "_t.dat", stem + "_h.dat", dat_path):
            if path != dat_path and not os.path.exists(path):
                continue
            key = self._key_from_tail(self._read_tail(path))
            if key is not None and key not in keys:
                keys.append(key)
        legacy = self._derive_xor_key(dat_path)
        if legacy not in keys:
            keys.append(legacy)
        return keys[:4] or [0x88]

    def _derive_xor_key(self, dat_path: str) -> int:
        """从同图兄弟文件尾部反推单字节 XOR 密钥（推不出时返回旧默认 0x88）。"""
        stem = self._dat_stem(dat_path)
        for cand in (stem + "_t.dat", stem + "_h.dat", dat_path):
            if not os.path.exists(cand):
                continue
            key = self._key_from_tail(self._read_tail(cand))
            if key is not None:
                return key
        return 0x88

    def _scan_aes_key(self, monitor: bool = False,
                      monitor_timeout: float = 120.0) -> Optional[str]:
        """扫描 Weixin.exe 内存穷举候选密钥, AES-ECB 解密探针验证。

        候选两类模式(YARA 思路):
        - ASCII: 非字母数字 + 连续 32 个 [a-zA-Z0-9] + 非字母数字结尾,
          每候选取前 16 字节作 AES-128 key 解密探针, 明文为 JPEG SOI 命中;
        - UTF-16LE: 字母数字与 0x00 交错形式。

        时间预算（1.2.7 修复）：``monitor_timeout`` 现在是**真实墙钟上限**。
        现场一轮全量扫描约 110s（5 个 Weixin 进程 ~1.3GB），旧实现只在两轮
        扫描之间查超时，单轮扫多久都不管，导致「120 秒等待」实际拖 10~30
        分钟。现在每轮扫描内部逐内存区域检查 deadline，到点立即返回。
        """
        probes = self._probe_cts()
        if not probes:
            return None
        pids = self.db._find_weixin_pids()
        if not pids:
            return None
        from . import db as _dbmod
        k32 = _dbmod._k32
        MBI = _dbmod._MBI

        # 图片 AES 密钥是 16 字节 ASCII（字母/数字），并非 32 字符。
        # 旧实现 ASCII32_RE 要求「恰好 32 个字母数字」→ 16 字符的正确密钥
        # 被永久漏掉，内存扫描永远不命中（1.2.7 现场）。
        #
        # 密钥真实形态：MD5(str(cfgDword)+wxid) 的前 16 个 hex 字符，即
        # 16 字节的 [0-9a-f]。这里按两层匹配：
        #   HEX16_RE —— 恰好 16 字符的纯 hex（最可能，优先且快速）；
        #   KEY_RE   —— 16~32 字符字母数字（兜底，覆盖其它编码/长度）。
        # 候选去重后再做 AES 探针验证，避免海量重复候选拖慢扫描。
        HEX16_RE = re.compile(rb"(?<![0-9a-fA-F])([0-9a-f]{16})(?![0-9a-fA-F])")
        KEY_RE = re.compile(rb"(?<![0-9a-zA-Z])([0-9a-zA-Z]{16,32})(?![0-9a-zA-Z])")
        U16_RE = re.compile(rb"(?:[a-zA-Z0-9]\x00){16,32}")

        def read_mem(h, addr: int, n: int):
            buf = ctypes.create_string_buffer(n)
            br = ctypes.c_size_t(0)
            if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(br)) and br.value:
                return buf.raw[: br.value]
            return None

        def _try_key(key16: bytes) -> bool:
            try:
                from cryptography.hazmat.primitives.ciphers import (
                    Cipher, algorithms, modes,
                )
                for probe in probes:
                    pt = Cipher(algorithms.AES(key16), modes.ECB()).decryptor()
                    out = pt.update(probe) + pt.finalize()
                    if out[:3] == b"\xff\xd8\xff" or _jpeg_like(out):
                        return True
            except Exception:
                return False
            return False

        def _candidates(buf: bytes):
            seen = set()
            for m in HEX16_RE.finditer(buf):
                k = m.group(1)
                if k not in seen:
                    seen.add(k)
                    yield k
            for m in KEY_RE.finditer(buf):
                k = m.group(1)[:16]
                if k not in seen:
                    seen.add(k)
                    yield k
            for m in U16_RE.finditer(buf):
                k = bytes(b for i, b in enumerate(m.group()) if i % 2 == 0)[:16]
                if k not in seen:
                    seen.add(k)
                    yield k

        def _scan_once() -> Optional[str]:
            # 保持微信进程原顺序扫描(主进程靠前, 命中率高)；逐区域检查
            # deadline，保证任何一次扫描都在时间预算内结束。
            for pid in pids:
                if time.monotonic() > deadline:
                    return None
                h = k32.OpenProcess(0x0010 | 0x0400, False, pid)
                if not h:
                    continue
                try:
                    addr = 0
                    while True:
                        if time.monotonic() > deadline:
                            return None
                        mbi = MBI()
                        r = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
                        if r == 0:
                            break
                        if (
                            mbi.State == 0x1000
                            and (mbi.Protect & 0xFF) & 0xE6
                            and not (mbi.Protect & 0x100)
                            and 0 < mbi.RegionSize < 0x2000000
                        ):
                            buf = read_mem(h, mbi.BaseAddress or 0, mbi.RegionSize)
                            if buf:
                                for key in _candidates(buf):
                                    if _try_key(key):
                                        return key.decode("ascii", "replace")
                        addr = (mbi.BaseAddress or 0) + mbi.RegionSize
                finally:
                    k32.CloseHandle(h)
            return None

        # 时间预算：monitor_timeout 即整个调用（首轮扫描 + monitor 等待循环）
        # 的墙钟上限。pythonw 打包环境下 print 用户看不到，改走日志。
        deadline = time.monotonic() + max(5.0, float(monitor_timeout))

        found = _scan_once()
        if found or not monitor:
            return found

        logger.info(
            "未在微信进程内存中找到图片 AES 密钥。请现在打开微信，进入任意"
            "聊天，点击一张图片查看大图，本程序将在 %.0f 秒内自动捕获密钥...",
            max(0.0, deadline - time.monotonic()))
        while time.monotonic() < deadline:
            time.sleep(2.0)
            found = _scan_once()
            if found:
                return found
        return None

    def detect_image_key(self, refresh: bool = False,
                         monitor_timeout: float = 120.0
                         ) -> Optional[Tuple[str, int]]:
        """返回 (AES 密钥, XOR 密钥)；失败返回 None。结果缓存，refresh=True 强制重扫。

        Args:
            refresh: True 强制重新扫描（忽略进程内缓存）。
            monitor_timeout: 内存扫描的墙钟预算（秒）。一轮全量扫描实测约
                110s，预算必须给足，否则首轮扫描会被截断漏掉密钥。

        总流程:
          定位缓存目录 → 收集 *_t.dat 模板 → 文件尾推 XOR 密钥(众数统计)
          → 文件头取 AES 密文 → cfgDword 派生 / 扫描 Weixin.exe 内存穷举
          候选密钥 → AES-ECB 解密验证(JPEG SOI)。

        密钥来源优先级：cfgDword 派生(确定性离线, 免看图驻留) → 显式注入
        image_key → 本地缓存 → 进程内存扫描。命中后持久化, 下次免扫。
        """
        if self._img_key and not refresh:
            return self._img_key
        if not self._probe_cts():
            return None
        templates = self._collect_templates()
        # 1) XOR: 模板文件尾众数统计
        xor_key = self._get_xor_key(templates)
        if xor_key is None:
            # 旧实现调用了一个不存在的 _dbg_last_dat()：模板全反推不出时
            # detect_image_key() 直接 AttributeError。用同一份缓存样本兜底。
            dat = templates[0] if templates else None
            xor_key = self._derive_xor_key(dat) if dat else 0x88
        # 2) AES: cfgDword 派生优先
        derived = self._derive_cfg_key()
        if derived:
            self._img_key = (derived[0], xor_key)
            self._notify_key_ready(derived[0])
            return self._img_key
        aes_key = None
        if self._image_key and self._validate_key(self._image_key):
            aes_key = self._image_key
        if not aes_key:
            aes_key = self._load_persisted_key()
        if not aes_key:
            aes_key = self._scan_aes_key(monitor=True,
                                         monitor_timeout=monitor_timeout)
            if aes_key:
                self._persist_key(aes_key)
        if not aes_key:
            return None
        self._img_key = (aes_key, xor_key)
        self._notify_key_ready(aes_key)
        return self._img_key

    # ------------------------------------------------------------------
    # 图片解密
    # ------------------------------------------------------------------
    def decrypt_image(self, dat_path: str, aes_key: Optional[str] = None,
                      xor_key: Optional[int] = None) -> bytes:
        """解密单个 .dat 为图片字节（自动识别 v1/v2 格式）"""
        with open(dat_path, "rb") as f:
            data = f.read()
        if not data:
            raise ValueError("空文件: %s" % dat_path)
        magic = data[:6]
        if magic == V2_MAGIC:
            return self._decrypt_v2(data, dat_path, aes_key, xor_key)
        if magic == V1_MAGIC:
            if xor_key is None:
                xor_key = self._derive_xor_key(dat_path)
            key = data[6:22]
            body = data[22:]
            return xor_bytes(body, xor_key)
        # 早期纯异或格式：逐字节 ^ 0xFF（无签名），按 JPEG/PNG 魔数回退判断
        for cand in (0x88, 0x30, 0xFF, 0xE9):
            out = bytes(b ^ cand for b in data)
            if out[:3] == b"\xff\xd8\xff" or out[:4] == b"\x89PNG":
                return out
        raise ValueError("无法识别的图片加密格式: %s" % dat_path)

    def _resolve_aes_key(self) -> Optional[str]:
        """统一密钥解析：显式注入 → cfgDword派生 → 本地缓存（**不做内存扫描**）。

        1.2.7 现场实测：图片解密路径里做 ``_scan_aes_key(monitor=True)`` 会把
        单次取图拖到 30+ 分钟（全量扫 5 个 Weixin 进程约 1.3GB 内存，一轮
        ~110s，monitor 等待循环每轮再扫一次；账号 XOR 学习的「实测」还会对
        每个候选再触发整套扫描），表现就是「图片永远不处理、日志静默」。
        解密路径只查**零成本**来源（注入/派生/持久化），查不到立刻抛出可
        操作的 RuntimeError 让上层快速失败并排队重试；主动内存扫描只保留在
        :meth:`detect_image_key`（设置页「重新检查」触发，有时间预算）。
        """
        if self._image_key and self._validate_key(self._image_key):
            self._notify_key_ready(self._image_key)
            return self._image_key
        # 尝试cfgDword派生
        derived = self._derive_cfg_key()
        if derived:
            self._notify_key_ready(derived[0])
            return derived[0]
        cached = self._load_persisted_key()
        if cached:
            return cached
        return None

    def _decrypt_v2(self, data: bytes, dat_path: str,
                    aes_key: Optional[str], xor_key: Optional[int]) -> bytes:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        aes_size, xor_size = struct.unpack_from("<LL", data, 6)
        if xor_key is None:
            xor_key = self._derive_xor_key(dat_path)
        if aes_key is None:
            aes_key = self._resolve_aes_key()
            if not aes_key:
                raise RuntimeError(
                    "无法获取图片 AES 密钥：请在微信中打开（点击查看大图）任意"
                    "一张图片，然后到本系统「设置 → 图片解密密钥」点「重新检查」"
                    "捕获；捕获成功后未识别的图片会自动补识别。"
                )
        aes_blk = aligned_aes_block_size(aes_size)
        off = 15
        aes_data = data[off: off + aes_blk]
        off += aes_blk
        raw_data = data[off: len(data) - xor_size]
        xor_data = data[len(data) - xor_size:]
        dec = Cipher(algorithms.AES(aes_key.encode()), modes.ECB()).decryptor()
        pt = dec.update(aes_data) + dec.finalize()
        pad = pt[-1] if pt else 0
        if 1 <= pad <= 16 and all(b == pad for b in pt[-pad:]):
            pt = pt[:-pad]
        return pt + raw_data + xor_bytes(xor_data, xor_key)

    # ------------------------------------------------------------------
    # 定位本地文件
    # ------------------------------------------------------------------
    def _chat_md5(self, user: str) -> str:
        import hashlib
        return hashlib.md5(user.encode()).hexdigest()

    def _month_of(self, create_time: int) -> str:
        return time.strftime("%Y-%m", time.localtime(create_time))

    def _find_dat(self, user: str, md5: str, create_time: int,
                  thumbnail: bool = False) -> Optional[str]:
        base = os.path.join(self.db.account_dir, "msg", "attach", self._chat_md5(user))
        target = md5 + ("_t.dat" if thumbnail else ".dat")
        for root, _, files in os.walk(base):
            for f in files:
                if f == target:
                    return os.path.join(root, f)
        return None

    def _find_h_dat(self, user: str, md5: str) -> Optional[str]:
        """查找原图 _h.dat 文件（点击'图片原始大小'后微信下载的高分辨率版本）。"""
        base = os.path.join(self.db.account_dir, "msg", "attach", self._chat_md5(user))
        target = md5 + "_h.dat"
        for root, _, files in os.walk(base):
            for f in files:
                if f == target:
                    return os.path.join(root, f)
        return None

    # ------------------------------------------------------------------
    # 各类媒体下载
    # ------------------------------------------------------------------
    def _out(self, save_dir: Optional[str], name: str) -> str:
        d = save_dir or self.save_dir
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, name)

    # ------------------------------------------------------------------
    # WXAM (wxgf) 解码：微信 4.x 普通图片的新存储格式，内部为 HEVC 裸流
    # ------------------------------------------------------------------
    def _extract_hevc(self, data: bytes) -> Optional[bytes]:
        """从 wxgf 容器提取 HEVC Annex-B 裸流（自首个 NALU 起始码起）。"""
        start = data.find(b"\x00\x00\x00\x01")
        return data[start:] if start >= 0 else None

    @staticmethod
    def _ffmpeg_exe() -> Optional[str]:
        import shutil
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None

    def _wxgf_to_jpg(self, data: bytes) -> Optional[bytes]:
        """把 wxgf 内的 HEVC 裸流转码为 jpg：先 ffmpeg，失败再用 OpenCV。

        为什么要 cv2 兜底：本机（以及绝大多数只装 requirements 的环境）没有
        ffmpeg，原图**必然**回退到 ``_t.dat`` 缩略图（实测 136x180）。缩略图
        里 VIN/件号只有 2~3 像素高，视觉模型读不出来就会硬编一个（实测把
        ``LFV3A24F2A3102185`` 读成 ``LFV3A23P2N3782165``）。OpenCV 自带
        FFmpeg 后端能直接解 HEVC 裸流（本机 cv2 5.0.0 实测输出 1706x1280 原图），
        所以这里加一条零新增依赖的兜底路径。
        """
        hevc = self._extract_hevc(data)
        if not hevc:
            return None
        out = self._hevc_to_jpg_ffmpeg(hevc)
        if out:
            return out
        return self._hevc_to_jpg_cv2(hevc)

    @staticmethod
    def _hevc_to_jpg_ffmpeg(hevc: bytes) -> Optional[bytes]:
        """ffmpeg 转码（PATH 里的 ffmpeg 或 imageio_ffmpeg 自带的可执行文件）。"""
        exe = MediaDownloader._ffmpeg_exe()
        if exe is None:
            return None
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.hevc")
            dst = os.path.join(td, "out.jpg")
            with open(src, "wb") as f:
                f.write(hevc)
            try:
                r = subprocess.run(
                    [exe, "-y", "-v", "error", "-i", src, "-frames:v", "1", dst],
                    capture_output=True, timeout=30,
                    # Windows 上不隐藏控制台子进程会闪出 ffmpeg.exe 黑框
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                return None
            if r.returncode == 0:
                try:
                    with open(dst, "rb") as f:
                        out = f.read()
                    return out if out[:3] == b"\xff\xd8\xff" else None
                except OSError:
                    return None
        return None

    @staticmethod
    def _hevc_to_jpg_cv2(hevc: bytes) -> Optional[bytes]:
        """OpenCV 解码 HEVC 裸流（无 ffmpeg 时的兜底）。

        cv2.VideoCapture 只吃文件/流地址，不吃内存字节，所以落一个临时
        ``.h265`` 再读首帧；任何异常返回 None（调用方继续回退缩略图）。
        """
        try:
            import cv2
        except Exception:
            return None
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".h265")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(hevc)
            cap = cv2.VideoCapture(path)
            try:
                if not cap.isOpened():
                    return None
                ok, frame = cap.read()
            finally:
                cap.release()
            if not ok or frame is None:
                return None
            ok, buf = cv2.imencode(
                ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
            return buf.tobytes() if ok else None
        except Exception:
            logger.debug("cv2 解码 HEVC 失败（回退缩略图）", exc_info=True)
            return None
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _img_md5(self, row: dict) -> Optional[str]:
        pi = row.get("packed_info")
        content = row.get("content")
        for blob in (pi, content):
            if isinstance(blob, bytes):
                m = re.search(rb"([0-9a-fA-F]{32})", blob)
                if m:
                    return m.group(1).decode().lower()
        return None

    _IMAGE_EXTS = ("jpg", "png", "gif")

    @staticmethod
    def _image_ext(data: bytes) -> str:
        """图片字节 → 落盘后缀（无法内嵌展示的格式返回 "img"）。"""
        if data[:3] == b"\xff\xd8\xff":
            return "jpg"
        if data[:4] == b"\x89PNG":
            return "png"
        if data[:3] == b"GIF":
            return "gif"
        return "img"

    @classmethod
    def _cache_name(cls, user: str, local_id: int, ext: str,
                    thumbnail: bool = False) -> str:
        """缓存文件名：``<user>_<local_id>[_thumb].<ext>``。"""
        return "%s_%s%s.%s" % (user, local_id,
                               "_thumb" if thumbnail else "", ext)

    def _cached_image(self, user: str, local_id: int,
                      save_dir: Optional[str] = None) -> Optional[str]:
        """查找**内容合格**的图片缓存；坏缓存改名为 ``.bak``（自愈）。

        旧版本会把半截/花图写进缓存，命中后用户永远看到花图。这里即使
        命中缓存也校验一次：已验证过的路径走进程内集合（不重复解码），
        未验证的读出来校验，不合格就**改名 .bak 让调用方重新解密**。

        注意：不直接删除。判坏规则（单色占比）对白底商品图存在理论误杀风险，
        改名保留原件便于人工找回与规则复盘；下次访问会重新解密覆盖同名缓存。
        """
        directory = save_dir or self.save_dir
        for thumbnail in (False, True):
            for ext in self._IMAGE_EXTS:
                path = os.path.join(
                    directory, self._cache_name(user, local_id, ext, thumbnail))
                if not os.path.isfile(path):
                    continue
                if is_path_verified(path):
                    return path
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                except OSError:
                    continue
                if is_usable_image(data):
                    _remember_verified(path)
                    return path
                forget_verified(path)
                backup = path + ".bak"
                try:
                    os.replace(path, backup)
                    logger.info("不合格的图片缓存已改名备份（花图自愈）：%s → %s",
                                path, backup)
                except OSError as exc:
                    logger.warning("不合格的图片缓存改名失败（%s）：%s",
                                   path, exc)
        return None

    def _write_image_cache(self, user: str, local_id: int, data: bytes,
                           save_dir: Optional[str] = None,
                           thumbnail: bool = False) -> Optional[str]:
        """写图片缓存（``.part`` + 原子改名）；只接受合格内容。"""
        ext = self._image_ext(data)
        if ext == "img":
            return None
        directory = save_dir or self.save_dir
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            logger.warning("创建图片缓存目录失败（%s）：%s", directory, exc)
            return None
        out = os.path.join(directory,
                           self._cache_name(user, local_id, ext, thumbnail))
        tmp = out + ".part"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, out)
        except OSError as exc:
            logger.warning("写图片缓存失败（%s）：%s", out, exc)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return None
        _remember_verified(out)
        return out

    # 已尝试过「缩略图缓存 → 原图」升级的 (user, local_id)：同一张图只试一次，
    # 避免原图确实不存在（微信没下载）时每次取图都白跑一遍解密
    _upgrade_tried: Set[Tuple[str, int]] = set()

    def _upgrade_thumbnail_cache(self, user: str, local_id: int, md5: str,
                                 create_time: int, cached: str, current: bytes,
                                 aes_key: Optional[str], xor_key: Optional[int],
                                 save_dir: Optional[str]
                                 ) -> Optional[Tuple[bytes, Optional[str]]]:
        """命中缩略图缓存时，再尝试解一次原图（拿到就升级缓存）。

        背景：旧版本没有 HEVC 解码能力，原图必然回落缩略图并写成
        ``*_thumb.jpg``；这类缓存**命中后不再解密**，于是视觉模型永远只看到
        136x180 的图（VIN/件号 2~3 像素高，读不出就会硬编）。补上 OpenCV
        兜底后原图可解，这里给老缓存一次升级机会；原图仍拿不到就保持现状
        （不删缓存、不抛异常、不影响调用方）。
        """
        if "_thumb" not in os.path.basename(cached):
            return None
        key = (str(user), int(local_id))
        if key in self._upgrade_tried:
            return None
        self._upgrade_tried.add(key)
        if not md5:
            return None
        try:
            decoded = self._decode_usable_image(
                user, md5, create_time, aes_key, xor_key)
        except Exception:
            logger.debug("缩略图缓存升级失败（%s/%s）", user, local_id,
                         exc_info=True)
            return None
        if not decoded:
            return None
        data, thumbnail = decoded
        if thumbnail or len(data) <= len(current):
            return None
        path = self._write_image_cache(user, local_id, data, save_dir,
                                       thumbnail=False)
        logger.info("图片缓存由缩略图升级为原图：%s/%s（%d → %d 字节）",
                    user, local_id, len(current), len(data))
        return data, path or cached

    # 账号级 XOR 密钥学习参数（见 :meth:`_learn_account_xor_key`）
    _ACCT_KEY_TTL = 600.0        # 学到后的缓存时长
    _ACCT_KEY_NEG_TTL = 60.0     # 学不出来时的短缓存（别每张图都扫盘）
    _ACCT_KEY_VOTE_FILES = 16    # 参与投票的缩略图数量
    _ACCT_KEY_VERIFY_FILES = 3   # 胜出者实测几张

    def _decode_file(self, path: str, keys: List[int],
                     aes_key: Optional[str]) -> Optional[bytes]:
        """按候选密钥逐个解一个 .dat，返回第一个**可用**的图片字节。

        Args:
            path: .dat 文件路径。
            keys: 待尝试的单字节 XOR 密钥（按可信度排序）。
            aes_key: 图片 AES 密钥（None = 自动解析）。

        Returns:
            可用的图片字节；全部密钥都解不出可用图返回 None。
        """
        last_size = 0
        decrypted = False
        for index, key in enumerate(keys):
            try:
                data = self.decrypt_image(path, aes_key, xor_key=key)
            except Exception as exc:
                logger.warning("图片解密失败（%s）：%s",
                               os.path.basename(path), exc)
                break
            decrypted = True
            last_size = len(data)
            if data[:4] == b"wxgf":
                # HEVC 容器转码后同样要过校验（这条路径最容易出半截绿块）
                jpg = self._wxgf_to_jpg(data)
                if jpg is None:
                    logger.info("wxgf 转码失败（%s key=0x%02x），换密钥重试",
                                os.path.basename(path), key)
                    continue
                data = jpg
            if not is_usable_image(data):
                continue
            if index:
                logger.info(
                    "图片 XOR 密钥回退命中：%s 用 key=0x%02x"
                    "（首选 0x%02x 解不出可用图）",
                    os.path.basename(path), key, keys[0])
            return data
        if decrypted:
            logger.info("图片不完整或疑似花屏，跳过：%s（%d 字节）",
                        os.path.basename(path), last_size)
        return None

    def _decode_usable_image(self, user: str, md5: str, create_time: int,
                             aes_key: Optional[str],
                             xor_key: Optional[int]
                             ) -> Optional[Tuple[bytes, bool]]:
        """逐候选解密并校验，返回 (图片字节, 是否来自缩略图)；全不合格返回 None。

        候选顺序：原图 ``.dat`` → 原图高清 ``_h.dat`` → 缩略图 ``_t.dat``。
        原图常因微信只下载了一部分而不完整，此时回落到缩略图——缩略图在
        群聊里通常完整，尺寸小但至少是真图。

        密钥策略（两轮，批次 13 需求 7 补第二轮）：

        1. 逐文件反推（``原图 → _h → _t`` 兄弟尾部）——绝大多数情况够用；
        2. **账号级密钥兜底**：实测同账号 40 张缩略图里 35 张能反推出真密钥，
           另外 5 张因明文尾部带填充/噪声反推出假密钥（``0x11/0x2A/...``），
           结果是 AES 首块（JPEG 头）正常、之后全乱码 → 被误判成「半截/花屏」，
           误报「微信还没下载完」。这类图等多久都不会好，只能靠账号级密钥救。
        """
        candidates: List[Tuple[str, bool]] = []
        seen: Set[str] = set()
        for path, thumbnail in (
            (self._find_dat(user, md5, create_time), False),
            (self._find_h_dat(user, md5), False),
            (self._find_dat(user, md5, create_time, thumbnail=True), True),
        ):
            if path and path not in seen:
                seen.add(path)
                candidates.append((path, thumbnail))
        for path, thumbnail in candidates:
            # 显式传入密钥时只认那一个；否则按可信度逐个试，直到解出可用图。
            # 单字节密钥推导依赖「文件已写完整」，微信边下边写时尾部是半截
            # 数据、推出来的密钥是错的，多候选 + 可用性校验才能自愈。
            keys = [xor_key] if xor_key is not None \
                else self._xor_key_candidates(path)
            data = self._decode_file(path, keys, aes_key)
            if data is None:
                continue
            if thumbnail:
                logger.info("原图不可用，已回退缩略图：%s %s",
                            user, os.path.basename(path))
            return data, thumbnail
        # 第二轮：账号级密钥兜底（尾字节反推在「尾部带填充」的文件上会失手）
        learned = self._learn_account_xor_key() if xor_key is None else None
        if learned is not None:
            for path, thumbnail in candidates:
                if learned in self._xor_key_candidates(path):
                    continue            # 已试过这个密钥，别再解一遍
                data = self._decode_file(path, [learned], aes_key)
                if data is None:
                    continue
                logger.info("账号级 XOR 密钥兜底命中：%s key=0x%02x"
                            "（逐文件反推失手，文件本身是完整的）",
                            os.path.basename(path), learned)
                if thumbnail:
                    logger.info("原图不可用，已回退缩略图：%s %s",
                                user, os.path.basename(path))
                return data, thumbnail
        return None

    def _learn_account_xor_key(self) -> Optional[int]:
        """学出**账号级**单字节 XOR 密钥（尾字节投票 + 实测校验）。

        单字节 XOR 密钥按账号固定，但逐文件反推依赖「明文恰好以 ``FF D9``
        结尾」；微信写 .dat 时可能带尾部填充，于是反推出的是假密钥。这里取
        账号内最新一批缩略图各自反推并投票，得票最高者再用真实文件实测，
        实测通过才采信。

        Returns:
            密钥（0-255）或 None（拿不到账号目录 / 样本不足 / 学不出来）。
        """
        account_dir = str(getattr(self.db, "account_dir", "") or "")
        if not account_dir:
            return None
        now = time.monotonic()
        with self._acct_key_lock:
            hit = self._acct_key_cache.get(account_dir)
            if hit is not None and now < hit[0]:
                return hit[1]
            flight = self._acct_key_flight.get(account_dir)
            if flight is None:
                # 本线程负责探测；其余 vision worker 等结果，不再各自扫盘
                flight = threading.Event()
                self._acct_key_flight[account_dir] = flight
                owner = True
            else:
                owner = False
        if not owner:
            flight.wait(timeout=10.0)
            with self._acct_key_lock:
                hit = self._acct_key_cache.get(account_dir)
                if hit is not None and time.monotonic() < hit[0]:
                    return hit[1]
            return None
        try:
            key = self._detect_account_xor_key()
            ttl = self._ACCT_KEY_TTL if key is not None \
                else self._ACCT_KEY_NEG_TTL
            with self._acct_key_lock:
                self._acct_key_cache[account_dir] = (
                    time.monotonic() + ttl, key)
            return key
        finally:
            with self._acct_key_lock:
                self._acct_key_flight.pop(account_dir, None)
            flight.set()

    def _detect_account_xor_key(self) -> Optional[int]:
        """:meth:`_learn_account_xor_key` 的实际探测（不含缓存）。"""
        try:
            files = self._collect_templates(
                limit=self._ACCT_KEY_VOTE_FILES * 4,
                keep=self._ACCT_KEY_VOTE_FILES)
        except Exception:
            logger.exception("收集缩略图样本失败（学不出账号密钥，已忽略）")
            return None
        votes: Counter = Counter()
        samples: Dict[int, List[str]] = {}
        for path in files:
            try:
                key = self._key_from_tail(self._read_tail(path))
            except OSError:
                continue
            if key is None:
                continue            # 尾部带填充的文件反推不出来，跳过
            votes[key] += 1
            samples.setdefault(key, []).append(path)
        if not votes:
            logger.warning("学不出账号级图片 XOR 密钥（%d 个样本都反推不出）",
                           len(files))
            return None
        for key, count in votes.most_common(3):
            paths = samples.get(key, [])[:self._ACCT_KEY_VERIFY_FILES]
            if not self._verify_account_key(key, paths):
                logger.info("账号密钥候选 0x%02x（%d 票）实测不可用，换下一个",
                            key, count)
                continue
            logger.info("已学出账号级图片 XOR 密钥 0x%02x（%d/%d 票，实测通过）",
                        key, count, len(files))
            return key
        logger.warning("学不出账号级图片 XOR 密钥（%d 个样本，候选均未通过实测）",
                       len(files))
        return None

    def _verify_account_key(self, key: int, paths: List[str]) -> bool:
        """用真实文件实测某候选密钥是不是账号密钥（最多 3 张，够判）。"""
        ok = tried = 0
        for path in paths:
            tried += 1
            try:
                data = self.decrypt_image(path, None, xor_key=key)
            except Exception:
                continue
            if is_usable_image(data):
                ok += 1
        if tried >= 2:
            return ok >= 2
        return tried == 1 and ok == 1

    def load_image(self, user: str, local_id: int,
                   save_dir: Optional[str] = None,
                   aes_key: Optional[str] = None,
                   xor_key: Optional[int] = None
                   ) -> Tuple[Optional[bytes], Optional[str], str]:
        """取一张图片消息的**内容合格**字节：命中缓存 → 原图 → 缩略图。

        候选顺序固定为 ``原图(.dat) → _h.dat → _t.dat``，逐个做「完整性 +
        花屏」双重校验，第一个合格的胜出；全都不合格返回 None（宁可让前端
        显示占位，也不要花图）。

        注意：原图解密出来常见两种形态——``wxgf``（微信自研 HEVC 容器，走
        OpenCV/ffmpeg 转码）和 PNG（复制粘贴的图片，``_h.dat`` 就是原图）。
        单字节 XOR 密钥按账号固定但推导依赖文件已写完整，所以这里对每个候选
        文件按可信度试多个密钥（见 :meth:`_xor_key_candidates`），解不出可用
        图就继续下一个候选，最后才回退缩略图。

        Args:
            user: 会话 username。
            local_id: 消息 local_id。
            save_dir: 缓存目录（默认 ``self.save_dir``）。
            aes_key: 图片 AES 密钥（None = 自动解析）。
            xor_key: 单字节 XOR 密钥（None = 自动推导）。

        Returns:
            ``(图片字节, 缓存路径, 失败原因)``：成功时原因为空串；
            失败时字节与路径为 None、原因为中文说明（可直接写日志/展示）。
        """
        row = self.db.get_message_row(user, local_id)
        if not row or row["local_type"] != 3:
            return None, None, "不是图片消息"
        md5 = self._img_md5(row)
        if not md5:
            return None, None, "消息里取不到图片 md5（无法定位 .dat）"
        cached = self._cached_image(user, local_id, save_dir)
        if cached:
            try:
                with open(cached, "rb") as f:
                    data = f.read()
            except OSError as exc:
                logger.warning("读取图片缓存失败（%s）：%s", cached, exc)
                forget_verified(cached)
                data = b""
            if data:
                upgraded = self._upgrade_thumbnail_cache(
                    user, local_id, md5, row["create_time"], cached, data,
                    aes_key, xor_key, save_dir)
                if upgraded is not None:
                    return upgraded[0], upgraded[1], ""
                return data, cached, ""
        decoded = self._decode_usable_image(
            user, md5, row["create_time"], aes_key, xor_key)
        if decoded is None:
            return None, None, "原图与缩略图均不完整，返回占位"
        data, thumbnail = decoded
        path = self._write_image_cache(user, local_id, data, save_dir,
                                       thumbnail)
        return data, path, ""

    def download_image(self, user: str, local_id: int, save_dir: Optional[str] = None,
                       aes_key: Optional[str] = None, xor_key: Optional[int] = None) -> Optional[str]:
        """下载图片消息并解密为 jpg/png/gif，返回落盘路径。

        只返回**内容合格**的图片（批次 6 待办 1）：原图不完整/花屏时自动
        回退缩略图；两者都不合格时返回 None，由调用方展示占位卡片
        （宁可占位也不要花图）。具体流程见 :meth:`load_image`。
        """
        data, path, reason = self.load_image(
            user, local_id, save_dir, aes_key, xor_key)
        if data is None:
            logger.info("图片下载失败（%s #%s）：%s", user, local_id, reason)
            return None
        return path

    def download_voice(self, user: str, local_id: int, save_dir: Optional[str] = None) -> Optional[str]:
        """语音：media_*.db VoiceInfo.voice_data（SILK 二进制），落盘 .silk

        微信按账号/时间把语音分片存到多个 media_*.db，逐个搜索直到找到。
        """
        row = self.db.get_message_row(user, local_id)
        if not row or row["local_type"] != 34 or not row["server_id"]:
            return None
        for rel, path, _ in self.db._db_files:
            if not os.path.basename(path).startswith("media_"):
                continue
            conn = self.db._open(rel)
            try:
                cid = conn.execute(
                    "SELECT rowid FROM Name2Id WHERE user_name=?", (user,)
                ).fetchone()
                chat_id = cid[0] if cid else None
                if chat_id is None:
                    continue
                v = conn.execute(
                    "SELECT voice_data FROM VoiceInfo WHERE chat_name_id=? AND svr_id=? "
                    "ORDER BY create_time DESC LIMIT 1",
                    (chat_id, row["server_id"]),
                ).fetchone()
            finally:
                conn.close()
            if v and v["voice_data"]:
                out = self._out(save_dir, "%s_%s.silk" % (user, local_id))
                with open(out, "wb") as f:
                    f.write(v["voice_data"])
                return out
        return None

    def download_video(self, user: str, local_id: int, save_dir: Optional[str] = None) -> Optional[str]:
        """视频：按 packed_info 中的 id 在 msg/video 下查找 <id>.mp4"""
        row = self.db.get_message_row(user, local_id)
        if not row or row["local_type"] != 43:
            return None
        pi = row.get("packed_info")
        if not isinstance(pi, bytes):
            return None
        m = re.search(rb"([0-9a-fA-F]{32})", pi)
        vid = m.group(1).decode().lower() if m else None
        base = os.path.join(self.db.account_dir, "msg", "video")
        for root, _, files in os.walk(base):
            for f in files:
                if vid and f == vid + ".mp4":
                    out = self._out(save_dir, "%s_%s.mp4" % (user, local_id))
                    with open(out, "wb") as w:
                        with open(os.path.join(root, f), "rb") as r:
                            w.write(r.read())
                    return out
        return None

    def _file_name(self, row: dict) -> Optional[str]:
        if not row["server_id"]:
            return None
        for rel, path, _ in self.db._db_files:
            if os.path.basename(path) != "message_resource.db":
                continue
            conn = self.db._open(rel)
            try:
                r = conn.execute(
                    "SELECT d.packed_info FROM MessageResourceDetail d "
                    "LEFT JOIN MessageResourceInfo i ON d.message_id=i.message_id "
                    "WHERE i.message_svr_id=? LIMIT 1",
                    (row["server_id"],),
                ).fetchone()
            finally:
                conn.close()
            if r and r["packed_info"]:
                name = r["packed_info"].decode("utf-8", "replace").strip()
                name = re.sub(r"[\r\n\x00]+", "", name)
                if "/" in name or "\\" in name:
                    name = name.split("/")[-1].split("\\")[-1]
                return name or None
            break
        return None

    def download_file(self, user: str, local_id: int, save_dir: Optional[str] = None) -> Optional[str]:
        """文件：msg/file/<YYYY-MM>/<原文件名>，原文件名来自 message_resource"""
        row = self.db.get_message_row(user, local_id)
        if not row or row["local_type"] != 49:
            return None
        name = self._file_name(row)
        if not name:
            return None
        base = os.path.join(self.db.account_dir, "msg", "file")
        for root, _, files in os.walk(base):
            for f in files:
                if f == name:
                    out = self._out(save_dir, "%s_%s_%s" % (user, local_id, name))
                    with open(out, "wb") as w:
                        with open(os.path.join(root, f), "rb") as r:
                            w.write(r.read())
                    return out
        return None

    @staticmethod
    def _find_preview_button(ctrl, name, max_depth=8):
        """在 PreviewWindow 中递归查找指定名称的按钮。"""
        if max_depth <= 0:
            return None
        for kid in ctrl.GetChildren():
            try:
                if kid.Name == name:
                    return kid
                found = MediaDownloader._find_preview_button(kid, name, max_depth - 1)
                if found:
                    return found
            except Exception:
                pass
        return None

    def download_image_original(self, user: str, local_id: int, save_dir: Optional[str] = None,
                              aes_key: Optional[str] = None, xor_key: Optional[int] = None,
                              timeout: float = 30.0, chat_name: Optional[str] = None) -> Optional[str]:
        """下载原图：通过UI自动化点击图片消息触发微信下载原图。

        原理：群聊图片默认只下发缩略图（_t.dat），原图（.dat）只有在微信中
        点击查看大图后才会下载到本地。本方法模拟用户点击图片消息，等待原图
        下载完成后解密保存。

        Args:
            user: 会话用户名（wxid 或群聊 ID）
            local_id: 消息 local_id
            save_dir: 保存目录（默认 ~/Documents/wechatauto_media）
            aes_key: 图片 AES 密钥（可选，自动检测）
            xor_key: XOR 密钥（可选，自动检测）
            timeout: 等待原图下载的超时时间（秒）
            chat_name: 用于UI搜索的会话名称（微信名，默认使用 user）

        Returns:
            解密后的原图文件路径，失败返回 None
        """
        row = self.db.get_message_row(user, local_id)
        if not row or row["local_type"] != 3:
            return None
        md5 = self._img_md5(row)
        if not md5:
            return None

        from .uia_driver import WeChatUIA
        import uiautomation as auto
        from .guia import WinInput

        _uia = WeChatUIA()
        if not _uia.ensure_window():
            return None
        time.sleep(1.0)

        for _retry in range(3):
            try:
                from .wx import WeChat
                wx = WeChat()
                wx.ChatWith(chat_name or user)
                time.sleep(2.0)
                break
            except Exception as e:
                print("[DBG] ChatWith retry", _retry, "err:", type(e).__name__, str(e)[:100])
                time.sleep(1.0)

        clicked = False
        try:
            time.sleep(1.0)
            lst = _uia._message_list()
            if lst is None:
                print("[DBG] message_list is None")
                return None
            inp = WinInput()

            lst_rect = lst.BoundingRectangle
            print("[DBG] list rect:", lst_rect.left, lst_rect.top, lst_rect.right, lst_rect.bottom)
            images = []
            for ch in lst.GetChildren():
                try:
                    cn = ch.ClassName or ""
                    nm = ch.Name or ""
                    if cn == "mmui::ChatBubbleReferItemView" and nm == "图片":
                        r = ch.BoundingRectangle
                        cx = int((r.left + r.right) / 2)
                        cy = int((r.top + r.bottom) / 2)
                        in_lst = (lst_rect.left <= cx <= lst_rect.right and
                                  lst_rect.top <= cy <= lst_rect.bottom)
                        print("[DBG] img:", r.left, r.top, r.right, r.bottom, "in=", in_lst)
                        if in_lst:
                            images.append(ch)
                except Exception as e:
                    print("[DBG] img err:", e)
                    continue
            print("[DBG] images:", len(images))

            for img_ch in images:
                r = img_ch.BoundingRectangle
                # UIA 矩形是全宽列表项；实际图片缩略图在左侧。
                # 实测命中带约为 left+8.7%宽度 ~ left+15.8%宽度，中心≈12%。
                # 用相对偏移（而非固定像素），窗口宽度/DPI 变化时可自适应。
                cx = r.left + int((r.right - r.left) * 0.12)
                cy = int((r.top + r.bottom) / 2)
                print(f"[DBG] real_click image at ({cx},{cy})")

                inp.real_click(cx, cy)
                time.sleep(3.0)

                preview_win = None
                root = auto.GetRootControl()
                candidates = [w for w in root.GetChildren()
                              if "PreviewWindow" in (w.ClassName or "")]
                print(f"[DBG] after click, preview windows found: {len(candidates)}")
                # 优先选包含"图片原始大小"按钮的预览窗口
                for w in candidates:
                    if self._find_preview_button(w, "图片原始大小"):
                        preview_win = w
                        break
                if preview_win is None and candidates:
                    preview_win = max(candidates,
                                      key=lambda w: (w.BoundingRectangle.right - w.BoundingRectangle.left) *
                                                    (w.BoundingRectangle.bottom - w.BoundingRectangle.top))

                if not preview_win:
                    print("[DBG] NO preview window after click")
                    continue

                btn = self._find_preview_button(preview_win, "图片原始大小")
                print(f"[DBG] 图片原始大小 btn found: {btn is not None}")
                if btn:
                    # 按钮是完整 UIA 控件，用 UIA 原生 Click（不依赖全局 SetCursorPos 坐标映射）
                    try:
                        btn.Click()
                    except Exception:
                        btn_r = btn.BoundingRectangle
                        btn_cx = int((btn_r.left + btn_r.right) / 2)
                        btn_cy = int((btn_r.top + btn_r.bottom) / 2)
                        inp.real_click(btn_cx, btn_cy)
                    time.sleep(3.0)

                h_dat = self._find_h_dat(user, md5)
                print(f"[DBG] h_dat after click: {h_dat} size={os.path.getsize(h_dat) if h_dat else None}")
                if h_dat and os.path.getsize(h_dat) > 102400:
                    clicked = True
                    break
        except Exception as e:
            print("[DBG] outer err:", type(e).__name__, str(e)[:200])
            pass

        if not clicked:
            return None

        # 直接解密 _h.dat（原图）
        h_dat = self._find_h_dat(user, md5)
        if not h_dat:
            return None
        data = self.decrypt_image(h_dat, aes_key, xor_key)
        if data[:3] == b"\xff\xd8\xff":
            ext = "jpg"
        elif data[:4] == b"\x89PNG":
            ext = "png"
        elif data[:4] == b"wxgf":
            jpg = self._wxgf_to_jpg(data)
            if jpg is not None:
                out = self._out(save_dir, "%s_%s.jpg" % (user, local_id))
                with open(out, "wb") as f:
                    f.write(jpg)
                return out
            ext = "wxgf"
        else:
            ext = "img"
        out = self._out(save_dir, "%s_%s.%s" % (user, local_id, ext))
        with open(out, "wb") as f:
            f.write(data)
        return out

    def download_media(self, user: str, local_id: int, save_dir: Optional[str] = None) -> Optional[str]:
        """按消息类型自动分发：3 图片 / 34 语音 / 43 视频 / 49 文件"""
        row = self.db.get_message_row(user, local_id)
        if not row:
            return None
        t = row["local_type"]
        if t == 3:
            return self.download_image(user, local_id, save_dir)
        if t == 34:
            return self.download_voice(user, local_id, save_dir)
        if t == 43:
            return self.download_video(user, local_id, save_dir)
        if t == 49:
            return self.download_file(user, local_id, save_dir)
        return None
