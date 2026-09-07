#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PXB7 timestamp__1366 离线生成工具（Python + Node 桥接）。

复用 Panzhi 的离线 WAF 思路：不打开浏览器，直接把 pxb7 的阿里云 WAF SDK
(`.cdp_script15.js`) 放进 Node 的 `vm` 沙箱里执行，调用其核心签名函数
`LK(input, body, method, 2)` 得到带 `timestamp__1366` 的完整请求 URL。

签名结构（已逆向确认）：
    timestamp__1366 = "2790552d-" + u(LR)
    LR = [ h1, h2, env, now, wafBd, wafATs, fpId ].join("|")
        h1     = Li(LS(encodeURIComponent(wafBd + method + "2790552d" + url + body)))
        h2     = Li(LS(encodeURIComponent(wafBd + method + "2790552d" + url)))
        env    = L3()   # 浏览器环境指纹
        now    = Date.now()
        wafBd  = window._waf_bd8ce2ce37
        wafATs = window._waf_a86dfdc5f2
        fpId   = I()    # 设备指纹 id
    u() = LZ77 压缩 + 自定义 base64 编码
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional


APP_DIR = Path(__file__).resolve().parent
WAF_RUNNER = APP_DIR / "pxb7_waf_runner.js"
WAF_SCRIPT = APP_DIR / ".cdp_script15.js"

DEFAULT_WAF_BD = "c%2BD96VcC5QUdG0n6rJJarYsAFz2Fi5SZBvVIIIqSh2A%3D||1779495996761"
DEFAULT_WAF_A_TS = "1779452796761||1779495996761"
DEFAULT_WAF_FP_ID = "773980977a19e4f2b715e||1794996707684"
DEFAULT_WAF_DYSIG = "true||1779495996649"


def _default_local_storage() -> Dict[str, str]:
    return {
        "_waf_bd8ce2ce37": DEFAULT_WAF_BD,
        "_waf_a86dfdc5f2": DEFAULT_WAF_A_TS,
        "__00b204e9800998__": DEFAULT_WAF_FP_ID,
        "api-pc.pxb7.com_dySig": DEFAULT_WAF_DYSIG,
    }


_STATE_FILE = APP_DIR / "pxb7_waf_state.json"
_STATE_KEYS = ("_waf_bd8ce2ce37", "_waf_a86dfdc5f2", "__00b204e9800998__", "api-pc.pxb7.com_dySig")


def load_waf_state(path: Optional[Path] = None) -> Dict[str, str]:
    """读取 pxb7_refresh_waf_state.py 刷新的 WAF 状态，返回可直接传给
    ``build_request_url(..., local_storage_data=...)`` 的字典。

    未找到文件时返回空 dict（此时 build_request_url 会回退到内置默认值）。
    """
    state_path = Path(path) if path else _STATE_FILE
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: str(data[key]) for key in _STATE_KEYS if data.get(key) not in (None, "")}


def load_device_identity(path: Optional[Path] = None) -> Dict[str, str]:
    """读取设备指纹（device_id / gio_device），返回可直接塞进请求头的字典。

    缺失时返回空 dict（调用方按需生成稳定的 UUID）。这两个是应用层设备标识，
    与 ``timestamp__1366`` 签名无关，但缺失时后端可能拒绝请求。
    """
    state_path = Path(path) if path else _STATE_FILE
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: str(data[key])
        for key in ("device_id", "gio_device")
        if data.get(key) not in (None, "")
    }


def _build_payload(
    url: str,
    body_text: str,
    *,
    method: str = "POST",
    local_storage_data: Optional[Dict[str, Any]] = None,
    browser_fingerprint: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    local_storage = _default_local_storage()
    if local_storage_data:
        for key, value in local_storage_data.items():
            if value not in (None, ""):
                local_storage[str(key)] = str(value)
    payload: Dict[str, Any] = {
        "url": url,
        "body": body_text,
        "method": method,
        "scriptPath": str(WAF_SCRIPT),
        "scriptSource": WAF_SCRIPT.read_text(encoding="utf-8") if WAF_SCRIPT.exists() else "",
        "localStorageData": local_storage,
        "quiet": True,
    }
    # 动态浏览器指纹：让 Node 沙箱里的 navigator/screen/webgl/canvas 与请求头一致且可刷新。
    if browser_fingerprint is not None:
        payload["browserFingerprint"] = browser_fingerprint
    return payload


def run_decode_runner(
    url: str,
    body: Any,
    *,
    method: str = "POST",
    local_storage_data: Optional[Dict[str, Any]] = None,
    browser_fingerprint: Optional[Dict[str, Any]] = None,
    node: str = "node",
    timeout: int = 90,
) -> Dict[str, Any]:
    """运行 pxb7 WAF runner，返回原始 JSON 结果。"""
    if not WAF_RUNNER.exists():
        raise FileNotFoundError(f"missing runner: {WAF_RUNNER}")
    if not WAF_SCRIPT.exists():
        raise FileNotFoundError(f"missing WAF script: {WAF_SCRIPT}")

    body_text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    payload = _build_payload(
        url,
        body_text,
        method=method,
        local_storage_data=local_storage_data,
        browser_fingerprint=browser_fingerprint,
    )

    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as fh:
            json.dump(payload, fh, ensure_ascii=False)
            temp_path = fh.name
        completed = subprocess.run(
            [node, str(WAF_RUNNER), "--input", temp_path],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            # ★ 冻结的 GUI 程序（console=False）里子进程是控制台程序（node.exe），
            #   不设此标志会弹出一个一闪而过的黑窗。Windows 下用 CREATE_NO_WINDOW 隐藏。
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            stdout = (completed.stdout or "").strip()
            raise RuntimeError(stderr or stdout or "pxb7 decode runner failed")
        return json.loads(completed.stdout)
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def generate_timestamp_value(
    url: str,
    body: Any,
    *,
    method: str = "POST",
    local_storage_data: Optional[Dict[str, Any]] = None,
    browser_fingerprint: Optional[Dict[str, Any]] = None,
    node: str = "node",
    timeout: int = 90,
) -> str:
    """返回 `timestamp__1366` 的查询值（不含参数名与 URL）。"""
    result = run_decode_runner(
        url,
        body,
        method=method,
        local_storage_data=local_storage_data,
        browser_fingerprint=browser_fingerprint,
        node=node,
        timeout=timeout,
    )

    direct = str(result.get("timestampValue") or "").strip()
    if direct:
        return direct

    signed_url = str(result.get("decodeUrlFromA4") or "").strip()
    if signed_url:
        parsed = urllib.parse.urlsplit(signed_url)
        values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True).get("timestamp__1366") or []
        if values and values[0]:
            return values[0]

    raise RuntimeError("timestamp__1366 generation failed: no valid source")


def append_timestamp_param(url: str, timestamp_value: str) -> str:
    """给 URL 追加或替换 `timestamp__1366` 查询参数。"""
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "timestamp__1366"]
    query.append(("timestamp__1366", timestamp_value))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
    )


def build_request_url(
    url: str,
    body: Any,
    *,
    method: str = "POST",
    local_storage_data: Optional[Dict[str, Any]] = None,
    browser_fingerprint: Optional[Dict[str, Any]] = None,
    node: str = "node",
    timeout: int = 90,
) -> str:
    """便捷封装：返回带 `timestamp__1366` 的最终请求 URL。"""
    timestamp_value = generate_timestamp_value(
        url,
        body,
        method=method,
        local_storage_data=local_storage_data,
        browser_fingerprint=browser_fingerprint,
        node=node,
        timeout=timeout,
    )
    return append_timestamp_param(url, timestamp_value)


__all__ = [
    "append_timestamp_param",
    "build_request_url",
    "generate_timestamp_value",
    "run_decode_runner",
    "load_waf_state",
    "load_device_identity",
    "DEFAULT_WAF_BD",
    "DEFAULT_WAF_A_TS",
    "DEFAULT_WAF_FP_ID",
    "DEFAULT_WAF_DYSIG",
]
