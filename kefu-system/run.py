# -*- coding: utf-8 -*-
"""kefu-system 启动入口。

用法（在 kefu-system 目录下）::

    python run.py                      # 默认 127.0.0.1:8765
    python run.py --port 9000          # 命令行参数优先（仅本次启动生效）
    python run.py --port 9000 --save   # 保存端口到配置库，之后直接 python run.py 即用 9000
    KEFU_PORT=9000 python run.py       # 其次读环境变量
    python run.py --port 8765 --save   # 恢复默认端口（把 8765 写回配置库）

电脑授权（默认强制，未激活只锁功能、不动数据）::

    python run.py --license            # 交付客户用：强制授权（环境变量优先级最高）
    python run.py --license-dev 口令   # 内部调试：切到测试模式（关闭授权校验）
    python run.py --license-on         # 交付前：切回强制授权（清掉测试模式）

监听地址优先级：**命令行参数 > 环境变量 > 已保存配置 > 默认值**。
默认仍是 127.0.0.1:8765（前端 dev 代理也默认指向该端口，不改默认行为）。

退出（Ctrl+C）行为：uvicorn 先等未完成的请求收尾，但**最多等 5 秒**
（``GRACEFUL_SHUTDOWN_SEC``），超时直接取消；之后跑 lifespan shutdown
（停监听、刷 watermark、关库），最后用 ``os._exit`` 立即结束，不等第三方
（UIA/COM）残留线程。万一 20 秒还没退，会自动把所有线程的 Python 栈打印到
控制台并写进 ``data/shutdown-dump-<时间>.log``，然后强制结束——不会再出现
「按了 Ctrl+C 卡住、进程还在后台跑」的情况。

``--save`` 会把本次**最终解析出**的 host 与 port 写入配置库（settings 表的
``run.host`` / ``run.port`` 键），下次启动不再需要重复指定；不带 ``--save``
时端口只对本次启动生效。注意：配置库由系统首次正常启动时创建，若尚未启动过
（配置库不存在），``--save`` 会拒绝保存而不是替你建库。

环境变量：
- ``KEFU_HOST``：监听地址（默认 127.0.0.1）
- ``KEFU_PORT``：监听端口（默认 8765，合法范围 1~65535）

前端联调提示：前端 dev 模式的代理 target 读取 ``KEFU_PORT`` 环境变量；若
持久化端口 ≠ 8765，前端开发时需同步 ``set KEFU_PORT=...``；生产为后端
同源托管，不受影响。
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

import uvicorn

from app.config import DB_PATH

# 默认监听地址（**不得改变**：前端 dev 代理与生产静态托管均默认指向它）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Ctrl+C 之后：uvicorn 最多等这么久就让「还没结束的请求/任务」强制取消。
# 不设（uvicorn 默认 None）= **无限等**，一个卡住的同步请求就能让进程永远停在
# "Shutting down"，而且 lifespan 的 shutdown（停监听、刷 watermark、关库）
# 根本不会执行 —— 2026-11-13 23:2x 现场就是这么卡住的。
GRACEFUL_SHUTDOWN_SEC = 5
# 再兜一层：Ctrl+C 后这么多秒还没退出，打印全部线程栈再强制结束。
EXIT_WATCHDOG_SEC = 20.0

ENV_HOST = "KEFU_HOST"
ENV_PORT = "KEFU_PORT"

# settings 表配置键（与既有 ai.* / analysis.* 命名风格一致）
SETTING_KEY_HOST = "run.host"
SETTING_KEY_PORT = "run.port"
# 授权模式开关（与 app.config.KEY_LICENSE_ENABLED 同一个键）
SETTING_KEY_LICENSE = "license.enabled"

# 端口/地址来源文案（用于启动日志，让人一眼看出端口从哪来）
ORIGIN_CLI = "命令行参数"
ORIGIN_ENV = "环境变量"
ORIGIN_SAVED = "已保存配置"
ORIGIN_DEFAULT = "默认值"


class ResolvedAddress(NamedTuple):
    """一次地址解析的完整结果（含来源标注，供启动日志与 --save 使用）。"""

    host: str
    port: int
    host_origin: str
    port_origin: str
    save: bool


def _parse_port(value: object, origin: str) -> int:
    """把端口取值解析成合法端口，非法时以退出码 2 结束（中文提示）。

    Args:
        value: 待解析值（命令行字符串 / 环境变量字符串 / 已保存配置 / 默认整数）。
        origin: 取值来源的中文描述（用于错误提示，如「命令行参数 --port」）。

    Returns:
        1~65535 之间的端口号。
    """
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        print(f"错误：{origin} 不是合法整数：{value!r}", file=sys.stderr)
        print(f"端口需为 1~65535 之间的整数，当前默认 {DEFAULT_PORT}。",
              file=sys.stderr)
        raise SystemExit(2)
    if not 1 <= port <= 65535:
        print(f"错误：{origin} 超出范围：{port}（需为 1~65535）", file=sys.stderr)
        print(f"端口需为 1~65535 之间的整数，当前默认 {DEFAULT_PORT}。",
              file=sys.stderr)
        raise SystemExit(2)
    return port


def _build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器（--help 文案与模块 docstring 保持同步）。"""
    parser = argparse.ArgumentParser(
        description=(
            "kefu-system 客服系统（默认监听 127.0.0.1:8765；地址优先级："
            "命令行 > 环境变量 > 已保存配置 > 默认值）"))
    parser.add_argument(
        "--host", default=None,
        help=f"监听地址（默认 {DEFAULT_HOST}，也可用环境变量 {ENV_HOST}）")
    parser.add_argument(
        "--port", default=None,
        help=f"监听端口（默认 {DEFAULT_PORT}，也可用环境变量 {ENV_PORT}）")
    parser.add_argument(
        "--save", action="store_true",
        help="把本次最终解析出的监听地址保存到配置库（run.host/run.port），"
             "下次启动直接生效；恢复默认用 --port 8765 --save")
    parser.add_argument(
        "--license", action="store_true",
        help="本次启动强制启用电脑授权校验（写入环境变量 "
             "KEFU_LICENSE_MODE=enforce，优先级高于测试口令）；"
             "交付给客户的启动脚本建议带上")
    parser.add_argument(
        "--license-dev", metavar="TOKEN", nargs="?", default=None,
        help="把本机切到厂商测试模式（关闭授权校验，界面显示红色横幅）后退出；"
             "必须把测试口令一起传进来（python run.py --license-dev 口令），"
             "这样客户拿到程序也无法自己关闭授权")
    parser.add_argument(
        "--license-on", action="store_true",
        help="把本机切回强制授权（写 license.enabled=1）后退出，"
             "交付客户前用它清掉测试模式")
    return parser


def _resolve_address_detail(
    argv: list[str] | None = None,
    settings_reader: Callable[[str], str] | None = None,
) -> ResolvedAddress:
    """按「命令行 > 环境变量 > 已保存配置 > 默认」解析监听地址（含来源）。

    Args:
        argv: 命令行参数（不含程序名）；None = 取 ``sys.argv[1:]``。
        settings_reader: 从配置库读值的函数（key → value）；None = 不读库
            （兼容既有测试与外部构造），此时优先级退化为
            「命令行 > 环境变量 > 默认」。

    Returns:
        :class:`ResolvedAddress`，host/port 之外还带来源文案与 --save 标记。
    """
    args = _build_parser().parse_args(argv)

    # 端口：命令行 > 环境变量 > 已保存配置 > 默认
    port: int
    if args.port is not None and str(args.port).strip() != "":
        port = _parse_port(args.port, "命令行参数 --port")
        port_origin = ORIGIN_CLI
    else:
        env_port = os.environ.get(ENV_PORT, "").strip()
        if env_port:
            port = _parse_port(env_port, f"环境变量 {ENV_PORT}")
            port_origin = ORIGIN_ENV
        else:
            saved_port = ""
            if settings_reader is not None:
                # 配置缺失/损坏由 reader 端容错（返回空串），这里只解析值
                saved_port = str(settings_reader(SETTING_KEY_PORT) or "").strip()
            if saved_port:
                # 损坏值同样报错退出，提示里注明来源「已保存配置」
                port = _parse_port(saved_port, "已保存配置 run.port")
                port_origin = ORIGIN_SAVED
            else:
                port = DEFAULT_PORT
                port_origin = ORIGIN_DEFAULT

    # 地址：命令行 > 环境变量 > 已保存配置 > 默认（不做格式校验，交给 uvicorn）
    if args.host is not None and str(args.host).strip() != "":
        host = str(args.host).strip()
        host_origin = ORIGIN_CLI
    else:
        env_host = os.environ.get(ENV_HOST, "").strip()
        saved_host = ""
        if not env_host and settings_reader is not None:
            saved_host = str(settings_reader(SETTING_KEY_HOST) or "").strip()
        if env_host:
            host = env_host
            host_origin = ORIGIN_ENV
        elif saved_host:
            host = saved_host
            host_origin = ORIGIN_SAVED
        else:
            host = DEFAULT_HOST
            host_origin = ORIGIN_DEFAULT
    return ResolvedAddress(
        host=host, port=port,
        host_origin=host_origin, port_origin=port_origin,
        save=bool(args.save))


def resolve_address(
    argv: list[str] | None = None,
    settings_reader: Callable[[str], str] | None = None,
) -> tuple[str, int]:
    """按「命令行 > 环境变量 > 已保存配置 > 默认」解析监听地址。

    Args:
        argv: 命令行参数（不含程序名）；None = 取 ``sys.argv[1:]``。
        settings_reader: 从配置库读值的函数；None = 不读库（默认，行为与
            旧版「命令行 > 环境变量 > 默认」完全一致）。

    Returns:
        ``(host, port)``。
    """
    resolved = _resolve_address_detail(argv, settings_reader)
    return resolved.host, resolved.port


def save_address(host: str, port: int, db_path: Path | None = None) -> None:
    """把最终解析出的监听地址写入配置库（settings 表），失败以退出码 2 结束。

    独立成小函数并支持注入 ``db_path``，便于测试；不经过 ``app.db.Database``
    （那里会跑全套迁移，启动入口要保持轻量）。

    Args:
        host: 要保存的监听地址。
        port: 要保存的监听端口。
        db_path: 配置库路径；None = 使用 ``app.config.DB_PATH``。

    Raises:
        SystemExit: 配置库不存在（系统尚未启动过）或写入失败时，退出码 2。
    """
    path = Path(db_path) if db_path is not None else DB_PATH
    if not path.exists():
        # 不要替 Database 建库：避免出现半初始化状态
        print("错误：配置库不存在（系统可能尚未启动过），无法保存。",
              file=sys.stderr)
        print("请先正常启动一次系统后，再使用 --save 保存监听地址。",
              file=sys.stderr)
        raise SystemExit(2)
    try:
        conn = sqlite3.connect(str(path))
        try:
            with conn:      # 事务：两条配置要么都写入，要么都不写
                conn.execute(
                    "INSERT INTO settings (key, value, updated_at) "
                    "VALUES (?, ?, datetime('now','localtime')) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (SETTING_KEY_HOST, host))
                conn.execute(
                    "INSERT INTO settings (key, value, updated_at) "
                    "VALUES (?, ?, datetime('now','localtime')) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (SETTING_KEY_PORT, str(port)))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"错误：保存监听地址失败：{exc}", file=sys.stderr)
        raise SystemExit(2)
    print(f"已保存监听地址 http://{host}:{port}，下次启动生效。")


def _write_saved_setting(key: str, value: str,
                         db_path: Path | str | None = None) -> bool:
    """把单个 settings 值写入配置库；成功返回 True（失败打印原因并返回 False）。

    与 :func:`save_address` 一样只用 ``sqlite3``，不 import ``app.db.Database``
    （那里会跑全套迁移，启动入口要保持轻量）。库不存在时不替它建库。

    Args:
        key: settings 键。
        value: 要写入的值。
        db_path: 配置库路径；None = 使用 ``app.config.DB_PATH``（测试可注入）。
    """
    path = Path(db_path) if db_path is not None else Path(DB_PATH)
    if not path.exists():
        print("错误：配置库不存在（系统可能尚未启动过），无法写入授权开关。",
              file=sys.stderr)
        print("请先正常启动一次系统后，再切换授权模式。", file=sys.stderr)
        return False
    try:
        conn = sqlite3.connect(str(path))
        try:
            with conn:
                conn.execute(
                    "INSERT INTO settings (key, value, updated_at) "
                    "VALUES (?, ?, datetime('now','localtime')) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (key, value))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"错误：写入授权开关失败：{exc}", file=sys.stderr)
        return False
    return True


def license_mode_command(flag: str, db_path: Path | str | None = None,
                         token: str = "") -> int | None:
    """处理 ``--license-dev`` / ``--license-on``（切换授权模式后退出）。

    Args:
        flag: 命中的命令行开关，``"--license-dev"`` 或 ``"--license-on"``。
        db_path: 配置库路径；None = 使用 ``app.config.DB_PATH``（测试可注入）。
        token: ``--license-dev`` 必须带的口令**明文**；不匹配直接拒绝。

    Returns:
        需要退出时的退出码（0 成功 / 2 失败）；``None`` 表示没命中、继续启动。
    """
    from app import license as license_mod

    if flag == "--license-dev":
        # 必须现场出示口令：客户拿到同一份程序也跑不了这条命令，
        # 否则「本机改个开关就免授权」的漏洞等于没堵。
        if not license_mod.is_dev_token(token):
            print("错误：--license-dev 需要正确的测试口令。", file=sys.stderr)
            print("用法：python run.py --license-dev <测试口令>",
                  file=sys.stderr)
            return 2
        # 写入口令**指纹**而不是明文：数据库被客户看到也拿不到可用口令，
        # 且换个口令只需重新生成指纹，不用改业务代码。
        if not _write_saved_setting(SETTING_KEY_LICENSE,
                                    license_mod.DEV_TOKEN_SHA256, db_path):
            return 2
        print("已切换到厂商测试模式：授权校验关闭，无需激活。")
        print("界面会显示红色「测试模式」横幅。交付客户前请执行："
              "python run.py --license-on")
        return 0
    if flag == "--license-on":
        if not _write_saved_setting(SETTING_KEY_LICENSE, "1", db_path):
            return 2
        print("已切回强制授权模式（license.enabled=1）。")
        print("若之前处于测试模式，重启系统后即要求激活。")
        return 0
    return None


def _read_saved_setting(key: str) -> str:
    """从配置库只读读取一个 settings 值；任何缺失/失败都返回空串，不挡启动。

    只用 ``sqlite3`` 做只读查询，不 import ``app.db.Database``（其初始化会
    跑全套迁移，启动入口要保持轻量）。库文件不存在 / 表不存在 / 表锁 /
    权限问题时一律返回空串，继续用默认值。
    """
    path = Path(DB_PATH)
    if not path.exists():
        return ""
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row else ""


def _format_origin_note(resolved: ResolvedAddress) -> str:
    """拼装启动日志里的来源标注（端口必标；host 仅在非默认时标注）。"""
    parts = [f"端口来源：{resolved.port_origin}"]
    if resolved.host != DEFAULT_HOST:
        parts.append(f"监听地址来源：{resolved.host_origin}")
    return f"（{'；'.join(parts)}）"


# ---------------------------------------------------------------------------
# 退出诊断：Ctrl+C 卡在 "Shutting down" 时抓线程栈并强制结束
# ---------------------------------------------------------------------------
_watchdog_lock = threading.Lock()
_watchdog_started = False


def _dump_all_threads(reason: str) -> str:
    """抓取当前所有线程的 Python 栈（纯标准库，不需要 py-spy）。

    Args:
        reason: 触发原因（写进快照首行，方便事后翻日志）。

    Returns:
        多行文本：首行是时间与原因，之后每个线程一段
        ``--- 线程 名称 (id=xxx) ---`` + 该线程的调用栈（**主线程排第一**）。
    """
    frames = sys._current_frames()
    names = {th.ident: th.name for th in threading.enumerate()
             if th.ident is not None}
    main_id = threading.main_thread().ident
    lines = [f"===== 线程栈快照（{reason}）"
             f"{datetime.now():%Y-%m-%d %H:%M:%S} ====="]
    order = sorted(frames, key=lambda tid: (tid != main_id,
                                            names.get(tid, "")))
    for tid in order:
        mark = " [main]" if tid == main_id else ""
        lines.append(f"--- 线程 {names.get(tid, '?')} (id={tid}){mark} ---")
        try:
            lines.extend(line.rstrip("\n")
                         for line in traceback.format_stack(frames[tid]))
        except Exception as exc:   # noqa: BLE001 - 线程可能在同一瞬间退出
            lines.append(f"（该线程栈取不到：{exc!r}）")
    return "\n".join(lines)


def _write_dump(text: str, dump_dir: Optional[Path] = None) -> str:
    """把线程栈写到 ``data/shutdown-dump-<时间>.log``；失败返回空串。"""
    base = Path(dump_dir) if dump_dir is not None else Path(DB_PATH).parent
    try:
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"shutdown-dump-{datetime.now():%Y%m%d-%H%M%S}.log"
        path.write_text(text, encoding="utf-8")
        return str(path)
    except OSError as exc:
        print(f"（线程栈写文件失败：{exc}）", file=sys.stderr, flush=True)
        return ""


def _start_exit_watchdog(
    timeout_sec: float = EXIT_WATCHDOG_SEC,
    exit_fn: Callable[[int], None] = os._exit,
    dump_dir: Optional[Path] = None,
) -> bool:
    """Ctrl+C 后启动「超时抓栈并强制结束」看门狗（只启一次）。

    为什么需要：uvicorn 收到 Ctrl+C 后先等所有连接与后台任务收尾
    （``timeout_graceful_shutdown`` 默认无限等）。只要有一个同步请求卡在
    线程池里，进程就会永远停在 "Shutting down"，而 lifespan 的 shutdown
    （停监听 / 刷 watermark / 关库）根本不会执行。现场表现为：控制台不再
    输出、但微信轮询日志还在刷、进程杀掉之前一直占 CPU。

    Args:
        timeout_sec: 等多久还没退出就抓栈并强杀。
        exit_fn: 实际结束进程的函数（测试可注入，避免真的杀掉 pytest）。
        dump_dir: 线程栈落盘目录；None = 配置库所在的 ``data/``。

    Returns:
        True = 本次真正启动了看门狗；False = 之前已经启动过。
    """
    global _watchdog_started
    with _watchdog_lock:
        if _watchdog_started:
            return False
        _watchdog_started = True
    print(f"正在退出…（{timeout_sec:.0f} 秒内没退干净会自动打印线程栈并"
          f"强制结束，不用反复按 Ctrl+C）", flush=True)

    def _run() -> None:
        time.sleep(timeout_sec)
        text = _dump_all_threads("退出超时（多半是某个请求卡住了）")
        path = _write_dump(text, dump_dir)
        print(text, flush=True)
        if path:
            print(f"线程栈已保存：{path}", flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        exit_fn(130)

    threading.Thread(target=_run, name="kefu-exit-watchdog",
                     daemon=True).start()
    return True


class _KefuServer(uvicorn.Server):
    """uvicorn Server 子类：把 Ctrl+C（SIGINT/SIGTERM）先挂上退出看门狗。"""

    def handle_exit(self, sig: int, frame: Any) -> None:  # noqa: ANN401
        """收到退出信号时先启动看门狗，再走 uvicorn 的正常关闭流程。"""
        _start_exit_watchdog()
        super().handle_exit(sig, frame)


def main() -> None:
    """启动 FastAPI 应用（单进程，兼容 GUI 自动化线程模型）。"""
    argv = sys.argv[1:]
    # 授权模式切换：--license-dev / --license-on 只改配置库然后退出，不启服务。
    # --license-dev 后面紧跟的那个参数是测试口令，必须出示（客户端无法自己关授权）。
    for flag in ("--license-dev", "--license-on"):
        if flag in argv:
            token = ""
            if flag == "--license-dev":
                idx = argv.index(flag)
                if idx + 1 < len(argv) and not argv[idx + 1].startswith("-"):
                    token = argv[idx + 1]
            raise SystemExit(license_mode_command(flag, token=token) or 0)
    # --license：强制启用授权（交付包用）。直接写环境变量，因为 LicenseClient
    # 只读环境变量；这里不复用 argparse 结果（地址解析内部自己 parse 一次，
    # 重复解析会让 --port 的非法值报两遍）。
    if "--license" in argv:
        os.environ["KEFU_LICENSE_MODE"] = "enforce"
    resolved = _resolve_address_detail(
        settings_reader=_read_saved_setting)
    if resolved.save:
        # --save：把最终解析出的 host 与 port（无论来自哪一级）写入配置库
        save_address(resolved.host, resolved.port)
    note = _format_origin_note(resolved)
    print(f"kefu-system 正在启动，"
          f"监听地址：http://{resolved.host}:{resolved.port}{note}")
    config = uvicorn.Config(
        "app.main:app",
        host=resolved.host,
        port=resolved.port,
        log_level="info",
        # 关键：Ctrl+C 后最多等 5 秒，没结束的请求直接取消。默认 None = 无限等，
        # 一个卡住的同步请求就会让进程永远停在 "Shutting down"。
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SEC,
    )
    server = _KefuServer(config)
    code = 0
    try:
        server.run()            # 内部捕获 SIGINT，先挂看门狗再优雅关闭
    except KeyboardInterrupt:   # uvicorn 关完会把 SIGINT 重新抛出来
        pass
    except BaseException:       # noqa: BLE001 - 退出路径必须兜住，不能留悬挂进程
        traceback.print_exc()
        code = 1
    else:
        if not server.started:
            code = 3            # 与 uvicorn 启动失败退出码保持一致
    print("kefu-system 已退出。", flush=True)
    try:
        logging.shutdown()      # 文件日志刷盘（下面 os._exit 不会跑 atexit）
    except Exception:           # noqa: BLE001 - 退出路径绝不因刷盘失败而卡住
        pass
    sys.stdout.flush()
    sys.stderr.flush()
    # 直接用 _exit 结束：此时 lifespan shutdown 已跑完（监听已停、watermark 已
    # 刷回、库已关），剩下的都是 daemon/第三方（UIA、COM）线程；正常退出要等
    # 它们 join，反而会被卡住的 COM 线程拖成「关不掉」。看门狗会兜住真正的
    # 卡死场景并留下线程栈。
    os._exit(code)


if __name__ == "__main__":
    main()
