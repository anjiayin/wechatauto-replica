# -*- coding: utf-8 -*-
"""FastAPI 应用工厂：日志、路由注册、lifespan、静态托管前端 dist、异常处理。

启动顺序（lifespan）：
    初始化日志 → 建库建表 → 组装单例（数据库/通知/管线/发送/微信服务）
    → 注入依赖 → 启动 管线 worker + 发送线程 + 微信监听 + 合并窗口
退出顺序：全部 stop（watermark 刷回在 WeChatService.stop 内完成）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.aggregator import Aggregator
from app.api import deps
from app.api import ALL_ROUTERS
from app.api.license_guard import make_license_guard
from app.config import (
    APP_VERSION,
    DATA_DIR,
    FRONTEND_DIST,
    LOG_BACKUP_COUNT,
    LOG_MAX_BYTES,
    LOG_PATH,
    MERGE_WINDOW_SEC_DEFAULT,
    QUEUE_MAX,
    WORKER_COUNT,
)
from app.db import Database, read_analysis_settings
from app import license as license_mod
from app.notifier import Notifier
from app.pipeline import Pipeline
from app.sender import Sender
from app.summary import SummaryScheduler, SummaryService
from app.update_service import reconcile_full_install, repair_bundle_root_files
from app.wechat_service import WeChatService

logger = logging.getLogger("kefu")

# 历史数据清理（反馈 7）已按用户要求改为「仅手动」：
# 不再有每日 03:30 定时任务与启动补跑，只保留
# POST /api/system/purge 由用户在「设置 → 数据与账单」手动触发。


class SafeRotatingFileHandler(RotatingFileHandler):
    """轮转失败也不中断的滚动文件日志处理器。

    背景：Windows 上 ``RotatingFileHandler.doRollover()`` 需要把
    ``kefu.log`` 重命名为 ``kefu.log.1``；若此时文件正被另一个进程占用
    （例如 pytest 进程同样调用过 ``setup_logging()``），rename 会抛
    ``PermissionError``。基类实现此时已经把 ``self.stream`` close 掉了，
    异常又被 ``handleError`` 吞掉，于是后续每次 emit 都失败 —— 进程从此
    永久静默（现场表现：数据库还在写，日志文件却不再增长）。

    策略：轮转失败时放弃本次轮转、重新以追加方式打开原文件继续写。
    宁可文件超过上限，也绝不丢日志。
    """

    def doRollover(self) -> None:  # noqa: D102 - 覆盖基类，语义见类文档
        try:
            super().doRollover()
        except OSError:  # PermissionError 也是 OSError 子类
            # 牺牲一次轮转：把可能已被 close 的流重新打开，继续追加
            self.stream = self._reopen_quietly()

    def _reopen_quietly(self) -> Optional[IO[str]]:
        """重新打开当前日志文件（追加模式）；失败返回 None 不抛异常。"""
        try:
            if self.stream is not None:
                try:
                    self.stream.close()
                except OSError:  # 防御：关闭失败不影响重新打开
                    pass
            return self._open()
        except OSError:
            return None

    def emit(self, record: logging.LogRecord) -> None:
        """流被关闭/未建立时先重新打开再写，避免永久静默。"""
        if self.stream is None:
            self.stream = self._reopen_quietly()
        super().emit(record)


def setup_logging() -> None:
    """滚动文件日志（data/kefu.log）+ 控制台输出（共享约定 4/8-7）。

    幂等：测试里 ``create_app()`` 会被多次调用，重复执行不会叠加 handler。
    启动时会打印日志文件的绝对路径与当前大小，便于用户自查「日志还在写吗」。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # 幂等识别：本模块创建的 handler 带有 _kefu_file / _kefu_console 标记
    file_handler = next(
        (h for h in root.handlers if getattr(h, "_kefu_file", False)), None)
    if file_handler is None:
        file_handler = SafeRotatingFileHandler(
            LOG_PATH, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8")
        file_handler.setFormatter(fmt)
        setattr(file_handler, "_kefu_file", True)
        root.addHandler(file_handler)

    if not any(getattr(h, "_kefu_console", False) for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        setattr(console, "_kefu_console", True)
        root.addHandler(console)

    # 启动自检：日志文件位置与大小（超过上限说明轮转未成功，需人工处理）
    try:
        size_bytes = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0
    except OSError:  # 文件系统异常不影响启动
        size_bytes = 0
    logger.info(
        "日志文件：%s（当前 %.2f MB，轮转上限 %.2f MB，保留 %d 个备份）",
        Path(LOG_PATH).resolve(), size_bytes / 1024 / 1024,
        LOG_MAX_BYTES / 1024 / 1024, LOG_BACKUP_COUNT)


def _license_heartbeat_loop(client, check_interval_sec: float = 300.0) -> None:
    """后台静默联网确认授权（续费、吊销、换到期日都能自动生效）。

    每 5 分钟看一次「距上次成功确认是否超过配置间隔」（默认 6 小时），到期就
    向授权服务器确认一次。全部失败只记日志，不弹错误——真正的提示由
    ``/api/license/status`` 的状态机和前端横幅负责，避免断网时刷屏。

    Args:
        client: :class:`app.license.LicenseClient`。
        check_interval_sec: 轮询粒度（秒）。
    """
    while True:
        time.sleep(check_interval_sec)
        try:
            if not client.needs_refresh():
                continue
            status = client.refresh()
            logger.info("授权后台确认完成：%s（到期 %s）",
                        status.state, status.expires_at or "-")
        except Exception:            # 线程绝不能因异常退出
            logger.exception("授权后台确认异常（已忽略，稍后重试）")


def create_app() -> FastAPI:
    """创建 FastAPI 应用。"""
    setup_logging()
    app = FastAPI(
        title="微信商机监测与客服系统",
        version=APP_VERSION,
        description="基于 wechatauto 的本地商机监测与客服闭环系统",
    )
    # 授权拦截（必须在 CORS 之前注册：Starlette 中间件是后加的先执行，
    # 这样 CORS 位于外层，403 响应也能带上跨域头）
    app.middleware("http")(make_license_guard(
        lambda: deps.get_license_client().status()))
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
        allow_headers=["*"],
    )
    for router in ALL_ROUTERS:
        app.include_router(router)

    # ------------------------------------------------------------------
    # 异常处理：用户可见错误一律中文（共享约定 2/7）
    # ------------------------------------------------------------------
    @app.exception_handler(Exception)
    async def on_unexpected_error(request: Request, exc: Exception):
        logger.exception("未处理异常（%s %s）", request.method, request.url.path)
        # JSONResponse 无 detail 参数，错误体须经 content 传递（共享约定 2）
        return JSONResponse(status_code=500,
                            content={"detail": f"服务器内部错误：{exc}"})

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @app.on_event("startup")
    async def _startup() -> None:
        # 启动自检补写（消除「首跳缺口」）：从旧版升上来的一次 apply 不认识
        # bundle_root/，版本层里会留下解压产物；这里每次启动检查一次并补写
        # 安装根白名单文件。幂等 + fail-open（内部吞掉一切异常只打日志），
        # 必须放在最前面，不依赖数据库与业务组件。
        repair_bundle_root_files()
        db = Database()
        # 全量安装确认：只有新版本真的启动起来才推进 last_sequence；旧版
        # 提前推进导致的「下次不再提示」由此自愈（1.3.2 事故）。fail-open。
        reconcile_full_install(db)
        notifier = Notifier()
        notifier.set_loop(asyncio.get_running_loop())
        analysis_cfg = read_analysis_settings(db)
        aggregator = Aggregator(
            flush_callback=lambda unit: _pipeline.enqueue(unit),
            window_sec_provider=lambda: max(
                5, int(read_analysis_settings(_db).get("merge_window_sec",
                                                       MERGE_WINDOW_SEC_DEFAULT))),
        )
        pipeline = Pipeline(db, notifier, worker_count=WORKER_COUNT,
                            queue_max=QUEUE_MAX)
        sender = Sender(db, notifier)
        wechat = WeChatService(db, aggregator)
        # 授权客户端：绑定数据库读配置，注入 deps 供 API / 拦截中间件共用
        license_client = license_mod.LicenseClient(db=db)
        license_mod.set_client(license_client)
        deps.configure(db=db, wechat=wechat, pipeline=pipeline,
                       sender=sender, notifier=notifier,
                       license_client=license_client)
        # 全局引用（供闭包/查找使用）
        globals()["_db"] = db
        globals()["_pipeline"] = pipeline
        globals()["_aggregator"] = aggregator
        # 注入引用消息查询（避免 sender ↔ wechat_service 循环导入）
        sender.set_message_lookup(wechat.get_message_by_local_id)
        sender.set_latest_lookup(wechat.get_latest_local_id)
        # 双开防发错账号：发送前按当前监控账号定位到对应 Weixin.exe 实例
        sender.set_wechat_binding_provider(wechat.account_binding)
        # 注入语音补转写（分析线程兜住历史上未转写的 [语音] 消息）
        pipeline.set_voice_lookup(wechat.transcribe_voice)
        # 注入 @人 ID 还原（群聊 AI 判定上下文线索，批次 5 需求 ④）
        pipeline.set_at_resolver(wechat.resolve_at_tokens)
        # 定时总结调度器：常驻 daemon 线程随运行时启停（P1 T05）。
        # 独立常驻 SummaryService 实例（deps.get_summary_service 是请求级
        # 轻量构造，调度线程不能每次 tick 重建）；配置未开启时线程空转、
        # 每分钟只读一次 settings，开销可忽略。
        summary_scheduler = SummaryScheduler(
            SummaryService(db, wechat), notifier)
        globals()["_summary_scheduler"] = summary_scheduler

        def _start_runtime() -> None:
            """启动抓取 / 分析 / 发送（授权通过后才会被调用）。"""
            pipeline.start()
            # 启动预热（后台线程、持 _gui_lock、不激活微信窗口）：把 UIA 控件树
            # 首次物化的 15~30 秒从「用户点发送的那一刻」挪到服务启动时。
            # 实测首次发送「打开会话 25.4s」，预热后这部分归零。当年「预热把
            # 发送拖到 60s+」的事故根因是预热线程没持 _gui_lock，与发送线程
            # 抢微信单线程 UIA provider；现在 _prewarm 全程持锁，抢不到就放弃。
            sender.start(prewarm=True)
            aggregator.start()
            # 微信初始化（密钥提取）可能耗时十几秒：放后台线程，避免阻塞事件循环
            wx_start = threading.Thread(target=wechat.start,
                                        name="kefu-wx-start", daemon=True)
            wx_start.start()
            globals()["_wx_start_thread"] = wx_start
            # 定时总结与业务运行时同生命周期：只在抓取/分析可用后开始轮询
            summary_scheduler.start()

        # 授权通过后（含「首次激活」）由 deps 回调现场拉起运行时，无需重启程序
        deps.configure_runtime(_start_runtime)
        lic_status = license_client.status(touch=True)
        if lic_status.ok:
            deps.mark_runtime_started()
            _start_runtime()
            if lic_status.enabled:
                logger.info("授权校验通过：状态 %s，到期 %s（剩余 %s 天）",
                            lic_status.state, lic_status.expires_at or "-",
                            lic_status.days_left if lic_status.days_left
                            is not None else "-")
            else:
                # 测试模式（厂商自己调试用）：日志里必须显眼，否则交付件忘了
                # 切回来就是一整批客户都能白用。
                logger.warning(
                    "⚠ 授权校验处于【厂商测试模式】：本机无需激活即可使用全部"
                    "功能。交付客户前务必执行 python run.py --license-on")
        else:
            logger.warning(
                "授权不可用（%s）：%s。已暂停微信抓取与分析，请在界面完成激活"
                "（机器码 %s）；历史数据未做任何改动。",
                lic_status.state, lic_status.message, lic_status.machine_code)
        # 授权启用时：后台线程按间隔静默联网确认（续费/吊销能及时生效）
        if lic_status.enabled:
            lic_thread = threading.Thread(
                target=_license_heartbeat_loop, args=(license_client,),
                name="kefu-license-heartbeat", daemon=True)
            lic_thread.start()
            globals()["_license_thread"] = lic_thread
        logger.info("kefu-system 启动完成（前端 dist：%s，版本 %s）",
                    "已托管" if (FRONTEND_DIST / "index.html").exists()
                    else "未构建", APP_VERSION)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        wechat = deps.get_wechat_service()
        pipeline = deps.get_pipeline()
        sender = deps.get_sender()
        db = deps.get_db()
        aggregator = globals().get("_aggregator")
        # 先停定时总结调度器（不再触发 AI 调用），再等微信初始化线程
        summary_scheduler = globals().get("_summary_scheduler")
        if summary_scheduler is not None:
            try:
                summary_scheduler.stop()
            except Exception:
                logger.exception("停止定时总结调度器异常（已忽略）")
        # 等待微信初始化线程结束（避免与 stop 并发竞争）
        wx_start = globals().get("_wx_start_thread")
        if wx_start is not None:
            wx_start.join(timeout=60)
        # 先停合并窗口（flush 出的单元进入管线队列后由 worker 消化），
        # 避免调度线程在 db 关闭后仍读库报错
        if aggregator is not None:
            try:
                aggregator.stop(flush_all=False)
            except Exception:
                logger.exception("停止合并窗口异常（已忽略）")
        for stop in (wechat.stop, pipeline.stop, sender.stop, db.close):
            try:
                stop()
            except Exception:
                logger.exception("退出清理异常（已忽略）")
        logger.info("kefu-system 已退出")

    # ------------------------------------------------------------------
    # 静态托管前端 dist（不存在时返回构建引导页）
    # ------------------------------------------------------------------
    index_html = FRONTEND_DIST / "index.html"
    if index_html.exists():
        assets_dir = FRONTEND_DIST / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)),
                      name="assets")

        @app.get("/", include_in_schema=False)
        async def _index() -> HTMLResponse:
            return HTMLResponse(index_html.read_text(encoding="utf-8"))

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _spa_fallback(full_path: str) -> HTMLResponse:
            """SPA 兜底：非 /api、/ws 的路径一律回 index.html。"""
            if full_path.startswith(("api/", "ws/", "assets/")):
                return JSONResponse(status_code=404,
                                    detail="资源不存在：" + full_path)
            candidate = FRONTEND_DIST / full_path
            if full_path and candidate.is_file():
                return HTMLResponse(
                    candidate.read_bytes().decode("utf-8", "replace"))
            return HTMLResponse(index_html.read_text(encoding="utf-8"))
    else:

        @app.get("/", include_in_schema=False)
        async def _guide() -> HTMLResponse:
            """dist 未构建时的中文引导页。"""
            return HTMLResponse(_GUIDE_HTML, status_code=200)

    return app


_GUIDE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>微信商机监测与客服系统</title>
<style>body{font-family:"Microsoft YaHei",sans-serif;max-width:640px;margin:80px auto;
color:#333;line-height:1.8}code{background:#f2f2f2;padding:2px 6px;border-radius:4px}</style>
</head><body>
<h2>前端尚未构建</h2>
<p>后端服务已正常运行，但前端构建产物 <code>frontend/dist</code> 不存在。</p>
<p>请在 <code>kefu-system/frontend</code> 目录执行：</p>
<pre><code>npm install
npm run build</code></pre>
<p>构建完成后刷新本页即可使用控制台界面。</p>
</body></html>"""


app = create_app()
