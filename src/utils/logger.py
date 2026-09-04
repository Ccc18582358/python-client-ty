"""
统一日志配置。

用法：在程序入口最早期（任何其他模块创建 logger 之前）调用一次：

    from utils.logger import setup_logging
    setup_logging()

之后所有模块正常使用 logging.getLogger(__name__) 即可，
日志会自动写入 EXE 同目录下的 data/app.log。

特性：
- 文件输出：data/app.log（RotatingFileHandler，10 MiB × 5 个备份）
- 控制台输出：stderr（INFO 级别，打包后无控制台时自动静默）
- 格式：时间 - 模块名 - 级别 - 消息
- 线程名包含在格式中，方便追踪多线程爬虫
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _app_root() -> str:
    """应用根目录：开发环境=项目根，打包后=exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # src/utils/logger.py → src → 项目根
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── 控制台彩色输出 ─────────────────────────────────────────────
# 四个爬虫平台（盼之/螃蟹/氪金兽/7881）的「抓取」日志整行红色，方便在大量日志里一眼定位；
# 其余日志（估价/修改价格/落库/调度等）保持原色，不染色。
# 颜色只加到「控制台」handler；「文件」handler 保持纯文本，避免 app.log 混入 ANSI 转义码。

_RESET = "\033[0m"
_RED = "\033[31m"

# 平台关键词：logger name 包含其一即判定为爬虫平台（含 panzhi_folder 的 WAF 工具）
_CRAWLER_KEYWORDS = ("panzhi", "pangxie", "kejinshou", "7881")
# 螃蟹 scanner 的 logger 名是 services.jingxi_scanner，不含 pangxie，单独精确匹配
_CRAWLER_EXACT = ("services.jingxi_scanner",)


def _is_crawler_logger(name: str) -> bool:
    """判断 logger 是否属于四个爬虫平台。"""
    if name in _CRAWLER_EXACT:
        return True
    low = name.lower()
    return any(k in low for k in _CRAWLER_KEYWORDS)


def _enable_windows_ansi() -> None:
    """Windows 控制台启用 ANSI 虚拟终端（否则彩色码显示成 ←[31m 乱码）。

    Windows 10 起原生支持 VT，但默认关闭，需通过 SetConsoleMode 打开；
    Windows Terminal / VS Code 集成终端本身已支持，这里是无害兜底。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        for stream_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(stream_id)
            if not handle:
                continue
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(
                    handle,
                    mode.value | 0x0004,  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                )
    except Exception:  # noqa: BLE001 — 颜色是锦上添花，失败不影响日志
        pass


class _ColoredFormatter(logging.Formatter):
    """控制台彩色 formatter：仅爬虫平台抓取日志红色，其余（含估价）原色。"""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        if _is_crawler_logger(record.name):
            return f"{_RED}{base}{_RESET}"
        return base


def setup_logging(*, level: int = logging.DEBUG, console_level: int = logging.INFO) -> str:
    """配置全局日志：文件（DEBUG）+ 控制台（INFO）。

    必须在其他模块创建 logger 之前调用。
    幂等：重复调用不会重复添加 handler。

    Returns:
        app.log 的完整路径（供调用方打印确认）。
    """
    root = logging.getLogger()

    # 幂等：已经配置过就不再重复
    if getattr(setup_logging, "_installed", False):
        return getattr(setup_logging, "_log_path", "")

    app_root = _app_root()
    data_dir = os.path.join(app_root, "data")
    os.makedirs(data_dir, exist_ok=True)
    log_path = os.path.join(data_dir, "app.log")

    # 清除已有 handler（包括 basicConfig 设的默认 stderr handler）
    root.handlers.clear()
    root.setLevel(level)

    # ── 格式 ──
    _log_fmt = "%(asctime)s [%(threadName)s] %(name)s %(levelname)s %(message)s"
    _date_fmt = "%Y-%m-%d %H:%M:%S"
    fmt = logging.Formatter(_log_fmt, _date_fmt)

    # ── 文件 handler：DEBUG，自动轮转 ──
    fh = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,  # 10 MiB
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # ── 控制台 handler：INFO，带颜色（爬虫平台红色）──
    _enable_windows_ansi()
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(console_level)
    # 仅真终端上色；重定向到文件 / 打包后无控制台时退回纯文本，避免 ANSI 码混入
    try:
        _use_color = sys.stderr is not None and sys.stderr.isatty()
    except Exception:  # noqa: BLE001
        _use_color = False
    ch.setFormatter(_ColoredFormatter(_log_fmt, _date_fmt) if _use_color else fmt)
    root.addHandler(ch)

    # ── 降低第三方库日志噪音 ──
    for noisy in ("urllib3", "requests", "httpx", "httpcore", "PIL", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    setup_logging._installed = True  # type: ignore[attr-defined]
    setup_logging._log_path = log_path  # type: ignore[attr-defined]

    # 写一条启动标记
    root.info("=" * 60)
    root.info("日志系统初始化完成，日志文件: %s", log_path)
    root.info("应用根目录: %s", app_root)
    root.info("Python: %s", sys.version)
    root.info("sys.frozen: %s", getattr(sys, "frozen", False))
    root.info("=" * 60)

    return log_path
