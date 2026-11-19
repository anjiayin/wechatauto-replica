# -*- coding: utf-8 -*-
"""QA 独立验证：忽略此版本 Bug 修复（不复用工程师测试函数/夹具）。

覆盖 6 项正确性属性中的 1–4：现场自愈、忽略四态、签名兼容/旧 pyc、
API 与 license 白名单。整体以线上真实 manifest 为第一证据源。
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import inspect
import json
import py_compile
import sqlite3
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import update_core as core
from app import update_service as us
from app.api import ALL_ROUTERS, deps, license_guard
from app.api.router_update import set_service
from app.db import Database

MANIFEST_URL = ("https://chehche-statics1.oss-cn-beijing.aliyuncs.com"
                "/wechat-kefu/manifest.json")

# 现场 DB 的真实脏状态（team-lead 取证），本文件独立重建而非连接生产库
FIELD_IGNORED = "1.2.5"
FIELD_CURRENT = "1.2.4"
FIELD_LAST_SEEN = 0


@pytest.fixture(scope="module")
def live_manifest() -> dict:
    """线上 manifest（urllib，20s 超时），只取原始 JSON，不经业务代码验签。"""
    with urllib.request.urlopen(MANIFEST_URL, timeout=20) as resp:
        raw = resp.read()
    m = json.loads(raw.decode("utf-8"))
    assert m["channel"] == "stable"
    assert core.VERSION_RE.match(str(m["latest"]))
    return m


class QADB:
    """QA 自建 setting 替身：显式区分「键不存在」与「键存在但为空」。"""

    def __init__(self, **seed: str) -> None:
        self.store: dict[str, str] = dict(seed)

    def get_setting(self, key: str, default: str = "") -> str:
        return self.store.get(key, default)

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.store[key])
        except (KeyError, TypeError, ValueError):
            return default

    def set_setting(self, key: str, value: str) -> None:
        self.store[key] = str(value)


def qa_manifest(*, latest: str = "1.2.5", sequence: int = 1250,
                channel: str = "stable", min_supported: str = "0.0.0") -> dict:
    return {
        "channel": channel,
        "sequence": sequence,
        "latest": latest,
        "min_supported": min_supported,
        "notes": "QA 独立构造",
        "released_at": "2026-01-01T00:00:00",
        "min_wechat_version": "4.0.0",
        "requires_antivirus_off": True,
        "packages": [{"type": "app", "version": latest, "size": 2048,
                      "url": "http://example.invalid/qa.zip",
                      "sha256": "0" * 64, "from_versions": [FIELD_CURRENT]}],
    }


def build_service(manifest: dict, db: QADB | None = None,
                  current: str = FIELD_CURRENT) -> us.UpdateService:
    s = us.UpdateService(db or QADB())
    s._manifest = lambda: (manifest, "")  # type: ignore[method-assign]
    return s


@pytest.fixture
def patch_update_env(monkeypatch):
    monkeypatch.setattr(us, "_manifest_url", "https://qa.invalid/kefu")
    monkeypatch.setattr(us, "_channel", "stable")
    monkeypatch.setattr(us, "APP_VERSION", FIELD_CURRENT)


# ---------------------------------------------------------------------------
# 属性 1：现场自愈（真实 manifest + 现场 DB 状态）
# ---------------------------------------------------------------------------
def test_field_core_self_heal_with_live_manifest(live_manifest):
    m = live_manifest
    assert str(m["latest"]) == FIELD_IGNORED, m["latest"]
    assert int(m["sequence"]) >= 1250, m["sequence"]
    assert core.compare_versions(FIELD_CURRENT,
                                 str(m.get("min_supported", "0.0.0"))) >= 0

    action = core.decide(FIELD_CURRENT, m, FIELD_IGNORED,
                         FIELD_LAST_SEEN, "stable", None)
    assert action == "optional", (action, m["sequence"], m["latest"])

    summary = core.public_summary(m, action, FIELD_CURRENT, ignored=False)
    assert summary["action"] == "optional"
    assert summary["ignored"] is False
    assert summary["latest"] == "1.2.5"
    assert summary["sequence"] == int(m["sequence"])


def test_field_service_self_heal_no_db_mutation(live_manifest,
                                                patch_update_env, monkeypatch):
    monkeypatch.setattr(us, "APP_VERSION", FIELD_CURRENT)
    db = QADB(**{
        core.KEY_IGNORED_VERSION: FIELD_IGNORED,
        core.KEY_LAST_SEQUENCE: str(FIELD_LAST_SEEN),
        # 关键：没有 update.ignored_sequence 键（现场老 DB）
    })
    assert core.KEY_IGNORED_SEQUENCE not in db.store
    svc = build_service(live_manifest, db)

    out = svc.check()
    assert out["action"] == "optional"
    assert out["ignored"] is False
    assert out["latest"] == "1.2.5"
    assert out["sequence"] == int(live_manifest["sequence"])
    # check 是只读操作，不允许顺手清掉现场数据
    assert db.store[core.KEY_IGNORED_VERSION] == FIELD_IGNORED
    assert db.store[core.KEY_LAST_SEQUENCE] == "0"


# ---------------------------------------------------------------------------
# 属性 2：忽略语义四态
# ---------------------------------------------------------------------------
def test_ignore_same_sequence_blocks_and_flags(patch_update_env):
    m = qa_manifest(sequence=1250)
    svc = build_service(m)
    assert svc.check()["action"] == "optional"

    svc.ignore("1.2.5")
    assert svc.db.store[core.KEY_IGNORED_SEQUENCE] == "1250"
    out = svc.check()
    assert out["action"] == "none"
    assert out["ignored"] is True
    assert out["latest"] == "1.2.5"        # 文案可展示，但不能说「已是最新」


def test_republish_higher_sequence_auto_unignores(patch_update_env):
    svc = build_service(qa_manifest(sequence=1250))
    # 真实 UI 流程：先 check 拿到 manifest 上下文，再点「忽略此版本」
    assert svc.check()["action"] == "optional"
    svc.ignore("1.2.5")
    assert svc.check()["action"] == "none"

    svc._manifest = lambda: (qa_manifest(sequence=1251), "")  # type: ignore[method-assign]
    out = svc.check()
    assert out["action"] == "optional"
    assert out["ignored"] is False
    assert out["sequence"] == 1251


def test_forced_beats_ignore(patch_update_env):
    m = qa_manifest(sequence=1250, min_supported="1.3.0")
    svc = build_service(m)
    out = svc.check()
    assert out["action"] == "forced"
    assert out["ignored"] is False

    svc.ignore("1.2.5")
    out = svc.check()
    assert out["action"] == "forced"
    assert out["ignored"] is False          # 强制更新不能误标为「已忽略」


@pytest.mark.parametrize("raw", [None, "", "   ", "abc", "0", "-1", "12.5"])
def test_legacy_or_invalid_ignore_sequence_never_blocks(raw):
    m = qa_manifest(sequence=1250)
    assert core.is_ignored("1.2.5", raw, m) is False
    assert core.decide("1.2.4", m, "1.2.5", 0, "stable", raw) == "optional"


def test_ignore_version_mismatch_never_blocks():
    m = qa_manifest(sequence=1250)
    assert core.is_ignored("1.2.4", 1250, m) is False
    assert core.decide("1.2.4", m, "1.2.4", 0, "stable", 1250) == "optional"


# ignored 标志不能误报：三种「看似忽略其实不该提示已忽略」的前置拦截
@pytest.mark.parametrize("case", ["already_latest", "replay", "wrong_channel"])
def test_ignored_flag_no_false_positive(patch_update_env, monkeypatch, case):
    if case == "already_latest":
        monkeypatch.setattr(us, "APP_VERSION", "1.2.5")
        m = qa_manifest(sequence=1251)
        db = QADB(**{core.KEY_IGNORED_VERSION: "1.2.5",
                     core.KEY_IGNORED_SEQUENCE: "1250"})
    elif case == "replay":
        m = qa_manifest(sequence=1250)
        db = QADB(**{core.KEY_IGNORED_VERSION: "1.2.5",
                     core.KEY_IGNORED_SEQUENCE: "1250",
                     core.KEY_LAST_SEQUENCE: "1250"})
    else:
        m = qa_manifest(sequence=1251, channel="beta")
        db = QADB(**{core.KEY_IGNORED_VERSION: "1.2.5",
                     core.KEY_IGNORED_SEQUENCE: "1250"})

    out = build_service(m, db).check()
    assert out["action"] == "none", case
    assert out["ignored"] is False, case


# ---------------------------------------------------------------------------
# 属性 3：签名兼容 / 现场旧 pyc
# ---------------------------------------------------------------------------
def test_decide_signature_defaults_and_legacy_arity(live_manifest):
    sig = inspect.signature(core.decide)
    assert sig.parameters["ignored_sequence"].default is None
    assert sig.parameters["channel"].default == "stable"

    m = live_manifest
    assert core.decide("1.2.4", m, "1.2.5", 0) == "optional"              # 4 参
    assert core.decide("1.2.4", m, "1.2.5", 0, "stable") == "optional"    # 5 参
    assert core.decide("1.2.4", m, "1.2.5", 0, "stable", None) == "optional"
    assert core.decide("1.2.4", m, "1.2.5", 0, "stable", 1250) == "none"


def test_legacy_compiled_pyc_calls_new_core_and_self_heals(
        tmp_path, live_manifest, monkeypatch):
    """把「旧 updater.pyc」真实编译出来加载：4 参调用 → 新 core 默认 None。"""
    import sys
    src = tmp_path / "legacy_updater.py"
    src.write_text(
        "from app.update_core import decide as _d\n"
        "def decide(current, m, ignored, last_seen):\n"
        "    return _d(current, m, ignored, last_seen)\n",
        encoding="utf-8")
    pyc = tmp_path / "legacy_updater.pyc"
    py_compile.compile(str(src), cfile=str(pyc), doraise=True)
    assert pyc.is_file()

    monkeypatch.syspath_prepend(str(tmp_path))
    loader = importlib.machinery.SourcelessFileLoader(
        "qa_legacy_updater_pyc", str(pyc))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod
    loader.exec_module(mod)

    assert list(inspect.signature(mod.decide).parameters) == [
        "current", "m", "ignored", "last_seen"]
    assert mod.decide(FIELD_CURRENT, live_manifest, FIELD_IGNORED,
                      FIELD_LAST_SEEN) == "optional"


# ---------------------------------------------------------------------------
# 属性 4：API 与 license 白名单
# ---------------------------------------------------------------------------
def test_unignore_endpoint_clears_both_settings(tmp_path, patch_update_env):
    db = Database(str(tmp_path / "qa_unignore.sqlite3"))
    db.set_setting(core.KEY_IGNORED_VERSION, "1.2.5")
    db.set_setting(core.KEY_IGNORED_SEQUENCE, "1250")
    svc = build_service(qa_manifest(), db)
    svc.ignore("1.2.5")                   # 走真实 DB 写入一遍

    app = FastAPI()
    for router in ALL_ROUTERS:
        app.include_router(router)
    set_service(svc)
    try:
        with TestClient(app) as client:
            r = client.post("/api/system/update/unignore")
            assert r.status_code == 200, r.text
            assert r.json() == {"ok": True, "ignored": ""}
    finally:
        set_service(None)
        db.close()

    # 重新打开确认真的落库（不是内存态）
    verify = Database(str(tmp_path / "qa_unignore.sqlite3"))
    try:
        assert verify.get_setting(core.KEY_IGNORED_VERSION, "X") == ""
        assert verify.get_setting(core.KEY_IGNORED_SEQUENCE, "X") == ""
    finally:
        verify.close()


def test_unignore_is_license_exempt_but_business_api_is_blocked(
        tmp_path, patch_update_env):
    db = Database(str(tmp_path / "qa_license.sqlite3"))
    db.set_setting(core.KEY_IGNORED_VERSION, "1.2.5")
    db.set_setting(core.KEY_IGNORED_SEQUENCE, "1250")
    svc = build_service(qa_manifest(), db)
    set_service(svc)

    expired = SimpleNamespace(ok=False, state="expired",
                              message="授权已过期", machine_code="QA-MACHINE")
    app = FastAPI()
    app.middleware("http")(
        license_guard.make_license_guard(lambda: expired, cache_sec=0))
    for router in ALL_ROUTERS:
        app.include_router(router)
    try:
        with TestClient(app) as client:
            ok = client.post("/api/system/update/unignore")
            assert ok.status_code == 200, (ok.status_code, ok.text)
            blocked = client.get("/api/opportunities")
            assert blocked.status_code == 403, (
                "中间件没生效，本用例无效", blocked.status_code, blocked.text)
            assert blocked.json().get("code") == "license_required"
    finally:
        set_service(None)
        db.close()


def test_check_ignored_flag_four_states(tmp_path, patch_update_env,
                                        monkeypatch):
    """通过真实 HTTP check 接口确认 ignored 标志四种情况均准确。"""
    db = Database(str(tmp_path / "qa_check_flag.sqlite3"))
    svc = build_service(qa_manifest(), db)
    app = FastAPI()
    for router in ALL_ROUTERS:
        app.include_router(router)
    set_service(svc)

    def do_check():
        with TestClient(app) as client:
            r = client.get("/api/system/update/check")
            assert r.status_code == 200, r.text
            return r.json()

    try:
        # 1) 有更新未忽略
        out = do_check()
        assert (out["action"], out["ignored"]) == ("optional", False)

        # 2) 真忽略
        svc.ignore("1.2.5")
        out = do_check()
        assert (out["action"], out["ignored"]) == ("none", True)

        # 3) 防重放（sequence 已消费）
        db.set_setting(core.KEY_IGNORED_SEQUENCE, "1250")
        db.set_setting(core.KEY_LAST_SEQUENCE, "1250")
        out = do_check()
        assert (out["action"], out["ignored"]) == ("none", False)

        # 4) 已是最新（无新版本）
        db.set_setting(core.KEY_LAST_SEQUENCE, "0")
        monkeypatch.setattr(us, "APP_VERSION", "1.2.5")
        svc._manifest = lambda: (qa_manifest(sequence=1251), "")  # type: ignore[method-assign]
        out = do_check()
        assert (out["action"], out["ignored"]) == ("none", False)
    finally:
        set_service(None)
        db.close()


def test_tray_ignored_sequence_sql_roundtrip(tmp_path, monkeypatch):
    """托盘链路独立验证：老库无键 / 新键读写 / 取消忽略清键。"""
    updater_path = Path(__file__).resolve().parents[2] / "packaging" / "updater.py"
    spec = importlib.util.spec_from_file_location("qa_p1_updater", updater_path)
    tray = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(tray)

    monkeypatch.setattr(tray, "DATA", tmp_path)
    dbf = tmp_path / "kefu.sqlite3"
    con = sqlite3.connect(dbf)
    con.execute("create table settings(key text primary key, value text)")
    con.execute("insert into settings values(?,?)",
                (core.KEY_IGNORED_VERSION, "1.2.5"))
    con.commit()
    con.close()

    assert tray._ignored_sequence() is None   # 老库键缺失 → 过期
    m = {"channel": "stable", "sequence": 1250, "latest": "1.2.5",
         "min_supported": "0.0.0"}
    assert tray.decide("1.2.4", m, "1.2.5", 0) == "optional"
    assert tray.decide("1.2.4", m, "1.2.5", 0, "stable", None) == "optional"

    tray._save_state("1.2.5", 0, 1250)
    assert tray._ignored_sequence() == 1250
    assert tray.decide("1.2.4", m, "1.2.5", 0, "stable",
                       tray._ignored_sequence()) == "none"
    assert tray.decide("1.2.4", dict(m, sequence=1251), "1.2.5", 0,
                       "stable", tray._ignored_sequence()) == "optional"

    tray._save_state("", 1251)
    con = sqlite3.connect(dbf)
    rows = dict(con.execute("select key,value from settings").fetchall())
    con.close()
    assert rows[core.KEY_IGNORED_SEQUENCE] == ""
    assert tray._ignored_sequence() is None
