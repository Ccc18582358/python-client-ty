#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盼之代售「商品列表页」离线参数生成 + 拉取（纯 Python，免 node / 免 WASM / 免 WAF 状态）。

结论（对比 tencentspider 参考工程后确认）：
  盼之有两条复杂度完全不同的接口链，别混用：

  1. 发布链（重）—— saveGoods：
     需要 ssxmod_itna/itna2 + decode__1174(Node 解 WAF challenge) + v17 WASM 签名
     + 每 8 小时 cron 刷新的 waf_state.json（与出口 IP 绑定）。
     参考工程 panzhi_publish_helper.py 里「发布能成功」靠的就是这套 + 新鲜 WAF 状态。

  2. 列表链（轻）—— general_goods/page（参考工程 `盼之商品列表.py` 正是这条）：
     只要「纯 MD5 签名 + 一个登录 token + 普通 requests」，不要 ssxmod、不要 acw_tc、
     不要 decode__1174、不要 WASM、不要 WAF 状态。这就是本模块实现的东西，
     可以完全离线生成参数（唯一的外部依赖是拿到一个有效的登录 token）。

  主仓 jingxi_spider_panzhi._fetch_list_page 的问题：它把「发布链的重型 WAF 管线」
  错套到了列表页（目标 goodsPublic/page），且 waf_state 已过期 ~28 天 → decode 失败 →
  被 WAF challenge/滑块拦截。goodsPublic/page 是「匿名浏览列表」，确实吃 WAF；
  而「登录用户自己的在售列表」走 general_goods/page 是轻接口。

用法：
    from services.panzhi_folder.panzhi_list_offline import fetch_goods_list
    records, err = fetch_goods_list(token, page=1, page_size=50)
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

try:
    from curl_cffi import requests as curl_requests  # type: ignore
    _HAS_CURL_CFFI = True
except Exception:  # noqa: BLE001
    curl_requests = None
    _HAS_CURL_CFFI = False

import requests as _plain_requests

# ── 常量（与参考工程 panzhi.py / 盼之商品列表.py 一致）──
ACCESS_KEY = "3qXyB7uf"
SIGN_MAGIC = "2147483647"  # 2^31-1，签名字符串里的固定占位
LIST_URL = "https://api.pzds.com/api/web-client/user/general_goods/page"
REFERER = "https://www.pzds.com/"
ORIGIN = "https://www.pzds.com"
PZ_VERSION = "1.0.0"  # 列表接口的 PZVersion 用 1.0.0（发布接口才用 26.x）


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def generate_list_sign(body: Dict[str, Any]) -> Dict[str, Any]:
    """生成列表接口的纯 MD5 签名（对齐 handle_sign，绝不用 WASM v17）。

    关键：body JSON 用 `urllib.parse.quote(..., safe='')` 编码 —— 连 -_.~ 也百分号化，
    与参考工程 handle_sign 完全一致（query_general_goods 里的 quote_js/safe='-_.~'
    是另一条路子，列表接口以 handle_sign 为准）。
    """
    timestamp = int(time.time() * 1000)
    # 参考工程用固定 '287882'，实际 Random 服务端不校验随机性；这里随机生成更稳妥。
    import random as _random
    random_str = "".join(str(_random.randint(0, 9)) for _ in range(6))
    body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    encoded = urllib.parse.quote(body_text, safe="")
    data_string = (
        f"PZTimestamp={timestamp}"
        f"&Random={random_str}"
        f"&{SIGN_MAGIC}={encoded}"
        f"&accessKey={ACCESS_KEY}"
    )
    return {
        "PZTimestamp": timestamp,
        "Random": random_str,
        "Sign": _md5(data_string),
    }


def build_list_headers(sign: Dict[str, Any], token: str) -> Dict[str, str]:
    """构建列表接口请求头（对齐盼之商品列表.py，无 ssxmod / 无 X-Sign-Version）。"""
    return {
        "PZTimestamp": str(sign["PZTimestamp"]),
        "Random": str(sign["Random"]),
        "Sign": str(sign["Sign"]),
        "Content-Type": "application/json",
        "PZPlatform": "pc",
        "token": token,
        "Referer": REFERER,
        "PZVersion": PZ_VERSION,
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/89.0.4389.114 Safari/537.36"
        ),
    }


def build_list_body(page: int, page_size: int = 50) -> Dict[str, Any]:
    """列表 body：拉取「在售」商品（可自行扩展 action 过滤条件）。"""
    return {
        "action": {"status": "ON_STAND"},
        "page": page,
        "pageSize": page_size,
    }


def _post(url: str, body: Dict[str, Any], headers: Dict[str, str],
          use_curl_cffi: bool = True) -> Tuple[int, str]:
    body_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if use_curl_cffi and _HAS_CURL_CFFI:
        resp = curl_requests.post(
            url, data=body_bytes, headers=headers, timeout=30,
            verify=False, impersonate="chrome136",
        )
        return resp.status_code, resp.text
    resp = _plain_requests.post(url, data=body_bytes, headers=headers,
                                timeout=30, verify=False)
    return resp.status_code, resp.text


def fetch_goods_list(
    token: str,
    page: int = 1,
    page_size: int = 50,
    use_curl_cffi: bool = True,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """拉取一页在售商品列表。

    Returns:
        (records, None) 成功；records 为商品记录列表。
        (None, err) 失败；err 是失败原因（如 token 过期 / NOT_LOGGED_IN）。
    """
    if not token:
        return None, "缺少登录 token（general_goods/page 是登录用户自己的在售列表接口）"

    body = build_list_body(page, page_size)
    sign = generate_list_sign(body)
    headers = build_list_headers(sign, token)

    status, text = _post(LIST_URL, body, headers, use_curl_cffi=use_curl_cffi)
    if status != 200:
        return None, f"HTTP {status}: {text[:200]}"
    try:
        data = json.loads(text)
    except Exception as e:  # noqa: BLE001
        return None, f"响应非 JSON: {e} / {text[:200]}"

    if not data.get("success"):
        return None, data.get("info") or data.get("code") or data.get("message") or "请求失败"

    payload = data.get("data") or {}
    records = payload.get("records") or payload.get("list") or []
    return list(records), None


def fetch_all_goods(
    token: str,
    page_size: int = 50,
    max_pages: int = 200,
    use_curl_cffi: bool = True,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """分页拉取全部在售商品。"""
    all_records: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        records, err = fetch_goods_list(token, page, page_size, use_curl_cffi)
        if err:
            return all_records, err
        if not records:
            break
        all_records.extend(records)
        if len(records) < page_size:
            break
        time.sleep(0.5)
    return all_records, None


__all__ = [
    "ACCESS_KEY",
    "SIGN_MAGIC",
    "LIST_URL",
    "generate_list_sign",
    "build_list_headers",
    "build_list_body",
    "fetch_goods_list",
    "fetch_all_goods",
]


if __name__ == "__main__":
    import sys

    token = sys.argv[1] if len(sys.argv) > 1 else ""
    if not token:
        print("用法: python panzhi_list_offline.py <token>")
        print("  token 为登录后拿到的 32 位 session token（参考工程硬编码的那个已过期）。")
        sys.exit(1)

    print("=== 生成列表页签名（纯 MD5，离线）===")
    body = build_list_body(1, 50)
    sign = generate_list_sign(body)
    print(json.dumps(sign, ensure_ascii=False, indent=2))

    print("\n=== 请求 general_goods/page ===")
    records, err = fetch_goods_list(token, page=1, page_size=50)
    if err:
        print("失败:", err)
        sys.exit(1)
    print(f"成功: 本页 {len(records)} 条")
    if records:
        print("样例字段:", list(records[0].keys())[:25])
        print(json.dumps(records[0], ensure_ascii=False)[:500])
