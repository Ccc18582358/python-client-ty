"""
爬虫函数注册中心

═══════════════════════════════════════════════════════════════════
                  爬 虫 作 者 必 读
═══════════════════════════════════════════════════════════════════

【你能做什么】
  1. 写一个 `func(task_id, payload) -> None` 签名的函数
  2. 调 `register_scanner(func)` 注册到全局列表
  3. 调度器到点会调你的函数

【怎么注册】
  把你的模块 import 进 main_window 即可（模块 import 副作用自动注册）
  例：main_window.py 加 `import services.crawler_pangxie`

【你的函数会收到什么】
  task_id:  int   — 扫号任务 ID
  payload:  dict  — {
      "task_name":  "螃蟹火影日扫",
      "platforms":  ["螃蟹"],
      "games":      ["火影忍者"],
      "scan_mode":  "latest" / "full",
      "scan_limit": 50,
      "pricing":    {"secondary_real_name_ratio": 0.1, "platform_bargain_ratio": 0.05},
  }

【你必须做什么】
  1. 自己爬数据（HTTP/Selenium/...）
  2. 调 `submit_scan_results(task_id, platform, results)` 落库
     - `results` 是 list[dict]，每条字段见 scanner_service.py
  3. 出错时打日志（`logger.error`）——调度器会跳过你的这次任务

【完整示例】见 services/demo_scanner.py

═══════════════════════════════════════════════════════════════════
"""
import logging
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# 已注册的爬虫函数列表（模块级全局变量）
# 签名：func(task_id: int, payload: dict) -> None
_scanner_funcs: List[Callable[[int, dict], None]] = []


def register_scanner(func: Callable[[int, dict], None]) -> None:
    """注册一个爬虫函数。

    Args:
        func: 你的爬虫函数，签名 `(task_id: int, payload: dict) -> None`

    重复注册同名函数会被跳过（避免 import 多次重复执行）。

    通常在模块底部调用一次即可：
        # services/crawler_pangxie.py
        def my_crawler(task_id, payload): ...
        register_scanner(my_crawler)
    """
    if func in _scanner_funcs:
        return
    _scanner_funcs.append(func)
    logger.info(f"[scanner_registry] 注册爬虫: {func.__name__}（共 {len(_scanner_funcs)} 个）")


def unregister_scanner(func: Callable[[int, dict], None]) -> None:
    """反注册（一般测试时用）"""
    if func in _scanner_funcs:
        _scanner_funcs.remove(func)
        logger.info(f"[scanner_registry] 注销爬虫: {func.__name__}")


def get_scanners() -> List[Callable[[int, dict], None]]:
    """返回当前所有已注册的爬虫函数（副本）"""
    return list(_scanner_funcs)


def run_scanners(task_id: int, payload: dict) -> int:
    """调度器调用：执行所有已注册的爬虫

    行为：
      - 依次调用每个已注册函数（注册顺序）
      - 单个爬虫抛异常不影响其他爬虫
      - 异常会被 logger.error 记录（含完整 traceback）

    Returns:
        成功执行的爬虫数量
    """
    if not _scanner_funcs:
        logger.warning("[scanner_registry] task_id=%d: 没有注册任何爬虫函数，跳过", task_id)
        return 0

    platforms = payload.get('platforms', [])
    games = payload.get('games', [])
    logger.info(
        "[scanner_registry] task_id=%d: 开始依次调用 %d 个爬虫（platforms=%s games=%s）",
        task_id, len(_scanner_funcs), platforms, games,
    )

    success = 0
    skipped = 0
    failed = 0
    for func in _scanner_funcs:
        try:
            func(task_id, payload)
            success += 1
        except Exception as e:
            failed += 1
            # 隔离单个爬虫失败，避免影响其他爬虫
            logger.error(
                "[scanner_registry] task_id=%d: 爬虫 %s 异常: %s",
                task_id, func.__name__, e,
                exc_info=True,
            )

    logger.info(
        "[scanner_registry] task_id=%d: 爬虫执行完毕 — 成功 %d, 失败 %d",
        task_id, success, failed,
    )
    return success
