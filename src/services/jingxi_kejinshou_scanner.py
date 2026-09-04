#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
氪金兽 (kejinshou.com) 爬虫适配器 — 接入 python-client GUI 调度系统。

═══════════════════════════════════════════════════════════════════
                      爬 虫 作 者 必 读
═══════════════════════════════════════════════════════════════════

【注册方式】
  在 main_window.py 中 `import services.jingxi_kejinshou_scanner` 即可自动注册。
  爬虫函数 `kejinshou_scanner` 已被 register_scanner() 注册到全局注册中心。

【调度流程】
  1. ScanScheduler 每 30s tick → 判定哪些 task 该执行
  2. 调度器调 run_scanners() → 调 kejinshou_scanner(task_id, payload)
  3. kejinshou_scanner 创建 JingxiKejinshouCrawler 实例
  4. 根据 payload["scan_mode"] 选择 run_full() 或 run_latest()
  5. on_batch 回调 → submit_scan_results() 落库
  6. scanner_service 自动推入 EstimationWorker 估价队列

【payload 字段】
  {
      "task_id":   1,
      "task_name": "氪金兽火影日扫",
      "platforms": ["氪金兽"],
      "games":     ["火影忍者"],
      "scan_mode": "latest" / "full",
      "scan_limit": 5,         # 最新模式下的页数（每页约60条）
      "pricing":   {"secondary_real_name_ratio": 0.1, "platform_bargain_ratio": 0.05},
  }

【鉴权配置】
  h5_token / h5_token_enc 读取顺序:
    1. config/kejinshou.json 中的 h5_token / h5_token_enc 字段
    2. 环境变量 KEJINSHOU_H5_TOKEN / KEJINSHOU_H5_TOKEN_ENC
  两者都缺失时抛错。

【游戏 ID 映射】
  氪金兽平台不同游戏对应不同 gameId:
    火影忍者 → 170
  后续上新游戏时在 GAME_ID_MAP 中追加即可。

【支持的模式】
  - full (全量抓取):   run_full(), 翻完所有商品自动停止
  - latest (最新抓取): run_latest(pages=N), 抓最新 N 页

═══════════════════════════════════════════════════════════════════
"""
import asyncio
import json
import logging
import os
import sys
import threading
from typing import Dict, Optional

from services.jingxi_spider_kejinshou import JingxiKejinshouCrawler
from services.scanner_registry import register_scanner
from services.scanner_service import submit_scan_results
from services.proxy_manager import set_active_task, clear_active_task

logger = logging.getLogger(__name__)

# ── 游戏名 → 氪金兽 gameId 映射 ──
GAME_ID_MAP: Dict[str, str] = {
    "火影忍者": "170",
    "王者荣耀": "1",
    "三角洲行动": "7476",
    "金铲铲之战": "221",
    "QQ飞车手游": "211",
    "使命召唤手游": "385",
    "无畏契约": "1001",
    "穿越火线": "57",
}

# ── 停止事件（按 task_id 索引，支持多任务独立停止）──
_stop_events: Dict[int, threading.Event] = {}
_stop_events_lock = threading.Lock()


def _app_root() -> str:
    """开发环境=项目根目录;打包后=exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_kejinshou_config() -> Dict[str, str]:
    """加载氪金兽鉴权配置。

    优先级: config/kejinshou.json > 环境变量

    Returns:
        {"h5_token": "...", "h5_token_enc": "..."}

    Raises:
        RuntimeError: 两者都缺失时
    """
    h5_token: Optional[str] = None
    h5_token_enc: Optional[str] = None

    # 1) 尝试 config/kejinshou.json
    config_path = os.path.join(_app_root(), "config", "kejinshou.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
            if isinstance(cfg, dict):
                h5_token = str(cfg.get("h5_token") or "").strip() or None
                h5_token_enc = str(cfg.get("h5_token_enc") or "").strip() or None
        except Exception as e:
            logger.warning("读取 kejinshou.json 失败: %s", e)

    # 2) 环境变量兜底
    if not h5_token:
        h5_token = os.environ.get("KEJINSHOU_H5_TOKEN", "").strip() or None
    if not h5_token_enc:
        h5_token_enc = os.environ.get("KEJINSHOU_H5_TOKEN_ENC", "").strip() or None

    if not h5_token or not h5_token_enc:
        raise RuntimeError(
            "氪金兽 h5_token / h5_token_enc 未配置。"
            "请在 config/kejinshou.json 中设置，或设置环境变量 "
            "KEJINSHOU_H5_TOKEN / KEJINSHOU_H5_TOKEN_ENC"
        )

    return {"h5_token": h5_token, "h5_token_enc": h5_token_enc}


def stop_task(task_id: int) -> None:
    """通知指定任务的爬虫停止（由 GUI 状态切换触发）。"""
    with _stop_events_lock:
        event = _stop_events.get(task_id)
    if event:
        event.set()
        logger.info(f"[氪金兽] task_id={task_id} 已发送停止信号")


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


def kejinshou_scanner(task_id: int, payload: dict) -> None:
    """氪金兽平台爬虫适配器。

    自动根据 payload["scan_mode"] 选择:
      - "full"   → run_full()    全量抓取
      - "latest" → run_latest()  最新抓取

    只处理 payload["platforms"] 中包含 "氪金兽" 的任务。
    """
    platforms = payload.get("platforms") or []
    games = payload.get("games") or []
    scan_mode = payload.get("scan_mode", "latest") or "latest"
    scan_limit = payload.get("scan_limit", 50) or 50
    task_name = payload.get("task_name", "?")

    # ── 只处理氪金兽平台 ──
    if "氪金兽" not in platforms:
        logger.debug("[氪金兽] task_id=%d: 不包含氪金兽平台，跳过（platforms=%s）", task_id, platforms)
        return

    logger.info(
        "[氪金兽] task_id=%d '%s': ▷ 开始扫描（games=%s mode=%s limit=%d）",
        task_id, task_name, games, scan_mode, scan_limit,
    )

    # ── 加载鉴权配置 ──
    try:
        auth = _load_kejinshou_config()
    except RuntimeError as e:
        logger.error("[氪金兽] task_id=%d: ✗ 鉴权失败: %s", task_id, e)
        return

    set_active_task(task_id)

    # ── 逐个游戏抓取 ──
    for game in games:
        game_id = GAME_ID_MAP.get(game)
        if not game_id:
            logger.warning(
                "[氪金兽] task_id=%d: 不支持的游戏 '%s'，请在 GAME_ID_MAP 中添加映射",
                task_id, game,
            )
            continue

        logger.info(
            "[氪金兽] task_id=%d: 抓取 game=%s(id=%s) mode=%s pages=%d",
            task_id, game, game_id, scan_mode, max(1, int(scan_limit)),
        )

        # ── 构造爬虫实例 ──
        crawler = JingxiKejinshouCrawler(
            h5_token=auth["h5_token"],
            h5_token_enc=auth["h5_token_enc"],
            game_id=game_id,
            game_name=game,
            platform="氪金兽",
            batch_size=30,
            requests_per_second=2.0,
            max_pages=2000,
            page_size=60,
            is_proxy=None,  # None = 读取 config/proxy.json
        )

        # ── 重置停止事件 ──
        stop_evt = _get_stop_event(task_id)
        stop_evt.clear()

        # ── on_batch: 爬虫回调 → 落库 ──
        _batch_total = [0]

        def on_batch(batch: dict) -> None:
            """爬虫每攒够 batch_size 条触发一次。

            batch = {
                "task_id":  "e1501640192c...",
                "platform": "氪金兽",
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
                    "[氪金兽] task_id=%d: 本批落库 %d/%d 条（累计 %d）",
                    task_id, n, len(batch["results"]), _batch_total[0],
                )
            except Exception:
                logger.exception("[氪金兽] task_id=%d: on_batch 落库失败", task_id)

        # ── on_progress: 进度回调 → 日志 ──
        _progress_page = [0]

        def on_progress(state: dict) -> None:
            """爬虫每翻一页或每消费一批触发一次（高频，已内置 0.2s 去抖）。"""
            pg = state.get("page")
            if pg is not None:
                _progress_page[0] = pg

            pages_ok = state.get("list_pages_ok", 0)
            produced = state.get("produced", 0)

            if pg is not None and pg % 5 == 1:  # 每 5 页打一条
                logger.info(
                    "[氪金兽] task_id=%d: 翻页进度 第%d页（请求%d页 产出%d）",
                    task_id, pg, pages_ok, produced,
                )

        # ── 选择模式并运行 ──
        try:
            t0 = __import__('time').monotonic()
            if scan_mode == "full":
                result = asyncio.run(crawler.run_full(
                    on_batch=on_batch,
                    on_progress=on_progress,
                    should_stop=stop_evt.is_set,
                ))
            else:
                pages = max(1, int(scan_limit))
                result = asyncio.run(crawler.run_latest(
                    pages=pages,
                    on_batch=on_batch,
                    on_progress=on_progress,
                    should_stop=stop_evt.is_set,
                ))
            elapsed = __import__('time').monotonic() - t0

            stats = result.get("stats", {})
            logger.info(
                "[氪金兽] task_id=%d: game=%s 抓取完成 | "
                "mode=%s 产出=%d 请求页=%d 结束原因=%s 耗时=%.1fs",
                task_id, game,
                stats.get("mode", "?"),
                stats.get("produced", 0),
                stats.get("list_pages_ok", 0),
                stats.get("list_end_reason", "?"),
                elapsed,
            )

        except Exception:
            logger.exception("[氪金兽] task_id=%d: ✗ 抓取异常", task_id)
        finally:
            _cleanup_stop_event(task_id)

    logger.info(
        "[氪金兽] task_id=%d: ✓ 全部完成（累计落库 %d 条）",
        task_id, _batch_total[0],
    )
    clear_active_task()


# ── 自动注册（main_window.py import 此模块即生效）──
register_scanner(kejinshou_scanner)
logger.info("[氪金兽] kejinshou_scanner 已注册到扫描器注册中心")
