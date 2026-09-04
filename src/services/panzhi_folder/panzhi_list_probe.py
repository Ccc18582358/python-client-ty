#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone list-page probe for panzhi.com.

This script does not open a browser. It only exercises the local signing,
WAF cookie generation, and list-page request pipeline so you can run it from
IDEA or a terminal and inspect the exact upstream result.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional


ROOT_DIR = Path(__file__).resolve().parents[3]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

from services.jingxi_spider_panzhi import PanzhiAccountCrawler  # noqa: E402


def _brief(text: Any, limit: int = 800) -> str:
    value = "" if text is None else str(text)
    value = value.replace("\r", "\\r").replace("\n", "\\n")
    return value[:limit]


def _parse_cookie(value: Optional[str]) -> Dict[str, str]:
    if not value:
        return {}
    cookies: Dict[str, str] = {}
    for part in value.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        if key:
            cookies[key] = val
    return cookies


def _load_context(value: str) -> Dict[str, Any]:
    if not value:
        return {}
    text = value.strip()
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    path = Path(text)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Panzhi list-page request without browser.")
    parser.add_argument("--game-id", default="11", help="Panzhi gameId, default: 11")
    parser.add_argument("--game-name", default="火影忍者", help="Display game name")
    parser.add_argument("--page", type=int, default=1, help="Page number to fetch")
    parser.add_argument("--page-size", type=int, default=10, help="Page size")
    parser.add_argument("--url", default="https://api.pzds.com/api/web-client/v2/public/goodsPublic/page")
    parser.add_argument("--proxy", default="", help="Optional explicit proxy URL")
    parser.add_argument("--verify-ssl", action="store_true", help="Enable SSL verification")
    parser.add_argument("--cookie", default="", help="Optional extra Cookie header values")
    parser.add_argument("--browser-context", default="", help="Browser snapshot JSON string or file path")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    crawler = PanzhiAccountCrawler(
        game_id=str(args.game_id),
        game_name=str(args.game_name),
        list_url=str(args.url),
        page_size=max(1, int(args.page_size)),
        proxy=str(args.proxy).strip() or None,
        verify_ssl=bool(args.verify_ssl),
    )

    extra_cookie = _parse_cookie(args.cookie)
    if extra_cookie:
        crawler._waf_cookies.update(extra_cookie)

    browser_context = _load_context(args.browser_context)
    if browser_context:
        crawler.load_browser_context(browser_context)

    response_json, err = crawler._fetch_list_page(int(args.page))
    output: Dict[str, Any] = {
        "ok": err is None and isinstance(response_json, dict),
        "error": err,
        "page": int(args.page),
        "page_size": int(args.page_size),
        "game_id": str(args.game_id),
        "game_name": str(args.game_name),
        "waf_bd": crawler._waf_bd,
        "waf_a_ts": crawler._waf_a_ts,
        "waf_request_info": crawler._waf_request_info,
        "waf_cookie_keys": sorted(crawler._waf_cookies.keys()),
        "last_list_fetch_debug": crawler._last_list_fetch_debug,
    }

    if isinstance(response_json, dict):
        data_block = response_json.get("data")
        output["success"] = response_json.get("success")
        output["code"] = response_json.get("code")
        output["info"] = response_json.get("info")
        output["data_type"] = type(data_block).__name__
        if isinstance(data_block, dict):
            output["data_keys"] = sorted(str(k) for k in data_block.keys())
            records = data_block.get("records") or data_block.get("list") or []
            output["record_count"] = len(records) if isinstance(records, list) else None
            if isinstance(records, list) and records:
                output["first_record"] = records[0]
        elif isinstance(data_block, list):
            output["record_count"] = len(data_block)
            if data_block:
                output["first_record"] = data_block[0]
    else:
        output["response_preview"] = _brief(getattr(response_json, "text", None) if response_json is not None else None)

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if err is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
