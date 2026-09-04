#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PXB7 阿里云 WAF 状态刷新脚本 —— 对齐 Panzhi 的 refresh_waf.py。

背景
----
pxb7.com（螃蟹游戏交易平台）的接口域名 ``api-pc.pxb7.com`` 有一层阿里云 WAF，
请求需要带 ``timestamp__1366`` 签名。签名所需的 SDK 状态来自服务端下发的一次
WAF challenge（HTML 里的 ``<textarea id="renderData">`` 携带 ``_waf_bd8ce2ce37``）。

本脚本做的事（离线、不打开浏览器，与 Panzhi 同思路）：
  1. 通过神龙代理取一个 IP，向列表接口发一次 **不带签名** 的请求触发 challenge；
  2. 从返回 HTML 的 renderData 里解析出 ``_waf_bd8ce2ce37``；
  3. 组装 ``_waf_bd8ce2ce37`` / ``_waf_a86dfdc5f2``（时间戳）等 SDK 状态；
  4. 写入 ``pxb7/pxb7_waf_state.json``，供 ``pxb7_waf.build_request_url`` 消费。

crontab 示例（每 8 小时刷新一次，和 Panzhi 一致）：
  0 */8 * * * cd C:\\PythonProduct\\tongyong\\python-client && python pxb7/pxb7_refresh_waf_state.py

重要说明
--------
pxb7.com 的 WAF 只有 ``timestamp__1366`` 一层（CDP 抓包确认：整个真实浏览器会话
无任何 ``acw_sc__v2`` / ``acw_sc__v3`` / ``acw_tc`` cookie，也无 Cookie 头）。
之前看到的「滑块」是请求格式不对 / WAF 状态过期导致的误判：只要
  1) 请求体用 compact JSON（无空格、字段顺序与浏览器一致），
  2) 签名计算用到的 body 与实际发送的 body 完全一致，
  3) WAF 状态（本脚本刷新的 token）来自同一代理 IP 的 challenge，
就能纯离线通过校验，不需要滑块求解器。
"""
import json
import re
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests as plain_requests

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL = True
except Exception:
    curl_requests = None
    HAS_CURL = False

MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent
STATE_FILE = MODULE_DIR / "pxb7_waf_state.json"

LIST_URL = "https://api-pc.pxb7.com/api/search/product/v2/selectSearchPageList"
# 真实浏览器 body（CDP 抓包确认）：compact JSON、无 sortAttrId/mineFav、pageToken 翻页才带
DEFAULT_BODY = {
    "query": "",
    "gameId": "10032",
    "pageIndex": 1,
    "pageSize": 16,
    "bizProd": 1,
    "type": "4",
    "posType": 1,
    "filterDTOList": [],
    "combineFilterList": [],
}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# 12 小时过期（与 SDK 下发的 expire 时间对齐：now + 43200000ms）
EXPIRE_MS = 12 * 60 * 60 * 1000


def _new_device_identity() -> Dict[str, str]:
    """生成稳定的应用层设备标识（device_id / gio_device，UUID v4）。"""
    return {
        "device_id": str(uuid.uuid4()),
        "gio_device": str(uuid.uuid4()),
    }


def _headers(device_id: str = "", gio_device: str = "") -> Dict[str, str]:
    return {
        "accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "Origin": "https://www.pxb7.com",
        "Referer": "https://www.pxb7.com/",
        "User-Agent": DEFAULT_USER_AGENT,
        "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "client_type": "0",
        "os_type": "5",
        # 设备 / 会话指纹字段（无登录态，user_id 与 px-authorization-* 为空串）
        "device_id": device_id,
        "gio_device": gio_device,
        "user_id": "",
        "px-authorization-merchant": "",
        "px-authorization-user": "",
    }


def get_proxy() -> Tuple[Optional[Dict[str, str]], str]:
    """从神龙代理取一个 IP，返回 (proxies, ip_port)。失败返回 (None, "")。"""
    cfg_path = APP_ROOT / "config" / "proxy.json"
    if not cfg_path.exists():
        return None, ""
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return None, ""
    shenlong = cfg.get("shenlong") or {}
    api_url = shenlong.get("api_url", "")
    user = shenlong.get("proxy_user", "")
    pw = shenlong.get("proxy_pass", "")
    if not api_url:
        return None, ""
    try:
        raw = plain_requests.get(api_url, timeout=10).text
        obj = json.loads(raw)
        data = obj.get("data") or []
        ip_port = ""
        if data and data[0].get("ip") and data[0].get("port"):
            ip_port = f"{data[0]['ip']}:{data[0]['port']}"
    except Exception:
        ip_port = ""
    if not ip_port:
        return None, ""
    auth = f"{user}:{pw}@" if user or pw else ""
    proxy = {"http": f"http://{auth}{ip_port}", "https": f"http://{auth}{ip_port}"}
    return proxy, ip_port


def _post(url: str, body: str, proxies: Dict[str, str], device_id: str = "", gio_device: str = ""):
    headers = _headers(device_id, gio_device)
    if HAS_CURL:
        return curl_requests.post(
            url, data=body, headers=headers,
            impersonate="chrome", timeout=30, verify=False, proxies=proxies,
        )
    return plain_requests.post(url, data=body, headers=headers, timeout=30, verify=False, proxies=proxies)


def extract_token(html: str) -> str:
    """从 challenge HTML 的 renderData textarea 里解析出 _waf_bd8ce2ce37。"""
    match = re.search(r'<textarea[^>]*id="renderData"[^>]*>([\s\S]*?)</textarea>', html)
    if not match:
        return ""
    try:
        return json.loads(match.group(1)).get("_waf_bd8ce2ce37", "")
    except Exception:
        return ""


def load_state() -> Dict[str, Any]:
    """读取已保存的 PXB7 WAF 状态（若存在）。"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8")) or {}
        except Exception:
            pass
    return {}


def refresh(
    proxies: Optional[Dict[str, str]] = None,
    *,
    device_id: str = "",
    gio_device: str = "",
) -> Dict[str, Any]:
    """触发一次 challenge 并返回刷新后的 SDK 状态字典。

    ``proxies=None`` 表示直连（不再自动取代理）；需要走代理时由调用方传入。
    ``device_id`` / ``gio_device`` 可显式传入以保持会话内设备身份稳定；缺省从既有状态继承。
    """
    body = json.dumps(DEFAULT_BODY, ensure_ascii=False, separators=(",", ":"))

    # 指纹 / dySig / 设备标识保留既有值（与 Panzhi 保留 deviceId/globalId/PZid 同理）
    existing = load_state()
    if not device_id:
        device_id = existing.get("device_id") or ""
    if not gio_device:
        gio_device = existing.get("gio_device") or ""
    if not device_id or not gio_device:
        identity = _new_device_identity()
        device_id = identity["device_id"]
        gio_device = identity["gio_device"]

    resp = _post(LIST_URL, body, proxies, device_id, gio_device)
    html = resp.text or ""
    if "aliyun_waf" not in html and "renderData" not in html:
        raise RuntimeError(f"未触发 WAF challenge（status={resp.status_code}, len={len(html)}）")

    token = extract_token(html)
    if not token:
        raise RuntimeError("challenge HTML 中没有 renderData / _waf_bd8ce2ce37")

    now_ms = int(time.time() * 1000)
    expire_ms = now_ms + EXPIRE_MS
    encoded = urllib.parse.quote(token, safe="")

    # 设备指纹 fpId（SDK 本地生成，格式：<20位随机hex>||<时间戳>；缺失时新生成并持久化）
    fp_id = existing.get("__00b204e9800998__") or f"{uuid.uuid4().hex[:20]}||{now_ms}"

    state = {
        "_waf_bd8ce2ce37": f"{encoded}||{expire_ms}",
        "_waf_a86dfdc5f2": f"{now_ms}||{expire_ms}",
        "__00b204e9800998__": fp_id,
        "api-pc.pxb7.com_dySig": existing.get("api-pc.pxb7.com_dySig", "") or "true||%d" % now_ms,
        "device_id": device_id,
        "gio_device": gio_device,
        "refreshed_at": now_ms,
        "proxy_ip": (proxies or {}).get("https", ""),
    }
    return state


def persist_state(state: Dict[str, Any]) -> None:
    """原子写入 WAF 状态文件（crontab 与采集进程内惰性刷新共用）。"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    tmp.rename(STATE_FILE)


def run() -> bool:
    print(f"[pxb7_refresh_waf] 请求 WAF challenge: {LIST_URL}")
    for attempt in range(1, 4):
        proxies, ip = get_proxy()
        if not proxies:
            print("[pxb7_refresh_waf] 取代理失败")
            return False
        print(f"[pxb7_refresh_waf] 代理 IP = {ip}")
        try:
            state = refresh(proxies)
            break
        except (TimeoutError, ConnectionError, OSError) as exc:
            print(f"  [retry] {type(exc).__name__}: {exc}")
            continue
        except Exception as exc:
            print(f"  [retry] {type(exc).__name__}: {exc}")
            continue
    else:
        print("[pxb7_refresh_waf] 所有代理尝试均失败")
        return False

    persist_state(state)

    bd = str(state.get("_waf_bd8ce2ce37", ""))
    print(f"[pxb7_refresh_waf] waf_bd={bd[:30]}... refreshed_at={state['refreshed_at']}")
    print(f"[pxb7_refresh_waf] 已写入 {STATE_FILE}")
    return True


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
