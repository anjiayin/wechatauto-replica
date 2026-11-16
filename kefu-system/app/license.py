# -*- coding: utf-8 -*-
"""离线可用 + 在线校验的电脑授权（机器码绑定 + Ed25519 签名许可证）。

## 为什么这样设计

**1. 机器码用系统标识派生，不落盘。**
Windows 上取注册表 ``MachineGuid`` + 系统盘卷序列号（Win32 API 直读），
POSIX 上取 ``/etc/machine-id`` + 主机名，归一再取 SHA256。之所以**不用**
WMI 的主板 UUID / CPU ID：本机实测 WMI 仓库已损坏（「无效类」），而 WMI 被
Windows 更新打坏并不罕见——一旦它坏了机器码就会漂移、全体客户授权同时失效。
稳定优先。**刻意不做本地缓存**：缓存文件一改就能伪造机器码，而现算成本不到
10 毫秒（不起子进程、不查 WMI）。

**2. 许可证用非对称签名，客户端只内置公钥。**
服务端用 Ed25519 私钥签 `payload`，客户端公钥验签。客户把程序目录全翻出来
也拿不到伪造能力（对称 HMAC 做不到这一点）。

**3. 离线宽限期，避免断网即锁死。**
许可证本身签了 `expires_at`（绝对到期日）；另外本地记录**最近一次成功联网
确认的时间** `last_ok_at`。服务器不可达时，只要 `now - last_ok_at` 还在
`grace_days`（默认 7 天）内就继续可用，只在界面提示「离线运行中」。超过则
进入「离线超期」状态，必须联网确认一次才能继续用。

**4. 防时钟回拨。**
本地记录历史见过的最大时间戳 `max_seen_ts`（只在联网成功/启动时落盘）。
若系统时间比它早 1 天以上，判定为人为调早时钟，直接锁死并要求校正时间。

**5. 到期只锁功能，绝不动数据。**
`ok=False` 时后端只拦截 `/api/*` 业务接口（白名单放行授权与状态查询），
数据库、图片缓存、语音等一律不删不改；续费后原样恢复。

## 状态机

| state | 含义 | ok |
|---|---|---|
| `disabled` | 未启用授权（开发/测试） | ✅ |
| `active` | 正常，已联网确认 | ✅ |
| `offline_grace` | 服务器不可达，仍在宽限期内 | ✅ |
| `unlicensed` | 从未激活 | ❌ |
| `expired` | 超过许可证到期日 | ❌ |
| `offline_expired` | 离线超过宽限期 | ❌ |
| `invalid` | 签名错 / 机器码不符 / 格式坏 | ❌ |
| `revoked` | 服务器明确吊销 | ❌ |
| `clock_rollback` | 系统时间被调早 | ❌ |

## 许可证文件格式（服务端签发，客户端原样保存）

```json
{
  "key_id": "dev",
  "payload": {
    "license_id": "LIC-20261113-0001",
    "machine_code": "ABCD-EFGH-...",
    "customer": "某某汽配",
    "edition": "standard",
    "issued_at": "2026-11-13T21:00:00+08:00",
    "expires_at": "2027-11-13T23:59:59+08:00",
    "grace_days": 7,
    "features": ["monitor", "reply", "ai"]
  },
  "signature": "<base64(Ed25519_sign(payload_bytes))>"
}
```

`payload_bytes` 的序列化方式**必须与服务端完全一致**，否则验签必然失败：

```python
json.dumps(payload, ensure_ascii=False, sort_keys=True,
           separators=(",", ":")).encode("utf-8")
```

接口契约见 `docs/授权服务接口文档.md`。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 内置公钥（客户端验签用；私钥只在授权服务器）
# ---------------------------------------------------------------------------
# 用 tools/gen_license_keys.py 生成。**正式交付前必须换成自己的密钥对**，
# 并把 key_id 同步到服务端；支持多把公钥共存，便于密钥轮换（老许可证仍可验）。
PUBLIC_KEYS: dict[str, str] = {
    # 开发/联调用密钥对，私钥见 tools/license_keys/dev_private.pem
    "dev": "6efHWx5yQieQo3MF9VIDw3vof2HBD3EEqk3AKrSQSgw=",
	"prod-2026": "WEDZ/3S+jw1mtohsgahZaoKxFfaJ/TbN54TSc7kN00s=",
}

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DEFAULT_SERVER_URL = "http://127.0.0.1:8799"   # 模拟授权服务器默认地址
DEFAULT_REFRESH_HOURS = 6                      # 联网确认间隔（小时）
DEFAULT_GRACE_DAYS = 7                         # 离线宽限（天）
HTTP_TIMEOUT_SEC = 10                          # 授权接口超时（秒）
CLOCK_ROLLBACK_TOLERANCE_SEC = 86400           # 时钟回拨容忍（1 天）

# 机器码无效成分的黑名单（主板厂商没填时的占位串，各机器都一样，
# 参与哈希会让一堆机器算出同一个码）
_INVALID_HW_VALUES = {
    "", "0", "none", "null", "n/a", "na", "unknown", "default",
    "default string", "to be filled by o.e.m.", "to be filled by oem",
    "system serial number", "not specified", "not applicable", "invalid",
    "00000000", "ffffffff",
    "03000200-0400-0500-0006-000700080009",   # 常见虚拟机占位 UUID
    "00020003-0004-0005-0006-000700080009",
}

# 机器码按 4 位分组展示，方便客户口头/微信报给厂商
_GROUP_SIZE = 4

# ---------------------------------------------------------------------------
# 「测试模式」口令（唯一能关闭授权校验的取值）
# ---------------------------------------------------------------------------
# v2 起授权**默认强制**：``license.enabled`` 不再接受 0/false/空 这类「关掉」，
# 任何值都按「需要授权」处理（fail-closed）。只有等于下面这个口令才进测试模式。
#
# 这里**只存口令的 SHA256**，不存明文——交付出去的程序目录里搜不到口令，
# 客户即使打开源码也拿不到；明文只出现在不随程序分发的内部维护文档里。
# 生成方式（内部维护用）::
#
#     hashlib.sha256((TOKEN_SALT + "明文口令").encode()).hexdigest()
#
# 改口令时同步更新 ``DEV_TOKEN_PREFIX``（仅用于界面/日志里指代，前 8 位即可）。
TOKEN_SALT = "kefu-license-dev-token-v1|"
DEV_TOKEN_SHA256 = (
    "8528a3998caa172797da9c1f71c704a783f6416b722192f40d7fc78120131c7f")
DEV_TOKEN_PREFIX = "8528a399"

# 强制授权：环境变量（**只认环境变量，不读数据库**）。
# 交付脚本把它设上后，即使客户猜到了测试口令也进不了测试模式——
# 数据库/配置文件被改都不会解除。取值 enforce/1/on/yes/true 生效。
FORCE_ENV = "KEFU_LICENSE_MODE"


def token_digest(raw: str) -> str:
    """口令的 SHA256（十六进制小写）。"""
    return hashlib.sha256((TOKEN_SALT + str(raw).strip()).encode("utf-8")
                          ).hexdigest()


def is_dev_token(raw: Any) -> bool:
    """该取值是否为「测试模式口令」。

    接受两种等价写法：

    - 口令明文（内部维护文档里那个特殊值）；
    - 口令指纹（64 位十六进制 SHA256，``run.py --license-dev`` 写进库的就是它，
      这样数据库里不出现明文）。

    用 ``hmac.compare_digest`` 比较，避免长度/前缀差异造成的时序泄漏
    （强度要求不高，但免费）。
    """
    text = str(raw or "").strip()
    if not text:
        return False
    if hmac.compare_digest(token_digest(text), DEV_TOKEN_SHA256):
        return True
    # 指纹形式：先比长度，避免把中文等非 ASCII 值喂给 compare_digest
    # （对 str 做 compare_digest 遇到非 ASCII 会抛 TypeError）
    candidate = text.lower()
    if len(candidate) != len(DEV_TOKEN_SHA256):
        return False
    return hmac.compare_digest(candidate.encode("utf-8"),
                               DEV_TOKEN_SHA256.encode("utf-8"))


def force_enforced() -> bool:
    """是否被环境变量强制要求授权（优先级高于测试口令）。"""
    raw = os.environ.get(FORCE_ENV, "").strip().lower()
    return raw in ("enforce", "1", "on", "yes", "true")


class LicenseError(Exception):
    """授权接口调用/许可证处理失败（中文消息可直接展示给用户）。

    Attributes:
        code: 机器可读错误码（前端按码做差异化提示，如 ``network`` /
            ``invalid_code`` / ``machine_mismatch``）。
    """

    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# 时间工具（一律带时区，避免夏令时/跨时区比较出错）
# ---------------------------------------------------------------------------
def now() -> datetime:
    """当前本地时间（带时区）。"""
    return datetime.now().astimezone()


def parse_dt(value: Any) -> Optional[datetime]:
    """解析 ISO8601 时间串；兼容末尾 ``Z``；失败返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:      # 服务端漏带时区时按本地时区解释
        dt = dt.astimezone()
    return dt


def _ts(dt: Optional[datetime]) -> Optional[float]:
    """datetime → UTC 时间戳（None 透传）。"""
    return None if dt is None else dt.timestamp()


# ---------------------------------------------------------------------------
# 机器码：硬件指纹
# ---------------------------------------------------------------------------
def _clean_hw(value: Any) -> str:
    """归一化单个硬件标识；占位值/无效值返回空串。

    统一去空白、转大写、压缩内部连续空格——同一块主板用不同 API 读出来的
    大小写/空格经常不一致，不归一化会导致重启一次机器码就变。
    """
    text = " ".join(str(value or "").split()).strip().upper()
    if text.lower() in _INVALID_HW_VALUES:
        return ""
    return text


def _run_powershell(script: str, timeout: float = 15.0) -> str:
    """执行 PowerShell 片段并返回 stdout（失败返回空串，绝不让授权拖垮启动）。

    **只用于排障深探**（:func:`machine_info` 的 WMI 明细），机器码计算本身
    不依赖它——见 :func:`collect_fingerprint` 的说明。
    """
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return ""
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("读取硬件信息失败（PowerShell）：%s", exc)
        return ""
    if proc.returncode != 0:
        logger.warning("读取硬件信息失败（退出码 %s）：%s",
                       proc.returncode, (proc.stderr or "").strip()[:200])
        return ""
    return (proc.stdout or "").strip()


def _win_machine_guid() -> str:
    """Windows 安装 ID（注册表 MachineGuid）。

    为什么用它当主成分：**永远可读、毫秒级、不依赖 WMI**。实测本机 WMI 仓库
    是坏的（``Get-CimInstance Win32_BaseBoard`` 直接报「无效类」），而主板
    UUID 只能从 WMI 取；把 WMI 结果算进机器码的后果是「WMI 一旦损坏，全体
    客户机器码同时变化、授权集体失效」。稳定优先，所以 WMI 只用于排障展示。
    """
    try:
        import winreg
    except ImportError:          # 非 Windows
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
    except OSError:
        return ""
    return _clean_hw(value)


def _win_volume_serial(root: str = "C:\\") -> str:
    """系统盘卷序列号（Win32 API 直读，无子进程、无 WMI）。

    换硬盘/格式化系统盘会变——这在业务上等价于换机，需要重新授权，可接受。
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return ""
    try:
        serial = wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), None, 0, ctypes.byref(serial),
            None, None, None, 0)
    except (OSError, AttributeError):
        return ""
    if not ok:
        return ""
    return f"{serial.value:08X}"


def _collect_posix() -> dict[str, str]:
    """Linux/macOS 兜底：systemd 机器 ID + 主机名。"""
    out: dict[str, str] = {}
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cleaned = _clean_hw(text)
        if cleaned:
            out["machine_guid"] = cleaned
            break
    node = _clean_hw(platform.node())
    if node:
        out["host"] = node
    return out


def collect_fingerprint() -> dict[str, str]:
    """采集参与机器码计算的标识（已归一化；取不到的项不出现）。

    **设计约束：只用「永远可读」的标识。**
    Windows 上只有两项：注册表 MachineGuid + 系统盘卷序列号。二者都不经过
    WMI、不起子进程，实测耗时 < 10 毫秒且不会因系统组件损坏而失效——机器码
    一旦随环境漂移，客户就要重新申请授权，这是最不能接受的故障。

    WMI 的主板 UUID / CPU ID / 主板序列号更"硬件级"，但**刻意不参与哈希**
    （本机 WMI 已损坏，且 WMI 仓库被 Windows 更新打坏并不罕见）。它们只在
    :func:`machine_info` 里作为排障信息展示。

    防「整盘 Ghost 到另一台机器」靠服务端策略（并发数限制/异常登录告警），
    不靠客户端指纹——客户端永远可能被绕过。
    """
    parts: dict[str, str] = {}
    if os.name == "nt":
        guid = _win_machine_guid()
        if guid:
            parts["machine_guid"] = guid
        volume = _win_volume_serial()
        if volume:
            parts["volume_serial"] = volume
    else:
        parts.update(_collect_posix())
    if not parts:
        node = _clean_hw(platform.node())
        if node:
            parts["host"] = node
    return parts


_MACHINE_CODE_CACHE: dict[str, str] = {}
_MACHINE_CODE_LOCK = threading.Lock()


def machine_code(force: bool = False) -> str:
    """本机机器码（形如 ``A1B2-C3D4-...``，32 位十六进制分 8 组）。

    进程内缓存：硬件不会在运行期变化。``force=True`` 用于「重新读取」按钮。
    """
    with _MACHINE_CODE_LOCK:
        if not force and _MACHINE_CODE_CACHE.get("code"):
            return _MACHINE_CODE_CACHE["code"]
    parts = collect_fingerprint()
    # 顺序固定，保证同一台机器每次算出同一个码
    ordered = [parts.get(k, "")
               for k in ("machine_guid", "volume_serial", "host")]
    material = "|".join(ordered).strip("|")
    if not material:
        # 极端兜底：连注册表和卷序列号都读不到（权限/非标准环境）。
        # 稳定性差但不至于完全无法授权，日志明确告警便于人工介入。
        material = f"{platform.node()}|{os.environ.get('USERNAME', '')}"
        logger.warning("未能读取任何系统标识，机器码退化为主机名+用户名"
                       "（换用户名后会变化，请人工核对）")
    digest = hashlib.sha256(f"kefu-license-v2|{material}".encode("utf-8"))
    hexed = digest.hexdigest()[:32].upper()
    code = "-".join(hexed[i:i + _GROUP_SIZE]
                    for i in range(0, len(hexed), _GROUP_SIZE))
    with _MACHINE_CODE_LOCK:
        _MACHINE_CODE_CACHE["code"] = code
    logger.info("本机机器码：%s（成分：%s）", code,
                "、".join(k for k in ("machine_guid", "volume_serial", "host")
                         if parts.get(k)) or "退化主机名")
    return code


def machine_info(deep: bool = False, code: Optional[str] = None) -> dict:
    """机器码 + 采集到的成分（排障/客服核对用，不含敏感信息）。

    Args:
        deep: 是否额外尝试 WMI 明细（主板 UUID / CPU / 主板序列号）。WMI 不
            可用时这一步会等 5~15 秒，**只在「查看详细信息」时传 True**。
        code: 直接指定机器码（API 层用注入的客户端算，便于测试替换）。
    """
    parts = collect_fingerprint()
    info: dict[str, Any] = {
        "machine_code": code or machine_code(),
        "platform": f"{platform.system()} {platform.release()}",
        "components": sorted(parts.keys()),
    }
    if deep and os.name == "nt":
        raw = _run_powershell(
            "$ErrorActionPreference='SilentlyContinue';"
            "$cs=Get-CimInstance Win32_ComputerSystemProduct;"
            "$bb=Get-CimInstance Win32_BaseBoard;"
            "$cpu=Get-CimInstance Win32_Processor | Select-Object -First 1;"
            "[pscustomobject]@{uuid=$cs.UUID;board=$bb.SerialNumber;"
            "cpu=$cpu.ProcessorId} | ConvertTo-Json -Compress")
        try:
            info["wmi"] = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            info["wmi"] = {}
        info["wmi_note"] = ("WMI 仅作排障展示，不参与机器码计算"
                           "（避免 WMI 损坏导致机器码漂移）")
    return info


# ---------------------------------------------------------------------------
# 签名与验签
# ---------------------------------------------------------------------------
def payload_bytes(payload: dict) -> bytes:
    """许可证签名原文的规范化序列化（**必须与服务端一致**）。

    sort_keys + 紧凑分隔符 + 不转义中文：这三项任何一项不一致，签名就对不上。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def verify_license(doc: Any) -> tuple[bool, str]:
    """验签并做基本结构校验，返回 (是否通过, 中文原因)。"""
    if not isinstance(doc, dict):
        return False, "许可证格式错误（不是 JSON 对象）"
    payload = doc.get("payload")
    signature = doc.get("signature")
    key_id = str(doc.get("key_id") or "dev")
    if not isinstance(payload, dict) or not payload:
        return False, "许可证缺少 payload 内容"
    if not signature:
        return False, "许可证缺少 signature 签名"
    pub_b64 = PUBLIC_KEYS.get(key_id)
    if not pub_b64:
        return False, f"许可证密钥标识未知：{key_id}（客户端版本可能过旧）"
    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(pub_b64))
        public_key.verify(base64.b64decode(str(signature)),
                          payload_bytes(payload))
    except InvalidSignature:
        return False, "许可证签名校验失败（文件被修改过或不是本系统签发）"
    except (ValueError, TypeError) as exc:
        return False, f"许可证签名数据损坏：{exc}"
    return True, ""


# ---------------------------------------------------------------------------
# 本地文件读写（原子写，避免断电留下半个 JSON）
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> Optional[dict]:
    """读取 JSON 文件；不存在/损坏返回 None（不抛异常）。"""
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write_json(path: Path, data: dict) -> None:
    """原子写 JSON：先写临时文件再替换，避免中途崩溃留下坏文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
@dataclass
class LicenseStatus:
    """授权状态快照（API 直接返回它，前端按 state 决定界面）。"""

    enabled: bool = False
    state: str = "disabled"
    ok: bool = True
    # 是否处于「厂商测试模式」（= 授权校验被口令显式关闭）。
    # 前端据此显示醒目的红色横幅——交付件里忘了关测试模式，一眼就能发现。
    dev_mode: bool = False
    machine_code: str = ""
    license_id: str = ""
    customer: str = ""
    edition: str = ""
    issued_at: str = ""
    expires_at: str = ""
    days_left: Optional[int] = None
    grace_days: int = DEFAULT_GRACE_DAYS
    last_ok_at: str = ""
    offline: bool = False
    server_url: str = ""
    message: str = ""
    # 「是否启用授权」这个开关的生效来源：env / db / default。
    # 现场最常踩的坑是改了 app/config.py 的默认值却不见效——因为 settings 表
    # 用 INSERT OR IGNORE 播种，已有库里那行 license.enabled=0 会一直盖住代码
    # 默认值。把来源透出到界面，用户一眼能看出该改哪里。
    enabled_source: str = ""
    # 最近一次联网确认失败的原因（成功时清空）。界面据此提示"数据可能已过期"，
    # 否则服务端记录被删/吊销时，客户端在宽限期内仍显示「授权正常」，很难解释。
    last_error: str = ""

    def to_dict(self) -> dict:
        """转 dict（API 响应体）。"""
        return asdict(self)


_STATE_MESSAGES = {
    "disabled": ("厂商测试模式：授权校验已关闭（仅限内部调试，交付客户前请执行 "
                 "`python run.py --license-on` 恢复校验）"),
    "active": "授权正常",
    "offline_grace": "离线运行中：暂时连不上授权服务器，宽限期内可继续使用",
    "unlicensed": "尚未激活，请输入激活码完成授权",
    "expired": "授权已到期，请续费后重新激活",
    "offline_expired": "离线时间超过宽限期，请联网后点击「刷新授权」",
    "invalid": "许可证无效，请重新激活",
    "revoked": "授权已被吊销，请联系供应商",
    "clock_rollback": "检测到系统时间被调早，请校正系统时间后重试",
}

# 「是否启用授权」开关的来源说明（拼在状态提示里）
_SOURCE_LABELS = {
    "env-force": f"环境变量 {FORCE_ENV}（强制授权，无视测试口令）",
    "env": "环境变量 KEFU_LICENSE_ENABLED",
    "db": "数据库设置 license.enabled",
    "default": "app/config.py 的默认值",
}


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
class LicenseClient:
    """授权客户端（配置读 settings 表，可注入替身便于测试）。

    Args:
        db: 数据库对象（只用 ``get_setting``）；None 时配置全部走
            环境变量 + 默认值（单元测试常见用法）。
        http_post: 注入的 HTTP POST 实现，签名
            ``(url, payload, timeout) -> dict``；None 时用 urllib 真实请求。
        machine_code_provider: 注入的机器码函数（测试里固定机器码）。
        license_path / state_path: 许可证与状态文件路径（测试用临时目录）。
    """

    def __init__(self, db: Any = None,
                 http_post: Optional[Any] = None,
                 machine_code_provider: Optional[Any] = None,
                 license_path: Optional[Path] = None,
                 state_path: Optional[Path] = None) -> None:
        self._db = db
        self._http_post = http_post
        self._machine_code_provider = machine_code_provider or machine_code
        self.license_path = Path(license_path or config.LICENSE_FILE)
        self.state_path = Path(state_path or config.LICENSE_STATE_FILE)
        self._doc_cache: Optional[dict] = None
        self._doc_mtime: float = -1.0
        self._lock = threading.RLock()
        # 内存中的历史最大时间戳（避免每次 status 都写盘）
        self._max_seen_ts: Optional[float] = None

    # -- 配置 ----------------------------------------------------------
    def machine_code(self) -> str:
        """本机机器码（走注入的 provider，便于测试与环境适配）。"""
        return self._machine_code_provider()

    def _setting(self, key: str, default: str = "") -> str:
        """读配置：settings 表 > 环境变量 > 默认值。

        环境变量优先级更高，方便客户现场不打开界面就切换授权服务器
        （部署脚本里 ``set KEFU_LICENSE_SERVER=https://license.xxx.com``）。
        """
        env_map = {
            config.KEY_LICENSE_ENABLED: "KEFU_LICENSE_ENABLED",
            config.KEY_LICENSE_SERVER_URL: "KEFU_LICENSE_SERVER",
        }
        env_name = env_map.get(key)
        if env_name:
            env_val = os.environ.get(env_name, "").strip()
            if env_val:
                return env_val
        if self._db is not None:
            try:
                value = self._db.get_setting(key, default)
            except Exception:      # 库未就绪/被锁：退回默认，绝不抛给调用方
                value = default
            if value:
                return str(value)
        return default

    def mode(self) -> str:
        """授权模式：``"enforce"``（默认，必须授权）或 ``"dev"``（测试模式）。

        判定顺序（**任何一步「不确定」都按 enforce 处理，绝不默认放行**）：

        1. 环境变量 ``KEFU_LICENSE_MODE=enforce`` → 强制授权，后面都不看。
           交付脚本带上它，客户就算知道测试口令也关不掉授权。
        2. ``license.enabled`` 的取值（环境变量 > settings 表 > 代码默认值）
           **恰好等于测试口令** → 测试模式。
        3. 其余一切取值（``1`` / ``0`` / ``false`` / 空 / 乱写）→ 需要授权。

        v2 之前 ``0``/``false`` 是「关闭授权」，客户改一下数据库就白用了；
        现在这条捷径已经堵死，只有知道口令才能关。
        """
        if force_enforced():
            return "enforce"
        raw = self._setting(config.KEY_LICENSE_ENABLED, "1")
        return "dev" if is_dev_token(raw) else "enforce"

    def enabled(self) -> bool:
        """是否启用授权校验（``mode() != "dev"``）。"""
        return self.mode() != "dev"

    def enabled_source(self) -> str:
        """「是否启用授权」这个开关到底来自哪里（排障用）。

        Returns:
            ``"env-force"``：环境变量 ``KEFU_LICENSE_MODE=enforce``（强制授权）；
            ``"env"``：环境变量 ``KEFU_LICENSE_ENABLED``；
            ``"db"``：settings 表的 ``license.enabled`` 行；
            ``"default"``：都没给，用 ``app/config.py`` 的默认值。
        """
        if force_enforced():
            return "env-force"
        env_val = os.environ.get("KEFU_LICENSE_ENABLED", "").strip()
        if env_val:
            return "env"
        if self._db is not None:
            try:
                stored = self._db.get_setting(config.KEY_LICENSE_ENABLED, "")
            except Exception:      # 库未就绪/被锁：按默认值处理
                stored = ""
            if str(stored or "").strip():
                return "db"
        return "default"

    def server_url(self) -> str:
        """授权服务器地址（去掉末尾斜杠）。"""
        return (self._setting(config.KEY_LICENSE_SERVER_URL,
                              DEFAULT_SERVER_URL).strip().rstrip("/")
                or DEFAULT_SERVER_URL)

    def refresh_interval_hours(self) -> int:
        """联网确认间隔（小时），非法值退回默认。"""
        raw = self._setting(config.KEY_LICENSE_REFRESH_INTERVAL, "")
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            return DEFAULT_REFRESH_HOURS
        return value if value > 0 else DEFAULT_REFRESH_HOURS

    def grace_days_default(self) -> int:
        """本地兜底宽限天数（许可证 payload 里有 ``grace_days`` 时以它为准）。"""
        try:
            value = int(float(self._setting(config.KEY_LICENSE_GRACE_DAYS, "")))
        except (TypeError, ValueError):
            return DEFAULT_GRACE_DAYS
        return value if value >= 0 else DEFAULT_GRACE_DAYS

    # -- 本地文件 ------------------------------------------------------
    def _load_doc(self) -> Optional[dict]:
        """读许可证（按 mtime 缓存，避免每次状态查询都读盘解析）。"""
        with self._lock:
            try:
                mtime = self.license_path.stat().st_mtime
            except OSError:
                self._doc_cache, self._doc_mtime = None, -1.0
                return None
            if self._doc_cache is not None and mtime == self._doc_mtime:
                return self._doc_cache
            doc = _read_json(self.license_path)
            self._doc_cache, self._doc_mtime = doc, mtime
            return doc

    def _load_state(self) -> dict:
        """读本地状态（last_ok_at / max_seen_ts / revoked 标记）。"""
        return _read_json(self.state_path) or {}

    def _save_state(self, **fields: Any) -> None:
        """合并写入状态文件（原子写；失败只告警不影响主流程）。"""
        state = self._load_state()
        state.update({k: v for k, v in fields.items() if v is not None})
        try:
            _atomic_write_json(self.state_path, state)
        except OSError as exc:
            logger.warning("写入授权状态文件失败（已忽略）：%s", exc)

    # -- 状态判定 ------------------------------------------------------
    def status(self, touch: bool = False) -> LicenseStatus:
        """计算当前授权状态（纯本地判断，不发网络请求）。

        Args:
            touch: 是否顺便更新 ``max_seen_ts``（启动/联网成功时用；普通查询
                传 False，避免 5 秒一次的状态轮询频繁写盘）。
        """
        machine = self._machine_code_provider()
        if not self.enabled():
            src = self.enabled_source()
            return LicenseStatus(
                enabled=False, state="disabled", ok=True, dev_mode=True,
                machine_code=machine, server_url=self.server_url(),
                enabled_source=src,
                message=(f"{_STATE_MESSAGES['disabled']}；口令来自"
                         f"{_SOURCE_LABELS.get(src, src)}"
                         f"（口令指纹 {DEV_TOKEN_PREFIX}）"))

        base = dict(enabled=True, machine_code=machine,
                    server_url=self.server_url(),
                    enabled_source=self.enabled_source(),
                    grace_days=self.grace_days_default())

        doc = self._load_doc()
        if not doc:
            return self._finish(LicenseStatus(state="unlicensed", ok=False,
                                              **base))

        valid, reason = verify_license(doc)
        if not valid:
            logger.warning("许可证无效：%s", reason)
            return self._finish(LicenseStatus(
                state="invalid", ok=False, message=reason, **base))

        payload = doc.get("payload") or {}
        bound = str(payload.get("machine_code") or "").strip()
        if bound and bound != machine:
            logger.warning("许可证绑定机器码不符：许可证 %s / 本机 %s",
                           bound, machine)
            return self._finish(LicenseStatus(
                state="invalid", ok=False,
                message="许可证与本机不匹配（机器码不一致），请用本机机器码"
                        "重新申请授权", **base))

        expires = parse_dt(payload.get("expires_at"))
        issued = parse_dt(payload.get("issued_at"))
        grace = payload.get("grace_days")
        if isinstance(grace, (int, float)) and int(grace) >= 0:
            base["grace_days"] = int(grace)

        state_data = self._load_state()
        last_ok = parse_dt(state_data.get("last_ok_at"))
        revoked_at = str(state_data.get("revoked_at") or "").strip()

        detail = dict(
            license_id=str(payload.get("license_id") or ""),
            customer=str(payload.get("customer") or ""),
            edition=str(payload.get("edition") or ""),
            issued_at=issued.isoformat() if issued else "",
            expires_at=expires.isoformat() if expires else "",
            last_ok_at=last_ok.isoformat() if last_ok else "",
            last_error=str(state_data.get("last_error") or ""),
        )
        merged = {**base, **detail}

        if revoked_at:
            return self._finish(LicenseStatus(
                state="revoked", ok=False,
                message=f"授权已于 {revoked_at} 被吊销，请联系供应商", **merged))

        if expires is None:
            return self._finish(LicenseStatus(
                state="invalid", ok=False,
                message="许可证缺少有效的到期时间 expires_at", **merged))

        current = now()
        # 时钟回拨检测：见过的最大时间戳比当前时间超前 1 天以上
        max_seen = self._max_seen_ts
        if max_seen is None:
            stored = state_data.get("max_seen_ts")
            max_seen = (float(stored)
                        if isinstance(stored, (int, float)) else None)
        if touch:
            previous = max_seen or 0
            self._max_seen_ts = max(previous, current.timestamp())
            if self._max_seen_ts > previous:
                self._save_state(max_seen_ts=self._max_seen_ts)
            max_seen = self._max_seen_ts
        if max_seen and current.timestamp() < (
                max_seen - CLOCK_ROLLBACK_TOLERANCE_SEC):
            return self._finish(LicenseStatus(
                state="clock_rollback", ok=False, **merged))

        if current > expires:
            return self._finish(LicenseStatus(
                state="expired", ok=False, days_left=0, **merged))

        days_left = max(0, (expires - current).days)
        if last_ok is None:
            # 有签名许可证但从没联过服务器：正常流程不可能出现（激活成功即写
            # last_ok_at）。保守放行到到期日，只提示一次，避免误锁。
            logger.info("许可证缺少 last_ok_at，按已确认处理（仅到期日约束）")
            return self._finish(LicenseStatus(
                state="active", ok=True, days_left=days_left,
                offline=True, **merged))

        offline_days = (current - last_ok).total_seconds() / 86400
        if offline_days > base["grace_days"]:
            return self._finish(LicenseStatus(
                state="offline_expired", ok=False, days_left=days_left,
                offline=True, **merged))
        if offline_days >= 1:
            return self._finish(LicenseStatus(
                state="offline_grace", ok=True, days_left=days_left,
                offline=True, **merged))
        return self._finish(LicenseStatus(
            state="active", ok=True, days_left=days_left, **merged))

    @staticmethod
    def _finish(status: LicenseStatus) -> LicenseStatus:
        """补齐 message（未显式给原因时按 state 取默认文案）。"""
        if not status.message:
            status.message = _STATE_MESSAGES.get(status.state, "")
        return status

    def needs_refresh(self) -> bool:
        """是否需要向服务器确认一次（距上次成功确认超过刷新间隔）。"""
        if not self.enabled():
            return False
        last_ok = parse_dt(self._load_state().get("last_ok_at"))
        if last_ok is None:
            return True
        age_hours = (now() - last_ok).total_seconds() / 3600
        return age_hours >= self.refresh_interval_hours()

    # -- 网络 ----------------------------------------------------------
    def _post(self, path: str, payload: dict) -> dict:
        """POST 到授权服务器，返回解析后的 JSON（失败抛 LicenseError）。"""
        url = self.server_url() + path
        poster = self._http_post or _http_post_json
        try:
            data = poster(url, payload, HTTP_TIMEOUT_SEC)
        except LicenseError:
            raise
        except URLError as exc:
            raise LicenseError(
                f"连接授权服务器失败：{getattr(exc, 'reason', exc)}",
                code="network") from exc
        except Exception as exc:       # 超时/JSON 坏等都归为网络问题
            raise LicenseError(f"授权服务器请求失败：{exc}",
                               code="network") from exc
        if not isinstance(data, dict):
            raise LicenseError("授权服务器返回格式错误", code="bad_response")
        return data

    @staticmethod
    def _server_error(data: dict) -> LicenseError:
        """从错误响应里提取 code/message。

        兼容两种包装：服务端约定的顶层 ``{"error": {...}}``，以及 FastAPI
        ``HTTPException`` 自动包出来的 ``{"detail": {"error": {...}}}``——
        后台同学用 FastAPI 实现时很容易写成后者，客户端两种都认。
        """
        code, message = extract_error(data)
        return LicenseError(message or "授权服务器拒绝了请求",
                            code=code or "server_error")

    def _install_license(self, data: dict) -> dict:
        """接收服务端返回的许可证：验签 + 机器码核对后落盘。"""
        raw_license = data.get("license")
        doc = raw_license if isinstance(raw_license, dict) else data
        valid, reason = verify_license(doc)
        if not valid:
            raise LicenseError(f"服务器返回的许可证无效：{reason}",
                               code="bad_license")
        payload = doc.get("payload") or {}
        machine = self._machine_code_provider()
        bound = str(payload.get("machine_code") or "").strip()
        if bound and bound != machine:
            raise LicenseError(
                "服务器签发的许可证绑定了其它机器码，请核对机器码是否一致",
                code="machine_mismatch")
        try:
            _atomic_write_json(self.license_path, doc)
        except OSError as exc:
            raise LicenseError(f"保存许可证失败：{exc}", code="io") from exc
        self._doc_cache = doc
        try:
            self._doc_mtime = self.license_path.stat().st_mtime
        except OSError:
            self._doc_mtime = -1.0
        current = now().isoformat()
        self._save_state(last_ok_at=current, last_error="",
                         revoked_at="",
                         max_seen_ts=time.time())
        self._max_seen_ts = time.time()
        logger.info("授权已生效：%s（到期 %s）",
                    payload.get("license_id"), payload.get("expires_at"))
        return doc

    def activate(self, activation_code: str = "") -> LicenseStatus:
        """用激活码激活本机（联网请求服务器签发许可证）。

        Args:
            activation_code: 激活码；留空时用配置里的预置激活码（批量部署时
                由部署脚本写入 settings 或环境变量）。
        """
        if not self.enabled():
            return self.status()
        code = (activation_code or "").strip() or self._setting(
            config.KEY_LICENSE_ACTIVATION_CODE, "").strip()
        machine = self._machine_code_provider()
        data = self._post("/api/v1/license/activate", {
            "machine_code": machine,
            "activation_code": code,
            "app_version": config.APP_VERSION,
            "os": f"{platform.system()} {platform.release()}",
        })
        # 有的网关把业务错误包在 200 响应里（不抛 4xx），这里统一兜住
        if data.get("error") or not data.get("ok", True):
            raise self._server_error(data)
        self._install_license(data)
        return self.status(touch=True)

    def refresh(self) -> LicenseStatus:
        """向服务器确认一次（心搏）。成功刷新 last_ok_at，失败改判离线状态。

        网络失败**不抛异常**：调用方（后台线程/前端按钮）只需要拿回状态。
        """
        if not self.enabled():
            return self.status()
        doc = self._load_doc()
        payload = (doc or {}).get("payload") or {}
        machine = self._machine_code_provider()
        try:
            data = self._post("/api/v1/license/heartbeat", {
                "machine_code": machine,
                "license_id": str(payload.get("license_id") or ""),
                "app_version": config.APP_VERSION,
            })
        except LicenseError as exc:
            logger.warning("授权联网确认失败（%s）：%s", exc.code, exc)
            self._save_state(last_error=str(exc))
            if exc.code == "revoked":
                self._save_state(revoked_at=now().strftime("%Y-%m-%d %H:%M"))
                return self.status(touch=True)
            return self.status(touch=True)
        if not data.get("ok", True):
            err = self._server_error(data)
            self._save_state(last_error=str(err))
            if err.code == "revoked":
                self._save_state(revoked_at=now().strftime("%Y-%m-%d %H:%M"))
            return self.status(touch=True)
        # 服务端可能顺带续期：返回体里带 license 时覆盖本地
        if isinstance(data.get("license"), dict):
            try:
                self._install_license(data)
            except LicenseError as exc:
                logger.warning("服务器续期许可证无效（保留原证）：%s", exc)
        self._save_state(last_ok_at=now().isoformat(), last_error="")
        return self.status(touch=True)


# ---------------------------------------------------------------------------
# 默认 HTTP 实现（urllib，不引入额外依赖）
# ---------------------------------------------------------------------------
def extract_error(data: Any) -> tuple[str, str]:
    """从服务端错误响应里取 ``(code, message)``；取不到返回空串。

    支持两种包装（见 :meth:`LicenseClient._server_error`）：顶层
    ``{"error": {...}}`` 与 FastAPI 的 ``{"detail": {"error": {...}}}``。
    """
    if not isinstance(data, dict):
        return "", ""
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or ""), str(error.get("message") or "")
    detail = data.get("detail")
    if isinstance(detail, dict):
        inner = detail.get("error")
        if isinstance(inner, dict):
            return (str(inner.get("code") or ""),
                    str(inner.get("message") or ""))
        return "", str(detail.get("message") or "")
    if isinstance(detail, str) and detail:
        return "", detail          # 只有一句中文原因（无错误码）
    return "", ""


def _http_post_json(url: str, payload: dict, timeout: float) -> dict:
    """POST JSON 并解析响应；HTTP 错误也尝试解析 body 里的中文原因。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": f"kefu-system/{config.APP_VERSION}",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:       # 读不到 body 就按状态码给通用文案
            pass
        data: Any = None
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = None
        code, message = extract_error(data)
        raise LicenseError(message or f"授权服务器返回 HTTP {exc.code}",
                           code=code or "http_error")
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise LicenseError("授权服务器返回的不是 JSON", code="bad_response") \
            from exc


# ---------------------------------------------------------------------------
# 进程级单例（API 依赖注入用，测试可覆盖）
# ---------------------------------------------------------------------------
_client: Optional[LicenseClient] = None
_client_lock = threading.Lock()


def get_client() -> LicenseClient:
    """进程级授权客户端（未初始化时用无 db 的默认实例）。"""
    global _client
    with _client_lock:
        if _client is None:
            _client = LicenseClient()
        return _client


def set_client(client: Optional[LicenseClient]) -> None:
    """注入/重置客户端（main.py 注入带 db 的实例；测试重置）。"""
    global _client
    with _client_lock:
        _client = client


def reset_machine_code_cache() -> None:
    """清空机器码缓存（测试与「重新读取」按钮用）。"""
    with _MACHINE_CODE_LOCK:
        _MACHINE_CODE_CACHE.clear()
