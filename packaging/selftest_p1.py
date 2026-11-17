# -*- coding: utf-8 -*-
"""P1 交付包自检探针（由 build_p1.py 编译成 selftest_p1.pyc 放进 bundle 根）。

在 P0 九项基础上增加 P1 专属检查：
  - 三层布局：app/current junction 指向正确版本、data/ 存在且可写、runtime 资源齐全
  - 全量 pyc：整个 bundle 无人工可读 .py（selftest 自身已编成 pyc）
  - 数据分离：注入 KEFU_DATA_DIR / KEFU_ASSETS_DIR 后 config 指向 install 根
刻意不启动 uvicorn lifespan（会拉起微信监听线程），P1 不碰微信。
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
APP_CURRENT = BUNDLE / "app" / "current"
DATA = BUNDLE / "data"
TEST_WAV = BUNDLE / "testdata" / "test_zh.wav"
CURRENT_JSON = BUNDLE / "current.json"

_VERSION = json.loads(CURRENT_JSON.read_text(encoding="utf-8"))["version"]
KEFU = APP_CURRENT / "kefu-system"

# ---- 启动器职责（P1 由 launcher.pyc 承担，这里复刻以便自检） ----
os.environ["PATH"] = str(RUNTIME) + os.pathsep + os.environ.get("PATH", "")
os.environ.setdefault("QT_ACCESSIBILITY", "1")
os.environ.setdefault("KEFU_VERSION_FILE", str(KEFU / "version.txt"))
os.environ.setdefault("KEFU_APP_ROOT", str(KEFU))
os.environ.setdefault("KEFU_DATA_DIR", str(DATA))
os.environ.setdefault("KEFU_ASSETS_DIR", str(RUNTIME))
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


def probe(name, fn):
    t = time.monotonic()
    try:
        value = fn()
        RESULTS[name] = {"ok": True, "ms": int((time.monotonic() - t) * 1000), "value": value}
    except Exception as exc:                   # noqa: BLE001
        RESULTS[name] = {"ok": False, "ms": int((time.monotonic() - t) * 1000),
                         "error": f"{type(exc).__name__}: {exc}"}
    mark = "PASS" if RESULTS[name]["ok"] else "FAIL"
    print(f"[{mark}] {name:<20} {RESULTS[name]['ms']:>6} ms", flush=True)


def p_layout():
    pyfiles = [str(p.relative_to(BUNDLE)) for p in BUNDLE.rglob("*.py")]
    real_ver = (APP_CURRENT / "kefu-system" / "version.txt")
    return {
        "version": _VERSION,
        "junction_target": os.readlink(APP_CURRENT) if APP_CURRENT.is_symlink() else "(junction)",
        "app_version_txt": real_ver.is_file(),
        "data_writable": (lambda: (DATA / ".selftest_probe").write_text("ok", encoding="ascii")
                          or (DATA / ".selftest_probe").unlink() is None)() if DATA.is_dir() else False,
        "runtime_assets": all((RUNTIME / n).exists() for n in
                              ("asr_models/model.int8.onnx", "asr_models/tokens.txt",
                               "silk_v3_decoder.exe", "ffmpeg.exe")),
        "source_py_files": pyfiles[:10],
        "source_py_count": len(pyfiles),
    }


def p_config():
    from app import config
    return {
        "APP_VERSION": config.APP_VERSION,
        "DATA_DIR": str(config.DATA_DIR),
        "ASR_MODEL_DIR": str(config.ASR_MODEL_DIR),
        "SILK_DECODER_PATH": str(config.SILK_DECODER_PATH),
        "FRONTEND_DIST": str(config.FRONTEND_DIST),
        "assets_exist": {k: Path(v).exists() for k, v in {
            "model": config.ASR_MODEL_DIR / "model.int8.onnx",
            "silk": config.SILK_DECODER_PATH,
            "frontend": config.FRONTEND_DIST / "index.html",
        }.items()},
    }


def p_asr():
    from app import voice_asr as va
    if not TEST_WAV.is_file():
        raise RuntimeError(f"测试音频缺失：{TEST_WAV}")
    with wave.open(str(TEST_WAV), "rb") as w:
        pcm = w.readframes(w.getnframes())
        meta = {"rate": w.getframerate(), "seconds": round(w.getnframes()/w.getframerate(), 2)}
    vt = va.VoiceTranscriber()
    text = vt._recognize(pcm)
    return {"audio": meta, "text": text, "timing": vt.last_timing()}


def p_ffmpeg():
    import tempfile
    from wechatauto.media import MediaDownloader
    exe = MediaDownloader._ffmpeg_exe()
    bundled = (RUNTIME / "ffmpeg.exe").resolve()
    with tempfile.TemporaryDirectory(prefix="kefu_p1_") as td:
        hevc = Path(td) / "in.hevc"
        r = subprocess.run([str(exe), "-y", "-v", "error", "-f", "lavfi",
                            "-i", "testsrc=size=320x240:rate=1", "-frames:v", "1",
                            "-c:v", "libx265", "-f", "hevc", str(hevc)],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"HEVC 生成失败：{r.stderr.decode('utf-8', 'replace')[:160]}")
        jpg = MediaDownloader._hevc_to_jpg_ffmpeg(hevc.read_bytes())
    return {"is_bundled": Path(exe).resolve() == bundled,
            "jpeg_magic": jpg[:3].hex() if jpg else None}


def p_imports():
    from app.main import app
    return {"routes": len(app.routes), "title": app.title}


def p_native():
    """零注入子进程：不注入 DLL 路径、不设 ASSETS_DIR以外的东西，验证原生 import。"""
    code = (
        "import os,sys;"
        f"os.environ['KEFU_APP_ROOT']=r'{KEFU}';"
        f"os.environ['KEFU_DATA_DIR']=r'{DATA}';"
        f"os.environ['KEFU_ASSETS_DIR']=r'{RUNTIME}';"
        f"os.environ['KEFU_VERSION_FILE']=r'{KEFU / 'version.txt'}';"
        f"os.chdir(r'{KEFU}');"
        "from wechatauto.media import MediaDownloader;"
        "from app import voice_asr;"
        "print('NATIVE_OK')"
    )
    r = subprocess.run([str(RUNTIME / "python" / "python.exe"), "-c", code],
                       capture_output=True, timeout=90, cwd=str(BUNDLE))
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    if r.returncode != 0 or "NATIVE_OK" not in out:
        raise RuntimeError(f"原生 import 失败 rc={r.returncode}: {out[-300:]}")
    return {"native_ok": True}


def main() -> None:
    print(f"=== P1 交付包自检  {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"bundle : {BUNDLE}\n", flush=True)
    probe("layout_3tier", p_layout)
    probe("config_paths", p_config)
    probe("asr_transcribe", p_asr)
    probe("ffmpeg_hevc", p_ffmpeg)
    probe("app_import", p_imports)
    probe("native_no_inject", p_native)

    lay = RESULTS["layout_3tier"]["value"]
    cfg = RESULTS["config_paths"]["value"]
    checks = {
        "三层布局齐全": RESULTS["layout_3tier"]["ok"] and lay["app_version_txt"]
                     and lay["data_writable"] and lay["runtime_assets"],
        "无人工可读 .py": RESULTS["layout_3tier"]["ok"] and lay["source_py_count"] == 0,
        "config 数据分离": (RESULTS["config_paths"]["ok"]
                       and cfg["APP_VERSION"] == _VERSION
                       and cfg["DATA_DIR"] == str(DATA)
                       and cfg["ASR_MODEL_DIR"] == str(RUNTIME / "asr_models")
                       and all(cfg["assets_exist"].values())),
        "中文转写有文本": (RESULTS["asr_transcribe"]["ok"]
                      and bool(RESULTS["asr_transcribe"]["value"]["text"].strip())),
        "内置 ffmpeg 解码": (RESULTS["ffmpeg_hevc"]["ok"]
                         and RESULTS["ffmpeg_hevc"]["value"]["is_bundled"]
                         and RESULTS["ffmpeg_hevc"]["value"]["jpeg_magic"] == "ffd8ff"),
        "FastAPI 可导入": RESULTS["app_import"]["ok"] and RESULTS["app_import"]["value"]["routes"] > 0,
        "原生启动免注入": RESULTS["native_no_inject"]["ok"],
    }
    report = {
        "elapsed_ms": int((time.monotonic() - STARTED) * 1000),
        "version": _VERSION,
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
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if report["all_pass"] else 1)


if __name__ == "__main__":
    main()
