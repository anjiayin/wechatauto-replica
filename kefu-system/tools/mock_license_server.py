# -*- coding: utf-8 -*-
"""模拟授权服务器（开发/联调用，也是正式后台的实现参考）。

**它不是玩具**：客户端的激活、联网确认、续期、吊销全部按这里的契约走，
正式后台照着 `docs/授权服务接口文档.md` 实现同样的接口即可替换本文件。

启动（在 kefu-system 目录下）::

    python tools/mock_license_server.py                 # 默认 127.0.0.1:8799
    python tools/mock_license_server.py --port 9000

打开 http://127.0.0.1:8799/admin 有简易管理页：生成激活码、看授权列表、
延期、吊销。

命令行快速造码::

    python tools/mock_license_server.py --issue-code 365 --customer 某某汽配

密钥：默认用 ``tools/license_keys/dev_private.pem``（与客户端内置的 ``dev``
公钥配对）。正式环境换成自己的密钥对，见 tools/gen_license_keys.py。

数据落盘：``tools/mock_license_data.json``（每次写入都是原子替换，掉电不会
写坏；删掉即重置全部授权数据）。
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import secrets
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_KEY_DIR = BASE_DIR / "license_keys"
DEFAULT_DATA = BASE_DIR / "mock_license_data.json"

logger = logging.getLogger(__name__)

# 与客户端 app/license.py 保持一致的常量（改了这里必须同步客户端）
DEFAULT_GRACE_DAYS = 7
TZ = timezone(timedelta(hours=8))       # 服务端统一按东八区签发（客户端只比对绝对时间）


# ---------------------------------------------------------------------------
# 签名（**必须与客户端 app/license.py 的 payload_bytes 完全一致**）
# ---------------------------------------------------------------------------
def payload_bytes(payload: dict) -> bytes:
    """签名原文的规范化序列化。

    ``sort_keys=True`` + 紧凑分隔符 + 不转义中文——任何一项不同，客户端验签
    就会失败。正式后台也必须用同样的规则（其它语言实现时注意：JSON 键必须
    递归排序、分隔符不能带空格、非 ASCII 不要转成 \\uXXXX）。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def load_private_key(path: Path) -> Ed25519PrivateKey:
    """读取 PEM 私钥（失败给出中文提示并退出）。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        print(f"错误：读取私钥失败：{exc}\n"
              f"请先执行：python tools/gen_license_keys.py --out tools/license_keys",
              file=sys.stderr)
        raise SystemExit(2)
    try:
        key = load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        print(f"错误：私钥格式不正确：{exc}", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(key, Ed25519PrivateKey):
        print("错误：私钥不是 Ed25519 类型", file=sys.stderr)
        raise SystemExit(2)
    return key


# ---------------------------------------------------------------------------
# 数据存储（JSON + 原子写 + 线程锁）
# ---------------------------------------------------------------------------
class Store:
    """授权数据存储（激活码 + 许可证）。单文件 JSON，够用且便于人工查看。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, list] = {"codes": [], "licenses": []}
        self._load()

    def _load(self) -> None:
        """读取数据文件；不存在/损坏时用空结构（不覆盖原文件）。"""
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"警告：数据文件无法解析（按空数据启动）：{exc}", file=sys.stderr)
            return
        if isinstance(raw, dict):
            self._data = {
                "codes": list(raw.get("codes") or []),
                "licenses": list(raw.get("licenses") or []),
            }

    def _flush(self) -> None:
        """原子写（先写临时文件再替换）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # -- 激活码 --------------------------------------------------------
    def create_codes(self, count: int, days: int, customer: str,
                     edition: str, note: str) -> list[dict]:
        """批量生成激活码。"""
        with self._lock:
            created = []
            for _ in range(max(1, min(count, 500))):
                code = "KF-" + "-".join(
                    secrets.token_hex(2).upper() for _ in range(4))
                item = {
                    "code": code,
                    "days": int(days),
                    "customer": customer,
                    "edition": edition,
                    "note": note,
                    "created_at": now_iso(),
                    "used_by": "",
                    "used_at": "",
                }
                self._data["codes"].append(item)
                created.append(item)
            self._flush()
            return created

    def find_code(self, code: str) -> Optional[dict]:
        """按激活码查找。"""
        code = (code or "").strip().upper()
        for item in self._data["codes"]:
            if item.get("code") == code:
                return item
        return None

    def codes(self) -> list[dict]:
        """全部激活码（新的在前）。"""
        return list(reversed(self._data["codes"]))

    # -- 许可证 --------------------------------------------------------
    def find_license(self, license_id: str = "",
                     machine_code: str = "") -> Optional[dict]:
        """按许可证号或机器码查找（**同机器多条时返回最新一条**）。

        顺序很重要：同一台机器被吊销后重新发码时，旧记录会保留作历史，
        若按插入顺序返回旧记录，激活接口会一直认为这台机仍处于吊销状态。
        """
        for item in reversed(self._data["licenses"]):
            if license_id and item.get("license_id") == license_id:
                return item
            if machine_code and item.get("machine_code") == machine_code:
                return item
        return None

    def active_for_machine(self, machine_code: str,
                           exclude_id: str = "") -> Optional[dict]:
        """某台机器当前生效（未吊销）的许可证（排除指定许可证号）。"""
        for item in reversed(self._data["licenses"]):
            if item.get("machine_code") != machine_code:
                continue
            if exclude_id and item.get("license_id") == exclude_id:
                continue
            if not item.get("revoked_at"):
                return item
        return None

    def licenses(self) -> list[dict]:
        """全部许可证（新的在前）。"""
        return list(reversed(self._data["licenses"]))

    def upsert_license(self, item: dict) -> dict:
        """新增或替换许可证记录。"""
        with self._lock:
            for idx, old in enumerate(self._data["licenses"]):
                if old.get("license_id") == item["license_id"]:
                    self._data["licenses"][idx] = item
                    self._flush()
                    return item
            self._data["licenses"].append(item)
            self._flush()
            return item

    def mark_code_used(self, code: str, machine_code: str) -> None:
        """标记激活码已被某台机器使用。"""
        with self._lock:
            item = self.find_code(code)
            if item is None:
                return
            item["used_by"] = machine_code
            item["used_at"] = now_iso()
            self._flush()

    def renew(self, license_id: str, days: int) -> Optional[dict]:
        """延期（从「当前到期日」和「今天」中较晚者起算）。"""
        with self._lock:
            item = self.find_license(license_id=license_id)
            if item is None:
                return None
            current = parse_iso(item.get("expires_at"))
            base = max(current, now()) if current else now()
            item["expires_at"] = (base + timedelta(days=days)).isoformat()
            item["updated_at"] = now_iso()
            self._flush()
            return item

    def revoke(self, license_id: str) -> Optional[dict]:
        """吊销（客户端下次联网确认时会锁死）。"""
        with self._lock:
            item = self.find_license(license_id=license_id)
            if item is None:
                return None
            item["revoked_at"] = now_iso()
            self._flush()
            return item

    def restore(self, license_id: str) -> Optional[dict]:
        """撤销吊销（误操作恢复；客户下次联网确认即可继续用）。"""
        with self._lock:
            item = self.find_license(license_id=license_id)
            if item is None:
                return None
            item["revoked_at"] = ""
            item["updated_at"] = now_iso()
            self._flush()
            return item


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------
def now() -> datetime:
    """当前时间（东八区）。"""
    return datetime.now(TZ)


def now_iso() -> str:
    """当前时间 ISO 字符串（秒级，带时区）。"""
    return now().replace(microsecond=0).isoformat()


def parse_iso(value: Any) -> Optional[datetime]:
    """解析 ISO 时间串（带时区）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def new_license_id() -> str:
    """许可证号：LIC-年月日-四位随机。"""
    return f"LIC-{now():%Y%m%d}-{uuid.uuid4().hex[:4].upper()}"


def valid_machine_code(code: str) -> bool:
    """机器码格式校验：32 位十六进制，允许 4 位分组。"""
    compact = "".join(ch for ch in str(code or "") if ch != "-")
    return len(compact) == 32 and all(c in "0123456789ABCDEF"
                                      for c in compact.upper())


def annotate_licenses(items: list[dict]) -> list[dict]:
    """给管理台的许可证列表补 ``role`` 标记（不改动落盘数据）。

    Args:
        items: 按**最新在前**排列的许可证（``Store.licenses()`` 已是该顺序）。

    同一台机器可能有多条记录（吊销后重新授权、续期换发都会留下旧记录）。
    不标出来的话，管理员看到两条同机器码的记录会分不清哪条在生效 —— 现场
    就因为旧记录到期日更晚而误判成"客户端状态不对"。
    """
    active: dict[str, str] = {}
    for item in items:
        machine = str(item.get("machine_code") or "")
        if not item.get("revoked_at") and machine not in active:
            active[machine] = str(item.get("license_id") or "")
    rows: list[dict] = []
    for item in items:
        row = dict(item)
        machine = str(row.get("machine_code") or "")
        lid = str(row.get("license_id") or "")
        if row.get("revoked_at"):
            row["role"] = "revoked"
            # 该机器已经拿到新许可证时，旧记录不应再被"恢复"
            row["can_restore"] = machine not in active
        elif active.get(machine) == lid:
            row["role"] = "active"
        else:
            row["role"] = "superseded"
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------
class ActivateIn(BaseModel):
    """激活请求。"""
    machine_code: str
    activation_code: str = ""
    app_version: str = ""
    os: str = ""


class HeartbeatIn(BaseModel):
    """联网确认请求。"""
    machine_code: str
    license_id: str = ""
    app_version: str = ""


class CreateCodesIn(BaseModel):
    """生成激活码。"""
    count: int = Field(1, ge=1, le=500)
    days: int = Field(365, ge=1, le=3650)
    customer: str = ""
    edition: str = "standard"
    note: str = ""


class RenewIn(BaseModel):
    """延期。"""
    days: int = Field(365, ge=1, le=3650)


def err(code: str, message: str, status: int = 400) -> HTTPException:
    """构造统一错误响应（客户端按 code 做中文提示）。"""
    return HTTPException(status_code=status,
                         detail={"error": {"code": code, "message": message}})


def sign_doc(payload: dict, private_key: Ed25519PrivateKey,
             key_id: str) -> dict:
    """给 payload 签名，返回客户端可直接保存的许可证文档。"""
    signature = private_key.sign(payload_bytes(payload))
    return {
        "key_id": key_id,
        "payload": payload,
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def build_payload(record: dict, grace_days: int = DEFAULT_GRACE_DAYS) -> dict:
    """由许可证记录生成 payload（每次下发都重新签，保证期限最新）。"""
    return {
        "license_id": record["license_id"],
        "machine_code": record["machine_code"],
        "customer": record.get("customer", ""),
        "edition": record.get("edition", "standard"),
        "issued_at": record.get("issued_at", now_iso()),
        "expires_at": record["expires_at"],
        "grace_days": int(record.get("grace_days", grace_days)),
        "features": list(record.get("features") or ["monitor", "reply", "ai"]),
    }


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------
def create_app(private_key: Ed25519PrivateKey, key_id: str,
               data_path: Path) -> FastAPI:
    """创建模拟授权服务器应用。"""
    store = Store(data_path)
    app = FastAPI(
        title="模拟授权服务器（kefu-system license server）",
        version="1.0.0",
        description="开发/联调用；正式后台按 docs/授权服务接口文档.md 实现",
    )

    @app.exception_handler(HTTPException)
    async def _flatten_error(request, exc: HTTPException):
        """把 ``{"detail": {"error": {...}}}`` 摊平成 ``{"error": {...}}``。

        接口文档约定错误体是顶层 ``error`` 对象；客户端两种都兼容，但服务端
        统一成一种，正式后台照抄不会写出两套格式。
        """
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            return JSONResponse(status_code=exc.status_code, content=detail)
        return JSONResponse(status_code=exc.status_code, content={
            "error": {"code": "http_error", "message": str(detail)}})

    # ------------------------------------------------------------------
    # 客户端接口（正式后台必须实现这三个）
    # ------------------------------------------------------------------
    @app.post("/api/v1/license/activate")
    def activate(body: ActivateIn) -> dict:
        """激活：校验激活码 → 绑定机器码 → 签发许可证。"""
        machine = body.machine_code.strip().upper()
        if not valid_machine_code(machine):
            raise err("invalid_machine_code", "机器码格式不正确（应为 32 位十六进制）")
        code = body.activation_code.strip().upper()

        record = store.find_license(machine_code=machine)
        # 已授权过：重复激活不报错（客户重装/重装系统都会再来一次）。
        # **带新激活码时按"续期"处理**：有效期从「当前到期日」与「今天」中较晚
        # 者起叠加，客户买第二次就能直接叠加时长，不用先等旧证过期。
        if record and not record.get("revoked_at"):
            if code:
                item = store.find_code(code)
                if item is None:
                    raise err("invalid_code", "激活码不存在，请核对后重试")
                if item.get("used_by") and item["used_by"] != machine:
                    raise err("code_used", "该激活码已被其它电脑使用")
                base = max(parse_iso(record.get("expires_at")) or now(), now())
                record["expires_at"] = (
                    base + timedelta(days=int(item.get("days") or 365))
                ).isoformat()
                record["activation_code"] = code
                if item.get("customer"):
                    record["customer"] = item["customer"]
                record["updated_at"] = now_iso()
                store.mark_code_used(code, machine)
            store.upsert_license(record)
            return {"ok": True, "license": sign_doc(
                build_payload(record), private_key, key_id)}

        if not code:
            raise err("activation_required", "需要激活码：请把机器码发给供应商换取")

        item = store.find_code(code)
        if item is None:
            raise err("invalid_code", "激活码不存在，请核对后重试")
        if item.get("used_by") and item["used_by"] != machine:
            raise err("code_used", "该激活码已被其它电脑使用")

        reissued_from = ""
        if record and record.get("revoked_at"):
            # 已吊销的机器：只认「全新的、没用过的激活码」。
            # - 客户拿吊销前那个旧码反复激活 = 自助解封，必须堵死；
            # - 供应商重新发一个码 = 明确解禁，必须放行，否则返修/退款后
            #   想恢复只能改数据库。旧记录保留作历史，新记录用新许可证号。
            if item.get("used_by"):
                raise err("revoked", "该机器码的授权已被吊销，"
                                     "需要供应商重新发一个激活码")
            reissued_from = str(record.get("license_id") or "")
            logger.info("已吊销机器重新授权：%s → 新码 %s（原 %s）",
                        machine, code, reissued_from)
            record = None

        issued = now()
        expires = issued + timedelta(days=int(item.get("days") or 365))
        record = {
            "license_id": new_license_id(),
            "machine_code": machine,
            "customer": item.get("customer", ""),
            "edition": item.get("edition", "standard"),
            "activation_code": code,
            "issued_at": issued.isoformat(),
            "expires_at": expires.isoformat(),
            "grace_days": DEFAULT_GRACE_DAYS,
            "features": ["monitor", "reply", "ai"],
            "revoked_at": "",
            "reissued_from": reissued_from,
            "last_seen_at": issued.isoformat(),
            "app_version": body.app_version,
            "os": body.os,
        }
        store.upsert_license(record)
        store.mark_code_used(code, machine)
        return {"ok": True, "license": sign_doc(
            build_payload(record), private_key, key_id)}

    @app.post("/api/v1/license/heartbeat")
    def heartbeat(body: HeartbeatIn) -> dict:
        """联网确认：返回当前授权状态；服务器改过期限时顺带下发新证。"""
        machine = body.machine_code.strip().upper()
        record = store.find_license(license_id=body.license_id.strip(),
                                    machine_code=machine)
        if record is None:
            raise err("not_found", "服务器上没有这台电脑的授权记录", status=404)
        if record.get("machine_code") != machine:
            raise err("machine_mismatch", "许可证绑定的机器码与本机不一致")
        if record.get("revoked_at"):
            return {"ok": False, "error": {
                "code": "revoked",
                "message": f"授权已于 {record['revoked_at']} 被吊销"}}
        record["last_seen_at"] = now_iso()
        record["app_version"] = body.app_version
        store.upsert_license(record)
        expires = parse_iso(record.get("expires_at"))
        if expires is None or expires <= now():
            return {"ok": False, "error": {
                "code": "expired", "message": "授权已到期，请续费"}}
        return {
            "ok": True,
            "status": "active",
            "license_id": record["license_id"],
            "expires_at": record["expires_at"],
            "license": sign_doc(build_payload(record), private_key, key_id),
        }

    @app.get("/api/v1/license/query")
    def query(machine_code: str = "") -> dict:
        """查询某台机器的授权（调试/客服核对用）。"""
        machine = machine_code.strip().upper()
        record = store.find_license(machine_code=machine)
        if record is None:
            return {"ok": False, "error": {"code": "not_found",
                                           "message": "无授权记录"}}
        return {"ok": True, "license": build_payload(record),
                "revoked": bool(record.get("revoked_at"))}

    # ------------------------------------------------------------------
    # 管理接口（正式后台应加登录鉴权 + 审计日志，这里只做演示）
    # ------------------------------------------------------------------
    @app.post("/admin/api/codes")
    def admin_create_codes(body: CreateCodesIn) -> dict:
        """生成激活码。"""
        items = store.create_codes(body.count, body.days, body.customer,
                                   body.edition, body.note)
        return {"ok": True, "items": items}

    @app.get("/admin/api/codes")
    def admin_list_codes() -> dict:
        """激活码列表。"""
        return {"ok": True, "items": store.codes()}

    @app.get("/admin/api/licenses")
    def admin_list_licenses() -> dict:
        """许可证列表（带 role 标记：当前生效 / 已被新证取代 / 已吊销）。"""
        return {"ok": True, "items": annotate_licenses(store.licenses())}

    @app.post("/admin/api/licenses/{license_id}/renew")
    def admin_renew(license_id: str, body: RenewIn) -> dict:
        """延期。"""
        item = store.renew(license_id, body.days)
        if item is None:
            raise HTTPException(status_code=404, detail="许可证不存在")
        return {"ok": True, "item": item}

    @app.post("/admin/api/licenses/{license_id}/revoke")
    def admin_revoke(license_id: str) -> dict:
        """吊销。"""
        item = store.revoke(license_id)
        if item is None:
            raise HTTPException(status_code=404, detail="许可证不存在")
        return {"ok": True, "item": item}

    @app.post("/admin/api/licenses/{license_id}/restore")
    def admin_restore(license_id: str) -> dict:
        """撤销吊销（误操作时用）。

        若这台机器已经拿到新许可证，就不该再复活旧记录（同一台机两条生效
        授权会让「按机器码查」的结果变得难以预测），直接拒绝。
        """
        target = store.find_license(license_id=license_id)
        if target is None:
            raise HTTPException(status_code=404, detail="许可证不存在")
        other = store.active_for_machine(target.get("machine_code", ""),
                                         exclude_id=license_id)
        if other is not None:
            raise err("already_active",
                      f"该机器码已有生效授权 {other['license_id']}，无需恢复",
                      status=409)
        item = store.restore(license_id)
        return {"ok": True, "item": item}

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page() -> str:
        """简易管理页（纯前端 fetch，够演示与联调）。"""
        return _ADMIN_HTML

    @app.get("/")
    def root() -> dict:
        """根路径：一句话说明 + 管理页地址。"""
        return {"ok": True, "service": "mock license server",
                "admin": "/admin", "docs": "/docs"}

    return app


_ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>授权服务器（模拟）管理台</title>
<style>
body{font-family:"Microsoft YaHei",sans-serif;margin:24px;color:#222;background:#fafafa}
h2{margin:24px 0 8px;font-size:18px}
input,select{padding:4px 6px;margin-right:6px;border:1px solid #ccc;border-radius:4px}
button{padding:4px 10px;border:0;border-radius:4px;background:#1976d2;color:#fff;
cursor:pointer}
button.warn{background:#c62828}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{border:1px solid #e0e0e0;padding:6px 8px;text-align:left}
th{background:#f0f4f8}
code{background:#eef;padding:1px 4px;border-radius:3px}
.hint{color:#666;font-size:12px}
tr.dim{color:#999}
tr.dim code{color:#999}
</style></head><body>
<h2>生成激活码</h2>
<div>
天数 <input id="days" type="number" value="365" style="width:70px">
数量 <input id="count" type="number" value="1" style="width:60px">
客户 <input id="customer" placeholder="客户名称（可空）">
版本 <input id="edition" value="standard" style="width:90px">
<button onclick="gen()">生成</button>
<span class="hint">生成后把激活码发给客户，客户在软件里激活</span>
</div>

<h2>许可证列表</h2>
<div class="hint">「延期」从当前到期日和今天中较晚者起算；「吊销」后客户下次联网确认即锁死；
同一台机器重新授权后旧记录会保留为历史（「已被新证取代」/「已吊销」），
只有「当前生效」那条会被客户端使用</div>
<table><thead><tr><th>许可证号</th><th>机器码</th><th>客户</th><th>到期日</th>
<th>最近确认</th><th>状态</th><th>操作</th></tr></thead>
<tbody id="lic"></tbody></table>

<h2>激活码列表</h2>
<table><thead><tr><th>激活码</th><th>天数</th><th>客户</th><th>状态</th>
<th>使用机器码</th></tr></thead><tbody id="codes"></tbody></table>

<script>
async function api(url, opt){
  const r = await fetch(url, opt); const j = await r.json();
  if(!r.ok) throw new Error(JSON.stringify(j.detail||j));
  return j;
}
async function gen(){
  const body = {
    count: +document.getElementById('count').value,
    days: +document.getElementById('days').value,
    customer: document.getElementById('customer').value,
    edition: document.getElementById('edition').value,
  };
  const j = await api('/admin/api/codes', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  alert('已生成：\\n' + j.items.map(i=>i.code).join('\\n'));
  load();
}
async function renew(id, days){
  await api(`/admin/api/licenses/${id}/renew`, {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({days})});
  load();
}
async function revoke(id){
  if(!confirm('确认吊销 ' + id + '？')) return;
  await api(`/admin/api/licenses/${id}/revoke`, {method:'POST'});
  load();
}
async function restore(id){
  if(!confirm('确认恢复 ' + id + '？恢复后客户下次联网确认即可继续使用')) return;
  try {
    await api(`/admin/api/licenses/${id}/restore`, {method:'POST'});
  } catch (e) {
    alert('恢复失败：' + e.message);
  }
  load();
}
async function load(){
  const lic = (await api('/admin/api/licenses')).items;
  const badges = {active:'当前生效', superseded:'已被新证取代', revoked:'已吊销'};
  document.getElementById('lic').innerHTML = lic.map(i=>{
    const badge = badges[i.role] || '正常';
    let ops = '';
    if (i.role === 'active') {
      ops = `<button onclick="renew('${i.license_id}',365)">+365 天</button>
             <button onclick="renew('${i.license_id}',30)">+30 天</button>
             <button class="warn" onclick="revoke('${i.license_id}')">吊销</button>`;
    } else if (i.role === 'revoked' && i.can_restore) {
      ops = `<button onclick="restore('${i.license_id}')">恢复</button>`;
    }
    const cls = i.role === 'active' ? '' : ' class="dim"';
    return `<tr${cls}><td><code>${i.license_id}</code></td><td>${i.machine_code}</td>
      <td>${i.customer||'-'}</td><td>${(i.expires_at||'').slice(0,10)}</td>
      <td>${(i.last_seen_at||'-').slice(0,19).replace('T',' ')}</td>
      <td>${badge}</td>
      <td>${ops || '<span class="hint">—</span>'}</td></tr>`;
  }).join('') || '<tr><td colspan="7">暂无授权</td></tr>';
  const codes = (await api('/admin/api/codes')).items;
  document.getElementById('codes').innerHTML = codes.map(c=>`<tr>
    <td><code>${c.code}</code></td><td>${c.days}</td><td>${c.customer||'-'}</td>
    <td>${c.used_by?'已使用':'未使用'}</td><td>${c.used_by||'-'}</td></tr>`
  ).join('') || '<tr><td colspan="5">暂无激活码</td></tr>';
}
load();
</script></body></html>"""


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """启动模拟授权服务器，或命令行造码。"""
    parser = argparse.ArgumentParser(description="模拟授权服务器（kefu-system）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--key-dir", default=str(DEFAULT_KEY_DIR),
                        help="密钥目录（默认 tools/license_keys）")
    parser.add_argument("--key-id", default="dev",
                        help="密钥标识（与客户端 PUBLIC_KEYS 的 key 对应）")
    parser.add_argument("--data", default=str(DEFAULT_DATA),
                        help="数据文件（默认 tools/mock_license_data.json）")
    parser.add_argument("--issue-code", type=int, metavar="DAYS",
                        help="不启动服务，直接生成一个 N 天的激活码并打印")
    parser.add_argument("--customer", default="", help="配合 --issue-code 指定客户名")
    args = parser.parse_args(argv)

    key_path = Path(args.key_dir) / f"{args.key_id}_private.pem"
    private_key = load_private_key(key_path)
    data_path = Path(args.data)

    if args.issue_code:
        store = Store(data_path)
        items = store.create_codes(1, args.issue_code, args.customer,
                                   "standard", "CLI 生成")
        print(items[0]["code"])
        return 0

    app = create_app(private_key, args.key_id, data_path)
    print(f"模拟授权服务器：http://{args.host}:{args.port}"
          f"（管理台 /admin，接口文档 /docs）")
    print(f"  密钥：{key_path}    数据：{data_path}")
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
