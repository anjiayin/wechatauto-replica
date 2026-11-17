# -*- coding: utf-8 -*-
"""P1 三层布局的守门用例：数据根 / 重资源根与环境解耦，且 P0 布局仍零改动兼容。

config.py 在 import 期根据环境变量固化路径，故用子进程隔离不同环境组合，
避免 sys.modules 缓存串味。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

KEFU_ROOT = Path(__file__).resolve().parents[1]


def _probe(env_extra: dict[str, str]) -> dict:
    """在干净子进程里 import config，打印关键路径的 JSON。"""
    code = (
        "import json\n"
        "from app import config as c\n"
        "print('__JSON__' + json.dumps({\n"
        "  'APP_ROOT': str(c.APP_ROOT),\n"
        "  'BASE_DIR': str(c.BASE_DIR),\n"
        "  'DATA_DIR': str(c.DATA_DIR),\n"
        "  'ASSETS_DIR': str(c.ASSETS_DIR) if c.ASSETS_DIR else None,\n"
        "  'ASR_MODEL_DIR': str(c.ASR_MODEL_DIR),\n"
        "  'SILK_DECODER_PATH': str(c.SILK_DECODER_PATH),\n"
        "  'FRONTEND_DIST': str(c.FRONTEND_DIST),\n"
        "  'DB_PATH': str(c.DB_PATH),\n"
        "}))\n"
    )
    env = os.environ.copy()
    for key in ("KEFU_APP_ROOT", "KEFU_DATA_DIR", "KEFU_ASSETS_DIR"):
        env.pop(key, None)
    env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(KEFU_ROOT), env=env,
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = next(x for x in proc.stdout.splitlines() if x.startswith("__JSON__"))
    return json.loads(line[len("__JSON__"):])


def test_p0_layout_fallback_without_env():
    """不设环境变量：所有路径回退到 APP_ROOT 下，现有 P0 bundle 零改动可跑。"""
    p = _probe({})
    assert Path(p["APP_ROOT"]) == KEFU_ROOT
    assert Path(p["BASE_DIR"]) == KEFU_ROOT
    assert Path(p["DATA_DIR"]) == KEFU_ROOT / "data"
    assert p["ASSETS_DIR"] is None
    assert Path(p["ASR_MODEL_DIR"]) == KEFU_ROOT / "data" / "asr_models"
    assert Path(p["SILK_DECODER_PATH"]) == KEFU_ROOT / "tools" / "silk_v3_decoder.exe"
    assert Path(p["DB_PATH"]) == KEFU_ROOT / "data" / "kefu.sqlite3"


def test_p1_three_layer_layout():
    """P1：data 与 assets 均被环境变量顶到版本层之外，升级互不覆盖。"""
    root = Path("D:/fake-install")
    app = root / "app" / "1.2.2" / "kefu-system"
    data = root / "data"
    assets = root / "runtime"
    p = _probe({
        "KEFU_APP_ROOT": str(app),
        "KEFU_DATA_DIR": str(data),
        "KEFU_ASSETS_DIR": str(assets),
    })
    assert Path(p["APP_ROOT"]) == app
    assert Path(p["DATA_DIR"]) == data
    assert Path(p["DB_PATH"]) == data / "kefu.sqlite3"
    assert Path(p["ASR_MODEL_DIR"]) == assets / "asr_models"
    assert Path(p["SILK_DECODER_PATH"]) == assets / "silk_v3_decoder.exe"
    # 前端仍在版本层：随版本升级更新
    assert Path(p["FRONTEND_DIST"]) == app / "frontend" / "dist"


def test_data_dir_independent_of_app_root():
    """只切版本（app root），data 不变：数据库不随版本漂移。"""
    a = _probe({"KEFU_APP_ROOT": "D:/install/app/1.2.1/kefu-system",
                "KEFU_DATA_DIR": "D:/install/data"})
    b = _probe({"KEFU_APP_ROOT": "D:/install/app/1.2.2/kefu-system",
                "KEFU_DATA_DIR": "D:/install/data"})
    assert a["DATA_DIR"] == b["DATA_DIR"]
    assert a["DB_PATH"] == b["DB_PATH"]
    assert a["APP_ROOT"] != b["APP_ROOT"]
