#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盼之代售 (panzhi.com) 爬虫适配器 — 接入 python-client GUI 调度系统。

═══════════════════════════════════════════════════════════════════
                      爬 虫 作 者 必 读
═══════════════════════════════════════════════════════════════════

【注册方式】
  在 main_window.py 中 `import services.jingxi_panzhi_scanner` 即可自动注册。
  爬虫函数 `panzhi_scanner` 已被 register_scanner() 注册到全局注册中心。

【调度流程】
  1. ScanScheduler 每 30s tick → 判定哪些 task 该执行
  2. 调度器调 run_scanners() → 调 panzhi_scanner(task_id, payload)
  3. panzhi_scanner 创建 PanzhiAccountCrawler 实例
  4. 根据 payload["scan_mode"] 选择 run_full() 或 run_latest()
  5. on_batch 回调 → submit_scan_results() 落库
  6. scanner_service 自动推入 EstimationWorker 估价队列

【payload 字段】
  {
      "task_id":   1,
      "task_name": "盼之火影日扫",
      "platforms": ["盼之"],
      "games":     ["火影忍者"],
      "scan_mode": "latest" / "full",
      "scan_limit": 5,         # 最新模式下的页数（每页约100条）
      "pricing":   {"secondary_real_name_ratio": 0.1, "platform_bargain_ratio": 0.05},
  }

【游戏 ID 映射】
  盼之平台不同游戏对应不同 gameId:
    火影忍者 → 11
  后续上新游戏时在 GAME_ID_MAP 中追加即可。

【支持的模式】
  - full (全量抓取):   run_full(), 翻完所有商品自动停止
  - latest (最新抓取): run_latest(pages=N), 抓最新 N 页

═══════════════════════════════════════════════════════════════════
"""
import json
import logging
import os
import sys
import threading
from typing import Any, Dict, Optional

from services.jingxi_spider_panzhi import PanzhiAccountCrawler
from services.scanner_registry import register_scanner
from services.scanner_service import submit_scan_results
from services.proxy_manager import set_active_task, clear_active_task

logger = logging.getLogger(__name__)

# ── 游戏名 → 盼之 gameId 映射 ──
GAME_ID_MAP: Dict[str, str] = {
    "火影忍者": "11",
    "王者荣耀": "7",
    "三角洲行动": "391",
    "穿越火线": "16",
    "无畏契约": "231",
    "QQ飞车手游": "20",
    "金铲铲之战": "25",
    "使命召唤手游": "55",
}

# ── 停止事件（按 task_id 索引，支持多任务独立停止）──
_stop_events: Dict[int, threading.Event] = {}
_stop_events_lock = threading.Lock()


def stop_task(task_id: int) -> None:
    """通知指定任务的爬虫停止（由 GUI 状态切换触发）。"""
    with _stop_events_lock:
        event = _stop_events.get(task_id)
    if event:
        event.set()
        logger.info(f"[盼之] task_id={task_id} 已发送停止信号")


def _get_stop_event(task_id: int) -> threading.Event:
    """获取或创建指定任务的停止事件。"""
    with _stop_events_lock:
        if task_id not in _stop_events:
            _stop_events[task_id] = threading.Event()
        return _stop_events[task_id]


def _cleanup_stop_event(task_id: int) -> None:
    """任务结束后清理停止事件。"""
    with _stop_events_lock:
        _stop_events.pop(task_id, None)


def _app_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_panzhi_config() -> Dict[str, str]:
    """加载盼之鉴权配置。优先级：config/panzhi.json > 环境变量

    Returns:
        {"token": "..."}  可能为空字符串
    """
    token: Optional[str] = None

    # 1) config/panzhi.json
    config_path = os.path.join(_app_root(), "config", "panzhi.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
            if isinstance(cfg, dict):
                token = str(cfg.get("token") or "").strip() or None
        except Exception as e:
            logger.warning("读取 panzhi.json 失败: %s", e)

    # 2) 环境变量兜底
    if not token:
        token = os.environ.get("PANZHI_TOKEN", "").strip() or None

    return {"token": token or ""}


def _default_browser_context_path() -> str:
    return os.path.join(_app_root(), "config", "panzhi_browser_context.json")


def _load_browser_context_snapshot(payload: dict) -> Dict[str, Any]:
    """Resolve browser_context from payload first, then from a configured JSON file."""
    inline_ctx = payload.get("browser_context") or payload.get("browserContext") or {}
    if isinstance(inline_ctx, dict) and inline_ctx:
        return inline_ctx

    config_path = os.environ.get("PANZHI_BROWSER_CONTEXT_PATH", "").strip() or _default_browser_context_path()
    if not os.path.exists(config_path):
        logger.info("[盼之] browser_context snapshot not found: %s", config_path)
        return {}

    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f) or {}
    except Exception:
        logger.exception("[盼之] failed to load browser_context snapshot: %s", config_path)
        return {}

    if not isinstance(data, dict):
        logger.warning("[盼之] browser_context snapshot is not a JSON object: %s", config_path)
        return {}

    logger.info("[盼之] loaded browser_context snapshot from %s", config_path)
    return data


def panzhi_scanner(task_id: int, payload: dict) -> None:
    """盼之平台爬虫适配器。

    自动根据 payload["scan_mode"] 选择:
      - "full"   → run_full()    全量抓取
      - "latest" → run_latest()  最新抓取

    只处理 payload["platforms"] 中包含 "盼之" 的任务。
    """
    platforms = payload.get("platforms") or []
    games = payload.get("games") or []
    scan_mode = payload.get("scan_mode", "latest") or "latest"
    scan_limit = payload.get("scan_limit", 50) or 50
    browser_context = _load_browser_context_snapshot(payload)
    task_name = payload.get("task_name", "?")

    # ── 只处理盼之平台 ──
    if "盼之" not in platforms:
        logger.debug("[盼之] task_id=%d: 不包含盼之平台，跳过（platforms=%s）", task_id, platforms)
        return

    logger.info(
        "[盼之] task_id=%d '%s': ▷ 开始扫描（games=%s mode=%s limit=%d）",
        task_id, task_name, games, scan_mode, scan_limit,
    )

    # ── 加载 token 配置（config/panzhi.json > 环境变量）──
    auth = _load_panzhi_config()
    config_token = auth.get("token", "")

    set_active_task(task_id)

    # ── 逐个游戏抓取 ──
    for game in games:
        game_id = GAME_ID_MAP.get(game)
        if not game_id:
            logger.warning(
                "[盼之] task_id=%d: 不支持的游戏 '%s'，请在 GAME_ID_MAP 中添加映射",
                task_id, game,
            )
            continue

        logger.info(
            "[盼之] task_id=%d: 抓取 game=%s(id=%s) mode=%s pages=%d",
            task_id, game, game_id, scan_mode, max(1, int(scan_limit)),
        )

        # ── 构造爬虫实例 ──
        # token 优先级：browser_context.token > config/panzhi.json > 环境变量
        crawler = PanzhiAccountCrawler(
            game_id=game_id,
            game_name=game,
            platform="盼之",
            token=config_token,  # ★ 从 panzhi.json 加载（browser_context 可覆盖）
            batch_size=30,
            detail_concurrency=1,
            requests_per_second=0.35,
            max_pages=2000,
            is_proxy=None,    # None = 读取 config/proxy.json
            browser_context=browser_context if isinstance(browser_context, dict) else None,
        )

        # ── 重置停止事件 ──
        stop_evt = _get_stop_event(task_id)
        stop_evt.clear()

        # ── on_batch: 爬虫回调 → 落库 ──
        _batch_total = [0]

        def on_batch(batch: dict) -> None:
            """爬虫每攒够 batch_size 条触发一次。

            batch = {
                "task_id":  "...",
                "platform": "盼之",
                "results": [
                    {
                        "product_id":        "...",
                        "product_info":      "...",
                        "original_price":    310.0,
                        "url":               "https://...",
                        "game_type":         "火影忍者",
                        "supports_realname": True,
                    }, ...
                ],
            }
            """
            try:
                n = submit_scan_results(
                    task_id=task_id,
                    platform=batch["platform"],
                    results=batch["results"],
                )
                _batch_total[0] += n
                logger.info(
                    "[盼之] task_id=%d: 本批落库 %d/%d 条（累计 %d）",
                    task_id, n, len(batch["results"]), _batch_total[0],
                )
            except Exception:
                logger.exception("[盼之] task_id=%d: on_batch 落库失败", task_id)

        # ── on_progress: 进度回调 → 日志 ──
        _progress_page = [0]

        def on_progress(state: dict) -> None:
            """爬虫每翻一页或每消费一批触发一次（高频，已内置 0.2s 去抖）。"""
            pg = state.get("page_index")
            if pg is not None:
                _progress_page[0] = pg

            pages_ok = state.get("list_pages_ok", 0)
            produced = state.get("produced", 0)
            consumed = state.get("consumed", 0)
            total = state.get("total", 0)

            if pg is not None and pg % 5 == 1:  # 每 5 页打一条
                logger.info(
                    "[盼之] task_id=%d: 翻页进度 第%d页（总量约%d）请求%d页 产出%d 完成%d",
                    task_id, pg, total, pages_ok, produced, consumed,
                )

        # ── 选择模式并运行 ──
        try:
            t0 = __import__('time').monotonic()
            if scan_mode == "full":
                result = crawler.run_full(
                    on_batch=on_batch,
                    on_progress=on_progress,
                    should_stop=stop_evt.is_set,
                )
            else:
                pages = max(1, int(scan_limit))
                result = crawler.run_latest(
                    pages=pages,
                    on_batch=on_batch,
                    on_progress=on_progress,
                    should_stop=stop_evt.is_set,
                )
            elapsed = __import__('time').monotonic() - t0

            stats = result.get("stats", {})
            logger.info(
                "[盼之] task_id=%d: game=%s 抓取完成 | "
                "mode=%s 产出=%d 请求页=%d 结束原因=%s 耗时=%.1fs",
                task_id, game,
                stats.get("mode", "?"),
                stats.get("produced", 0),
                stats.get("list_pages_ok", 0),
                stats.get("list_end_reason", "?"),
                elapsed,
            )

        except Exception:
            logger.exception("[盼之] task_id=%d: ✗ 抓取异常", task_id)
        finally:
            _cleanup_stop_event(task_id)

    logger.info(
        "[盼之] task_id=%d: ✓ 全部完成（累计落库 %d 条）",
        task_id, _batch_total[0],
    )
    clear_active_task()


# ── 自动注册（main_window.py import 此模块即生效）──
register_scanner(panzhi_scanner)
logger.info("[盼之] panzhi_scanner 已注册到扫描器注册中心")
