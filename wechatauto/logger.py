from .param import WxParam

import logging
import colorama
from pathlib import Path
from datetime import datetime
import sys
import io


colorama.init()

if hasattr(sys.stdout, 'buffer'):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='ignore')
    except Exception:
        pass

LOG_COLORS = {
    'DEBUG': colorama.Fore.CYAN,
    'INFO': colorama.Fore.GREEN,
    'WARNING': colorama.Fore.YELLOW,
    'ERROR': colorama.Fore.RED,
    'CRITICAL': colorama.Fore.MAGENTA
}

class ColoredFormatter(logging.Formatter):
    def format(self, record):
        levelname = record.levelname
        message = super().format(record)
        return f"{LOG_COLORS[levelname]}{message}{colorama.Style.RESET_ALL}"

class WechatautoLogger:
    name: str = 'wechatauto'

    def __init__(self):
        self.logger = self.setup_logger()
        self.file_handler = None  # 先不创建文件处理器
        self.set_debug(False)

    def setup_logger(self) -> logging.Logger:
        """设置日志记录器（只操作 ``wechatauto`` 命名 logger，绝不触碰根 logger）。

        根 logger 的 level 与 handler 归宿主应用管理（例如 kefu-system 的
        ``setup_logging()`` 把 kefu.log 的 file handler 挂在根 logger 上）。
        本库一旦 ``root.handlers.clear()`` 或改 root level，就会误伤宿主：
        生产环境曾因此出现「kefu.log 从启动第二秒起就不再写」的故障（wechatauto
        被延迟 import 时清掉了 kefu.log 的 handler）。故这里只给 wechatauto
        自己的命名 logger 挂 handler，并用 ``propagate=False`` 避免日志重复
        流向根 logger（kefu.log / 根控制台）。
        """
        # 第三方库降噪：只调 level、不碰 handler，对全局无副作用
        logging.getLogger('asyncio').setLevel(logging.WARNING)
        logging.getLogger('comtypes').setLevel(logging.WARNING)
        logging.getLogger('urllib3').setLevel(logging.WARNING)
        logging.getLogger('requests').setLevel(logging.WARNING)

        logger = logging.getLogger(self.name)
        logger.setLevel(logging.DEBUG)
        # 独立日志：不向根 logger 传播，避免与宿主的 kefu.log/控制台重复
        logger.propagate = False

        # 控制台处理器（带颜色），幂等：带 _wx_console 标记则不再重复添加
        existing = next(
            (h for h in logger.handlers if getattr(h, "_wx_console", False)),
            None)
        if existing is not None:
            self.console_handler = existing
        else:
            fmt = ('%(asctime)s [%(name)s] [%(levelname)s] '
                   '[%(filename)s:%(lineno)d]  %(message)s')
            self.console_handler = logging.StreamHandler()
            console_formatter = ColoredFormatter(
                fmt=fmt,
                datefmt="%Y-%m-%d %H:%M:%S"
            )
            self.console_handler.setFormatter(console_formatter)
            self.console_handler.setLevel(logging.DEBUG)
            setattr(self.console_handler, "_wx_console", True)
            logger.addHandler(self.console_handler)

        return logger

    def setup_file_logger(self):
        """根据WxParam.ENABLE_FILE_LOGGER决定是否创建文件日志处理器"""
        if not WxParam.ENABLE_FILE_LOGGER or self.file_handler is not None:
            return

        # 文件处理器（无颜色）
        log_dir = Path("wechatauto_logs")
        log_dir.mkdir(parents=True, exist_ok=True)

        # 使用当前时间创建日志文件
        current_time = datetime.now().strftime("%Y%m%d")
        log_file = log_dir / f"app_{current_time}.log"

        self.file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_formatter = logging.Formatter(
            '%(asctime)s [%(name)s] [%(levelname)s] [%(filename)s:%(lineno)d]  %(message)s',
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        self.file_handler.setFormatter(file_formatter)
        self.file_handler.setLevel(logging.DEBUG)
        setattr(self.file_handler, "_wx_file", True)

        # 将文件处理器添加到 wechatauto 命名 logger（而非根 logger），
        # 避免污染宿主的根 logger handler 集合
        logging.getLogger(self.name).addHandler(self.file_handler)

    def set_debug(self, debug=False):
        """动态设置日志级别"""
        if debug:
            self.console_handler.setLevel(logging.DEBUG)
        else:
            self.console_handler.setLevel(logging.INFO)

    def _ensure_file_logger(self):
        """确保文件日志处理器被初始化"""
        if WxParam.ENABLE_FILE_LOGGER and self.file_handler is None:
            self.setup_file_logger()

    def debug(self, msg: str, *args, stacklevel=2, **kwargs):
        self._ensure_file_logger()  # 确保文件日志初始化
        self.logger.debug(msg, *args, stacklevel=stacklevel, **kwargs)

    def info(self, msg: str, *args, stacklevel=2, **kwargs):
        self._ensure_file_logger()  # 确保文件日志初始化
        self.logger.info(msg, *args, stacklevel=stacklevel, **kwargs)

    def warning(self, msg: str, *args, stacklevel=2, **kwargs):
        self._ensure_file_logger()  # 确保文件日志初始化
        self.logger.warning(msg, *args, stacklevel=stacklevel, **kwargs)

    def error(self, msg: str, *args, stacklevel=2, **kwargs):
        self._ensure_file_logger()  # 确保文件日志初始化
        self.logger.error(msg, *args, stacklevel=stacklevel, **kwargs)

    def critical(self, msg: str, *args, stacklevel=2, **kwargs):
        self._ensure_file_logger()  # 确保文件日志初始化
        self.logger.critical(msg, *args, stacklevel=stacklevel, **kwargs)

# wxlog实例化的地方不再创建文件日志
wxlog = WechatautoLogger()
