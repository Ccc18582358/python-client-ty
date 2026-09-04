#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
7881 列表页爬虫.

Supports two list modes:
- full: normal sort
- latest: latest sort observed in the browser

Observed request signature:

    lb-timestamp = Date.now()
    lb-sign = md5(md5(reverse(SEED) + lb-timestamp) + JSON.stringify(payload))

The list response provides `goodsId`, which is used to build the sku URL:

    https://search.7881.com/{goodsId}.html

The sku page exposes the fields we need in:
- <meta name="description" content="...">
- <input type="hidden" id="price" value="1370.00"/>
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

# ── 价格缓存：批量查 DB，同价跳过详情 ──


def _app_root_db() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _query_existing_prices(product_ids: list) -> dict:
    if not product_ids:
        return {}
    db = os.path.join(_app_root_db(), "data", "price_monitor_v2.db")
    if not os.path.exists(db):
        return {}
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in product_ids)
        conn.execute(
            f"SELECT product_id, original_price FROM products "
            f"WHERE product_id IN ({placeholders}) ORDER BY id DESC",
            product_ids,
        )
        seen = set()
        result = {}
        for row in conn.fetchall():
            pid = row["product_id"]
            if pid not in seen:
                result[pid] = float(row["original_price"] or 0)
                seen.add(pid)
        conn.close()
        return result
    except Exception:
        return {}

logger = logging.getLogger("jingxi_spider_7881")

LIST_API_URL = "https://gw.7881.com/goods-service-api/api/goods/list"
LIST_REFERER = "https://search.7881.com/"
LIST_ORIGIN = "https://search.7881.com"
DETAIL_URL_TEMPLATE = "https://search.7881.com/{goods_id}.html"
SIGN_SEED = "5c2c538a3937c6db2d04bce3d03bbe88bl"

DEFAULT_HEADERS: Dict[str, str] = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "content-type": "application/json",
    "referer": LIST_REFERER,
    "origin": LIST_ORIGIN,
    "sec-ch-ua": '"Chromium";v="149", "Not)A;Brand";v="24", "Google Chrome";v="149"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
}

DETAIL_HEADERS: Dict[str, str] = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "cache-control": "max-age=0",
    "referer": "https://search.7881.com/A5468-100003-0-0-0.html?pageNum=1",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
}

CAPTCHA_TRIGGER_URL = "https://netsec-img-req-cn.zijieapi.com"

DEFAULT_PROXY_CONFIG: Dict[str, Any] = {
    "_comment": "enabled=true 使用代理，false 直连；打包后可直接修改这个文件",
    "enabled": False,
    "provider": "shenlong",
    "explicit_proxy": "",
    "cache_db": "shenlong_proxy.db",
    "cache_ttl_seconds": 150,
    "api_timeout_seconds": 6,
    "shenlong": {
        "_comment": "api_url 填神龙取 IP 接口，proxy_user/proxy_pass 填代理认证",
        "api_url": "http://api.shenlongip.com/ip?...",
        "proxy_user": "xxx",
        "proxy_pass": "xxx",
    },
}


class _CrawlerStopped(RuntimeError):
    pass


class _CaptchaTriggerDetected(RuntimeError):
    pass


def _md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _json_body(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _app_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _proxy_config_path() -> str:
    return os.path.join(_app_root(), "config", "proxy.json")


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_proxy_config() -> Dict[str, Any]:
    path = _proxy_config_path()
    if not os.path.exists(path):
        return deepcopy(DEFAULT_PROXY_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if not isinstance(data, dict):
            raise ValueError("proxy.json root must be object")
        return _deep_merge(DEFAULT_PROXY_CONFIG, data)
    except Exception as exc:
        logger.warning("load proxy config failed, using defaults: %s", exc)
        return deepcopy(DEFAULT_PROXY_CONFIG)


def _normalize_game_type(game_name: str) -> str:
    name = (game_name or "").strip()
    if "火影忍者" in name:
        return "火影忍者"
    return name


def _coerce_price(value: Any) -> Optional[float]:
    """7881 列表 API 价格已是"元"（如 659.00、500.00），直接转 float。"""
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _first_match(patterns: List[str], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.S)
        if match:
            return match.group(1).strip()
    return ""


def _extract_meta_description(html: str) -> str:
    return _first_match(
        [
            r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']*)["\']',
            r'<meta[^>]*content=["\']([^"\']*)["\'][^>]*name=["\']description["\']',
        ],
        html,
    )


def _extract_price(html: str) -> Optional[float]:
    raw = _first_match(
        [
            r'<input[^>]*id=["\']price["\'][^>]*value=["\']([^"\']*)["\']',
            r'<input[^>]*value=["\']([^"\']*)["\'][^>]*id=["\']price["\']',
        ],
        html,
    )
    # 详情页 HTML 价格已是"元"（如 value="1370.00"），不需要分→元转换
    try:
        return round(float(raw), 2)
    except (TypeError, ValueError):
        return None


def _response_contains_captcha_trigger(text: str) -> bool:
    return CAPTCHA_TRIGGER_URL in (text or "")


def _build_detail_headers(goods_id: str) -> Dict[str, str]:
    headers = dict(DETAIL_HEADERS)
    headers["referer"] = f"{LIST_ORIGIN}/{goods_id}.html"
    return headers


def build_lb_request(
    payload: Dict[str, Any],
    *,
    timestamp_ms: Optional[int] = None,
    delay_time: Optional[int] = None,
) -> Tuple[Dict[str, str], str]:
    ts = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
    if delay_time not in (None, "", 0, "0"):
        ts += int(delay_time)
    ts_text = str(ts)

    body = _json_body(payload)
    pub_key = _md5_hex(SIGN_SEED[::-1] + ts_text)
    sign = _md5_hex(pub_key + body)

    headers = dict(DEFAULT_HEADERS)
    headers["lb-timestamp"] = ts_text
    headers["lb-sign"] = sign
    return headers, body


def build_product_url(goods_id: str) -> str:
    return DETAIL_URL_TEMPLATE.format(goods_id=goods_id)


def build_product_info_from_detail(html: str, fallback_title: str = "") -> str:
    description = _extract_meta_description(html)
    return description or fallback_title


def build_result(
    item: Dict[str, Any],
    *,
    detail_html: str = "",
) -> Dict[str, Any]:
    goods_id = str(item.get("goodsId") or item.get("id") or "").strip()
    if detail_html:
        price = _extract_price(detail_html)
        price_source = "detail_html_input_id_price"
    else:
        price = _coerce_price(item.get("price") or item.get("salePrice") or item.get("amount"))
        price_source = "list_api_item_price"
    product_info = build_product_info_from_detail(
        detail_html,
        fallback_title=str(item.get("title") or item.get("goodsName") or item.get("name") or ""),
    )
    game_type = _normalize_game_type(str(item.get("gameName") or ""))
    return {
        "product_id": goods_id,
        "goods_id": goods_id,
        "product_info": product_info,
        "original_price": price,
        "url": build_product_url(goods_id) if goods_id else "",
        "game_type": game_type,
        "supports_realname": True,
        "raw_data": item,
        "detail_html": detail_html,
    }


# Known game ids used by the 7881 platform.
# When a game has multiple ids (e.g. Android + iOS), use a list.
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


def _list_payload(
    page_num: int,
    page_size: int,
    *,
    latest: bool,
    game_id: str = "A5468",
) -> Dict[str, Any]:
    return {
        "marketRequestSource": "search",
        "sellerType": "C",
        "gameId": game_id,
        "gtid": "100003",
        "tradePlace": "0",
        "goodsSortType": "6" if latest else "1",
        "extendAttrList": [],
        "pageNum": int(page_num),
        "pageSize": int(page_size),
    }


def fetch_goods_list_page(
    page_num: int,
    *,
    page_size: int = 30,
    timeout: float = 20.0,
    session: Optional[requests.Session] = None,
    proxies: Optional[Dict[str, str]] = None,
    verify_ssl: bool = True,
    delay_time: Optional[int] = None,
    retries: int = 3,
    latest: bool = False,
    game_id: str = "A5468",
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    if page_num < 1:
        raise ValueError("page_num must be >= 1")
    if page_size < 1:
        raise ValueError("page_size must be >= 1")

    payload = _list_payload(page_num, page_size, latest=latest, game_id=game_id)
    http = session or requests.Session()
    last_error: Optional[Exception] = None

    for attempt in range(max(1, retries)):
        if should_stop and should_stop():
            raise _CrawlerStopped("stopped before list request")
        headers, body = build_lb_request(payload, delay_time=delay_time)
        try:
            response = http.post(
                LIST_API_URL,
                headers=headers,
                data=body.encode("utf-8"),
                proxies=proxies,
                timeout=timeout,
                verify=verify_ssl,
            )
            response.raise_for_status()
            html = response.text or ""
            if _response_contains_captcha_trigger(html):
                raise _CaptchaTriggerDetected("captcha trigger url detected on list request")
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("list api returned non-object json")
            return data
        except Exception as exc:
            if isinstance(exc, _CaptchaTriggerDetected):
                raise
            last_error = exc
            if attempt + 1 >= max(1, retries):
                raise
            time.sleep(1.0 * (attempt + 1))

    raise RuntimeError(f"fetch_goods_list_page failed: {last_error}")


def fetch_goods_detail_page(
    goods_id: str,
    *,
    timeout: float = 20.0,
    session: Optional[requests.Session] = None,
    proxies: Optional[Dict[str, str]] = None,
    verify_ssl: bool = True,
    should_stop: Optional[Callable[[], bool]] = None,
) -> str:
    if not goods_id:
        raise ValueError("goods_id is empty")
    http = session or requests.Session()
    if should_stop and should_stop():
        raise _CrawlerStopped("stopped before detail request")
    response = http.get(
        build_product_url(goods_id),
        headers=_build_detail_headers(goods_id),
        proxies=proxies,
        timeout=timeout,
        verify=verify_ssl,
    )
    response.raise_for_status()
    return response.text or ""


def _extract_results(payload: Dict[str, Any]) -> Tuple[int, int, List[Dict[str, Any]]]:
    body = payload.get("body") or {}
    if not isinstance(body, dict):
        body = {}
    index = int(body.get("index") or 0)
    pages = int(body.get("pages") or 0)
    results = body.get("results") or []
    if not isinstance(results, list):
        results = []
    return index, pages, [item for item in results if isinstance(item, dict)]


class Jingxi7881Crawler:
    """Minimal 7881 crawler for list and detail pages."""

    def __init__(
        self,
        *,
        page_size: int = 30,
        timeout: float = 20.0,
        verify_ssl: bool = True,
        retries: int = 3,
        delay_time: Optional[int] = None,
        session: Optional[requests.Session] = None,
        proxy: Optional[str] = None,
        is_proxy: Optional[bool] = None,
        game_id: str = "A5468",
    ) -> None:
        self.page_size = max(1, int(page_size))
        self.timeout = float(timeout)
        self.verify_ssl = bool(verify_ssl)
        self.retries = max(1, int(retries))
        self.delay_time = delay_time
        self.game_id = game_id
        self.session = session or requests.Session()
        proxy_cfg = load_proxy_config()
        explicit_proxy = str(proxy_cfg.get("explicit_proxy") or "").strip()
        self.proxy = proxy or explicit_proxy or None
        self.is_proxy = bool(proxy_cfg.get("enabled", False)) if is_proxy is None else bool(is_proxy)
        # 实例级缓存已迁移到 services.proxy_manager (SQLite 单例)，本类不再持有
        # 旧字段 _proxy_cache_ip/_proxy_cache_time 已在 C3 删除

    def fetch_page(
        self,
        page_num: int,
        *,
        latest: bool = False,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        force_refresh_next = False
        for attempt in range(self.retries):
            force_refresh = force_refresh_next
            proxies = self._get_proxies(
                force_refresh=force_refresh,
                reason=f"list_p{page_num}_a{attempt+1}",
            )
            force_refresh_next = False
            if proxies is None and force_refresh:
                # budget 满或扣费失败 → 当次放弃, 不重试(给 budget 留时间窗)
                logger.warning("[7881] 列表 budget/扣费拒绝, page=%s 当次放弃", page_num)
                return self._empty_page_response(page_num)
            try:
                data = fetch_goods_list_page(
                    page_num,
                    page_size=self.page_size,
                    timeout=self.timeout,
                    session=self.session,
                    proxies=proxies,
                    verify_ssl=self.verify_ssl,
                    delay_time=self.delay_time,
                    retries=1,
                    latest=latest,
                    game_id=self.game_id,
                    should_stop=should_stop,
                )
                index, pages, raw_results = _extract_results(data)
                goods_ids = [str(item.get("goodsId") or item.get("id") or "").strip() for item in raw_results]
                goods_ids = [gid for gid in goods_ids if gid]
                return {
                    "page_num": page_num,
                    "index": index,
                    "pages": pages,
                    "goods_ids": goods_ids,
                    "raw": data,
                    "results": [build_result(item) for item in raw_results],
                }
            except _CaptchaTriggerDetected as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    logger.warning(
                        "list request hit captcha trigger url page=%s; refreshing proxy and retrying once",
                        page_num,
                    )
                    force_refresh_next = True
                    time.sleep(1.0)
                    continue
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "list request failed page=%s attempt=%s proxy_refresh=%s: %s",
                    page_num,
                    attempt + 1,
                    force_refresh,
                    exc,
                )
                if attempt + 1 < self.retries:
                    time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"fetch_page failed page={page_num}: {last_error}")

    def fetch_detail(
        self,
        goods_id: str,
        *,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        force_refresh_next = False
        for attempt in range(self.retries):
            force_refresh = force_refresh_next
            proxies = self._get_proxies(
                force_refresh=force_refresh,
                reason=f"detail_{goods_id}_a{attempt+1}",
            )
            force_refresh_next = False
            if proxies is None and force_refresh:
                # budget 满 → 当次放弃
                raise _CaptchaTriggerDetected(
                    f"detail captcha but proxy budget exhausted ({goods_id})"
                )
            try:
                html = fetch_goods_detail_page(
                    goods_id,
                    timeout=self.timeout,
                    session=self.session,
                    proxies=proxies,
                    verify_ssl=self.verify_ssl,
                    should_stop=should_stop,
                )
                if _response_contains_captcha_trigger(html):
                    if attempt == 0:
                        logger.warning(
                            "detail request hit captcha trigger url goods_id=%s; refreshing proxy and retrying once",
                            goods_id,
                        )
                        force_refresh_next = True
                        time.sleep(1.0)
                        continue
                    raise _CaptchaTriggerDetected("captcha trigger url detected on detail request")
                return {
                    "goods_id": goods_id,
                    "url": build_product_url(goods_id),
                    "product_info": build_product_info_from_detail(html),
                    "original_price": _extract_price(html),
                    "detail_html": html,
                }
            except Exception as exc:
                if isinstance(exc, _CaptchaTriggerDetected):
                    raise
                last_error = exc
                logger.warning(
                    "detail request failed goods_id=%s attempt=%s proxy_refresh=%s: %s",
                    goods_id,
                    attempt + 1,
                    force_refresh,
                    exc,
                )
                if attempt + 1 < self.retries:
                    time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"fetch_detail failed goods_id={goods_id}: {last_error}")

    @staticmethod
    def _empty_page_response(page_num: int) -> Dict[str, Any]:
        """budget 满时, 列表返回空页（让上层继续翻页, 不阻塞 scanner）"""
        return {
            "page_num": page_num,
            "index": 0,
            "pages": 0,
            "goods_ids": [],
            "raw": {"body": {"results": [], "pages": 0, "index": 0}},
            "results": [],
        }

    def _collect_page(
        self,
        page_num: int,
        *,
        latest: bool,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        if should_stop and should_stop():
            raise _CrawlerStopped("stopped before page collect")
        page_data = self.fetch_page(page_num, latest=latest, should_stop=should_stop)
        # ── 价格对比：批量查 DB，同价跳过详情 ──
        batch_ids = [str(r.get("goods_id") or r.get("product_id") or "").strip() for r in page_data["results"]]
        batch_ids = [gid for gid in batch_ids if gid]
        existing_prices = _query_existing_prices(batch_ids)
        results: List[Dict[str, Any]] = []
        for item in page_data["results"]:
            if should_stop and should_stop():
                raise _CrawlerStopped("stopped during page collect")
            goods_id = str(item.get("goods_id") or item.get("product_id") or "").strip()
            detail_html = ""
            if goods_id:
                list_price = _coerce_price(
                    item.get("original_price") or item.get("price") or item.get("salePrice")
                )
                if goods_id in existing_prices and list_price is not None:
                    if abs(existing_prices[goods_id] - list_price) < 0.01:
                        logger.debug("7881 skip detail goods_id=%s (price unchanged)", goods_id)
                else:
                    try:
                        detail_html = self.fetch_detail(goods_id, should_stop=should_stop)["detail_html"]
                    except Exception as exc:
                        logger.warning("detail fetch failed goods_id=%s: %s", goods_id, exc)
            results.append(build_result(item.get("raw_data") or item, detail_html=detail_html))
        return {
            "page_num": page_num,
            "index": page_data.get("index"),
            "pages": page_data.get("pages"),
            "goods_ids": page_data.get("goods_ids") or [],
            "results": results,
            "raw": page_data["raw"],
        }

    def run_full(
        self,
        *,
        on_batch: Callable[[Dict[str, Any]], Any],
        on_progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        return self._run(
            mode="full",
            pages=None,
            on_batch=on_batch,
            on_progress=on_progress,
            should_stop=should_stop,
        )

    def run_latest(
        self,
        *,
        pages: int,
        on_batch: Callable[[Dict[str, Any]], Any],
        on_progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        return self._run(
            mode="latest",
            pages=max(1, int(pages)),
            on_batch=on_batch,
            on_progress=on_progress,
            should_stop=should_stop,
        )

    def _run(
        self,
        *,
        mode: str,
        pages: Optional[int],
        on_batch: Callable[[Dict[str, Any]], Any],
        on_progress: Optional[Callable[[Dict[str, Any]], Any]],
        should_stop: Optional[Callable[[], bool]],
    ) -> Dict[str, Any]:
        produced = 0
        pages_ok = 0
        last_total_pages: Optional[int] = None
        page_num = 1

        while True:
            if should_stop and should_stop():
                break
            if pages is not None and page_num > pages:
                break

            try:
                page_data = self._collect_page(
                    page_num,
                    latest=(mode == "latest"),
                    should_stop=should_stop,
                )
            except _CrawlerStopped:
                break
            pages_ok += 1
            last_total_pages = page_data.get("pages", last_total_pages)
            results = page_data["results"]
            if results:
                on_batch(
                    {
                        "platform": "7881",
                        "page_num": page_num,
                        "results": results,
                    }
                )
                produced += len(results)
            if on_progress:
                on_progress(
                    {
                        "page_index": page_num,
                        "list_pages_ok": pages_ok,
                        "produced": produced,
                        "consumed": produced,
                        "total": last_total_pages,
                    }
                )

            if pages is None and last_total_pages is not None and page_num >= int(last_total_pages):
                break
            page_num += 1

        return {
            "stats": {
                "mode": mode,
                "produced": produced,
                "list_pages_ok": pages_ok,
                "list_end_reason": "stop" if should_stop and should_stop() else "completed",
            }
        }

    def _get_proxies(self, *, force_refresh: bool = False, reason: str = "") -> Optional[Dict[str, str]]:
        """7881 同步路径代理获取：
        - 默认（force_refresh=False）：TTL 命中 + 必要扣费，**不消耗 budget**
        - force_refresh=True（captcha 路径触发）：budget 允许 → 扣费切 IP，否则 return None 让 caller 当次放弃
        - explicit_proxy 短路
        - 扣费连续失败时降级为直连，不阻塞爬虫（不含死循环）
        """
        if not self.is_proxy:
            logger.debug("[7881] 代理未启用，使用直连")
            return None
        if self.proxy:
            logger.debug("[7881] 使用显式代理: %s", self.proxy)
            return {"http": self.proxy, "https": self.proxy}

        from services.proxy_manager import get_manager as _get_pm
        mgr = _get_pm("7881")

        # 最多等待 300s（5 分钟），超时降级直连
        max_wait = 300
        waited = 0
        while True:
            proxies = (
                mgr.force_rotate(reason=reason or "captcha")
                if force_refresh else mgr.get_proxy()
            )
            wait_seconds = mgr.paid_cooldown_remaining()
            if proxies is not None:
                logger.info("[7881] 获取代理成功, reason=%s", reason)
                return proxies
            if wait_seconds <= 0:
                logger.info("[7881] 代理不可用（扣费失败/未开通/付费墙），使用直连, reason=%s", reason)
                return None
            if waited >= max_wait:
                logger.warning(
                    "[7881] 扣费接口持续冷却超过 %ds，放弃等待，使用直连, reason=%s",
                    max_wait, reason,
                )
                return None
            logger.warning(
                "[7881] 扣费接口冷却中，等待 %.1fs 后重试（已等 %.1fs/%ds）, reason=%s",
                wait_seconds, waited, max_wait, reason,
            )
            time.sleep(min(wait_seconds, 30))  # 每次最多睡 30s，避免一次性睡太久
            waited += wait_seconds


__all__ = [
    "GAME_ID_MAP",
    "Jingxi7881Crawler",
    "LIST_API_URL",
    "DETAIL_URL_TEMPLATE",
    "build_lb_request",
    "build_product_info_from_detail",
    "build_product_url",
    "build_result",
    "fetch_goods_detail_page",
    "fetch_goods_list_page",
]
