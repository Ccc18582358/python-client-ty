#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Refresh Panzhi WAF state for the current outbound IP."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional

import requests as _fallback_requests

try:
    from curl_cffi import requests as curl_requests  # type: ignore[import-untyped]
    HAS_CURL_CFFI = True
except ImportError:
    curl_requests = None  # type: ignore[assignment]
    HAS_CURL_CFFI = False

from services.panzhi_folder.decode_utils import (
    DEFAULT_DEVICE_ID,
    DEFAULT_GLOBAL_ID,
    DEFAULT_PZID,
    run_challenge,
)

logger = logging.getLogger(__name__)

DEFAULT_BROWSER_VERSION = os.environ.get("PZ_BROWSER_VERSION", "136")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{DEFAULT_BROWSER_VERSION}.0.0.0 Safari/537.36"
)
SAVE_GOODS_INIT_URL = (
    "https://api.pzds.com/api/web-client/v2/userCenter/saveGoods?decode__1174=init"
)


def _app_root() -> Path:
    return Path(__file__).resolve().parents[3]


def canonical_waf_state_path(app_root: Optional[Path] = None) -> Path:
    root = app_root or _app_root()
    return root / "config" / "waf_state.json"


def fallback_waf_state_path(app_root: Optional[Path] = None) -> Path:
    root = app_root or _app_root()
    return root / "config" / "waf_state.json.bak2"


def _load_existing_state(app_root: Optional[Path] = None) -> Dict[str, Any]:
    for path in (canonical_waf_state_path(app_root), fallback_waf_state_path(app_root)):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.warning("[panzhi] load existing waf state failed from %s: %s", path.name, exc)
    return {}


def _head(value: Any) -> str:
    return str(value or "").split("||", 1)[0]


def _expire_ms(value: Any) -> int:
    text = str(value or "")
    if "||" not in text:
        return 0
    _, tail = text.split("||", 1)
    return int(tail) if tail.isdigit() else 0


def _decode_head(value: Any) -> str:
    head = _head(value)
    try:
        return urllib.parse.unquote(head)
    except Exception:
        return head


def waf_state_needs_refresh(app_root: Optional[Path] = None, *, skew_seconds: int = 300) -> bool:
    path = canonical_waf_state_path(app_root)
    if not path.exists():
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return True
    if not isinstance(data, dict):
        return True
    expire_ms = _expire_ms(data.get("waf_a_ts"))
    if expire_ms <= 0:
        return True
    return expire_ms <= int((time.time() + skew_seconds) * 1000)


def refresh_waf_state(
    app_root: Optional[Path] = None,
    *,
    timeout: int = 30,
    verify_ssl: bool = False,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """Refresh and persist a fresh waf_state.json for the current IP."""
    root = app_root or _app_root()
    existing = _load_existing_state(root)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": "https://www.pzds.com/",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None

    logger.info("[panzhi] refreshing waf_state via saveGoods init")
    if HAS_CURL_CFFI:
        resp = curl_requests.post(
            SAVE_GOODS_INIT_URL,
            data="{}",
            headers=headers,
            impersonate=f"chrome{DEFAULT_BROWSER_VERSION}",
            timeout=timeout,
            verify=verify_ssl,
            proxies=proxies,
        )
    else:
        resp = _fallback_requests.post(
            SAVE_GOODS_INIT_URL,
            data="{}",
            headers=headers,
            timeout=timeout,
            verify=verify_ssl,
            proxies=proxies,
        )

    html = resp.text
    if "aliyun_waf" not in html:
        raise RuntimeError(f"saveGoods init did not return WAF HTML, status={resp.status_code}")

    result = run_challenge(
        html,
        page_url="https://api.pzds.com/api/web-client/v2/userCenter/saveGoods",
        local_storage_data={},
        cookie_data="",
        browser_fingerprint={},
        force_env="",
        timeout=max(timeout, 30),
        user_agent=DEFAULT_USER_AGENT,
        external_scripts={},
    )
    if not isinstance(result, dict):
        raise RuntimeError("challenge runner returned invalid payload")

    ls = result.get("localStorage") or {}
    if not isinstance(ls, dict):
        ls = {}

    waf_state = {
        "waf_bd": str(ls.get("_waf_bd8ce2ce37") or ""),
        "waf_a_ts": str(ls.get("_waf_a86dfdc5f2") or ""),
        "waf_fp_id": _decode_head(ls.get("__00b204e9800998__")),
        "waf_dysig": str(ls.get("api.pzds.com_dySig") or ls.get("www.pzds.com_dySig") or ""),
        "cookie": str(result.get("cookie") or ""),
        "deviceId": str(existing.get("deviceId") or DEFAULT_DEVICE_ID),
        "globalId": str(existing.get("globalId") or DEFAULT_GLOBAL_ID),
        "PZid": str(existing.get("PZid") or DEFAULT_PZID),
    }
    if "||" not in waf_state["waf_bd"] or len(waf_state["waf_bd"]) <= 40:
        raise RuntimeError("refreshed waf_bd is incomplete")

    target = canonical_waf_state_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(waf_state, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("[panzhi] refreshed waf_state saved to %s", target)
    return waf_state


__all__ = [
    "canonical_waf_state_path",
    "fallback_waf_state_path",
    "refresh_waf_state",
    "waf_state_needs_refresh",
]
