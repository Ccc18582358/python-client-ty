#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
鲸汐(氪金兽 kejinshou.com) 商品账号爬虫。

职责(只做这几件事):
    1. 翻页拉取商品列表(H5 搜索接口,GET 请求)。
    2. 提取列表中的 id、title、subTitle、price 等字段。
    3. 并发请求商品详情接口(mwp.kjs_product.product.detail)。
    4. 通过回调(on_batch / on_progress / should_stop)逐批返回给 GUI。

不在职责范围:
    - 不写数据库。
    - 不依赖 Django。
    - 不做算法筛选。
    - 不导出 Excel。

用法(示例):
    import asyncio
    import threading

    from services.jingxi_spider_kejinshou import JingxiKejinshouCrawler

    stop_event = threading.Event()

    def on_batch(batch):
        for it in batch["results"]:
            print(it)

    crawler = JingxiKejinshouCrawler(
        h5_token="4feda9296438b4fa52a74e7a31d80f77_1782272528875",
        h5_token_enc="55886cdbe6722b0a371d5afe4ed0c9ac",
        game_id="170",
        game_name="火影忍者",
    )
    threading.Thread(
        target=lambda: asyncio.run(crawler.run_full(
            on_batch=on_batch,
            on_progress=None,
            should_stop=stop_event.is_set,
        )),
        daemon=True,
    ).start()

注意:
    - 需要 h5_token 和 h5_token_enc(可从浏览器抓包中获取)。
    - 列表接口: GET https://api.kejinshou.com/h5/mwp.kjs_search.product.search/1.0
    - 详情接口: GET https://api.kejinshou.com/h5/mwp.kjs_product.product.detail/1.0
    - 签名算法: MD5(与 kejinshou.py get_en_post_data 一致,适配为 GET query params)。
    - 商品页 URL: https://www.kejinshou.com/goods/details/{id}
    - 代理: 默认自动获取神龙代理(本地 SQLite 缓存 150s),传 proxy 参数可覆盖。
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import sqlite3
import sys
import time
import uuid
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import httpx
import requests

# ── 价格缓存：批量查 DB，同价跳过详情 ──


def _app_root_db() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _query_existing_prices(product_ids: List[str]) -> Dict[str, float]:
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
        seen: Set[str] = set()
        result: Dict[str, float] = {}
        for row in conn.fetchall():
            pid = row["product_id"]
            if pid not in seen:
                result[pid] = float(row["original_price"] or 0)
                seen.add(pid)
        conn.close()
        return result
    except Exception:
        return {}

logger = logging.getLogger("jingxi_spider_kejinshou")

# ---------------- 常量 ---------------- #

# 商品详情页 URL(仅记录,不请求)
PRODUCT_URL_TEMPLATE = "https://www.kejinshou.com/goods/details/{product_id}"

# 列表接口(H5 搜索)
DEFAULT_LIST_URL = "https://api.kejinshou.com/h5/mwp.kjs_search.product.search/1.0"
# 详情接口
DEFAULT_DETAIL_URL = "https://api.kejinshou.com/h5/mwp.kjs_product.product.detail/1.0"

# 列表接口 URI 标识
LIST_URI = "mwp.kjs_search.product.search"
# 详情接口 URI 标识
DETAIL_URI = "mwp.kjs_product.product.detail"
API_VERSION = "1.0"

# 默认请求头(取自 H5 浏览器抓包)
DEFAULT_HEADERS = {
    "accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Origin": "https://www.kejinshou.com",
    "Referer": "https://www.kejinshou.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "content-type": "application/x-www-form-urlencoded",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# ── 风控相关已迁移到 ProxyManager（统一代理管理），此处不再持常量
#    - 旧 RISK_STOP_THRESHOLD / RISK_PAUSE_SECONDS / TOO_MANY_REQUESTS_PAUSE 已删除
#    - 氪金兽与螃蟹对称：不主动切 IP，仅 TTL 自然换

# ---------------- 神龙代理(与 pangxie.py 共用同一套配置和缓存) ---------------- #

DEFAULT_PROXY_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "provider": "shenlong",
    "explicit_proxy": "",
    "cache_db": "shenlong_proxy.db",
    "cache_ttl_seconds": 150,
    "api_timeout_seconds": 6,
    "shenlong": {
        "api_url": (
            "http://api.shenlongip.com/ip"
            "?key=h9emnx4j&protocol=1&mr=1&pattern=json"
            "&need=1001&count=1"
            "&sign=1e76b52ad5f8dfc5f4337a0da58b3d15"
        ),
        "proxy_user": "uemi6f",
        "proxy_pass": "jaow4843",
    },
}


def _app_root() -> str:
    """开发环境=项目根目录;打包后=exe 所在目录。"""
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
    """读取 config/proxy.json;缺失或解析失败时使用内置默认值。"""
    path = _proxy_config_path()
    if not os.path.exists(path):
        return deepcopy(DEFAULT_PROXY_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if not isinstance(data, dict):
            raise ValueError("proxy.json root must be object")
        return _deep_merge(DEFAULT_PROXY_CONFIG, data)
    except Exception as e:
        logger.warning("load proxy config failed, using defaults: %s", e)
        return deepcopy(DEFAULT_PROXY_CONFIG)


def is_proxy_enabled() -> bool:
    return bool(load_proxy_config().get("enabled", False))


def _proxy_cache_ttl() -> int:
    cfg = load_proxy_config()
    try:
        return max(1, int(cfg.get("cache_ttl_seconds", 150)))
    except (TypeError, ValueError):
        return 150


# ---------------- 工具函数(来自 kejinshou.py) ---------------- #


def md5_hex(text: str) -> str:
    """MD5 十六进制字符串。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def get_h5_xid() -> str:
    """
    生成 H5 端的 mw-id。
    格式: h-<md5>-5 (PC 端为 p-...-c)。
    """
    timestamp = int(time.time() * 1000)
    random_value = random.random()
    result = math.floor(random_value * timestamp)
    s = f"poppy-util{result}"
    a = md5_hex(s)
    return "h-" + a + "-5"


def build_product_url(product_id: str) -> str:
    """构建商品详情页 URL(仅记录,不请求)。"""
    return PRODUCT_URL_TEMPLATE.format(product_id=product_id)


def _coerce_price(value: Any) -> Optional[float]:
    """
    氪金兽列表里 price 已经是元(字符串 "1088" 即 1088 元)。
    直接转 float,失败返回 None。
    """
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


# ── 签名时排除的 key(与服务端 JS KEYS_OMIT_SIGN 一致) ──
_KEYS_OMIT_SIGN: Set[str] = {"mw-pv", "mw-sign", "mw-did", "mw-sid"}


def _build_signed_params(
    h5_token: str,
    h5_token_enc: str,
    data_dict: Dict[str, Any],
    uri: str,
) -> Dict[str, str]:
    """
    构建 H5 接口的签名 query params(GET 请求用)。

    签名逻辑与 kejinshou.com 前端 JS calcMwpSign 完全一致:
        1. 提取所有 mw-* 开头的 key,排除 KEYS_OMIT_SIGN
        2. 按 key 排序,取 values
        3. 追加 uri + version + MD5(data_str) + h5_token
        4. join "&" 后 MD5 → mw-sign

    Args:
        h5_token: H5 token
        h5_token_enc: H5 token 加密版
        data_dict: 业务参数(如 {"gameId":"170","cateId":11,...})
        uri: API URI(如 mwp.kjs_search.product.search)

    Returns:
        dict of query params(含 mw-sign, data 等),每次调用都是新的时间戳和签名
    """
    data_str = json.dumps(data_dict, ensure_ascii=False, separators=(",", ":"))

    params: Dict[str, str] = {
        "mw-appkey": "100222",
        "mw-pv": "H5",
        "mw-t": str(int(time.time() * 1000)),
        "mw-h5-token": h5_token,
        "mw-h5-token-enc": h5_token_enc,
        "mw-k3": "h5-nature-google-pc",
        "mw-k5": "0",
        "mw-k6": "",
        "mw-k7": "h5",
        "mw-k8": "",
        "mw-k9": "h5",
        "mw-id": get_h5_xid(),
        "mw-device": "undefined/1920/1080",
        "mw-os": "Windows/10",
        "mw-ver": "h5-nuxt/3.41.10",
        "mw-runon": "Chrome/150.0.0.0",
        "mw-smid": "",
        "mw-sid": "",
    }

    # 签名: 与 JS calcMwpSign 一致
    # 1) 过滤: 只保留 mw-* 开头且不在 KEYS_OMIT_SIGN 中的 key
    sign_keys = sorted(
        k for k in params
        if k.startswith("mw-") and k not in _KEYS_OMIT_SIGN
    )
    tmp_list: List[str] = [params[k] for k in sign_keys]
    # 2) 追加 URI + version
    tmp_list.append(uri)
    tmp_list.append(API_VERSION)
    # 3) MD5(data 字段的值)
    tmp_list.append(md5_hex(data_str))
    # 4) h5_token(非空时才追加,与 JS 的 c&&s.push(c) 一致)
    if h5_token:
        tmp_list.append(h5_token)
    # 5) join & → MD5
    mw_sign = md5_hex("&".join(tmp_list))

    params["mw-sign"] = mw_sign
    params["data"] = data_str
    return params


# ---------------- 限速器(全局,所有请求共用) ---------------- #


class RateLimiter:
    """
    令牌桶 + 随机抖动:每 1/requests_per_second 秒放一个令牌,
    实际放行时再额外加 0~jitter_max 秒的随机抖动,避免被识别成机器人节拍。
    """

    def __init__(self, requests_per_second: float, jitter_max: float = 0.0):
        self._interval = 1.0 / max(requests_per_second, 0.1)
        self._jitter_max = max(0.0, jitter_max)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                wait = self._last + self._interval - now
                if wait <= 0:
                    self._last = now
                    break
            await asyncio.sleep(wait)
        if self._jitter_max > 0:
            jitter = random.uniform(0.0, self._jitter_max)
            if jitter > 0:
                await asyncio.sleep(jitter)


# ---------------- 爬虫主体 ---------------- #


class JingxiKejinshouCrawler:
    """
    氪金兽独立爬虫,不依赖 Django。
    由 GUI 在后台线程调用 run_full / run_latest。
    """

    def __init__(
        self,
        *,
        # 鉴权(必填)
        h5_token: str,
        h5_token_enc: str,
        # 游戏
        game_id: str = "170",
        game_name: str = "",
        cate_id: int = 11,
        platform: str = "氪金兽",
        # 接口(可按需覆盖)
        list_url: str = DEFAULT_LIST_URL,
        detail_url: str = DEFAULT_DETAIL_URL,
        base_headers: Optional[Dict[str, str]] = None,
        # 网络
        proxy: Optional[str] = None,
        is_proxy: Optional[bool] = None,  # None=读 proxy.json; True=代理; False=直连
        # 调度
        detail_concurrency: int = 3,
        requests_per_second: float = 2.0,
        queue_size: int = 300,
        timeout: float = 20.0,
        max_retries: int = 3,
        batch_size: int = 30,
        max_pages: int = 2000,
        page_size: int = 60,
        verify_ssl: bool = False,
        # 防封
        jitter_max: float = 0.5,
        risk_pause_seconds: float = 120.0,
        # 测试用:注入 httpx 传输层
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.h5_token = h5_token
        self.h5_token_enc = h5_token_enc
        self.game_id = game_id
        self.game_name = game_name
        self.cate_id = cate_id
        self.platform = platform
        self.list_url = list_url
        self.detail_url = detail_url
        self.page_size = page_size

        proxy_cfg = load_proxy_config()
        explicit_proxy = str(proxy_cfg.get("explicit_proxy") or "").strip()
        self.proxy = proxy or explicit_proxy or None
        self.is_proxy = bool(proxy_cfg.get("enabled", False)) if is_proxy is None else bool(is_proxy)

        self.detail_concurrency = max(1, detail_concurrency)
        self.requests_per_second = max(0.1, requests_per_second)
        self.queue_size = max(1, queue_size)
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.batch_size = max(1, batch_size)
        self.max_pages = max(1, max_pages)
        self.verify_ssl = verify_ssl
        self.jitter_max = max(0.0, jitter_max)
        self.risk_pause_seconds = max(0.0, risk_pause_seconds)
        self._transport = transport

        self._base_headers = dict(DEFAULT_HEADERS)
        if base_headers:
            self._base_headers.update(base_headers)

    # ---------------- 公共入口 ---------------- #

    async def run_full(
        self,
        *,
        on_batch: Callable[[Dict[str, Any]], Any],
        on_progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """
        全量抓取:不带 originOrder,翻到 pagination.pages 为止。

        data = {"gameId": "170", "cateId": 11, "type": "goods", "size": 60, "page": 1}
        """
        return await self._run_pipeline(
            mode="full",
            max_pages_override=None,
            on_batch=on_batch,
            on_progress=on_progress,
            should_stop=should_stop,
        )

    async def run_latest(
        self,
        *,
        pages: int,
        on_batch: Callable[[Dict[str, Any]], Any],
        on_progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """
        最新发布:带 originOrder="upper_at_desc",抓够 pages 页就停。

        data = {"gameId": "170", "cateId": 11, "type": "goods", "originOrder": "upper_at_desc", "size": 60, "page": 1}
        """
        return await self._run_pipeline(
            mode="latest",
            max_pages_override=max(1, int(pages)),
            on_batch=on_batch,
            on_progress=on_progress,
            should_stop=should_stop,
        )

    # ---------------- 流水线(列表生产者 + 详情消费者) ---------------- #

    async def _run_pipeline(
        self,
        *,
        mode: str,
        max_pages_override: Optional[int],
        on_batch: Callable[[Dict[str, Any]], Any],
        on_progress: Optional[Callable[[Dict[str, Any]], Any]],
        should_stop: Optional[Callable[[], bool]],
    ) -> Dict[str, Any]:
        task_id = uuid.uuid4().hex
        seen_product_ids: Set[str] = set()
        batch_buffer: List[Dict[str, Any]] = []

        limiter = RateLimiter(self.requests_per_second, self.jitter_max)
        risk_streak = 0

        stats: Dict[str, Any] = {
            "task_id": task_id,
            "mode": mode,
            "platform": self.platform,
            "game_id": self.game_id,
            "game_name": self.game_name,
            "list_pages_ok": 0,
            "list_pages_fail": 0,
            "detail_ok": 0,
            "detail_fail": 0,
            "detail_skip": 0,
            "detail_repeat": 0,
            "produced": 0,
            "consumed": 0,
            "list_end_reason": "",
            "risk_paused": False,
        }

        last_progress_ts = 0.0

        def emit_progress(extra: Optional[Dict[str, Any]] = None) -> None:
            nonlocal last_progress_ts
            if not on_progress:
                return
            now = time.monotonic()
            if extra is None and now - last_progress_ts < 0.2:
                return
            last_progress_ts = now
            payload = dict(stats)
            payload.update(extra or {})
            try:
                on_progress(payload)
            except Exception:
                logger.exception("on_progress callback failed")

        def flush_buffer(force: bool = False) -> None:
            if not batch_buffer:
                return
            if force or len(batch_buffer) >= self.batch_size:
                payload = batch_buffer[:]
                batch_buffer.clear()
                batch_wrapped = {
                    "task_id": task_id,
                    "platform": self.platform,
                    "results": payload,
                }
                try:
                    on_batch(batch_wrapped)
                except Exception:
                    logger.exception("on_batch callback failed")

        # 构建 httpx 客户端
        client_kwargs: Dict[str, Any] = {
            "timeout": httpx.Timeout(self.timeout),
            "verify": self.verify_ssl,
            "headers": dict(self._base_headers),
            "http2": False,
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        # 代理
        if not self.is_proxy:
            proxy_state = None
            _proxy_ip = "direct"
        elif self.proxy:
            client_kwargs["proxy"] = self.proxy
            proxy_state = None
            _proxy_ip = self.proxy
        else:
            # ── 统一代理管理：ProxyManager 接管 TTL/扣费/付费墙 ──
            from services.proxy_manager import get_manager as _get_pm
            proxy_dict = _get_pm("kejinshou").get_proxy()
            if proxy_dict is None:
                # 付费墙 / 扣费失败 / 神龙失败 → 直连
                proxy_state = None
                _proxy_ip = "direct"
            else:
                _proxy_ip = proxy_dict["http"].split("@")[-1]
                _proxied_kwargs = dict(client_kwargs)
                _proxied_kwargs["proxy"] = proxy_dict["http"]
                _proxied_client = httpx.AsyncClient(**_proxied_kwargs)
                proxy_state = {
                    "fetched_at": time.monotonic(),
                    "ip": _proxy_ip,
                    "lock": asyncio.Lock(),
                    "_client": _proxied_client,
                    "_client_kwargs": dict(client_kwargs),
                    "_proxy_manager": None,
                }
        logger.info("\033[91m[proxy]\033[0m %s", _proxy_ip)

        # 详情队列(列表生产者 → 详情消费者)
        detail_queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue(
            maxsize=self.queue_size
        )

        async with httpx.AsyncClient(**client_kwargs) as client:
            # 启动详情 worker
            async def detail_worker(worker_id: int) -> None:
                nonlocal risk_streak
                while True:
                    item = await detail_queue.get()
                    try:
                        if item is None:
                            return
                        if should_stop and should_stop():
                            stats["detail_skip"] += 1
                            continue

                        result = await self._fetch_detail(
                            client=client,
                            item=item,
                            limiter=limiter,
                            proxy_state=proxy_state,
                        )
                        if result is None:
                            stats["detail_fail"] += 1
                            continue
                        if result.get("_risk"):
                            risk_streak += 1
                            stats["detail_fail"] += 1
                            # ── B 方案：首次风控 → 查付费墙 → 决定是否停 task ──
                            if risk_streak == 1:
                                from services.proxy_paywall import ProxyPaywall
                                if ProxyPaywall.is_blocked():
                                    logger.warning("[kejinshou] 付费墙生效, 首次风控停 task")
                                    stats["list_end_reason"] = "first_risk_stop"
                                    stats["risk_paused"] = True
                                    while not detail_queue.empty():
                                        try:
                                            detail_queue.get_nowait()
                                        except asyncio.QueueEmpty:
                                            break
                                    return
                                logger.info("[kejinshou] 首次风控但付费墙未生效, 继续累积")
                            continue

                        risk_streak = 0
                        stats["detail_ok"] += 1
                        batch_buffer.append(result)
                        flush_buffer(force=False)
                        stats["consumed"] += 1
                        emit_progress()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("detail_worker error")
                    finally:
                        detail_queue.task_done()

            workers = [
                asyncio.create_task(detail_worker(i))
                for i in range(self.detail_concurrency)
            ]

            try:
                await self._produce_list(
                    client=client,
                    mode=mode,
                    max_pages_override=max_pages_override,
                    detail_queue=detail_queue,
                    seen_product_ids=seen_product_ids,
                    stats=stats,
                    limiter=limiter,
                    should_stop=should_stop,
                    on_progress=emit_progress,
                    proxy_state=proxy_state,
                )
            except Exception:
                logger.exception("list producer crashed")
                stats["list_end_reason"] = "producer_exception"
            finally:
                # 发哨兵让 worker 退出
                for _ in workers:
                    await detail_queue.put(None)
                await asyncio.gather(*workers, return_exceptions=True)
                flush_buffer(force=True)
                if proxy_state is not None:
                    _pc = proxy_state.pop("_client", None)
                    if _pc is not None:
                        try:
                            await _pc.aclose()
                        except Exception:
                            pass

        stats["seen_count"] = len(seen_product_ids)
        return {
            "task_id": task_id,
            "platform": self.platform,
            "stats": stats,
        }

    # ---------------- 列表生产者 ---------------- #

    async def _produce_list(
        self,
        *,
        client: httpx.AsyncClient,
        mode: str,
        max_pages_override: Optional[int],
        detail_queue: asyncio.Queue,
        seen_product_ids: Set[str],
        stats: Dict[str, Any],
        limiter: RateLimiter,
        should_stop: Optional[Callable[[], bool]],
        on_progress: Callable[[Optional[Dict[str, Any]]], None],
        proxy_state: Optional[Dict[str, Any]] = None,
    ) -> None:
        page = 1
        prev_page_ids: List[str] = []
        total_pages_hint: Optional[int] = None  # 从 pagination.pages 获取

        while True:
            if page > self.max_pages:
                stats["list_end_reason"] = "max_pages"
                break
            if max_pages_override is not None and page > max_pages_override:
                stats["list_end_reason"] = "pages_reached"
                break
            # full 模式下,如果知道了总页数且已翻完,提前结束
            if mode == "full" and total_pages_hint is not None and page > total_pages_hint:
                stats["list_end_reason"] = "all_pages_done"
                break
            if should_stop and should_stop():
                stats["list_end_reason"] = "stopped"
                break
            if stats.get("risk_paused"):
                break

            # 构建业务 data
            data_dict: Dict[str, Any] = {
                "gameId": self.game_id,
                "cateId": self.cate_id,
                "type": "goods",
                "size": self.page_size,
                "page": page,
            }
            if mode == "latest":
                data_dict["originOrder"] = "upper_at_desc"

            # 签名 & 构建 query params(每次请求都是新的时间戳+签名)
            query_params = _build_signed_params(
                self.h5_token,
                self.h5_token_enc,
                data_dict,
                LIST_URI,
            )

            await limiter.acquire()
            response_json, err = await self._request_with_retry(
                client=client,
                method="GET",
                url=self.list_url,
                params=query_params,
                proxy_state=proxy_state,
            )
            if err:
                if err == "token_renewed":
                    logger.info("[kejinshou] H5 token 已刷新，重签列表 page=%d", page)
                    continue
                stats["list_pages_fail"] += 1
                if stats["list_pages_fail"] >= 5:
                    stats["list_end_reason"] = "list_repeated_failure"
                    break
                await asyncio.sleep(2.0)
                continue

            stats["list_pages_ok"] += 1

            # ── 解析真实响应结构 ──
            # resp = {
            #   "ret": "SUCCESS",
            #   "data": {
            #     "success": true,
            #     "data": {
            #       "list": [...],
            #       "pagination": {"pages": 69, "total": 4089, "page": 3, "size": 60}
            #     }
            #   }
            # }
            outer_data = response_json.get("data") or {}
            if not isinstance(outer_data, dict):
                outer_data = {}
            inner_data = outer_data.get("data") or {}
            if not isinstance(inner_data, dict):
                inner_data = {}

            items = inner_data.get("list") or []
            pagination = inner_data.get("pagination") or {}

            # 从 pagination 获取总页数
            if pagination:
                total_pages_hint = pagination.get("pages")
                total_count = pagination.get("total", 0)
                logger.info(
                    "[kejinshou] page=%d/%s mode=%s items=%d total=%s",
                    page, total_pages_hint or "?", mode, len(items), total_count,
                )
            else:
                logger.info(
                    "[kejinshou] page=%d mode=%s items=%d",
                    page, mode, len(items),
                )

            # 提取当前页 id 列表(用于去重检测)
            current_page_ids: List[str] = []
            for raw in items:
                pid = str(raw.get("id") or "").strip()
                if pid:
                    current_page_ids.append(pid)

            # 与上一页完全一样 → 终止
            if prev_page_ids and current_page_ids == prev_page_ids:
                stats["list_end_reason"] = "duplicate_page"
                break

            # ── 价格对比：批量查 DB，同价跳过详情 ──
            batch_pids = [str(r.get("id") or "").strip() for r in items if str(r.get("id") or "").strip()]
            existing_prices = _query_existing_prices(batch_pids)

            new_count = 0
            for raw in items:
                pid = str(raw.get("id") or "").strip()
                if not pid:
                    continue
                if pid in seen_product_ids:
                    stats["detail_repeat"] += 1
                    continue
                seen_product_ids.add(pid)
                new_count += 1

                list_price = _coerce_price(raw.get("price"))
                # 价格未变 → 直接用列表数据，不请求详情
                if pid in existing_prices and list_price is not None:
                    cached = existing_prices[pid]
                    if abs(cached - list_price) < 0.01:
                        stats["detail_skip"] += 1
                        result = {
                            "product_id": pid,
                            "product_info": raw.get("title") or raw.get("subTitle") or "",
                            "original_price": list_price,
                            "url": build_product_url(pid),
                            "game_type": raw.get("gameName") or self.game_name,
                            "supports_realname": not any(
                                kw in str(raw.get("subTitle") or "")
                                for kw in ("不可修改", "不可二次实名", "不可实名", "不能实名", "禁实名", "未实名")
                            ),
                            "_raw_detail": raw,
                            "_skip_detail": True,
                        }
                        batch_buffer.append(result)
                        flush_buffer(force=False)
                        stats["consumed"] += 1
                        continue

                # 构建列表级数据,推入详情队列
                enriched = self._build_list_item(raw)
                await detail_queue.put(enriched)
                stats["produced"] += 1

            if not items:
                if mode == "latest" and page < (max_pages_override or self.max_pages):
                    page += 1
                    continue
                stats["list_end_reason"] = "empty_list"
                break

            if new_count == 0:
                if mode != "latest":
                    stats["list_end_reason"] = "no_new_ids"
                    break
            # 注意: latest 模式下即使整页重复也继续翻够页数

            if len(items) < self.page_size:
                if mode == "latest":
                    if page >= (max_pages_override or self.max_pages):
                        stats["list_end_reason"] = "short_page"
                        break
                else:
                    # full 模式:短页=末页,但如果 pagination 说还有更多就继续
                    if total_pages_hint is None or page >= total_pages_hint:
                        stats["list_end_reason"] = "short_page"
                        break

            prev_page_ids = current_page_ids
            page += 1

            on_progress({"page": page, "produced": stats["produced"]})

        if not stats.get("list_end_reason"):
            stats["list_end_reason"] = "completed"

    # ---------------- 列表项提取(推入详情队列前的预处理) ---------------- #

    def _build_list_item(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """从列表 JSON 项提取字段,传给详情 worker。"""
        pid = str(raw.get("id") or "").strip()
        return {
            "product_id": pid,
            "game_name": raw.get("gameName") or self.game_name,
            "list_price": _coerce_price(raw.get("price")),
            "list_title": raw.get("title") or "",
            "list_subtitle": raw.get("subTitle") or "",
            "list_home_image": raw.get("homeImage") or "",
            "list_polish_at": raw.get("polishAt") or "",
            # 保留原始列表数据
            "_raw": raw,
        }

    # ---------------- 详情 Worker ---------------- #

    async def _fetch_detail(
        self,
        *,
        client: httpx.AsyncClient,
        item: Dict[str, Any],
        limiter: RateLimiter,
        proxy_state: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """请求商品详情接口,合并列表数据后返回统一结果。"""
        product_id = item["product_id"]

        # 详情级别重试:403 立即放弃;网络/5xx/超时最多重试 3 次
        max_detail_retries = 3
        response_json = None
        err = None

        for detail_attempt in range(max_detail_retries):
            # 每次重试都重新签名，便于处理 FAIL_SYS_TOKEN_NEED_RENEW 后的新 token。
            detail_data = {"id": int(product_id)}
            query_params = _build_signed_params(
                self.h5_token,
                self.h5_token_enc,
                detail_data,
                DETAIL_URI,
            )
            await limiter.acquire()
            response_json, err = await self._request_with_retry(
                client=client,
                method="GET",
                url=self.detail_url,
                params=query_params,
                proxy_state=proxy_state,
            )

            if err is None:
                break

            if err == "token_renewed":
                logger.info("[kejinshou] H5 token 已刷新，重签详情 product=%s", product_id)
                continue

            if err == "403":
                logger.warning(
                    "detail 403 product=%s (风控,不计入重试)", product_id,
                )
                return {"_risk": True}

            if detail_attempt < max_detail_retries - 1:
                wait = (detail_attempt + 1) * 3.0
                logger.warning(
                    "detail attempt %d/%d failed product=%s err=%s; retry in %.1fs",
                    detail_attempt + 1, max_detail_retries, product_id, err, wait,
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    "detail exhausted retries product=%s err=%s", product_id, err,
                )
                return None

        if response_json is None:
            return None

        # 解析详情响应
        outer_data = response_json.get("data") or {}
        if not isinstance(outer_data, dict):
            outer_data = {}
        detail_data_block = outer_data.get("data") or {}
        if not isinstance(detail_data_block, dict):
            detail_data_block = {}

        # 调试日志:代理 IP + 核心字段
        _px_ip = proxy_state["ip"] if proxy_state else "-"
        _dname = detail_data_block.get("title") or detail_data_block.get("productName") or item.get("list_title") or "-"
        if len(_dname) > 60:
            _dname = _dname[:57] + "..."
        logger.info(
            "\033[91m[%s]\033[0m pid=%s price=%s name=%s",
            _px_ip, product_id,
            detail_data_block.get("price") or item.get("list_price") or "-",
            _dname,
        )

        return self._build_result(item, detail_data_block)

    # ---------------- 结果组装 ---------------- #

    # 需要按星级去重的 fp(火影忍者专用)
    _NINJA_DEDUP_FP_TITLES = {"忍者"}

    @staticmethod
    def _parse_kjs_property_detail(
        detail: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        从氪金兽详情 JSON 的 propertyDetail 中提取结构化数据(游戏无关)。

        通用逻辑:
          - 每个 fp(含 ups) → 遍历 ups → 解析 relations[].{spId, pvId}
            → 在 sps.pvs 中查找 pvId → 得到 pv.title
            → 拼接: "pv1_title-pv2_title-...-up_title"

        返回:
          {
            "base_infos":     {"贵族等级": "V12", ...} or {"总资产": "393M", ...},
            "safe_infos":     {"实名": "可二次实名", ...},
            "fp_sections":    {"忍者": ["S-五星-xxx", ...], "典藏武器外观": ["优品B-AS Val..."], ...},
            # 向后兼容
            "ninjas":         [...],  # = fp_sections.get("忍者", [])
            "costumes":       [...],  # = fp_sections.get("角色时装", [])
            "summons":        [...],  # = fp_sections.get("通灵兽", [])
          }
        """
        pd = detail.get("propertyDetail") or {}
        if not isinstance(pd, dict):
            pd = {}

        # ── baseInfos ──
        base_infos: Dict[str, str] = {}
        for pair in pd.get("basePropertyPairs") or []:
            name = (pair.get("name") or "").strip()
            value = (pair.get("value") or "").strip()
            if name and value:
                base_infos[name] = value

        # ── safeInfos ──
        safe_infos: Dict[str, str] = {}
        for pair in pd.get("safePropertyPairs") or []:
            name = (pair.get("name") or "").strip()
            value = (pair.get("value") or "").strip()
            if name and value:
                safe_infos[name] = value

        # ── propertiesInfo.fps(通用提取) ──
        props = pd.get("propertiesInfo") or {}
        fps = props.get("fps") or []

        fp_sections: Dict[str, List[str]] = {}

        for fp in fps:
            if not isinstance(fp, dict):
                continue
            fp_title = (fp.get("title") or "").strip()
            ups = fp.get("ups") or []
            sps = fp.get("sps") or []
            if not ups or not fp_title:
                continue

            # 构建 sps 查找表: spId → {pvId → pv_title}
            sp_map: Dict[int, Dict[int, str]] = {}
            for sp in sps:
                sp_id = sp.get("id")
                pv_map: Dict[int, str] = {}
                for pv in sp.get("pvs") or []:
                    pv_id = pv.get("id")
                    pv_title = (pv.get("title") or "").strip()
                    if pv_id is not None and pv_title:
                        pv_map[pv_id] = pv_title
                if sp_id is not None:
                    sp_map[sp_id] = pv_map

            # ── 忍者(火影)特殊处理: 品质-星级-名, 同名去重保留最高星 ──
            if fp_title in JingxiKejinshouCrawler._NINJA_DEDUP_FP_TITLES:
                STAR_ORDER = {"五星": 0, "四星": 1, "三星": 2, "二星": 3, "一星": 4}
                ninja_best: Dict[str, Tuple[str, str, str]] = {}
                for up in ups:
                    up_title = (up.get("title") or "").strip()
                    if not up_title:
                        continue
                    quality = ""
                    star = ""
                    for rel in up.get("relations") or []:
                        pv_title = sp_map.get(rel.get("spId"), {}).get(rel.get("pvId"), "")
                        if pv_title and rel.get("spId") == 56:
                            quality = pv_title
                        elif pv_title and rel.get("spId") == 57:
                            star = pv_title
                    cur_order = STAR_ORDER.get(star, 99)
                    if up_title in ninja_best and STAR_ORDER.get(ninja_best[up_title][1], 99) <= cur_order:
                        continue
                    ninja_best[up_title] = (quality, star, up_title)
                s_list = [(q, s, t) for q, s, t in ninja_best.values() if q == "S"]
                a_list = [(q, s, t) for q, s, t in ninja_best.values() if q == "A"]
                s_list.sort(key=lambda x: STAR_ORDER.get(x[1], 99))
                a_list.sort(key=lambda x: STAR_ORDER.get(x[1], 99))
                fp_sections[fp_title] = [f"{q}-{s}-{t}" for q, s, t in s_list + a_list]
                continue

            # ── 通用提取: relations 中每个 pvId → pv.title, 拼接 pv_titles-up_title ──
            items: List[str] = []
            for up in ups:
                up_title = (up.get("title") or "").strip()
                if not up_title:
                    continue
                pv_titles: List[str] = []
                for rel in up.get("relations") or []:
                    pv_title = sp_map.get(rel.get("spId"), {}).get(rel.get("pvId"), "")
                    if pv_title:
                        pv_titles.append(pv_title)
                if pv_titles:
                    items.append("-".join(pv_titles + [up_title]))
                else:
                    items.append(up_title)
            if items:
                fp_sections[fp_title] = items

        return {
            "base_infos": base_infos,
            "safe_infos": safe_infos,
            "fp_sections": fp_sections,
            # 向后兼容
            "ninjas": fp_sections.get("忍者", []),
            "costumes": fp_sections.get("角色时装", []),
            "summons": fp_sections.get("通灵兽", []),
        }

    def _build_result(
        self,
        item: Dict[str, Any],
        detail: Dict[str, Any],
    ) -> Dict[str, Any]:
        """合并列表数据 + 详情数据,输出统一格式(对齐 pangxie.py 的 product_info 风格)。"""
        pid = item["product_id"]

        # ── 价格: 详情优先 ──
        price = (
            _coerce_price(detail.get("price"))
            or _coerce_price(detail.get("payPrice"))
            or item.get("list_price")
        )

        # ── 解析 propertyDetail 结构化数据 ──
        extracted = self._parse_kjs_property_detail(detail)

        base = extracted["base_infos"]
        safe = extracted["safe_infos"]
        fp_sections = extracted["fp_sections"]
        # 向后兼容
        ninjas = extracted["ninjas"]
        costumes = extracted["costumes"]
        summons = extracted["summons"]

        # ── 构建 product_info(用中文逗号/顿号分隔,与 pangxie.py 风格一致) ──
        parts: List[str] = []

        # 游戏名 + 区服
        area_name = detail.get("areaName") or ""
        server_name = detail.get("serverName") or ""
        if area_name and server_name:
            parts.append(f"{area_name} {server_name}")
        elif server_name:
            parts.append(server_name)

        # baseInfos: 优先展示已知字段,其余按顺序
        BASE_ORDER = ["贵族等级", "战力值", "S忍数", "A忍数", "游戏段位", "大区",
                       "总资产", "哈夫币", "烽火地带段位", "烽火地带等级",
                       "全面战场段位", "全面战场等级"]
        for key in BASE_ORDER:
            val = base.get(key)
            if val:
                parts.append(f"{key}：{val}")
        for key, val in base.items():
            if key not in BASE_ORDER and val:
                parts.append(f"{key}：{val}")

        # safeInfos
        SAFE_ORDER = ["实名", "实名人"]
        for key in SAFE_ORDER:
            val = safe.get(key)
            if val:
                parts.append(f"{key}：{val}")
        for key, val in safe.items():
            if key not in SAFE_ORDER and val:
                parts.append(f"{key}：{val}")

        # fp_sections(游戏无关): 每个 fp 排成一行
        for fp_title, items in fp_sections.items():
            if items:
                parts.append(f"{fp_title}：" + "、".join(items))

        product_info = "，".join(parts)

        # ── 游戏名 ──
        game_name = (
            detail.get("gameName")
            or item.get("game_name")
            or self.game_name
        )

        # ── 实名检测 ──
        realname_val = safe.get("实名", "") or safe.get("实名情况", "")
        if realname_val:
            supports_realname = not any(
                kw in realname_val
                for kw in ("不可修改", "不可二次实名", "不可实名",
                           "不能实名", "禁实名", "未实名")
            )
        else:
            subtitle = detail.get("subTitle") or item.get("list_subtitle") or ""
            supports_realname = not any(
                kw in subtitle
                for kw in ("不可修改", "不可二次实名", "不可实名",
                           "不能实名", "禁实名", "未实名")
            )

        return {
            "product_id": pid,
            "product_info": product_info,
            "original_price": price,
            "url": build_product_url(pid),
            "game_type": game_name,
            "game_id": str(detail.get("gameId") or self.game_id),
            "supports_realname": supports_realname,
            # 结构化数据(供 Excel / 筛选使用)
            "base_infos": base,
            "safe_infos": safe,
            "fp_sections": fp_sections,
            "ninjas": ninjas,
            "costumes": costumes,
            "summons": summons,
            # 额外列表字段
            "list_title": item.get("list_title") or "",
            "list_subtitle": item.get("list_subtitle") or "",
            "list_home_image": item.get("list_home_image") or "",
            "list_polish_at": item.get("list_polish_at") or "",
            "create_time": detail.get("createTime") or detail.get("upperAt") or "",
            # 保留原始数据
            "_raw_detail": detail,
        }

    # ---------------- HTTP / 重试 ---------------- #

    @staticmethod
    async def _resolve_proxy(proxy_state: Dict[str, Any]) -> Optional[httpx.AsyncClient]:
        """获取当前代理对应的 AsyncClient。

        氪金兽语义与螃蟹对称：
        - TTL > 150s 自然到期 → 走 ProxyManager.get_proxy()（不消耗 budget）
        - 没拿到新 IP → 保留旧 client
        - 不主动 force_refresh 切 IP
        """
        from services.proxy_manager import get_manager as _get_pm
        async with proxy_state["lock"]:
            now = time.monotonic()
            if proxy_state.get("_client") is not None and (now - proxy_state["fetched_at"]) < _proxy_cache_ttl():
                return proxy_state["_client"]

            # TTL 到, 走 ProxyManager 自然换 (不消耗 budget)
            new_proxy_dict = _get_pm("kejinshou").get_proxy()
            if new_proxy_dict is None:
                logger.info("[kejinshou] TTL 到但取不到新 IP, 沿用旧 client")
                return proxy_state.get("_client")

            proxy_state["fetched_at"] = now
            proxy_state["ip"] = new_proxy_dict["http"].split("@")[-1]
            logger.info(
                "\033[91m[proxy rotated]\033[0m %s", proxy_state["ip"],
            )

            old = proxy_state.pop("_client", None)
            if old is not None:
                try:
                    await old.aclose()
                except Exception:
                    pass

            kwargs = dict(proxy_state["_client_kwargs"])
            kwargs["proxy"] = new_proxy_dict["http"]
            proxy_state["_client"] = httpx.AsyncClient(**kwargs)
            return proxy_state["_client"]

    async def _request_with_retry(
        self,
        *,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        params: Optional[Dict[str, str]] = None,
        proxy_state: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        attempt = 0
        backoff = 1.0
        last_err: Optional[str] = None

        while attempt < self.max_retries:
            attempt += 1
            request_client: httpx.AsyncClient = client
            if proxy_state is not None:
                request_client = await self._resolve_proxy(proxy_state)

            try:
                resp = await request_client.request(
                    method, url, params=params,
                )
                status = resp.status_code

                if status == 403:
                    logger.warning(
                        "HTTP 403 from %s; pausing %.1fs (风控)",
                        url, self.risk_pause_seconds,
                    )
                    await asyncio.sleep(self.risk_pause_seconds)
                    return None, "403"

                if status == 429:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        wait_s = float(retry_after) if retry_after else 30.0
                    except (TypeError, ValueError):
                        wait_s = 30.0
                    logger.warning("HTTP 429 from %s; sleeping %.1fs", url, wait_s)
                    await asyncio.sleep(wait_s)
                    last_err = "http_429"
                    continue

                if 500 <= status < 600:
                    last_err = f"http_{status}"
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 8.0)
                    continue

                if status != 200:
                    last_err = f"http_{status}"
                    await asyncio.sleep(2.0)
                    continue

                try:
                    data = resp.json()
                except (json.JSONDecodeError, ValueError):
                    logger.warning(
                        "Non-JSON response from %s; status=%s len=%s",
                        url, status, len(resp.content or b""),
                    )
                    return None, "non_json_200"

                if data.get("ret") == "FAIL_SYS_TOKEN_NEED_RENEW":
                    new_token = str(data.get("token") or "").strip()
                    new_enc = str(data.get("encToken") or "").strip()
                    if new_token and new_enc:
                        self.h5_token = new_token
                        self.h5_token_enc = new_enc
                        logger.info("[kejinshou] H5 token 过期，已使用响应 token 刷新")
                        return None, "token_renewed"
                    return None, "token_need_renew"

                return data, None

            except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
                last_err = f"network:{type(e).__name__}"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            except httpx.HTTPError as e:
                last_err = f"http_error:{type(e).__name__}"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue

        return None, last_err or "max_retries"


__all__ = [
    # 旧常量 RISK_STOP_THRESHOLD / RISK_PAUSE_SECONDS / TOO_MANY_REQUESTS_PAUSE
    # 已迁移到 services.proxy_manager，不再 __all__ 导出
    "JingxiKejinshouCrawler",
    "build_product_url",
    "RateLimiter",
    "load_proxy_config",
    "is_proxy_enabled",
    "md5_hex",
    "get_h5_xid",
    "_build_signed_params",
    "DEFAULT_LIST_URL",
    "DEFAULT_DETAIL_URL",
    "DEFAULT_HEADERS",
    "PRODUCT_URL_TEMPLATE",
]
