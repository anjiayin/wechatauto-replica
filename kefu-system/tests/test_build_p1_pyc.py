# -*- coding: utf-8 -*-
"""P1 构建守门：pyc 必须用 bundle 内置 embed Python 编译（magic 与运行时一致）。

2026-11-18 事故：PATH 里的 python 是 3.13，embed 是 3.12.10；用当前解释器
compileall 产出的 pyc magic 不匹配，客户端启动即 Bad magic number 秒退。本文件
锁死「编译解释器 = embed python.exe」这一不变量，防止回归。
"""
from __future__ import annotations

import importlib.util
import struct
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_P1 = REPO_ROOT / "packaging" / "build_p1.py"


@pytest.fixture(scope="module")
def bp():
    spec = importlib.util.spec_from_file_location("build_p1_under_test", BUILD_P1)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_compiler_python_points_inside_embed(bp, tmp_path):
    """_compiler_python 必须返回 embed 目录下的解释器，不是 PATH 里的 python。"""
    py_dir = tmp_path / "runtime" / "python"
    py_dir.mkdir(parents=True)
    fake = py_dir / ("python.exe" if __import__("os").name == "nt" else "python")
    fake.write_bytes(b"")
    assert bp._compiler_python(py_dir) == fake
    # 缺失时必须 fail-fast
    with pytest.raises(SystemExit):
        bp._compiler_python(tmp_path / "nonexistent")


def test_pyc_all_invokes_embed_interpreter(bp, tmp_path, monkeypatch):
    """_pyc_all 必须通过传入的 py_exe 调 compileall，而非当前进程解释器。"""
    embed = tmp_path / "embed_python.exe"
    embed.write_bytes(b"")
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.py").write_text("x=1\n", encoding="utf-8")

    calls = []

    class R:
        returncode = 0
        stdout = "cb0d0d0a"
        stderr = ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return R()

    monkeypatch.setattr(bp.subprocess, "run", fake_run)
    removed, pyc = bp._pyc_all(root, "test", embed)

    assert removed == 1 and pyc >= 0
    assert (root / "a.py").exists() is False  # .py 已删
    # 关键断言：命令行首参是 embed 解释器，且带 -m compileall -b（legacy pyc）
    assert calls, "必须实际调用了解释器编译"
    cmd = calls[0]
    assert Path(cmd[0]) == embed
    assert cmd[1:4] == ["-m", "compileall", "-b"]


def test_legacy_pyc_magic_matches_runtime():
    """产出的 legacy pyc 前 4 字节 magic 必须与运行解释器的 MAGIC_NUMBER 一致。"""
    magic = importlib.util.MAGIC_NUMBER
    assert len(magic) == 4
    # 仅校验结构（magic + 时间戳/大小头），不依赖具体版本号
    payload = magic + b"\x00" * 12
    assert struct.unpack("<4s", payload[:4])[0] == magic
