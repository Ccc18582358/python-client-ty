#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
7881 扫号适配器.

This adapter keeps the UI and scheduler integration aligned with the existing
scanner architecture. It is list-first:

- full: keep fetching list pages until the API reports the end
- latest: fetch the configured page count with goodsSortType=6

The adapter submits list-page items directly so the task flow stays usable even
when detail pages are challenged by anti-bot verification.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Dict

from services.jingxi_spider_7881 import Jingxi7881Crawler
from services.scanner_registry import register_scanner
from services.scanner_service import submit_scan_results
from services.proxy_manager import set_active_task, clear_active_task

logger = logging.getLogger(__name__)

RED = "\x1b[31m"
RESET = "\x1b[0m"

GAME_ID_MAP: Dict[str, Any] = {
    "火影忍者": "A5468",
    "王者荣耀": ["A2705", "A2775"],
    "金铲铲之战": "A5661",
    "QQ飞车手游": ["A4869", "A4871"],
    "使命召唤手游": "A5403",
    "无畏契约": "G5706",
    "三角洲行动": "A5776",
    "穿越火线": "G68",
}

_stop_events: Dict[int, threading.Event] = {}
_stop_events_lock = threading.Lock()


def stop_task(task_id: int) -> None:
    with _stop_events_lock:
        event = _stop_events.get(task_id)
    if event:
        event.set()
        logger.info("[7881] task_id=%s 已发送停止信号", task_id)


def _get_stop_event(task_id: int) -> threading.Event:
    with _stop_events_lock:
        if task_id not in _stop_events:
            _stop_events[task_id] = threading.Event()
        return _stop_events[task_id]


def _cleanup_stop_event(task_id: int) -> None:
    with _stop_events_lock:
        _stop_events.pop(task_id, None)


def _emit_sku_json(result: dict) -> None:
    raw_price = result.get("original_price")
    try:
        price_value = float(raw_price) if raw_price is not None else None
    except (TypeError, ValueError):
        price_value = None
    payload = {
        "product_id": result.get("product_id") or result.get("goods_id") or "",
        "url": result.get("url") or "",
        "price": price_value,
        "product_info": str(result.get("product_info") or "")[:50],
        "has_valid_price": bool(price_value and price_value > 0),
    }
    print(f"{RED}{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}{RESET}", flush=True)


def jingxi_7881_scanner(task_id: int, payload: dict) -> None:
    platforms = payload.get("platforms") or []
    games = payload.get("games") or []
    scan_mode = payload.get("scan_mode", "latest") or "latest"
    scan_limit = payload.get("scan_limit", 50) or 50
    task_name = payload.get("task_name", "?")

    if "7881" not in platforms:
        logger.debug("[7881] task_id=%d: 不包含 7881 平台，跳过（platforms=%s）", task_id, platforms)
        return

    logger.info(
        "[7881] task_id=%d '%s': ▷ 开始扫描（games=%s mode=%s limit=%d）",
        task_id, task_name, games, scan_mode, scan_limit,
    )

    set_active_task(task_id)
    stop_evt = _get_stop_event(task_id)
    stop_evt.clear()
    batch_total = [0]

    try:

        def on_batch(batch: dict) -> None:
            try:
                for result in batch.get("results") or []:
                    _emit_sku_json(result)
                n = submit_scan_results(
                    task_id=task_id,
                    platform=batch["platform"],
                    results=batch["results"],
                )
                batch_total[0] += n
                logger.info(
                    "[7881] task_id=%d: 本批落库 %d/%d 条（累计 %d）",
                    task_id, n, len(batch["results"]), batch_total[0],
                )
            except Exception:
                logger.exception("[7881] task_id=%d: on_batch 落库失败", task_id)

        def on_progress(state: dict) -> None:
            pg = state.get("page_index")
            if pg is not None and pg % 5 == 1:  # 每 5 页打一条
                logger.info(
                    "[7881] task_id=%d: 翻页进度 第%d页（请求%d页 产出%d）",
                    task_id, pg,
                    state.get("list_pages_ok", 0),
                    state.get("produced", 0),
                )

        for game in games:
            game_id_or_list = GAME_ID_MAP.get(game)
            if not game_id_or_list:
                logger.warning("[7881] task_id=%d: 暂不支持游戏 '%s'，跳过", task_id, game)
                continue

            game_ids = (
                game_id_or_list
                if isinstance(game_id_or_list, list)
                else [game_id_or_list]
            )

            for game_id in game_ids:
                logger.info(
                    "[7881] task_id=%d: 抓取 game=%s(id=%s) mode=%s pages=%d",
                    task_id, game, game_id, scan_mode, max(1, int(scan_limit)),
                )

                crawler = Jingxi7881Crawler(
                    page_size=30, timeout=20.0, retries=3, game_id=game_id
                )

                t0 = __import__('time').monotonic()
                if scan_mode == "full":
                    result = crawler.run_full(
                        on_batch=on_batch,
                        on_progress=on_progress,
                        should_stop=stop_evt.is_set,
                    )
                else:
                    result = crawler.run_latest(
                        pages=max(1, int(scan_limit)),
                        on_batch=on_batch,
                        on_progress=on_progress,
                        should_stop=stop_evt.is_set,
                    )
                elapsed = __import__('time').monotonic() - t0

                stats = result.get("stats", {})
                logger.info(
                    "[7881] task_id=%d: game=%s 抓取完成 | "
                    "mode=%s 产出=%d 请求页=%d 结束原因=%s 耗时=%.1fs",
                    task_id, game,
                    stats.get("mode", "?"),
                    stats.get("produced", 0),
                    stats.get("list_pages_ok", 0),
                    stats.get("list_end_reason", "?"),
                    elapsed,
                )

        logger.info(
            "[7881] task_id=%d: ✓ 全部完成（累计落库 %d 条）",
            task_id, batch_total[0],
        )
    except Exception:
        logger.exception("[7881] task_id=%d: ✗ 抓取异常", task_id)
    finally:
        _cleanup_stop_event(task_id)
        clear_active_task()


register_scanner(jingxi_7881_scanner)
logger.info("[7881] jingxi_7881_scanner 已注册到扫描器注册中心")
