"""统一代理管理：每个平台一个 ProxyManager 实例。

设计要点：
1. 平台缓存 key 强制规范：f"shenlong_<platform>"，platform ∈ {"7881", "pangxie", "kejinshou", "panzhi"}
2. 旧裸 'shenlong' row（螃蟹历史）一次性兼容
3. 调用顺序：缓存命中 → 扣费 → 神龙（扣费失败不提取）
4. TTL=150s，4 平台统一；budget 限频 2min 内 ≤3 次
5. 螃蟹/氪金兽仅 TTL 自然换 IP；7881/盼之 captcha 走 force_rotate
6. B 方案付费墙：触发后直连继续，首次风控再停
7. 神龙连续 N 次取不到 IP（206/208）→ 暂停扣费一段时间（期间直连），避免持续空扣费
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

from services.proxy_switch_budget import ProxySwitchBudget
from services.proxy_paywall import ProxyPaywall
from services.shenlong_client import (
    ShenlongCode,
    ShenlongResult,
    fetch_ip as shenlong_fetch_ip,
)
from services.proxy_token_provider import get_proxy_token_headers

logger = logging.getLogger(__name__)


# ────────── 配置载入 ──────────


def _app_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _proxy_config_path() -> str:
    return os.path.join(_app_root(), "config", "proxy.json")


_DEFAULT_PROXY_CONFIG: Dict = {
    "enabled": False,
    "provider": "shenlong",
    "explicit_proxy": "",
    "cache_db": "shenlong_proxy.db",
    "cache_ttl_seconds": 150,
    "api_timeout_seconds": 6,
    "shenlong": {
        "api_url": "http://api.shenlongip.com/ip?key=...&protocol=1&mr=1&pattern=txt&count=1&sign=...",
        "proxy_user": "",
        "proxy_pass": "",
    },
    "paid": {
        # 是否使用扣费 API：true=扣费后提取；false=免扣费直连神龙（仍保留 TTL 缓存 + budget 限频防滥用）
        "enabled": True,
        "api_base": "https://api.nyyyds.com",
        "function_name": "扫号器代理",
        "function_type": 2,  # 付费功能类型: 2=全平台
        "timeout_seconds": 6,
        "network_cooldown_seconds": 150,
    },
    "budget": {
        "max_count": 3,
        "window_seconds": 120,
    },
    "behavior": {
        "206_backoff_seconds": 2,
        "206_retry_max": 2,
        "208_backoff_seconds": 3,
        "208_retry_max": 2,
        # 神龙连续取不到 IP 达到该次数后，暂停扣费一段时间（期间直连），防止持续空扣费
        "shenlong_fail_pause_threshold": 10,
        "shenlong_fail_pause_seconds": 300,
    },
}


def load_proxy_config() -> Dict:
    """读取 config/proxy.json；缺失字段走默认。"""
    path = _proxy_config_path()
    if not os.path.exists(path):
        return json.loads(json.dumps(_DEFAULT_PROXY_CONFIG))
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f) or {}
    except Exception as exc:
        logger.warning("[proxy] 读 proxy.json 失败: %s", exc)
        return json.loads(json.dumps(_DEFAULT_PROXY_CONFIG))

    return _deep_merge_dict(json.loads(json.dumps(_DEFAULT_PROXY_CONFIG)), user_cfg)


def _deep_merge_dict(base: Dict, over: Dict) -> Dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_dict(out[k], v)
        else:
            out[k] = v
    return out


# ────────── 扣费 + 开通校验 API 调用 ──────────


_PAID_SIGNATURE_MISSING_CODE = -403
_PAID_NETWORK_ERROR_CODE = -1001


def _paid_api_headers() -> Dict[str, str]:
    """拿一次当前登录 token header（不缓存，避免重登录后引用旧 token）"""
    return get_proxy_token_headers()


def _response_json(resp) -> Dict:
    content_type = str(resp.headers.get("content-type", "")).lower()
    if not content_type.startswith("application/json"):
        return {}
    try:
        body = resp.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _paid_http_error_message(resp) -> str:
    body = _response_json(resp)
    msg = str(body.get("msg") or body.get("message") or "").strip()
    if resp.status_code == 403 and msg.lower() == "missing signature headers":
        return "MISSING_SIGNATURE_HEADERS"
    return f"HTTP_{resp.status_code}"


def _check_open(cfg: Dict) -> Tuple[bool, str]:
    """调开通状态检测接口：GET /open/apipaid/function/checkOpen?name=扫号器代理

    Returns:
        (opened, message)
        - (True, "...") 业务码 0 + data=true → 已开通
        - (False, "未开通") 业务码 0 + data=false → 未开通（不致命, 仍可继续, 但账不会扣）
        - (False, "HTTP_404") HTTP 非 200 → 视同失败，不阻断流程
        - (False, "NO_TOKEN") 没有登录 token

    实现原则：1 次调用, 不重试; 失败不影响主流程 (仅 fallback 直连)
    """
    paid_cfg = cfg.get("paid") or {}
    api_base = paid_cfg.get("api_base", "")
    fn_name = paid_cfg.get("function_name", "扫号器代理")
    fn_type = int(paid_cfg.get("function_type", 2) or 2)
    timeout = float(paid_cfg.get("timeout_seconds", 6))
    if not api_base:
        return False, "NO_API_BASE"
    headers = _paid_api_headers()
    if not headers.get("token"):
        return False, "NO_TOKEN"
    try:
        import requests
        from urllib.parse import quote
        url = f"{api_base.rstrip('/')}/open/apipaid/function/checkOpen?name={quote(fn_name)}&type={fn_type}"
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            msg = _paid_http_error_message(resp)
            logger.warning("[proxy/check_open] HTTP %s msg=%s", resp.status_code, msg)
            return False, msg
        body = _response_json(resp)
        if not body:
            return False, "INVALID_BODY"
        business_code = int(body.get("code", -1) or 0)
        if business_code != 0:
            logger.warning("[proxy/check_open] 业务失败 code=%s msg=%s", business_code, body.get("msg", ""))
            return False, f"BUSINESS_{business_code}"
        opened = bool(body.get("data"))
        return opened, ("OK_OPENED" if opened else "NOT_OPENED")
    except Exception as exc:
        logger.warning("[proxy/check_open] 异常: %s", exc)
        return False, "NETWORK_ERROR"


def _should_mark_not_opened(msg: str) -> bool:
    """Only a confirmed unopened package should latch direct-mode fallback."""
    return str(msg or "").strip().upper() == "NOT_OPENED"


def _deduct_count(cfg: Dict) -> Tuple[bool, int]:
    """调扣费接口扣除 1 次扫号器代理使用。

    Returns:
        (success, business_code)
        - success=True 时 business_code=0
        - success=False 时 business_code 是非 0 或内部负数错误码

    网络异常时最多重试 2 次（间隔 1s/2s），避免因临时抖动浪费神龙已分配的 IP。
    """
    paid_cfg = cfg.get("paid") or {}
    api_base = paid_cfg.get("api_base", "")
    fn_name = paid_cfg.get("function_name", "扫号器代理")
    fn_type = int(paid_cfg.get("function_type", 2) or 2)
    timeout = float(paid_cfg.get("timeout_seconds", 6))
    if not api_base:
        return False, -1
    headers = _paid_api_headers()
    if not headers.get("token"):
        # 未登录 → 不能扣（不重试）
        return False, -1

    import requests
    from urllib.parse import quote
    url = f"{api_base.rstrip('/')}/open/apipaid/function/deductionCount?name={quote(fn_name)}&type={fn_type}"

    last_error_code = -1
    for attempt in range(3):  # 最多 3 次尝试（1 次初始 + 2 次重试）
        if attempt > 0:
            wait = attempt  # 1s, 2s backoff
            logger.debug("[proxy/charge] 扣费重试 %d/3，等待 %ds ...", attempt + 1, wait)
            time.sleep(wait)

        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code != 200:
                msg = _paid_http_error_message(resp)
                logger.warning(
                    "[proxy/charge] HTTP %s msg=%s (attempt %d/3)",
                    resp.status_code, msg, attempt + 1,
                )
                if msg == "MISSING_SIGNATURE_HEADERS":
                    return False, _PAID_SIGNATURE_MISSING_CODE
                last_error_code = -1
                continue  # 非 200 也重试（可能是临时网关错误）

            body = _response_json(resp)
            if not body:
                last_error_code = -1
                continue  # 空响应重试

            business_code = int(body.get("code", -1) or 0)
            if business_code != 0:
                # 业务错误（余额不足等）不重试，立即返回
                logger.warning(
                    "[proxy/charge] 业务失败 code=%s msg=%s",
                    business_code, body.get("msg", ""),
                )
                return False, business_code

            # 成功（deductionCount 接口返回 data:null，不返回剩余次数）
            if attempt > 0:
                logger.info("[proxy/charge] 扣费在第 %d 次尝试后成功", attempt + 1)
            return True, 0

        except Exception as exc:
            logger.warning(
                "[proxy/charge] 网络异常: %s (attempt %d/3)",
                exc, attempt + 1,
            )
            last_error_code = _PAID_NETWORK_ERROR_CODE
            # 继续重试

    # 重试耗尽
    logger.error("[proxy/charge] 扣费重试 3 次全部失败")
    return False, last_error_code


# ────────── SQLite 缓存 ──────────


def _cache_db_path(cfg: Dict) -> str:
    name = cfg.get("cache_db", "shenlong_proxy.db")
    if os.path.isabs(name):
        return name
    base = _app_root()
    # 数据落地在 data/ 下；和现有神龙实现保持一致
    return os.path.join(base, "data", name)


@dataclass
class CacheRow:
    ip: str = ""
    port: int = 0
    prov: str = ""
    city: str = ""
    fetched_at: float = 0.0


def _read_cache_row(db_path: str, key: str) -> Optional[CacheRow]:
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS proxy_cache ("
            "  key TEXT PRIMARY KEY,"
            "  ip TEXT NOT NULL,"
            "  port INTEGER NOT NULL,"
            "  prov TEXT,"
            "  city TEXT,"
            "  fetched_at REAL NOT NULL"
            ")"
        )
        cur = conn.execute(
            "SELECT ip, port, prov, city, fetched_at FROM proxy_cache WHERE key=?",
            (key,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        return CacheRow(ip=row[0], port=int(row[1] or 0),
                        prov=row[2] or "", city=row[3] or "",
                        fetched_at=float(row[4] or 0.0))
    except Exception as exc:
        logger.warning("[proxy] 读缓存 row 失败: %s", exc)
        return None


def _write_cache_row(db_path: str, key: str, ip: str, port: int,
                     prov: str = "", city: str = "") -> None:
    try:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS proxy_cache ("
            "  key TEXT PRIMARY KEY,"
            "  ip TEXT NOT NULL,"
            "  port INTEGER NOT NULL,"
            "  prov TEXT,"
            "  city TEXT,"
            "  fetched_at REAL NOT NULL"
            ")"
        )
        conn.execute(
            "INSERT OR REPLACE INTO proxy_cache (key, ip, port, prov, city, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, ip, int(port), prov, city, time.time()),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("[proxy] 写缓存 row 失败: %s", exc)


def _delete_cache_row(db_path: str, key: str) -> None:
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.execute("DELETE FROM proxy_cache WHERE key=?", (key,))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("[proxy] 删缓存 row 失败: %s", exc)


# ────────── ProxyManager（单平台代理管理）──────────

# ── 线程 → task_id 映射（供 checkOpen 失败时通知主线程弹窗）──
_active_task_ids: Dict[int, int] = {}

def set_active_task(task_id: int) -> None:
    """注册当前线程正在执行的任务 ID。scanner 适配器在开始爬取时调用。"""
    _active_task_ids[threading.get_ident()] = task_id

def clear_active_task() -> None:
    """清除当前线程的任务注册。scanner 适配器在爬取结束时调用。"""
    _active_task_ids.pop(threading.get_ident(), None)

def _get_active_task_id() -> Optional[int]:
    """获取当前线程关联的 task_id，未注册返回 None。"""
    return _active_task_ids.get(threading.get_ident())

# ── checkOpen 失败回调（主线程注入，复用 ProxyPaywall 的回调模式）──
_on_check_open_failed: Optional[Callable[[int, str], None]] = None

def set_check_open_failed_callback(cb: Callable[[int, str], None]) -> None:
    """注入 checkOpen 失败时的回调。MainWindow 启动时调用一次。"""
    global _on_check_open_failed
    _on_check_open_failed = cb


@dataclass
class ProxyManager:
    """单个平台的代理管理器。

    线程安全：通过 _lock 串行化 get_proxy/force_rotate 调用。
    """

    platform: str
    cfg: Dict
    _budget: ProxySwitchBudget
    _db_path: str = ""
    _ttl: float = 150.0
    _paid_cooldown_seconds: float = 150.0
    _paid_cooldown_until: float = 0.0
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _checked_open: bool = False  # 延迟校验：首次取代理时才调 checkOpen
    _not_opened: bool = False    # checkOpen 返回未开通 → 跳过所有代理逻辑（直连）
    _last_check_open_msg: str = ""
    _paid_enabled: bool = True               # 是否走扣费 API；false=免扣费直连神龙
    _shenlong_fail_streak: int = 0          # 神龙连续取不到 IP 的次数
    _shenlong_pause_until: float = 0.0      # 连续失败暂停扣费的截止时间（monotonic 秒）
    _shenlong_pause_threshold: int = 10     # 连续失败多少次后暂停扣费
    _shenlong_pause_seconds: float = 300.0  # 暂停时长（秒）

    def __post_init__(self):
        self._db_path = _cache_db_path(self.cfg)
        self._ttl = float(self.cfg.get("cache_ttl_seconds", 150))
        paid_cfg = self.cfg.get("paid") or {}
        self._paid_enabled = bool(paid_cfg.get("enabled", True))
        self._paid_cooldown_seconds = float(
            paid_cfg.get("network_cooldown_seconds", self._ttl) or self._ttl
        )
        behavior_cfg = self.cfg.get("behavior") or {}
        self._shenlong_pause_threshold = max(
            1, int(behavior_cfg.get("shenlong_fail_pause_threshold", 10) or 10)
        )
        self._shenlong_pause_seconds = max(
            0.0, float(behavior_cfg.get("shenlong_fail_pause_seconds", 300) or 300)
        )
        # 确保 db 目录存在
        try:
            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        except Exception:
            pass

    @property
    def cache_key(self) -> str:
        return f"shenlong_{self.platform}"

    # ──── 公开 API ────

    def get_proxy(self) -> Optional[Dict[str, str]]:
        """取代理 dict（带 150s TTL 缓存）。

        返回：
            {"http": "http://user:pass@ip:port", "https": "..."} 或 None
        """
        cfg = self.cfg
        if not cfg.get("enabled", False):
            return None

        explicit = str(cfg.get("explicit_proxy") or "").strip()
        if explicit:
            return {"http": explicit, "https": explicit}

        if not self._paid_enabled:
            # 免扣费模式：跳过付费墙 / 开通校验 / 扣费，直接提神龙；
            # 仍保留 TTL 缓存防滥用（150s 内复用同一 IP，不反复提取）
            with self._lock:
                cached = self._read_cache_with_legacy()
                if cached and (time.time() - cached.fetched_at) < self._ttl:
                    age = time.time() - cached.fetched_at
                    logger.debug(
                        "[proxy/%s] 缓存命中 %s:%s（age=%.1fs, ttl=%ds, 免扣费）",
                        self.platform, cached.ip, cached.port, age, self._ttl,
                    )
                    return self._format_proxy_url(cached.ip, cached.port)

                shen = self._fetch_with_backoff()
                if shen is None or not shen.is_ok:
                    self._note_shenlong_fail_locked()
                    logger.error(
                        "[proxy/%s] 免扣费模式神龙提取失败（code=%s msg=%s）",
                        self.platform,
                        shen.code.value if shen else "None",
                        shen.raw_msg if shen else "None",
                    )
                    return None
                self._note_shenlong_success_locked()
                _write_cache_row(
                    self._db_path, self.cache_key,
                    shen.ip, shen.port, shen.prov, shen.city,
                )
                logger.info(
                    "[proxy/%s] ✓ 取到新 IP %s:%s（已缓存，ttl=%ds, 免扣费）",
                    self.platform, shen.ip, shen.port, self._ttl,
                )
                return self._format_proxy_url(shen.ip, shen.port)

        if ProxyPaywall.is_blocked():
            # 付费墙生效 → 直连
            return None

        # 快速路径：已知未开通，跳过所有代理逻辑
        if self._not_opened:
            return None

        # 延迟校验开通状态（HTTP 在 lock 外，不阻塞其他线程）
        self._ensure_open_checked()
        if self._not_opened:
            return None

        with self._lock:
            # 1. SQLite 缓存命中
            cached = self._read_cache_with_legacy()
            if cached and (time.time() - cached.fetched_at) < self._ttl:
                age = time.time() - cached.fetched_at
                logger.debug(
                    "[proxy/%s] 缓存命中 %s:%s（age=%.1fs, ttl=%ds）",
                    self.platform, cached.ip, cached.port, age, self._ttl,
                )
                return self._format_proxy_url(cached.ip, cached.port)

            if cached:
                logger.debug(
                    "[proxy/%s] 缓存过期 %s:%s（age=%.1fs, ttl=%ds），重新获取",
                    self.platform, cached.ip, cached.port,
                    time.time() - cached.fetched_at, self._ttl,
                )
            else:
                logger.debug("[proxy/%s] 无缓存，需从神龙获取新 IP", self.platform)

            if self._paid_cooldown_remaining_locked() > 0:
                remaining = self._paid_cooldown_remaining_locked()
                logger.warning(
                    "[proxy/%s] 扣费接口冷却中，剩余 %.1fs 后再取新 IP",
                    self.platform, remaining,
                )
                return None

            # 神龙连续失败暂停扣费：期间直连，不扣费、不提取
            if self._shenlong_pause_remaining_locked() > 0:
                remaining = self._shenlong_pause_remaining_locked()
                logger.warning(
                    "[proxy/%s] 神龙连续失败暂停扣费中，剩余 %.1fs 后恢复（期间直连）",
                    self.platform, remaining,
                )
                return None

            # 2. 扣费（先扣费，成功后再提取，避免扣费失败浪费已提取的 IP）
            ok, business_code = _deduct_count(cfg)
            if not ok:
                logger.warning(
                    "[proxy/%s] 扣费失败: business_code=%s，未提取新 IP",
                    self.platform, business_code,
                )
                if business_code == 205:
                    logger.error(
                        "[proxy/%s] ★ 代理套餐次数已用完！将触发付费墙，后续全部直连",
                        self.platform,
                    )
                    ProxyPaywall.block(
                        code=205,
                        msg=f"扫号器代理套餐已无剩余次数（{self.platform}）",
                        platform=self.platform,
                    )
                elif business_code == _PAID_NETWORK_ERROR_CODE:
                    logger.error(
                        "[proxy/%s] ★ 扣费接口网络不通！进入 %.1fs 冷却",
                        self.platform, self._paid_cooldown_seconds,
                    )
                    self._start_paid_cooldown_locked("deduct_network_error")
                elif business_code == _PAID_SIGNATURE_MISSING_CODE:
                    logger.error(
                        "[proxy/%s] ★ 扣费接口签名缺失！请检查登录状态",
                        self.platform,
                    )
                else:
                    logger.warning(
                        "[proxy/%s] 扣费返回非 0 业务码: %s",
                        self.platform, business_code,
                    )
                return None

            # 3. 神龙 fetch（扣费成功后才提取；含 206/208 backoff，204/205 触发付费墙）
            logger.debug("[proxy/%s] 扣费成功，开始调用神龙获取代理 IP ...", self.platform)
            shen = self._fetch_with_backoff()
            if shen is None or not shen.is_ok:
                self._note_shenlong_fail_locked()
                logger.error(
                    "[proxy/%s] 扣费成功但神龙提取失败（code=%s msg=%s），已扣费未取到 IP",
                    self.platform,
                    shen.code.value if shen else "None",
                    shen.raw_msg if shen else "None",
                )
                return None
            logger.debug("[proxy/%s] 神龙返回 IP %s:%s", self.platform, shen.ip, shen.port)
            self._note_shenlong_success_locked()

            # 4. 写新缓存
            _write_cache_row(
                self._db_path, self.cache_key,
                shen.ip, shen.port, shen.prov, shen.city,
            )
            logger.info(
                "[proxy/%s] ✓ 取到新 IP %s:%s（已缓存，ttl=%ds）",
                self.platform, shen.ip, shen.port, self._ttl,
            )
            return self._format_proxy_url(shen.ip, shen.port)

    def force_rotate(self, *, reason: str) -> Optional[Dict[str, str]]:
        """主动切 IP（captcha / 风控触发），受 budget 限频。

        Returns:
            新 proxy dict，None 表示：
            - budget 已满（B 方案语义：spider 当次放弃、task 继续）
            - 神龙失败（含连续失败暂停扣费期间）
            - 扣费失败（不回滚 budget，让限频正常计数）
        """
        cfg = self.cfg
        if not cfg.get("enabled", False):
            return None

        explicit = str(cfg.get("explicit_proxy") or "").strip()
        if explicit:
            # explicit_proxy 不参与 budget（已经固定）
            return {"http": explicit, "https": explicit}

        if not self._paid_enabled:
            # 免扣费模式：跳过付费墙 / 开通校验 / 扣费，但保留 budget 限频防滥用
            with self._lock:
                if not self._budget.try_acquire():
                    logger.warning(
                        "[proxy/%s] budget 满, 当次不切 IP, reason=%s, %.1fs 后可重试",
                        self.platform, reason, self._budget.remaining_block_sec(),
                    )
                    return None
                shen = self._fetch_with_backoff()
                if shen is None or not shen.is_ok:
                    self._note_shenlong_fail_locked()
                    logger.error(
                        "[proxy/%s] 免扣费模式切 IP 但神龙提取失败, reason=%s（code=%s msg=%s）",
                        self.platform, reason,
                        shen.code.value if shen else "None",
                        shen.raw_msg if shen else "None",
                    )
                    return None
                self._note_shenlong_success_locked()
                _write_cache_row(
                    self._db_path, self.cache_key,
                    shen.ip, shen.port, shen.prov, shen.city,
                )
                logger.info(
                    "[proxy/%s] 主动切 IP（免扣费）, reason=%s, new=%s:%s",
                    self.platform, reason, shen.ip, shen.port,
                )
                return self._format_proxy_url(shen.ip, shen.port)

        if ProxyPaywall.is_blocked():
            # 付费墙生效时不再消耗 budget，让 spider 检测到后停 task
            return None

        # 快速路径：已知未开通，跳过所有代理逻辑
        if self._not_opened:
            return None

        self._ensure_open_checked()
        if self._not_opened:
            return None

        with self._lock:
            if self._paid_cooldown_remaining_locked() > 0:
                logger.warning(
                    "[proxy/%s] 扣费接口冷却中, 当次不切 IP, reason=%s, %.1fs 后可重试",
                    self.platform, reason, self._paid_cooldown_remaining_locked(),
                )
                return None

            # 神龙连续失败暂停扣费：期间不切 IP、不扣费、不提取
            if self._shenlong_pause_remaining_locked() > 0:
                logger.warning(
                    "[proxy/%s] 神龙连续失败暂停扣费中, 当次不切 IP, reason=%s, %.1fs 后恢复",
                    self.platform, reason, self._shenlong_pause_remaining_locked(),
                )
                return None

            # 1. budget gate
            if not self._budget.try_acquire():
                logger.warning(
                    "[proxy/%s] budget 满, 当次不切 IP, reason=%s, %.1fs 后可重试",
                    self.platform, reason, self._budget.remaining_block_sec(),
                )
                return None

            # 2. 扣费（先扣费，成功后再提取）
            ok, business_code = _deduct_count(cfg)
            if not ok:
                # 不回滚 budget：让限频正常计数，避免扣费失败时无限提取
                logger.warning(
                    "[proxy/%s] 主动切 IP 但扣费失败, reason=%s, code=%s（未提取 IP）",
                    self.platform, reason, business_code,
                )
                if business_code == 205:
                    ProxyPaywall.block(
                        code=205,
                        msg=f"扫号器代理套餐已无剩余次数（{self.platform}）",
                        platform=self.platform,
                    )
                elif business_code == _PAID_NETWORK_ERROR_CODE:
                    self._start_paid_cooldown_locked("deduct_network_error")
                return None

            # 3. 神龙 fetch（扣费成功后才提取）
            shen = self._fetch_with_backoff()
            if shen is None or not shen.is_ok:
                # 不回滚 budget：让限频正常计数，避免神龙失败时无限空扣费+空提取
                self._note_shenlong_fail_locked()
                logger.error(
                    "[proxy/%s] 主动切 IP 扣费成功但神龙提取失败, reason=%s（code=%s msg=%s）",
                    self.platform, reason,
                    shen.code.value if shen else "None",
                    shen.raw_msg if shen else "None",
                )
                return None

            self._note_shenlong_success_locked()

            # 4. 写新缓存（覆盖）
            _write_cache_row(
                self._db_path, self.cache_key,
                shen.ip, shen.port, shen.prov, shen.city,
            )
            logger.info(
                "[proxy/%s] 主动切 IP, reason=%s, new=%s:%s",
                self.platform, reason, shen.ip, shen.port,
            )
            return self._format_proxy_url(shen.ip, shen.port)

    def invalidate_cache(self, *, reason: str) -> None:
        """废弃当前平台缓存代理。

        用于请求阶段发现代理端口不可连/被拒绝的场景。这不是站点验证码风控，
        不消耗 force_rotate budget；下次 get_proxy() 会重新按规则扣费并取 IP。
        """
        cfg = self.cfg
        if not cfg.get("enabled", False):
            return
        explicit = str(cfg.get("explicit_proxy") or "").strip()
        if explicit:
            return
        with self._lock:
            cached = self._read_cache_with_legacy()
            _delete_cache_row(self._db_path, self.cache_key)
            if cached:
                logger.warning(
                    "[proxy/%s] 当前缓存代理已作废, reason=%s, old=%s:%s",
                    self.platform, reason, cached.ip, cached.port,
                )

    def cache_ttl_seconds(self) -> float:
        """返回当前平台代理 TTL 秒数。"""
        return self._ttl

    def paid_cooldown_remaining(self) -> float:
        """返回扣费接口网络错误冷却剩余秒数。"""
        with self._lock:
            return self._paid_cooldown_remaining_locked()

    def status(self) -> dict:
        """当前代理状态（用于日志 / UI 调试）。"""
        with self._lock:
            cached = self._read_cache_with_legacy()
            age = (time.time() - cached.fetched_at) if cached else None
            return {
                "platform": self.platform,
                "ip": (f"{cached.ip}:{cached.port}" if cached else None),
                "cache_age_sec": round(age, 1) if age is not None else None,
                "checked_open": self._checked_open,
                "not_opened": self._not_opened,
                "last_check_open_msg": self._last_check_open_msg,
                "paywall_blocked": ProxyPaywall.is_blocked(),
                "paid_cooldown_remaining_sec": round(self._paid_cooldown_remaining_locked(), 1),
                "shenlong_fail_streak": self._shenlong_fail_streak,
                "shenlong_pause_remaining_sec": round(self._shenlong_pause_remaining_locked(), 1),
                **self._budget.status(),
            }

    # ──── 内部方法 ────

    def _ensure_open_checked(self) -> None:
        """延迟校验：首次取代理时调一次 checkOpen API（仅一次）。

        HTTP 调用在 lock 外执行（~6s），避免阻塞其他线程取代理。
        _checked_open 标志仍在 lock 内原子设置，防止并发重复调用。
        未开通时设 _not_opened=True + 通过回调通知主线程弹窗。
        """
        # 快速路径：已校验或已知未开通，直接跳过
        if self._checked_open or self._not_opened:
            return

        # 原子抢占：只有一个线程能拿到"调用权"
        with self._lock:
            if self._checked_open or self._not_opened:
                return

        # HTTP 调用在 lock 外（不阻塞其他线程）
        logger.debug("[proxy/%s] 首次 checkOpen: 检查代理开通状态 ...", self.platform)
        opened, msg = _check_open(self.cfg)
        with self._lock:
            self._last_check_open_msg = str(msg or "")
        if opened:
            with self._lock:
                self._checked_open = True
            logger.info("[proxy/%s] ✓ checkOpen 已开通，代理可用", self.platform)
            return

        if _should_mark_not_opened(msg):
            # 只有明确未开通才锁定为直连；临时异常允许后续任务重新探测。
            with self._lock:
                self._checked_open = True
                self._not_opened = True
            logger.warning(
                "[proxy/%s] ✗ checkOpen 明确未开通: %s → 后续全部直连",
                self.platform, msg,
            )
            task_id = _get_active_task_id()
            if task_id is not None and _on_check_open_failed is not None:
                try:
                    _on_check_open_failed(task_id, self.platform)
                except Exception:
                    logger.exception("[proxy/%s] checkOpen 失败回调异常", self.platform)
            return

        logger.warning(
            "[proxy/%s] checkOpen 临时失败: %s → 本次直连，后续允许重试",
            self.platform, msg,
        )

    def _fetch_with_backoff(self) -> Optional[ShenlongResult]:
        """调用神龙，按 behavior 配置做 backoff。命中 204/205 自动触发付费墙。"""
        cfg = self.cfg
        shen_cfg = cfg.get("shenlong") or {}
        api_url = shen_cfg.get("api_url") or ""
        if not api_url:
            logger.error("[proxy/%s] shenlong.api_url 未配置", self.platform)
            return None

        behavior = cfg.get("behavior") or {}
        timeout = float(cfg.get("api_timeout_seconds", 6))

        # 206 / 208 各 backoff 重试，204/205 直接触发付费墙并返回 None
        for attempt in range(int(behavior.get("206_retry_max", 2)) + 1):
            result = shenlong_fetch_ip(api_url, timeout)
            if result.is_ok:
                return result

            if result.is_paywall:
                ProxyPaywall.block(
                    code=result.code.value,
                    msg=result.raw_msg or "神龙套餐不可用",
                    platform=self.platform,
                )
                return None

            if result.is_terminal:
                logger.error(
                    "[proxy/%s] 神龙致命错 code=%s msg=%s",
                    self.platform, result.code.value, result.raw_msg,
                )
                return None

            # backoff
            if result.code is ShenlongCode.RATE_LIMITED:
                wait = float(behavior.get("208_backoff_seconds", 3))
            elif result.code is ShenlongCode.NO_AVAILABLE_IP:
                wait = float(behavior.get("206_backoff_seconds", 2))
            else:
                # 其他临时错：不再重试
                logger.warning(
                    "[proxy/%s] 神龙临时错 code=%s 不重试, msg=%s",
                    self.platform, result.code.value, result.raw_msg,
                )
                return None
            time.sleep(wait)

        logger.warning("[proxy/%s] 神龙 backoff 重试耗尽", self.platform)
        return None

    def _paid_cooldown_remaining_locked(self) -> float:
        return max(0.0, self._paid_cooldown_until - time.monotonic())

    def _start_paid_cooldown_locked(self, reason: str) -> None:
        self._paid_cooldown_until = max(
            self._paid_cooldown_until,
            time.monotonic() + self._paid_cooldown_seconds,
        )
        logger.warning(
            "[proxy/%s] 扣费接口网络失败，进入 %.1fs 冷却, reason=%s",
            self.platform, self._paid_cooldown_seconds, reason,
        )

    def _shenlong_pause_remaining_locked(self) -> float:
        return max(0.0, self._shenlong_pause_until - time.monotonic())

    def _note_shenlong_success_locked(self) -> None:
        """神龙成功取到 IP：清零连续失败计数，解除暂停扣费。"""
        if self._shenlong_fail_streak or self._shenlong_pause_until:
            self._shenlong_fail_streak = 0
            self._shenlong_pause_until = 0.0

    def _note_shenlong_fail_locked(self) -> None:
        """神龙取不到 IP：累计连续失败次数，达到阈值则暂停扣费一段时间。"""
        self._shenlong_fail_streak += 1
        if self._shenlong_fail_streak >= self._shenlong_pause_threshold:
            self._shenlong_fail_streak = 0
            self._shenlong_pause_until = max(
                self._shenlong_pause_until,
                time.monotonic() + self._shenlong_pause_seconds,
            )
            logger.error(
                "[proxy/%s] 神龙连续 %d 次取不到 IP，暂停扣费 %.0fs（期间直连）",
                self.platform, self._shenlong_pause_threshold, self._shenlong_pause_seconds,
            )

    def _read_cache_with_legacy(self) -> Optional[CacheRow]:
        """读本平台缓存；如果 pangxie 读到旧裸 'shenlong' 自动迁移。"""
        cached = _read_cache_row(self._db_path, self.cache_key)
        if cached:
            return cached
        # 仅螃蟹做旧 key 兼容
        if self.platform == "pangxie":
            legacy = _read_cache_row(self._db_path, "shenlong")
            if legacy:
                logger.info(
                    "[proxy/%s] 兼容旧 cache key 'shenlong' → '%s'",
                    self.platform, self.cache_key,
                )
                _write_cache_row(
                    self._db_path, self.cache_key,
                    legacy.ip, legacy.port, legacy.prov, legacy.city,
                )
                _delete_cache_row(self._db_path, "shenlong")
                return CacheRow(
                    ip=legacy.ip, port=legacy.port,
                    prov=legacy.prov, city=legacy.city,
                    fetched_at=legacy.fetched_at,
                )
        return None

    def _format_proxy_url(self, ip: str, port: int) -> Dict[str, str]:
        cfg = self.cfg
        shen_cfg = cfg.get("shenlong") or {}
        user = str(shen_cfg.get("proxy_user") or "").strip()
        pwd = str(shen_cfg.get("proxy_pass") or "").strip()
        auth = f"{user}:{pwd}@" if user or pwd else ""
        url = f"http://{auth}{ip}:{port}"
        return {"http": url, "https": url}


# ────────── 模块级单例 ──────────


_managers: Dict[str, ProxyManager] = {}
_managers_lock = threading.Lock()
_paywall_installed = False


def get_manager(platform: str) -> ProxyManager:
    """获取（或懒创建）指定平台的 ProxyManager 单例。

    Args:
        platform: '7881' / 'pangxie' / 'kejinshou' / 'panzhi'
    """
    global _paywall_installed
    with _managers_lock:
        if platform in _managers:
            return _managers[platform]

        cfg = load_proxy_config()
        budget_cfg = cfg.get("budget") or {}
        budget = ProxySwitchBudget(
            max_count=int(budget_cfg.get("max_count", 3)),
            window_seconds=float(budget_cfg.get("window_seconds", 120)),
        )
        mgr = ProxyManager(
            platform=platform,
            cfg=cfg,
            _budget=budget,
        )
        _managers[platform] = mgr
        logger.info("[proxy] 创建 ProxyManager(%s)", platform)
        return mgr


def reset_managers() -> None:
    """清空所有 ProxyManager 单例。重新登录后调用，下次 get_manager 重建。"""
    global _paywall_installed
    with _managers_lock:
        _managers.clear()
        ProxyPaywall.clear()
        # 不重置 _paywall_installed 标志，避免重复 install
        logger.info("[proxy] 已清空所有 ProxyManager 单例")


def install_paywall_notifier(
    *,
    on_gui,
    on_dingtalk,
    throttle_seconds: float = 300.0,
) -> None:
    """在 app 启动时挂告警回调。"""
    ProxyPaywall.install(
        on_gui=on_gui,
        on_dingtalk=on_dingtalk,
        throttle_seconds=throttle_seconds,
    )
    global _paywall_installed
    _paywall_installed = True
    logger.info("[proxy] 付费墙告警回调已挂载")


def startup_verify(cfg: Optional[Dict] = None) -> Tuple[bool, str]:
    """启动期验证 1 次: 调用 /open/apipaid/function/checkOpen

    Returns:
        (enabled_override, message)
        - (True, "PROXY_DISABLED") 本地代理配置关闭, 跳过远端校验
        - (True, "OK_OPENED") 已开通, 代理可用
        - (False, "NOT_OPENED") 未开通, 建议关闭代理
        - (False, "MISSING_SIGNATURE_HEADERS" / "HTTP_xxx" / "BUSINESS_xxx") 接口异常, 仍按 cfg.enabled 走
        - (False, "NO_API_BASE") 配置缺 api_base

    设计: 这一步不阻断, 失败只是 log warning;
    caller 可根据返回值决定是否在 UI 弹窗告知用户 / 强制关闭代理 enabled flag.
    """
    cfg = cfg if cfg is not None else load_proxy_config()
    if not cfg.get("enabled", False):
        logger.info("[proxy/startup_verify] 代理配置未启用, 跳过开通校验")
        return True, "PROXY_DISABLED"
    if not bool((cfg.get("paid") or {}).get("enabled", True)):
        logger.info("[proxy/startup_verify] 扣费 API 已禁用（paid.enabled=false）, 跳过开通校验")
        return True, "PAID_DISABLED"
    opened, msg = _check_open(cfg)
    if opened:
        logger.info("[proxy/startup_verify] 扫号器代理已开通, 代理可用")
    else:
        logger.warning("[proxy/startup_verify] 扫号器代理未开通/异常: %s", msg)
    return opened, msg


__all__ = [
    "ProxyManager",
    "load_proxy_config",
    "get_manager",
    "reset_managers",
    "install_paywall_notifier",
    "startup_verify",
    "set_active_task",
    "clear_active_task",
    "set_check_open_failed_callback",
]
