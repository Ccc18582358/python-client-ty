"""
鲸汐(螃蟹 pxb7.com) 商品账号爬虫。

职责(只做这几件事):
    1. 翻页拉取商品列表。
    2. 提取 productId、gameName、price、attrNameList 等列表字段。
    3. 并发请求商品详情接口,提取商品描述。
    4. 通过回调(on_batch / on_progress / should_stop)逐批返回给 GUI。

不在职责范围:
    - 不写数据库。
    - 不依赖 Django。
    - 不解析商品详情 HTML(只取 description 字段)。
    - 不做算法筛选。
    - 不导出 Excel。
    - 不修改任何现有 Django 接口。

用法(示例):
    import threading
    import queue

    q = queue.Queue()
    stop_event = threading.Event()

    def on_batch(items):
        # GUI 在此处拿到一批结果,自己做线程切换
        for it in items:
            print(it)

    crawler = JingxiAccountCrawler(user_id="160807549501484")
    threading.Thread(
        target=lambda: asyncio.run(crawler.run_full(
            on_batch=on_batch,
            on_progress=None,
            should_stop=stop_event.is_set,
        )),
        daemon=True,
    ).start()

注意:
    - 不需要登录态(token / cookie);只需要 user_id(从浏览器抓包里能看到)。
    - 列表接口: https://api-pc.pxb7.com/api/search/product/v2/selectSearchPageList
    - 详情接口: https://api-pc.pxb7.com/api/product/web/product/detailPost
    - 默认 requests_per_second=2.0,jitter_max=0.5,已加防封策略。
    - 代理:默认自动获取神龙代理(本地 SQLite 缓存 150s),传 proxy 参数可覆盖。
"""

import asyncio
import hashlib
import json
import logging
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
    """批量查已有商品价格。返回 {product_id: original_price}。"""
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

logger = logging.getLogger("jingxi_spider_pangxie")

# ---------------- 常量(可按需调整,不要写死到业务里) ---------------- #

SHOW_KEY_SALT = "9n484bzy!08sgzxl"
PRODUCT_URL_TEMPLATE = "https://www.pxb7.com/product/{product_id}/1"

# 列表接口(可由调用方通过 list_url 覆盖)
DEFAULT_LIST_URL = "https://api-pc.pxb7.com/api/search/product/v2/selectSearchPageList"
# 详情接口(可由调用方通过 detail_url 覆盖)
DEFAULT_DETAIL_URL = "https://api-pc.pxb7.com/api/product/web/product/detailPost"

# 默认请求头(取自浏览器抓包,无登录态也能用)
DEFAULT_HEADERS = {
    # 浏览器基础字段(每次请求都会带)
    "accept": "application/json",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "content-type": "application/json",
    "Origin": "https://www.pxb7.com",
    "Referer": "https://www.pxb7.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    # 客户端指纹字段(可由调用方覆盖)
    "client_type": "0",
    "os_type": "5",
    # 设备 / 会话指纹字段(无登录态,值在 __init__ 里从 WAF 状态文件加载)
    "device_id": "",
    "gio_device": "",
    "user_id": "",
    "px-authorization-merchant": "",
    "px-authorization-user": "",
}

# ── 风控相关已迁移到 ProxyManager（统一代理管理），此处不再持常量
#    - 旧 RISK_STOP_THRESHOLD / RISK_PAUSE_SECONDS / TOO_MANY_REQUESTS_PAUSE 已删除
#    - B 方案："首次风控即停 task" 在 detail_worker 内通过 ProxyPaywall 检查触发
#    - 螃蟹不主动换 IP，仅 TTL 自然换；captcha/risk 不消耗 force_rotate budget

# ---------------- 神龙代理(配置文件 + 本地 SQLite 缓存,客户端无 Redis) ---------------- #

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


# ---------------- 工具函数 ---------------- #


def make_show_key(product_id: str) -> str:
    """
    详情接口需要的 showKey:
        value = product_id + "9n484bzy!08sgzxl"
        return md5(value).hexdigest().upper()
    """
    if product_id is None:
        return ""
    value = str(product_id) + SHOW_KEY_SALT
    return hashlib.md5(value.encode("utf-8")).hexdigest().upper()


def build_product_url(product_id: str) -> str:
    return PRODUCT_URL_TEMPLATE.format(product_id=product_id)


def _extract_attr_list(item: Dict[str, Any]) -> List[str]:
    """从列表项里抽取 attrNameList,容忍 None / 字段缺失。"""
    raw = item.get("attrNameList")
    if isinstance(raw, list):
        return [str(x) for x in raw if x is not None]
    if isinstance(raw, str):
        return [raw]
    return []


def _detect_supports_realname(item: Dict[str, Any]) -> bool:
    """
    列表里 attrNameList 出现否定关键字时认为"不可实名";
    否则默认认为是支持(支持实名)。
    """
    attrs = _extract_attr_list(item)
    # 列表里能见到的所有否定 / 半否定关键字(任一命中即不支持)
    deny_keywords = (
        "不可二次实名",
        "不可二次",
        "不可实名",
        "不能实名",
        "禁实名",
        "未实名",
    )
    for s in attrs:
        for kw in deny_keywords:
            if kw in s:
                return False
    return True


def _coerce_price(value: Any) -> Optional[float]:
    """螃蟹列表页和 SKU 详情页价格统一为"分"单位，始终除以 100 转为元。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f / 100.0, 2)


# productType 数字 → 中文(已知映射,可按需扩展)
PRODUCT_TYPE_MAP: Dict[int, str] = {
    1: "游戏币",
    2: "道具",
    3: "代练",
    4: "金币",
    6: "账号",
}


def _extract_all_attrs(
    attrs: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[str]]:
    """把详情响应里的 productAttrs 数组拆成扁平 dict，**不做白名单过滤**。

    自动推断每个属性的类型：
      - attrVals[0] 有 attrValue → single（取第一个 attrValue）
      - attrVals[0] 有 itemName → 1 个为 single，多个为 multi（list）

    Returns:
        (extracted_dict, attr_name_order)
        extracted_dict: {attrName: value}，key 使用 API 原始中文名
        attr_name_order: attrName 在 API 响应中的出现顺序
    """
    out: Dict[str, Any] = {}
    order: List[str] = []
    for attr in attrs or []:
        attr_name = (attr.get("attrName") or "").strip()
        if not attr_name:
            continue
        vals = attr.get("attrVals") or []
        if not vals:
            continue

        order.append(attr_name)

        # 探测类型：看第一个元素是否携带 attrValue
        first = vals[0]
        if first.get("attrValue") is not None:
            # single（数值型）：取第一个非空 attrValue
            for v in vals:
                raw = v.get("attrValue")
                if raw is not None and raw != "":
                    out[attr_name] = raw
                    break
        else:
            # itemName 型：收集所有非空 itemName
            items = [v.get("itemName") for v in vals if v.get("itemName")]
            if len(items) == 1:
                out[attr_name] = items[0]
            elif len(items) > 1:
                out[attr_name] = items
    return out, order


# 向后兼容别名（旧名保留，无 field_map 参数，内部调 _extract_all_attrs）
def _extract_product_attrs(
    attrs: List[Dict[str, Any]],
    field_map: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """已废弃 — 仅保留向后兼容。新代码请用 _extract_all_attrs。"""
    result, _order = _extract_all_attrs(attrs)
    return result


def _find_realname_text(attrs: Dict[str, Any]) -> Optional[str]:
    """在所有 productAttrs 中查找包含"实名"的字段值。"""
    for attr_name, value in attrs.items():
        if "实名" in attr_name:
            if isinstance(value, list):
                return "、".join(str(v) for v in value)
            return str(value) if value is not None else None
    return None


# ---------------- 限速器(全局,所有请求共用) ---------------- #


class RateLimiter:
    """
    令牌桶 + 随机抖动:每 1/requests_per_second 秒放一个令牌,
    实际放行时再额外加 0~jitter_max 秒的随机抖动,避免被识别成机器人节拍。
    全局共享,列表 / 详情请求都走同一个 limiter,保证总速率。
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
        # 加随机抖动,让请求间隔看起来更像真人(防封)
        if self._jitter_max > 0:
            jitter = random.uniform(0.0, self._jitter_max)
            if jitter > 0:
                await asyncio.sleep(jitter)


# ---------------- 爬虫主体 ---------------- #


class JingxiAccountCrawler:
    """
    独立爬虫,不依赖 Django。
    由 GUI 在后台线程调用 run_full / run_latest。
    """

    def __init__(
        self,
        *,
        list_url: str = DEFAULT_LIST_URL,
        detail_url: str = DEFAULT_DETAIL_URL,
        game_id: str = "10032",
        game_name: str = "",
        platform: str = "螃蟹",
        base_headers: Optional[Dict[str, str]] = None,
        # 网络
        proxy: Optional[str] = None,
        is_proxy: Optional[bool] = None,    # None=读 proxy.json; True=代理; False=直连
        # 调度
        detail_concurrency: int = 3,
        requests_per_second: float = 2.0,
        queue_size: int = 300,
        timeout: float = 20.0,
        max_retries: int = 3,
        batch_size: int = 30,
        max_pages: int = 2000,
        verify_ssl: bool = False,
        # 防封:每次请求后加随机抖动(秒),上限值;jitter_max=0.5 表示
        # 每两次请求间隔会额外随机加 0~0.5s,避免固定节拍被识别
        jitter_max: float = 0.5,
        # 遇到 403 后暂停秒数 (旧常量已迁移到 ProxyManager)
        risk_pause_seconds: float = 120.0,
        # 测试用:注入 httpx 传输层(单元测试 / Mock)
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.list_url = list_url
        self.detail_url = detail_url
        self.game_id = game_id
        self.game_name = game_name
        self.platform = platform
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
        # 无登录态:不传 user_id / token / cookie

        # ── 离线 WAF 签名状态 + 设备指纹（timestamp__1366）──
        self._waf_client = None  # Pxb7WafClient（惰性导入，进程内维护 token）
        self._browser_fingerprint: Dict[str, Any] = {}
        self._load_waf_context()

    # ---------------- 离线 WAF 签名 ---------------- #

    def _load_waf_context(self) -> None:
        """加载 WAF 客户端（进程内惰性维护 token）+ 设备指纹。

        token 生命周期由 Pxb7WafClient 管理：启动时热读既有状态，
        之后仅在过期/被拒时惰性重新 challenge（token 与 IP 无关，跨代理复用）。
        这里只做首次导入与动态浏览器指纹生成（每会话一次）。
        """
        try:
            from pxb7 import pxb7_waf_client
        except Exception as e:  # 打包环境缺失时降级为直连
            logger.warning("[pangxie] pxb7_waf_client 导入失败: %s", e)
            self._waf_client = None
            return
        try:
            self._waf_client = pxb7_waf_client.get_client()
            for key in ("device_id", "gio_device"):
                val = self._waf_client.device_headers().get(key)
                if val:
                    self._base_headers[key] = str(val)
        except Exception as e:
            logger.warning("[pangxie] 加载 WAF 客户端失败: %s", e)
            self._waf_client = None

        # ── 动态浏览器指纹（每会话生成一次，保证沙箱环境与请求头一致）──
        try:
            from pxb7 import pxb7_browser_fingerprint as fp_mod
            self._browser_fingerprint = fp_mod.generate_browser_fingerprint()
            ua = self._browser_fingerprint.get("userAgent")
            sec_ch_ua = self._browser_fingerprint.get("secChUa")
            if ua:
                self._base_headers["User-Agent"] = ua
            if sec_ch_ua:
                self._base_headers["sec-ch-ua"] = sec_ch_ua
        except Exception as e:  # 指纹模块缺失时保持默认静态环境
            logger.warning("[pangxie] 动态指纹生成失败: %s", e)

        logger.debug("[pangxie] waf_client ready")

    def _sign_url(self, url: str, body_text: str) -> str:
        """离线生成 timestamp__1366 并追加到 URL。

        签名计算用到的 body 必须与实际发送的 body 完全一致（compact JSON）。
        失败或无 token 时返回原 URL（此时会触发 WAF 挑战，由请求层惰性恢复）。
        """
        if self._waf_client is None:
            return url
        return self._waf_client.sign(
            url,
            body_text,
            method="POST",
            browser_fingerprint=self._browser_fingerprint or None,
        )

    # ---------------- 公共入口 ---------------- #

    async def run_full(
        self,
        *,
        on_batch: Callable[[List[Dict[str, Any]]], Any],
        on_progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        extra_filter_dto_list: Optional[List[Any]] = None,
        extra_combine_filter_list: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """
        全量抓取:type="4",不带 sortType。
        结束条件由 _should_stop_full 判定。
        """
        params: Dict[str, Any] = {
            "query": "",
            "gameId": self.game_id,
            "pageIndex": 1,
            "pageSize": 100,
            "bizProd": 1,
            "type": "4",
            "posType": 1,
            "filterDTOList": list(extra_filter_dto_list or []),
            "combineFilterList": list(extra_combine_filter_list or []),
        }
        return await self._run_pipeline(
            mode="full",
            params=params,
            max_pages_override=None,
            on_batch=on_batch,
            on_progress=on_progress,
            should_stop=should_stop,
        )

    async def run_latest(
        self,
        *,
        pages: int,
        on_batch: Callable[[List[Dict[str, Any]]], Any],
        on_progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        extra_filter_dto_list: Optional[List[Any]] = None,
        extra_combine_filter_list: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """
        最新发布:type="1",sortType=2。
        抓够 pages 页就结束,不判断全量尾页。
        """
        params: Dict[str, Any] = {
            "query": "",
            "gameId": self.game_id,
            "pageIndex": 1,
            "pageSize": 100,
            "bizProd": 1,
            "type": "1",
            "posType": 1,
            "filterDTOList": list(extra_filter_dto_list or []),
            "sortType": 2,
            "combineFilterList": list(extra_combine_filter_list or []),
        }
        return await self._run_pipeline(
            mode="latest",
            params=params,
            max_pages_override=max(1, int(pages)),
            on_batch=on_batch,
            on_progress=on_progress,
            should_stop=should_stop,
        )

    # ---------------- 流水线 ---------------- #

    async def _run_pipeline(
        self,
        *,
        mode: str,
        params: Dict[str, Any],
        max_pages_override: Optional[int],
        on_batch: Callable[[List[Dict[str, Any]]], Any],
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

        # ── 主动 WAF 状态检查：抓取开始前确保 token 新鲜（覆盖 app 挂机 >12h 再抓）──
        await self._ensure_waf_fresh()

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
                # on_batch 收到的是自包含的包装格式,可直接写 Excel/文件
                batch_wrapped = {
                    "task_id":  task_id,
                    "platform": self.platform,
                    "results":  payload,
                }
                try:
                    on_batch(batch_wrapped)
                except Exception:
                    logger.exception("on_batch callback failed")

        # 共享 httpx 客户端(无登录态,不需要传 cookies)
        client_kwargs: Dict[str, Any] = {
            "timeout": httpx.Timeout(self.timeout),
            "verify": self.verify_ssl,
            "headers": dict(self._base_headers),
            "http2": False,
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        # 代理: is_proxy=True → 神龙代理(按 proxy.json 的 TTL 自动轮换), is_proxy=False → 直连
        if not self.is_proxy:
            proxy_state = None
            _proxy_ip = "direct"
        elif self.proxy:
            client_kwargs["proxy"] = self.proxy
            proxy_state = None  # 显式代理不轮换
            _proxy_ip = self.proxy
        else:
            # ── 统一代理管理：ProxyManager 接管 TTL/扣费/付费墙 ──
            from services.proxy_manager import get_manager as _get_pm
            proxy_dict = _get_pm("pangxie").get_proxy()
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
                    "fetched_at":      time.monotonic(),
                    "ip":              _proxy_ip,
                    "lock":            asyncio.Lock(),
                    "_client":         _proxied_client,
                    "_client_kwargs":  dict(client_kwargs),  # 不含 proxies, 轮换时动态注入
                    "_proxy_manager":  None,  # 螃蟹不主动换 IP, 仅 TTL 自然换
                }
        logger.info("\033[91m[proxy]\033[0m %s", _proxy_ip)

        detail_queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue(
            maxsize=self.queue_size
        )

        async with httpx.AsyncClient(**client_kwargs) as client:
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
                                    logger.warning("[pangxie] 付费墙生效, 首次风控停 task")
                                    stats["list_end_reason"] = "first_risk_stop"
                                    stats["risk_paused"] = True
                                    while not detail_queue.empty():
                                        try:
                                            detail_queue.get_nowait()
                                        except asyncio.QueueEmpty:
                                            break
                                    return
                                # 付费墙未生效 → 继续累积, 螃蟹不主动切 IP
                                logger.info("[pangxie] 首次风控但付费墙未生效, 继续累积")
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
                    base_params=params,
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
                # 强制 flush 剩余批次
                flush_buffer(force=True)
                # 清理代理 client(如果存在)
                if proxy_state is not None:
                    _pc = proxy_state.pop("_client", None)
                    if _pc is not None:
                        try:
                            await _pc.aclose()
                        except Exception:
                            pass

        stats["seen_count"] = len(seen_product_ids)
        return {
            "task_id":  task_id,
            "platform": self.platform,
            "stats":    stats,
        }

    # ---------------- 列表生产者 ---------------- #

    async def _produce_list(
        self,
        *,
        client: httpx.AsyncClient,
        base_params: Dict[str, Any],
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
        page_index = 1
        page_token = base_params.get("pageToken", "") or ""
        prev_page_ids: List[str] = []
        empty_new_streak = 0

        while True:
            if page_index > self.max_pages:
                stats["list_end_reason"] = "max_pages"
                break
            if max_pages_override is not None and page_index > max_pages_override:
                stats["list_end_reason"] = "pages_reached"
                break
            if should_stop and should_stop():
                stats["list_end_reason"] = "stopped"
                break
            if stats.get("risk_paused"):
                break

            req_params = dict(base_params)
            req_params["pageIndex"] = page_index
            # 与浏览器一致：仅在有翻页游标时才携带 pageToken（第一页不带）
            if page_token:
                req_params["pageToken"] = page_token

            await limiter.acquire()
            response_json, err = await self._request_with_retry(
                client=client,
                method="POST",
                url=self.list_url,
                json_body=req_params,
                proxy_state=proxy_state,
            )
            if err:
                stats["list_pages_fail"] += 1
                if stats["list_pages_fail"] >= 5:
                    stats["list_end_reason"] = "list_repeated_failure"
                    break
                await asyncio.sleep(2.0)
                continue

            stats["list_pages_ok"] += 1
            data_block = response_json.get("data") or {}
            items = data_block.get("list") or []
            properties = data_block.get("properties") or {}
            next_token = properties.get("pageToken") or ""

            page_size = int(req_params.get("pageSize") or 100)

            current_page_ids: List[str] = []
            for raw in items:
                pid = str(raw.get("productId") or "").strip()
                if pid:
                    current_page_ids.append(pid)

            # 注意:不在这里检查 pageToken 是否重复 —— API 多次返回相同 pageToken 是正常的,
            # 真正的翻页依据是 body.pageIndex,pageToken 更像会话游标,允许保持不变。
            # (2026-06 用户反馈:实测 pageToken 会原样返回)

            if prev_page_ids and current_page_ids == prev_page_ids:
                stats["list_end_reason"] = "duplicate_page"
                break

            # ── 价格对比：批量查 DB，同价跳过详情 ──
            batch_pids = [str(r.get("productId") or "").strip() for r in items if str(r.get("productId") or "").strip()]
            existing_prices = _query_existing_prices(batch_pids)

            new_count = 0
            for raw in items:
                pid = str(raw.get("productId") or "").strip()
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
                            "product_info": "、".join(_extract_attr_list(raw) or []),
                            "original_price": list_price,
                            "url": raw.get("productUrl") or raw.get("url") or "",
                            "game_type": raw.get("gameName") or self.game_name,
                            "supports_realname": _detect_supports_realname(raw),
                            "_raw_detail": raw,
                            "_skip_detail": True,
                        }
                        batch_buffer.append(result)
                        flush_buffer(force=False)
                        stats["consumed"] += 1
                        continue

                enriched = {
                    "product_id": pid,
                    "game_id": raw.get("gameId") or self.game_id,
                    "game_name": raw.get("gameName") or self.game_name,
                    "list_price": list_price,
                    "rc_token": properties.get("rcToken") or "",
                    "attr_name_list": _extract_attr_list(raw),
                    "supports_realname_hint": _detect_supports_realname(raw),
                    "create_time": raw.get("createTime"),
                    "shelve_time": raw.get("shelveUpTime"),
                }
                await detail_queue.put(enriched)
                stats["produced"] += 1

            if not items:
                # latest 模式忽略空列表(用户指定页数为止);full 模式遇到空列表结束
                if mode == "latest" and page_index < (max_pages_override or self.max_pages):
                    # 但没有 next_token 就真没数据了
                    if not next_token:
                        stats["list_end_reason"] = "no_next_token"
                        break
                    # 走 fallback:page_token 不变继续翻(罕见)
                    page_index += 1
                    continue
                stats["list_end_reason"] = "empty_list"
                break

            if new_count == 0:
                empty_new_streak += 1
                # latest 模式只要还有页就继续,full 模式遇到整页重复就停
                if mode != "latest" and empty_new_streak >= 1:
                    stats["list_end_reason"] = "no_new_ids"
                    break
            else:
                empty_new_streak = 0

            if len(items) < page_size:
                # latest 模式忽略不足 100 条,跑够 pages 再说
                if mode == "latest":
                    if page_index >= (max_pages_override or self.max_pages):
                        stats["list_end_reason"] = "short_page"
                        break
                    # 不 break,继续下一页(虽然下一页可能也是空)
                else:
                    stats["list_end_reason"] = "short_page"
                    break

            if not next_token:
                # latest 模式无 token 也继续翻(可能服务端不返回 token)
                if mode != "latest":
                    stats["list_end_reason"] = "no_next_token"
                    break

            prev_page_ids = current_page_ids
            page_index += 1
            page_token = next_token

            on_progress({"page_index": page_index, "produced": stats["produced"]})

        if not stats.get("list_end_reason"):
            stats["list_end_reason"] = "completed"

    # ---------------- 详情 Worker ---------------- #

    async def _fetch_detail(
        self,
        *,
        client: httpx.AsyncClient,
        item: Dict[str, Any],
        limiter: RateLimiter,
        proxy_state: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        product_id = item["product_id"]
        body = {
            "productId": product_id,
            "showKey": make_show_key(product_id),
            "searchKeyword": "",
            "selectOptions": [],
        }

        # 详情级别重试:403 立即放弃;网络/5xx/超时最多重试 3 次
        max_detail_retries = 3
        for detail_attempt in range(max_detail_retries):
            await limiter.acquire()
            response_json, err = await self._request_with_retry(
                client=client,
                method="POST",
                url=self.detail_url,
                json_body=body,
                proxy_state=proxy_state,
            )

            if err is None:
                # 请求成功,解析响应
                break

            # 403 是真正的风控,不重试,直接返回 _risk 标记
            if err == "403":
                logger.warning(
                    "detail 403 product=%s (风控,不计入重试)", product_id,
                )
                return {"_risk": True}

            # 其他错误:等一等再重试这条
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
                return None  # 非风控失败,返回 None 不计入 risk_streak

        # --- 响应解析(与原来一致) ---
        if response_json.get("success") is False:
            logger.info(
                "detail success=false product=%s err=%s",
                product_id,
                response_json.get("errMessage"),
            )
            return self._build_result(
                item,
                description="",
                raw_data={},
            )

        data_block = response_json.get("data") or {}
        if not isinstance(data_block, dict):
            data_block = {}

        description = (
            data_block.get("productName")
            or data_block.get("showTitle")
            or ""
        )

        # 调试日志:代理 IP(红色) + 核心字段(productName 截断,避免太长)
        _px_ip = proxy_state["ip"] if proxy_state else "-"
        _name = description or data_block.get("productUniqueNo") or "-"
        if len(_name) > 60:
            _name = _name[:57] + "..."
        logger.info(
            "\033[91m[%s]\033[0m pid=%s price=%s name=%s",
            _px_ip, product_id,
            data_block.get("price") or "-",
            _name,
        )

        return self._build_result(
            item,
            description=description or "",
            raw_data=data_block,
        )

    def _build_product_info(
        self,
        *,
        game_type: str,
        product_code: Any,
        product_type: Any,
        create_time: Any,
        attrs: List[Tuple[str, Any]],
    ) -> str:
        """拼装 product_info 文本：「标签：值，标签：值，...」

        固定顺序：4 个 meta 字段 → 全部 productAttrs（按 API 原始顺序）。
        """
        parts: List[str] = []

        # ── 4 个 meta 字段 ──
        # 游戏名称
        if game_type:
            parts.append(f"游戏名称：{game_type}")

        # 商品编码
        if product_code:
            parts.append(f"商品编码：{product_code}")

        # 商品类型（本爬虫只抓账号类，API 的 productType 语义不总是准确，统一用"账号"）
        if product_type is not None:
            parts.append("商品类型：账号")

        # 发布时间（只保留日期）
        if create_time:
            parts.append(f"发布时间：{str(create_time)[:10]}")

        # ── 全部 productAttrs（按 API 原始顺序） ──
        for attr_name, value in attrs:
            if value is None or value == "" or value == []:
                continue
            if isinstance(value, list):
                parts.append(f"{attr_name}：{'、'.join(str(v) for v in value)}")
            else:
                parts.append(f"{attr_name}：{value}")

        return "，".join(parts)

    def _build_result(
        self,
        item: Dict[str, Any],
        *,
        description: str,
        raw_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        # 1) 价格(分→元)
        original_price = (
            _coerce_price(raw_data.get("price"))
            or _coerce_price(raw_data.get("salePrice"))
            or _coerce_price(raw_data.get("amount"))
            or item.get("list_price")
        )

        # 2) 提取全部 productAttrs（不再做白名单过滤）
        #    attrs dict 的 key 是 API 原始中文 attrName
        attrs_list = raw_data.get("productAttrs") or []
        attrs, _attr_order = _extract_all_attrs(attrs_list)

        # 3) supports_realname — 在所有 attrs 中搜包含"实名"的字段
        realname_text = _find_realname_text(attrs)
        if realname_text:
            supports_realname = not any(
                kw in realname_text
                for kw in (
                    "不可二次实名",
                    "不可实名",
                    "不能实名",
                    "禁实名",
                    "未实名",
                )
            )
        else:
            merged_attrs = list(item.get("attr_name_list") or []) + list(
                raw_data.get("attrNameList") or []
            )
            supports_realname = not any(
                kw in s
                for s in merged_attrs
                for kw in (
                    "不可二次实名",
                    "不可二次",
                    "不可实名",
                    "不能实名",
                    "禁实名",
                    "未实名",
                )
            )

        game_type = (
            item.get("game_name")
            or raw_data.get("gameName")
            or self.game_name
        )

        # 4) 组装 product_info — meta 字段 + 全部 attrs（按 API 原始顺序）
        ordered_attrs: List[Tuple[str, Any]] = [
            ((a.get("attrName") or "").strip(), attrs.get((a.get("attrName") or "").strip()))
            for a in attrs_list
            if (a.get("attrName") or "").strip() and attrs.get((a.get("attrName") or "").strip()) is not None
        ]

        product_info = self._build_product_info(
            game_type=game_type,
            product_code=raw_data.get("productUniqueNo"),
            product_type=raw_data.get("productType"),
            create_time=raw_data.get("createTime") or item.get("create_time") or "",
            attrs=ordered_attrs,
        )

        # 5) 组装结果 —— 只保留回调格式需要的字段(task_id/platform 由外层包装)
        return {
            "product_id":        item["product_id"],
            "product_info":      product_info,
            "original_price":    original_price,
            "url":               build_product_url(item["product_id"]),
            "game_type":         game_type,
            "supports_realname": supports_realname,
            "_raw_detail":       raw_data,
        }

    # ---------------- 通用 HTTP / 重试 ---------------- #

    @staticmethod
    async def _resolve_proxy(proxy_state: Dict[str, Any]) -> Optional[httpx.AsyncClient]:
        """获取当前代理对应的 AsyncClient。

        螃蟹语义：
        - TTL > 150s 自然到期 → 走 ProxyManager.get_proxy()（不消耗 budget）
        - 没拿到新 IP → 保留旧 client（避免暂时无代理时全断）
        - 不主动 force_refresh 切 IP（7881/盼之才走 force_rotate）
        """
        from services.proxy_manager import get_manager as _get_pm
        async with proxy_state["lock"]:
            now = time.monotonic()
            if proxy_state.get("_client") is not None and (now - proxy_state["fetched_at"]) < _proxy_cache_ttl():
                return proxy_state["_client"]

            # TTL 到, 走 ProxyManager 自然换 (不消耗 budget)
            new_proxy_dict = _get_pm("pangxie").get_proxy()
            if new_proxy_dict is None:
                logger.info("[pangxie] TTL 到但取不到新 IP, 沿用旧 client")
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

    @staticmethod
    def _is_challenge(resp: httpx.Response) -> bool:
        """判断 200 响应是否为 WAF 挑战页（token 过期/被拒时返回 HTML 而非 JSON）。"""
        ct = str(resp.headers.get("content-type") or "").lower()
        if "application/json" in ct:
            return False
        body = resp.content or b""
        return b"renderData" in body or b"aliyun_waf" in body

    async def _ensure_waf_fresh(self) -> None:
        """抓取开始前主动检查一次 token 新鲜度（覆盖 app 挂机 >12h 再抓的场景）。

        token 新鲜则零网络开销直接返回；过期则直连触发一次 challenge。
        失败不阻断抓取（随后请求层的 `_recover_waf` 会再次兜底）。
        """
        if self._waf_client is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._waf_client.ensure_fresh, None)

    async def _recover_waf(self) -> bool:
        """检测到挑战页后，惰性重新 challenge 一次。

        token 与 IP 无关（已实测），challenge 直接走直连，不必专门取代理/切 IP/扣费。
        """
        if self._waf_client is None:
            return False
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, self._waf_client.refresh, None)
        if ok:
            for key in ("device_id", "gio_device"):
                val = self._waf_client.device_headers().get(key)
                if val:
                    self._base_headers[key] = str(val)
        return ok

    async def _request_with_retry(
        self,
        *,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        json_body: Optional[Dict[str, Any]] = None,
        proxy_state: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        attempt = 0
        backoff = 1.0
        last_err: Optional[str] = None
        waf_recovered = False  # 本调用内是否已尝试过一次 WAF 惰性恢复

        while attempt < self.max_retries:
            attempt += 1
            # 每次请求前解析代理(超过 150s 自动换 IP)
            request_client: httpx.AsyncClient = client
            if proxy_state is not None:
                request_client = await self._resolve_proxy(proxy_state)

            try:
                # 序列化为 compact JSON（无空格），签名与实际发送共用同一份 body_text
                body_text: Optional[str] = None
                signed_url = url
                if method.upper() == "POST" and json_body is not None:
                    body_text = json.dumps(
                        json_body, ensure_ascii=False, separators=(",", ":"),
                    )
                    loop = asyncio.get_running_loop()
                    signed_url = await loop.run_in_executor(
                        None, self._sign_url, url, body_text,
                    )

                kwargs: Dict[str, Any] = {}
                if body_text is not None:
                    kwargs["content"] = body_text.encode("utf-8")
                resp = await request_client.request(method, signed_url, **kwargs)
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
                        wait_s = float(retry_after) if retry_after else 5.0
                    except (TypeError, ValueError):
                        wait_s = 5.0
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

                # 200 但可能是 WAF 挑战页（token 过期/被拒）—— 惰性重新 challenge 后重试一次
                if self._is_challenge(resp):
                    if waf_recovered:
                        logger.warning("WAF 恢复后仍返回挑战页, 放弃: %s", url)
                        return None, "waf_challenge"
                    logger.warning("检测到 WAF 挑战页, 惰性重新 challenge ...")
                    waf_recovered = True
                    if await self._recover_waf():
                        continue  # 重新进入循环：重新签名并重试
                    return None, "waf_challenge"

                try:
                    data = resp.json()
                except (json.JSONDecodeError, ValueError):
                    logger.warning(
                        "Non-JSON response from %s; status=%s len=%s",
                        url,
                        status,
                        len(resp.content or b""),
                    )
                    return None, "non_json_200"

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
    "JingxiAccountCrawler",
    "make_show_key",
    "build_product_url",
    "RateLimiter",
    "_extract_product_attrs",
    "_extract_all_attrs",
    "PRODUCT_TYPE_MAP",
    "SHOW_KEY_SALT",
    "PRODUCT_URL_TEMPLATE",
    "DEFAULT_LIST_URL",
    "DEFAULT_DETAIL_URL",
    "DEFAULT_HEADERS",
    "load_proxy_config",
    "is_proxy_enabled",
]
