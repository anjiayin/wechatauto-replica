# -*- coding: utf-8 -*-
"""语音消息离线转写：微信 SILK → 裸 PCM → SenseVoice-Small 文本。

设计约束（用户口径：完全本地、离线、免费，**音频绝不上传外部服务**）：
- 取音频：``wechatauto.media.MediaDownloader.download_voice`` 落盘 ``.silk``；
- 解码：仓库内置 ``tools/silk_v3_decoder.exe``（32 位程序，stdout 会刷大量
  逐包进度文本，必须 subprocess 捕获输出 + 超时保护）；
- 识别：``sherpa-onnx`` + SenseVoice-Small（``data/asr_models/model.int8.onnx``）。

关键实现要点（实测结论，勿照抄网上旧示例）：
- sherpa-onnx 2.0.1 只有 ``accept_waveform``，没有 ``read_wave`` /
  ``accept_wave_file``；
- 解码产物是**裸 PCM**（16kHz/16bit/单声道），没有 wav 头，直接
  ``np.frombuffer(..., dtype="<i2")`` 归一化后喂给识别器；
- 识别器加载模型约 2.5s，因此懒加载 + 进程级单例常驻（首次识别时才加载，
  **模块 import 时不加载模型**，否则测试与启动都会被 228MB 拖慢）；
- 识别是 CPU 密集的，用 ``threading.Lock`` 串行化，同一时刻只跑一条。
"""
from __future__ import annotations

import importlib
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from app.config import ASR_MODEL_DIR, SILK_DECODER_PATH

logger = logging.getLogger(__name__)

# 前端展示与 /api/system/asr 共用的引擎标识
ENGINE_NAME = "SenseVoice-Small（离线）"
# 识别采样率（SILK 解码固定 16kHz；SenseVoice 也只接受 16kHz）
SAMPLE_RATE = 16000
# 解码子进程超时（秒）：正常 20s 语音不到 1s，超时即视为异常
DECODE_TIMEOUT_SEC = 60
# 单条语音转写时长上限（秒）默认值（settings: asr.max_seconds 可覆盖）
MAX_SECONDS_DEFAULT = 180

MODEL_FILE_NAME = "model.int8.onnx"
TOKENS_FILE_NAME = "tokens.txt"

# 模型路径（模块级变量，测试可整体替换）
MODEL_PATH = ASR_MODEL_DIR / MODEL_FILE_NAME
TOKENS_PATH = ASR_MODEL_DIR / TOKENS_FILE_NAME
DECODER_PATH = SILK_DECODER_PATH

# 下载源（已实测：hf-mirror 镜像 15 秒左右下完 228MB）
MODEL_MIRROR_BASE = (
    "https://hf-mirror.com/csukuangfj/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main"
)
DECODER_DOWNLOAD_URL = (
    "https://ghproxy.net/https://raw.githubusercontent.com/"
    "kn007/silk-v3-decoder/master/windows/silk_v3_decoder.exe"
)

# 下载状态（模块级，供 /api/system/asr 查询）
STATUS_IDLE = "idle"
STATUS_DOWNLOADING = "downloading"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

_DOWNLOAD_LOCK = threading.Lock()
_download_thread: Optional[threading.Thread] = None
_download_state: dict = {
    "status": STATUS_IDLE,
    "downloaded_bytes": 0,
    "total_bytes": 0,
    "error": "",
}

# 下载分块大小（1MB：兼顾进度粒度与系统调用次数）
_CHUNK_SIZE = 1024 * 1024
# 单次网络读超时（秒）
_HTTP_TIMEOUT_SEC = 60


class _AsrError(Exception):
    """内部错误：消息本身就是给用户看的中文原因（不外抛给调用方）。"""


# ---------------------------------------------------------------------------
# 环境检测
# ---------------------------------------------------------------------------
def sherpa_ready() -> tuple[bool, str]:
    """sherpa-onnx 是否可导入；返回 (可用, 中文原因)。"""
    try:
        importlib.import_module("sherpa_onnx")
    except Exception as exc:  # 未安装 / 缺 DLL / 位数不匹配等
        return False, ("未安装语音识别库 sherpa-onnx（%s），"
                       "请执行：pip install sherpa-onnx" % exc)
    try:
        importlib.import_module("numpy")
    except Exception as exc:
        return False, "未安装 numpy（%s），请执行：pip install numpy" % exc
    return True, ""


def model_ready() -> bool:
    """模型两个文件是否都已就位（缺一不可）。"""
    return MODEL_PATH.is_file() and TOKENS_PATH.is_file()


def model_size_bytes() -> int:
    """模型占用字节数（两个文件之和；缺失按 0 计）。"""
    total = 0
    for path in (MODEL_PATH, TOKENS_PATH):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:  # 文件被占用/权限异常：按 0 计，不影响状态查询
            continue
    return total


def decoder_ready() -> bool:
    """SILK 解码器是否就位。"""
    return DECODER_PATH.is_file()


# ---------------------------------------------------------------------------
# 模型 / 解码器下载
# ---------------------------------------------------------------------------
def download_state() -> dict:
    """当前下载状态快照（拷贝，避免调用方读到半更新字段）。"""
    with _DOWNLOAD_LOCK:
        return dict(_download_state)


def reset_download_state() -> None:
    """把下载状态复位为 idle（失败后重试 / 测试用；不影响已下载文件）。"""
    with _DOWNLOAD_LOCK:
        _download_state.update(
            status=STATUS_IDLE, downloaded_bytes=0, total_bytes=0, error="")


def _update_state(**fields) -> None:
    """原子更新下载状态字段。"""
    with _DOWNLOAD_LOCK:
        _download_state.update(fields)


def _add_progress(downloaded: int = 0, total: int = 0) -> None:
    """累加下载进度（total 由各文件 Content-Length 逐个得知）。"""
    with _DOWNLOAD_LOCK:
        if total:
            _download_state["total_bytes"] = (
                int(_download_state.get("total_bytes") or 0) + int(total))
        if downloaded:
            _download_state["downloaded_bytes"] = (
                int(_download_state.get("downloaded_bytes") or 0)
                + int(downloaded))


def _download_file(url: str, target: Path,
                   progress_cb: Optional[Callable[[int, int], None]] = None
                   ) -> tuple[bool, str]:
    """下载单个文件到 target（先写 .part 再改名，避免半截文件被当成可用）。

    Args:
        url: 下载地址。
        target: 目标文件路径。
        progress_cb: 可选进度回调 (已下载字节, 总字节)，总字节未知时为 0。

    Returns:
        (是否成功, 中文失败原因)。
    """
    part = target.with_name(target.name + ".part")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"无法创建模型目录：{exc}"
    request = urllib.request.Request(
        url, headers={"User-Agent": "kefu-system/1.0 (local asr downloader)"})
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SEC) as resp:
            try:
                length = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            _add_progress(total=length)
            with open(part, "wb") as fh:
                while True:
                    block = resp.read(_CHUNK_SIZE)
                    if not block:
                        break
                    fh.write(block)
                    _add_progress(downloaded=len(block))
                    if progress_cb is not None:
                        try:
                            progress_cb(len(block), length)
                        except Exception:  # 进度回调异常不得中断下载
                            logger.debug("下载进度回调异常（已忽略）", exc_info=True)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _discard_file(part)
        return False, f"下载失败：{exc}"
    try:
        if part.stat().st_size <= 0:
            _discard_file(part)
            return False, "下载失败：文件为空"
        os.replace(part, target)
    except OSError as exc:
        _discard_file(part)
        return False, f"保存模型文件失败：{exc}"
    return True, ""


def _discard_file(path: Path) -> None:
    """尽力删除半截文件（失败忽略）。"""
    try:
        if path.exists():
            path.unlink()
    except OSError:
        logger.debug("删除半截文件失败：%s", path, exc_info=True)


def ensure_model(progress_cb: Optional[Callable[[int, int], None]] = None
                 ) -> tuple[bool, str]:
    """确保模型文件就位：缺失时从镜像下载，返回 (是否可用, 中文原因)。

    阻塞调用（下载 228MB 约 15 秒~数分钟），API 层应放在后台线程执行
    （见 :func:`start_model_download`）。先写 ``.part`` 再 ``os.replace``
    改名，避免下载中断后的半截文件被当成可用模型加载（会直接崩）。
    """
    if model_ready():
        return True, ""
    failures: list[str] = []
    # 目标路径统一取模块级常量（与 model_ready() 判定完全一致，避免漂移）
    for name, target in ((MODEL_FILE_NAME, MODEL_PATH),
                         (TOKENS_FILE_NAME, TOKENS_PATH)):
        if target.is_file():
            continue
        ok, err = _download_file(f"{MODEL_MIRROR_BASE}/{name}", target,
                                 progress_cb)
        if not ok:
            failures.append(f"{name}：{err}")
    if model_ready():
        return True, ""
    return False, "模型下载失败（" + "；".join(failures or ["文件缺失"]) + "）"


def download_decoder(progress_cb: Optional[Callable[[int, int], None]] = None
                     ) -> tuple[bool, str]:
    """确保 SILK 解码器就位：缺失时从镜像下载，返回 (是否可用, 中文原因)。"""
    if decoder_ready():
        return True, ""
    return _download_file(DECODER_DOWNLOAD_URL, Path(DECODER_PATH), progress_cb)


def _download_worker() -> None:
    """后台下载线程主体：模型（缺失时顺带补解码器），全程不抛异常。"""
    try:
        decoded_ok, decoded_err = download_decoder()
        if not decoded_ok:
            logger.warning("SILK 解码器下载失败（模型继续下载）：%s", decoded_err)
        ok, err = ensure_model()
    except Exception as exc:  # 防御：后台线程异常会静默，必须显式记录
        logger.exception("语音模型下载线程异常")
        _update_state(status=STATUS_FAILED, error=f"下载异常：{exc}")
        return
    if ok:
        size = model_size_bytes()
        logger.info("语音识别模型已就位：%s（%.1f MB）", ASR_MODEL_DIR,
                    size / 1024 / 1024)
        with _DOWNLOAD_LOCK:
            total = int(_download_state.get("total_bytes") or 0)
            _download_state.update(status=STATUS_READY, error="",
                                   downloaded_bytes=max(size, 0),
                                   total_bytes=total or size)
    else:
        logger.warning("语音识别模型下载失败：%s", err)
        _update_state(status=STATUS_FAILED, error=err)


def start_model_download() -> dict:
    """触发后台下载模型（幂等：正在下载时直接返回当前进度）。

    Returns:
        下载状态快照（status/downloaded_bytes/total_bytes/error）。
    """
    global _download_thread
    with _DOWNLOAD_LOCK:
        running = (_download_thread is not None and _download_thread.is_alive())
        if running and _download_state.get("status") == STATUS_DOWNLOADING:
            return dict(_download_state)
        if model_ready():
            size = model_size_bytes()
            _download_state.update(status=STATUS_READY, error="",
                                   downloaded_bytes=size, total_bytes=size)
            return dict(_download_state)
        # 新一轮下载：计数器归零（失败重试也走这里）
        _download_state.update(status=STATUS_DOWNLOADING,
                               downloaded_bytes=0, total_bytes=0, error="")
        _download_thread = threading.Thread(
            target=_download_worker, name="kefu-asr-download", daemon=True)
        _download_thread.start()
        return dict(_download_state)


# ---------------------------------------------------------------------------
# 转写器（进程级单例）
# ---------------------------------------------------------------------------
class VoiceTranscriber:
    """SILK 语音 → 文本（懒加载 SenseVoice，同一时刻只跑一条识别）。

    Args:
        model_path: SenseVoice 模型文件（默认 data/asr_models/model.int8.onnx）。
        tokens_path: 模型词表（默认 data/asr_models/tokens.txt）。
        decoder_path: SILK 解码器 exe（默认 tools/silk_v3_decoder.exe）。
        max_seconds_provider: 每次转写时实时读取「单条语音最长秒数」
            （settings 热更，默认 180 秒）。
    """

    def __init__(self, model_path: Optional[Path] = None,
                 tokens_path: Optional[Path] = None,
                 decoder_path: Optional[Path] = None,
                 max_seconds_provider: Optional[Callable[[], int]] = None
                 ) -> None:
        self._model_path = Path(model_path or MODEL_PATH)
        self._tokens_path = Path(tokens_path or TOKENS_PATH)
        self._decoder_path = Path(decoder_path or DECODER_PATH)
        # 模型加载 + 识别共用的串行锁（RLock：available() 可能在持锁路径里被调）
        self._lock = threading.RLock()
        self._recognizer = None
        self._max_seconds_provider = max_seconds_provider or (
            lambda: MAX_SECONDS_DEFAULT)
        # 本条（本线程）最近一次转写的分段耗时：线程局部，避免并发调用时
        # 互相覆盖（调用方 transcribe 后立刻读，得到的一定是自己那条的）。
        self._timing_local = threading.local()

    # ------------------------------------------------------------------
    # 能力检测
    # ------------------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        """是否可用；返回 (可用, 不可用原因)。原因文案直接给用户看。"""
        ok, reason = sherpa_ready()
        if not ok:
            return False, reason
        if not self._model_path.is_file() or not self._tokens_path.is_file():
            return False, ("语音识别模型未安装（%s），可在设置页"
                           "「数据与账单」下载约 228MB 模型"
                           % self._model_path.parent)
        if not self._decoder_path.is_file():
            return False, "缺少 SILK 解码器 tools/silk_v3_decoder.exe，无法解码语音"
        return True, ""

    def max_seconds(self) -> int:
        """当前生效的单条语音时长上限（秒，≥1；读取失败回落默认值）。"""
        try:
            value = int(self._max_seconds_provider() or MAX_SECONDS_DEFAULT)
        except Exception:  # 配置读取失败不影响转写能力
            return MAX_SECONDS_DEFAULT
        return max(1, value)

    def set_max_seconds_provider(
            self, provider: Optional[Callable[[], int]]) -> None:
        """注入「单条语音最长秒数」读取函数（settings 热更；None 恢复默认）。"""
        self._max_seconds_provider = provider or (lambda: MAX_SECONDS_DEFAULT)

    # ------------------------------------------------------------------
    # 转写主流程
    # ------------------------------------------------------------------
    def transcribe(self, silk_path: str) -> tuple[str, str]:
        """把 .silk 文件转成文字；返回 (文本, 失败原因)。

        成功：``(文本, "")``；失败：``("", 中文原因)``。
        **任何异常都在内部捕获并转成中文原因，绝不抛给调用方**
        （调用方是监听回调与分析管线，抛异常会污染主流程）。

        分段耗时通过 :meth:`last_timing` 暴露给调用方：``decode_ms``（SILK
        解码）与 ``recognize_ms``（decode_stream 本身），**不含**首次模型
        加载（``model_load_ms`` 单列，只进日志）。
        """
        self._timing_local.timing = {}
        ok, reason = self.available()
        if not ok:
            return "", reason
        path = Path(str(silk_path or ""))
        if not path.is_file():
            return "", "语音文件不存在或已被清理"
        try:
            decode_started = time.monotonic()
            pcm = self._decode_silk(path)
            self._set_timing(decode_ms=int(
                (time.monotonic() - decode_started) * 1000))
        except _AsrError as exc:
            return "", str(exc)
        except Exception as exc:  # 防御：解码链路的意外异常
            logger.exception("语音解码异常：%s", path)
            return "", f"语音解码失败：{exc}"
        duration_sec = len(pcm) / 2.0 / SAMPLE_RATE
        limit = self.max_seconds()
        if duration_sec > limit:
            return "", (f"语音过长（约 {duration_sec:.0f} 秒，超过上限 {limit} 秒），"
                        "已跳过转写")
        if duration_sec <= 0.05:
            return "", "语音内容为空或解码结果异常"
        try:
            text = self._recognize(pcm)
        except _AsrError as exc:
            return "", str(exc)
        except Exception as exc:
            logger.exception("语音识别异常：%s", path)
            return "", f"语音识别失败：{exc}"
        if not text:
            return "", "未识别到语音内容（可能是静音或环境噪声）"
        return text, ""

    # ------------------------------------------------------------------
    # 分段耗时（调用方统计 duration_ms 用；不含模型加载）
    # ------------------------------------------------------------------
    def _set_timing(self, **fields) -> None:
        """更新本线程当前这条语音的分段耗时（毫秒）。"""
        timing = getattr(self._timing_local, "timing", None)
        if timing is None:
            timing = {}
            self._timing_local.timing = timing
        timing.update(fields)

    def last_timing(self) -> dict:
        """本线程最近一次 transcribe 的分段耗时（毫秒）。

        Returns:
            {decode_ms, recognize_ms, model_load_ms}；未产生耗时时为 ``{}``。
            其中 ``model_load_ms`` 是首次加载 SenseVoice 模型的时间（最长
            可达十几秒），**不应**计入单条语音的识别耗时统计。
        """
        timing = getattr(self._timing_local, "timing", None)
        return dict(timing) if timing else {}

    # ------------------------------------------------------------------
    # 内部：解码 / 识别
    # ------------------------------------------------------------------
    def _decode_silk(self, silk_path: Path) -> bytes:
        """调用解码器把 .silk 转成裸 PCM（16kHz/16bit/单声道）。

        Raises:
            _AsrError: 超时 / 解码器无法执行 / 无输出，消息为中文原因。
        """
        with tempfile.TemporaryDirectory(prefix="kefu_asr_") as tmpdir:
            out_path = Path(tmpdir) / "out.pcm"
            cmd = [str(self._decoder_path), str(silk_path), str(out_path),
                   "-Fs_API", str(SAMPLE_RATE)]
            try:
                proc = subprocess.run(cmd, capture_output=True,
                                      timeout=DECODE_TIMEOUT_SEC)
            except subprocess.TimeoutExpired as exc:
                raise _AsrError(
                    f"语音解码超时（超过 {DECODE_TIMEOUT_SEC} 秒）") from exc
            except OSError as exc:
                raise _AsrError(f"无法执行 SILK 解码器：{exc}") from exc
            if not out_path.is_file():
                raise _AsrError(self._decode_hint(proc))
            try:
                data = out_path.read_bytes()
            except OSError as exc:
                raise _AsrError(f"读取解码结果失败：{exc}") from exc
            if len(data) < 2:
                raise _AsrError(self._decode_hint(proc))
            return data

    @staticmethod
    def _decode_hint(proc: subprocess.CompletedProcess) -> str:
        """解码失败的用户可读原因（解码器 stdout 是逐包进度，只取 stderr 摘要）。"""
        raw = (proc.stderr or b"").decode("utf-8", "replace").strip()
        if not raw and proc.stdout:
            raw = proc.stdout.decode("utf-8", "replace").strip()[-200:]
        raw = " ".join(raw.split())[:160]
        code = getattr(proc, "returncode", "")
        if raw:
            return f"语音解码失败（返回码 {code}）：{raw}"
        return f"语音解码失败（返回码 {code}），可能是加密语音或文件损坏"

    def _get_recognizer(self):
        """懒加载并缓存 OfflineRecognizer（首次约 2.5s，之后复用）。"""
        if self._recognizer is not None:
            return self._recognizer
        try:
            import sherpa_onnx
        except Exception as exc:  # 已在 available() 里检查过，双保险
            raise _AsrError(f"未安装语音识别库 sherpa-onnx：{exc}") from exc
        try:
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(self._model_path),
                tokens=str(self._tokens_path),
                num_threads=2,
                use_itn=True,
                language="zh",
                debug=False,
            )
        except Exception as exc:
            logger.exception("加载 SenseVoice 模型失败：%s", self._model_path)
            raise _AsrError(f"加载语音识别模型失败：{exc}") from exc
        logger.info("SenseVoice 语音识别模型已加载：%s", self._model_path)
        return self._recognizer

    def _recognize(self, pcm: bytes) -> str:
        """裸 PCM → 文本（持锁串行，避免多 worker 同时抢占 CPU）。

        分段计时：``model_load_ms`` 单列（首次加载可达十几秒，不计入识别
        耗时），``recognize_ms`` 只算 ``decode_stream`` 本身。
        """
        import numpy as np  # 延迟导入：numpy 缺失时 available() 已拦下

        samples = (np.frombuffer(pcm, dtype="<i2")
                   .astype(np.float32) / 32768.0)
        with self._lock:
            load_started = time.monotonic()
            recognizer = self._get_recognizer()
            load_ms = int((time.monotonic() - load_started) * 1000)
            stream = recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, samples)
            recognize_started = time.monotonic()
            recognizer.decode_stream(stream)
            recognize_ms = int((time.monotonic() - recognize_started) * 1000)
            text = _extract_text(stream)
        self._set_timing(model_load_ms=load_ms, recognize_ms=recognize_ms)
        if load_ms >= 100:
            # 首条语音会把模型加载时间算进来：单独打一行，避免用户误以为
            # 识别本身要十几秒（实测加载 2.5~11s，识别仅 0.4~1.5s）。
            logger.info("语音识别模型加载 %.2fs（仅首次，不计入识别耗时）",
                        load_ms / 1000.0)
        return _clean_text(text)

    def unload(self) -> None:
        """释放模型（测试/热更新用；下次识别会重新懒加载）。"""
        with self._lock:
            self._recognizer = None


def _extract_text(stream) -> str:
    """从识别流取文本（兼容 result.text / result 为字符串两种形态）。"""
    result = getattr(stream, "result", None)
    text = getattr(result, "text", None)
    if text is None and isinstance(result, str):
        text = result
    return str(text or "")


# SenseVoice 输出可能带 <|zh|><|NEUTRAL|><|Speech|> 这类标签，展示前剥掉
_LABEL_RE = re.compile(r"<\|[^|]*\|>")


def _clean_text(text: str) -> str:
    """清掉模型标签并压缩空白（保留中文标点）。"""
    cleaned = _LABEL_RE.sub("", str(text or ""))
    return " ".join(cleaned.split()).strip()


_transcriber: Optional[VoiceTranscriber] = None
_transcriber_lock = threading.Lock()


def get_transcriber() -> VoiceTranscriber:
    """进程级单例（首次调用只创建对象，不加载模型）。"""
    global _transcriber
    with _transcriber_lock:
        if _transcriber is None:
            _transcriber = VoiceTranscriber()
        return _transcriber


def reset_transcriber() -> None:
    """丢弃单例（测试/切换模型用）。"""
    global _transcriber
    with _transcriber_lock:
        if _transcriber is not None:
            _transcriber.unload()
        _transcriber = None
