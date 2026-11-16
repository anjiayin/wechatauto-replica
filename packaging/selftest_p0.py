# -*- coding: utf-8 -*-
"""P0 离线包自检探针（由 build_p0.py 复制到 bundle 根目录执行）。

只在本文件里做环境装配，业务代码一行不改：
  PATH 追加 runtime/（让 shutil.which("ffmpeg") 命中内置 exe）
  os.add_dll_directory（pywin32 / sherpa-onnx 的 DLL）
  KEFU_VERSION_FILE / KEFU_APP_ROOT（版本号、资源根）
  cwd 切到 kefu-system/（param.py 的 os.getcwd() 假设）

验证项：runtime、config 路径、ASR 模型就绪、真实中文语音转写、
内置 ffmpeg + HEVC 解码、前端 dist、FastAPI app 导入、SILK 解码器可执行。
刻意不启动 uvicorn —— lifespan 会拉起微信监听线程，P0 不碰微信。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent
RUNTIME = BUNDLE / "runtime"
SITE = RUNTIME / "site-packages"
KEFU = BUNDLE / "kefu-system"
TEST_WAV = BUNDLE / "testdata" / "test_zh.wav"

# ---- 启动器职责（P0 内嵌在探针里，P1 拆成独立 launcher） ----
os.environ["PATH"] = str(RUNTIME) + os.pathsep + os.environ.get("PATH", "")
os.environ.setdefault("QT_ACCESSIBILITY", "1")
os.environ.setdefault("KEFU_VERSION_FILE", str(BUNDLE / "version.txt"))
os.environ.setdefault("KEFU_APP_ROOT", str(KEFU))
for _d in (SITE / "pywin32_system32", SITE / "sherpa_onnx" / "lib",
           SITE / "winsdk", RUNTIME, RUNTIME / "python"):
    if _d.is_dir():
        try:
            os.add_dll_directory(str(_d))
        except OSError:
            pass
os.chdir(KEFU)

RESULTS: dict[str, dict] = {}
STARTED = time.monotonic()


def probe(name: str, fn):
    t = time.monotonic()
    try:
        value = fn()
        RESULTS[name] = {"ok": True, "ms": int((time.monotonic() - t) * 1000),
                         "value": value}
    except Exception as exc:                   # noqa: BLE001
        RESULTS[name] = {"ok": False, "ms": int((time.monotonic() - t) * 1000),
                         "error": f"{type(exc).__name__}: {exc}"}
    mark = "PASS" if RESULTS[name]["ok"] else "FAIL"
    print(f"[{mark}] {name:<18} {RESULTS[name]['ms']:>6} ms", flush=True)
    return RESULTS[name]


def p_runtime():
    import encodings  # noqa: F401
    return {"python": sys.version.split()[0], "exe": sys.executable,
            "prefix": sys.prefix, "bundle": str(BUNDLE),
            "pth_paths": [p for p in sys.path if "kefu" in p.lower() or "runtime" in p.lower()]}


def p_config():
    from app import config
    paths = {
        "BASE_DIR": config.BASE_DIR, "DATA_DIR": config.DATA_DIR,
        "ASR_MODEL_DIR": config.ASR_MODEL_DIR,
        "SILK_DECODER_PATH": config.SILK_DECODER_PATH,
        "FRONTEND_DIST": config.FRONTEND_DIST,
        "version_file": Path(os.environ["KEFU_VERSION_FILE"]),
    }
    return {"APP_VERSION": config.APP_VERSION,
            "paths": {k: str(v) for k, v in paths.items()},
            "exists": {k: Path(v).exists() for k, v in paths.items()}}


def p_frontend():
    from app import config
    index = config.FRONTEND_DIST / "index.html"
    assets = config.FRONTEND_DIST / "assets"
    return {"index_html": index.is_file(),
            "asset_files": len(list(assets.glob("*"))) if assets.is_dir() else 0}


def p_asr_ready():
    from app import voice_asr as va
    ok, reason = va.sherpa_ready()
    avail, avail_reason = va.VoiceTranscriber().available()
    return {"model_ready": va.model_ready(),
            "model_bytes": va.model_size_bytes(),
            "sherpa_ok": ok, "sherpa_reason": reason,
            "transcriber_available": avail, "transcriber_reason": avail_reason}


def p_asr_transcribe():
    """复用业务代码 VoiceTranscriber._recognize()，不改一行代码。"""
    from app import voice_asr as va
    if not TEST_WAV.is_file():
        raise RuntimeError(f"测试音频缺失：{TEST_WAV}")
    with wave.open(str(TEST_WAV), "rb") as w:
        meta = {"channels": w.getnchannels(), "rate": w.getframerate(),
                "width": w.getsampwidth(),
                "seconds": round(w.getnframes() / w.getframerate(), 2)}
        pcm = w.readframes(w.getnframes())
    vt = va.VoiceTranscriber()
    text = vt._recognize(pcm)
    return {"audio": meta, "pcm_bytes": len(pcm), "text": text,
            "timing": vt.last_timing()}


def p_ffmpeg():
    """生成一帧 HEVC 裸流，走业务代码 _hevc_to_jpg_ffmpeg() 解码。"""
    import tempfile
    from wechatauto.media import MediaDownloader
    exe = MediaDownloader._ffmpeg_exe()
    bundled = (RUNTIME / "ffmpeg.exe").resolve()
    if not exe:
        raise RuntimeError("_ffmpeg_exe() 返回 None")
    with tempfile.TemporaryDirectory(prefix="kefu_p0_") as td:
        hevc_path = Path(td) / "in.hevc"
        r = subprocess.run(
            [str(exe), "-y", "-v", "error", "-f", "lavfi",
             "-i", "testsrc=size=320x240:rate=1", "-frames:v", "1",
             "-c:v", "libx265", "-f", "hevc", str(hevc_path)],
            capture_output=True, timeout=60)
        if r.returncode != 0 or not hevc_path.is_file():
            raise RuntimeError(f"HEVC 生成失败 rc={r.returncode}: "
                               f"{r.stderr.decode('utf-8', 'replace')[:200]}")
        hevc = hevc_path.read_bytes()
        jpg = MediaDownloader._hevc_to_jpg_ffmpeg(hevc)
    return {"exe": str(exe), "is_bundled": Path(exe).resolve() == bundled,
            "hevc_bytes": len(hevc),
            "jpeg_bytes": len(jpg) if jpg else 0,
            "jpeg_magic": jpg[:3].hex() if jpg else None}


def p_silk():
    from app import config
    r = subprocess.run([str(config.SILK_DECODER_PATH)], capture_output=True, timeout=15)
    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    return {"returncode": r.returncode, "output_head": out[:160]}


def p_app_main():
    """只导入 ASGI app 对象，不启动 uvicorn / 不触发 lifespan（不碰微信）。"""
    from app.main import app
    return {"routes": len(app.routes), "title": app.title,
            "has_openapi": hasattr(app, "openapi")}


def main() -> None:
    print(f"=== P0 离线包自检  {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"runtime : {RUNTIME}\n", flush=True)

    probe("runtime", p_runtime)
    probe("config", p_config)
    probe("frontend", p_frontend)
    probe("asr_ready", p_asr_ready)
    probe("asr_transcribe", p_asr_transcribe)
    probe("ffmpeg_hevc", p_ffmpeg)
    probe("silk_decoder", p_silk)
    probe("app_main_import", p_app_main)

    # 关键判定
    checks = {
        "config 版本/路径": (RESULTS["config"]["ok"]
                          and RESULTS["config"]["value"]["APP_VERSION"] == "1.2.1"
                          and all(RESULTS["config"]["value"]["exists"].values())),
        "前端 dist": RESULTS["frontend"]["ok"] and RESULTS["frontend"]["value"]["index_html"],
        "ASR 模型就绪": (RESULTS["asr_ready"]["ok"]
                     and RESULTS["asr_ready"]["value"]["model_ready"]
                     and RESULTS["asr_ready"]["value"]["transcriber_available"]),
        "中文转写有文本": (RESULTS["asr_transcribe"]["ok"]
                     and bool(RESULTS["asr_transcribe"]["value"]["text"].strip())),
        "内置 ffmpeg 解码": (RESULTS["ffmpeg_hevc"]["ok"]
                        and RESULTS["ffmpeg_hevc"]["value"]["is_bundled"]
                        and RESULTS["ffmpeg_hevc"]["value"]["jpeg_magic"] == "ffd8ff"),
        "SILK 解码器可执行": RESULTS["silk_decoder"]["ok"],
        "FastAPI app 可导入": (RESULTS["app_main_import"]["ok"]
                          and RESULTS["app_main_import"]["value"]["routes"] > 0),
    }
    report = {
        "elapsed_ms": int((time.monotonic() - STARTED) * 1000),
        "checks": checks,
        "all_pass": all(checks.values()),
        "probes": RESULTS,
    }
    (BUNDLE / "selftest-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 关键检查 ===")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n总耗时 {report['elapsed_ms']} ms；结论："
          f"{'全部通过' if report['all_pass'] else '存在失败项'}")
    print(f"报告：{BUNDLE / 'selftest-report.json'}")

    # 导入 app.main 可能留下非 daemon 线程，强制退出避免挂住
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if report["all_pass"] else 1)


if __name__ == "__main__":
    main()
