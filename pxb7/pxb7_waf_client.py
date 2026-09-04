#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pxb7 WAF 客户端：采集进程内惰性维护「触发挑战 → 取 token → 离线签名 → 带签请求」。

已用 ``_pxb7_cross_ip_test.py`` 验证：``_waf_bd8ce2ce37`` token 与 IP 无关（本地拿的
token 从神龙代理 IP 发同样返回真实 JSON），因此 **不需要每个代理 IP 生成一个新的 WAF**；
全局只维护一份 token，仅在过期（约 12h）或被服务器拒绝时惰性重新 challenge 一次。

设备身份（``device_id`` / ``gio_device`` / ``fpId``）会话内保持稳定，不与 IP 绑定。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from pxb7 import pxb7_waf
from pxb7 import pxb7_refresh_waf_state as refresh_mod

logger = logging.getLogger("pxb7_waf_client")

# 与 pxb7_waf.load_waf_state 消费的键保持一致
_STATE_KEYS = (
    "_waf_bd8ce2ce37",
    "_waf_a86dfdc5f2",
    "__00b204e9800998__",
    "api-pc.pxb7.com_dySig",
)

# 成功刷新后的最小去抖间隔：并发 worker 同时发现挑战时，避免瞬间触发多次 challenge
_REFRESH_DEBOUNCE_SECONDS = 30.0


class Pxb7WafClient:
    """线程安全的 WAF 状态管理器。启动时热读既有状态，之后按需惰性刷新。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: Dict[str, str] = {}
        self._device_id = ""
        self._gio_device = ""
        self._last_refresh_ok_at = 0.0
        self._load()

    # ── 载入 ──
    def _load(self) -> None:
        existing = refresh_mod.load_state() or {}
        self._state = {
            k: str(existing[k])
            for k in _STATE_KEYS
            if existing.get(k) not in (None, "")
        }
        self._device_id = str(existing.get("device_id") or "")
        self._gio_device = str(existing.get("gio_device") or "")

    # ── 状态 ──
    def device_headers(self) -> Dict[str, str]:
        """返回请求头里的应用层设备标识（device_id / gio_device）。"""
        return {
            "device_id": self._device_id,
            "gio_device": self._gio_device,
        }

    def is_fresh(self) -> bool:
        """token 是否存在且距过期至少还剩 5 分钟。"""
        if not self._state.get("_waf_bd8ce2ce37"):
            return False
        try:
            raw = self._state.get("_waf_a86dfdc5f2") or self._state.get("_waf_bd8ce2ce37") or ""
            expire_ms = int(str(raw).split("||")[-1])
            return expire_ms - int(time.time() * 1000) > 5 * 60 * 1000
        except (ValueError, IndexError):
            return False

    def refresh(self, proxies: Optional[Dict[str, str]] = None) -> bool:
        """触发一次 challenge 取新 token 并持久化。``proxies=None`` 表示直连。

        并发安全：加锁 + 30s 去抖，避免多个 worker 同时触发 challenge。
        失败时保留旧状态（旧 token 可能仍可用），返回 False。
        """
        with self._lock:
            now = time.monotonic()
            if now - self._last_refresh_ok_at < _REFRESH_DEBOUNCE_SECONDS and self.is_fresh():
                logger.debug("[pxb7_waf] 30s 内刚刷新过且仍新鲜，跳过重复 challenge")
                return True
            logger.info("[pxb7_waf] 触发 challenge 刷新 WAF token（%s）", "直连" if not proxies else "代理")
            try:
                state = refresh_mod.refresh(
                    proxies,
                    device_id=self._device_id,
                    gio_device=self._gio_device,
                )
            except Exception as exc:
                logger.warning("[pxb7_waf] challenge 刷新失败，沿用旧 token：%s", exc)
                return False
            self._apply_state(state)
            try:
                refresh_mod.persist_state(state)
            except Exception as exc:
                logger.warning("[pxb7_waf] 状态持久化失败（不影响本次）：%s", exc)
            self._last_refresh_ok_at = now
            logger.info("[pxb7_waf] WAF token 刷新成功")
            return True

    def _apply_state(self, state: Dict[str, Any]) -> None:
        self._state = {
            k: str(state[k])
            for k in _STATE_KEYS
            if state.get(k) not in (None, "")
        }
        self._device_id = str(state.get("device_id") or self._device_id)
        self._gio_device = str(state.get("gio_device") or self._gio_device)

    def ensure_fresh(self, proxies: Optional[Dict[str, str]] = None) -> bool:
        """未过期直接返回 True；过期则惰性 challenge 一次。"""
        if self.is_fresh():
            return True
        return self.refresh(proxies)

    # ── 签名 ──
    def sign(
        self,
        url: str,
        body_text: str,
        *,
        method: str = "POST",
        browser_fingerprint: Optional[Dict[str, Any]] = None,
    ) -> str:
        """离线生成带 ``timestamp__1366`` 的最终请求 URL。无 token 时返回原 URL。"""
        if not self._state:
            return url
        with self._lock:
            state = dict(self._state)
        try:
            return pxb7_waf.build_request_url(
                url,
                body_text,
                method=method,
                local_storage_data=state,
                browser_fingerprint=browser_fingerprint,
            )
        except Exception:
            return url


_client: Optional[Pxb7WafClient] = None
_client_lock = threading.Lock()


def get_client() -> Pxb7WafClient:
    """返回进程内唯一的 WAF 客户端（懒创建，进程内设备身份稳定）。"""
    global _client
    with _client_lock:
        if _client is None:
            _client = Pxb7WafClient()
        return _client


__all__ = ["Pxb7WafClient", "get_client"]
