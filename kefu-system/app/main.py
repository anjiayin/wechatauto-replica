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
from app.config import (
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
from app.notifier import Notifier
from app.pipeline import Pipeline
from app.sender import Sender
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


def create_app() -> FastAPI:
    """创建 FastAPI 应用。"""
    setup_logging()
    app = FastAPI(
        title="微信商机监测与客服系统",
        version="1.0.0",
        description="基于 wechatauto 的本地商机监测与客服闭环系统",
    )
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
        db = Database()
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
        deps.configure(db=db, wechat=wechat, pipeline=pipeline,
                       sender=sender, notifier=notifier)
        # 全局引用（供闭包/查找使用）
        globals()["_db"] = db
        globals()["_pipeline"] = pipeline
        globals()["_aggregator"] = aggregator
        # 注入引用消息查询（避免 sender ↔ wechat_service 循环导入）
        sender.set_message_lookup(wechat.get_message_by_local_id)
        sender.set_latest_lookup(wechat.get_latest_local_id)
        # 启动后台组件
        pipeline.start()
        sender.start()
        aggregator.start()
        # 微信初始化（密钥提取）可能耗时十几秒：放后台线程，避免阻塞事件循环
        wx_start = threading.Thread(target=wechat.start,
                                    name="kefu-wx-start", daemon=True)
        wx_start.start()
        globals()["_wx_start_thread"] = wx_start
        logger.info("kefu-system 启动完成（前端 dist：%s）",
                    "已托管" if (FRONTEND_DIST / "index.html").exists()
                    else "未构建")

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        wechat = deps.get_wechat_service()
        pipeline = deps.get_pipeline()
        sender = deps.get_sender()
        db = deps.get_db()
        aggregator = globals().get("_aggregator")
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
