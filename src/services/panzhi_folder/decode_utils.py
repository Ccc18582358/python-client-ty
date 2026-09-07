#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PZDS decode__1174 生成工具。

This is the local Python bridge for the copied Node runner and browser decode
script. It mirrors the helper flow used in the source project:

1. Build the request payload for the dynamic runner.
2. Execute `pz_waf_dynamic_runner.js` with the local decode script.
3. Extract the `decode__1174` value from the returned URL.
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
WAF_RUNNER = APP_DIR / "pz_waf_dynamic_runner.js"
WAF_DYNAMIC_SCRIPT = APP_DIR / "pz_decode_root_dynamic_script_latest.js"

DEFAULT_DEVICE_ID = "2cc79df30c534ca4ba808aed2a88ab72"
DEFAULT_GLOBAL_ID = "59980215710446a7bb2d7670b4f4aa04"
DEFAULT_PZID = "187494449"
DEFAULT_WAF_BD = "c%2BD96VcC5QUdG0n6rJJarYsAFz2Fi5SZBvVIIIqSh2A%3D||1779495996761"
DEFAULT_WAF_A_TS = "1779452796761||1779495996761"
DEFAULT_WAF_FP_ID = "773980977a19e4f2b715e||1794996707684"
DEFAULT_WAF_DYSIG = "true||1779495996649"


def _default_local_storage() -> Dict[str, str]:
    return {
        "_waf_bd8ce2ce37": DEFAULT_WAF_BD,
        "_waf_a86dfdc5f2": DEFAULT_WAF_A_TS,
        "api.pzds.com_dySig": DEFAULT_WAF_DYSIG,
    }


def _build_payload(
    url: str,
    body_text: str,
    *,
    method: str = "POST",
    cookie_data: Optional[str] = None,
    local_storage_data: Optional[Dict[str, Any]] = None,
    request_info_data: Optional[Dict[str, Any]] = None,
    challenge_script_source: Optional[str] = None,
    render_data: Optional[str] = None,
    page_url: Optional[str] = None,
) -> Dict[str, Any]:
    local_storage = _default_local_storage()
    if local_storage_data:
        for key, value in local_storage_data.items():
            if value not in (None, ""):
                local_storage[str(key)] = str(value)
    dynamic_script_source = ""
    for key in ("_waf_a23a0b772", "_waf_3d7faf79"):
        value = local_storage.get(key)
        if value:
            dynamic_script_source = urllib.parse.unquote(str(value).split("||", 1)[0])
            if dynamic_script_source:
                break
    return {
        "url": url,
        "body": body_text,
        "method": method,
        "scriptPath": str(WAF_DYNAMIC_SCRIPT),
        "scriptSource": dynamic_script_source,
        "cookie": cookie_data or "",
        "localStorageData": local_storage,
        "requestInfo": request_info_data or None,
        "challengeScriptSource": challenge_script_source or "",
        "renderData": render_data or "",
        "pageUrl": page_url or "",
        "quiet": True,
    }


def run_decode_runner(
    url: str,
    body: Any,
    *,
    method: str = "POST",
    cookie_data: Optional[str] = None,
    local_storage_data: Optional[Dict[str, Any]] = None,
    request_info_data: Optional[Dict[str, Any]] = None,
    challenge_script_source: Optional[str] = None,
    render_data: Optional[str] = None,
    page_url: Optional[str] = None,
    node: str = "node",
    timeout: int = 90,
) -> Dict[str, Any]:
    """Run the copied WAF runner and return its raw JSON result."""
    if not WAF_RUNNER.exists():
        raise FileNotFoundError(f"missing runner: {WAF_RUNNER}")
    if not WAF_DYNAMIC_SCRIPT.exists():
        raise FileNotFoundError(f"missing dynamic script: {WAF_DYNAMIC_SCRIPT}")

    body_text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    payload = _build_payload(
        url,
        body_text,
        method=method,
        cookie_data=cookie_data,
        local_storage_data=local_storage_data,
        request_info_data=request_info_data,
        challenge_script_source=challenge_script_source,
        render_data=render_data,
        page_url=page_url,
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
            raise RuntimeError(stderr or stdout or "decode runner failed")
        return json.loads(completed.stdout)
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def generate_decode_value(
    url: str,
    body: Any,
    *,
    method: str = "POST",
    cookie_data: Optional[str] = None,
    local_storage_data: Optional[Dict[str, Any]] = None,
    request_info_data: Optional[Dict[str, Any]] = None,
    challenge_script_source: Optional[str] = None,
    render_data: Optional[str] = None,
    page_url: Optional[str] = None,
    node: str = "node",
    timeout: int = 90,
) -> str:
    """Return the decoded `decode__1174` query value.

    Args:
        url: Target API URL without `decode__1174`.
        body: Request body as dict or already serialized string.
        method: HTTP method, default POST.
        local_storage_data: Optional extra localStorage values.
        node: Node executable name or path.
        timeout: Subprocess timeout in seconds.
    """
    if not WAF_RUNNER.exists():
        raise FileNotFoundError(f"missing runner: {WAF_RUNNER}")
    if not WAF_DYNAMIC_SCRIPT.exists():
        raise FileNotFoundError(f"missing dynamic script: {WAF_DYNAMIC_SCRIPT}")

    result = run_decode_runner(
        url,
        body,
        method=method,
        cookie_data=cookie_data,
        local_storage_data=local_storage_data,
        request_info_data=request_info_data,
        challenge_script_source=challenge_script_source,
        render_data=render_data,
        page_url=page_url,
        node=node,
        timeout=timeout,
    )

    sources = []

    a4_url = str(result.get("decodeUrlFromA4") or "").strip()
    if a4_url:
        sources.append(("decodeUrlFromA4", a4_url, False))

    xhr_url = str(result.get("xhrUrl") or "").strip()
    if xhr_url and xhr_url != url:
        sources.append(("xhrUrl", xhr_url, False))

    xhr_a5_url = str(result.get("xhrA5Url") or "").strip()
    if xhr_a5_url and xhr_a5_url != url:
        sources.append(("xhrA5Url", xhr_a5_url, False))

    a5_url = str(result.get("xhrHookUrlFromA5") or "").strip()
    if a5_url and a5_url != url:
        sources.append(("xhrHookUrlFromA5", a5_url, False))

    manual_decode = str(result.get("manualDecode") or "").strip()
    if manual_decode:
        try:
            manual_decode = urllib.parse.unquote(manual_decode)
        except Exception:
            pass
        sources.append(("manualDecode", manual_decode, True))

    last_error = ""
    for source_name, source_value, is_direct in sources:
        if is_direct:
            if source_value:
                return source_value
            last_error = f"{source_name} 为空"
            continue
        parsed = urllib.parse.urlsplit(source_value)
        query_pairs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        values = query_pairs.get("decode__1174") or []
        if values and values[0]:
            return values[0]
        last_error = f"{source_name} 中未找到 decode__1174"

    raise RuntimeError(
        f"decode__1174 generation failed: {last_error or 'no valid decode source'}"
    )


def run_challenge(
    html: str,
    *,
    page_url: str = "https://api.pzds.com/api/web-client/v2/public/goodsPublic/page",
    local_storage_data: Optional[Dict[str, Any]] = None,
    cookie_data: Optional[str] = None,
    browser_fingerprint: Optional[Dict[str, Any]] = None,
    force_env: str = "",
    node: str = "node",
    timeout: int = 30,
    user_agent: str = "",
    external_scripts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute WAF challenge via Node.js runner, return updated state.

    When the API returns WAF challenge HTML (contains ``aliyun_waf`` markers),
    feed it to the Node runner which executes the challenge JS and extracts
    fresh localStorage / globals / cookie values.

    Returns:
        {
            "localStorage": {...},   # updated WAF localStorage values
            "globals": {...},        # raw globals from WAF JS execution
            "cookie": "...",         # updated cookie string
        }
    """
    if not WAF_RUNNER.exists():
        raise FileNotFoundError(f"missing runner: {WAF_RUNNER}")
    if not WAF_DYNAMIC_SCRIPT.exists():
        raise FileNotFoundError(f"missing dynamic script: {WAF_DYNAMIC_SCRIPT}")

    ls_data = dict(local_storage_data) if local_storage_data else _default_local_storage()
    payload: Dict[str, Any] = {
        "challenge": True,
        "html": html,
        "pageUrl": page_url,
        "localStorageData": ls_data,
        "browserFingerprint": browser_fingerprint or {},
        "forceEnv": force_env,
        "cookie": cookie_data or "",
        "quiet": True,
    }
    # 对齐 refresh_waf.py: 传入 userAgent 和 externalScripts
    if user_agent:
        payload["userAgent"] = user_agent
    if external_scripts is not None:
        payload["externalScripts"] = external_scripts

    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as fh:
            json.dump(payload, fh, ensure_ascii=False)
            temp_path = fh.name
        completed = subprocess.run(
            [node, str(WAF_RUNNER), "--input", temp_path, "--challenge"],
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
            raise RuntimeError(stderr or stdout or "challenge runner failed")
        result: Dict[str, Any] = json.loads(completed.stdout)
        if not isinstance(result, dict):
            raise RuntimeError("challenge runner returned non-dict")
        return result
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def append_decode_param(url: str, decode_value: str) -> str:
    """Append or replace the `decode__1174` query parameter."""
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "decode__1174"]
    query.append(("decode__1174", decode_value))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
    )


def build_request_url(
    url: str,
    body: Any,
    *,
    method: str = "POST",
    cookie_data: Optional[str] = None,
    local_storage_data: Optional[Dict[str, Any]] = None,
    request_info_data: Optional[Dict[str, Any]] = None,
    challenge_script_source: Optional[str] = None,
    render_data: Optional[str] = None,
    page_url: Optional[str] = None,
    node: str = "node",
    timeout: int = 90,
) -> str:
    """Convenience wrapper that returns the final URL with `decode__1174`."""
    decode_value = generate_decode_value(
        url,
        body,
        method=method,
        cookie_data=cookie_data,
        local_storage_data=local_storage_data,
        request_info_data=request_info_data,
        challenge_script_source=challenge_script_source,
        render_data=render_data,
        page_url=page_url,
        node=node,
        timeout=timeout,
    )
    return append_decode_param(url, decode_value)


__all__ = [
    "append_decode_param",
    "build_request_url",
    "generate_decode_value",
    "run_challenge",
    "run_decode_runner",
    "DEFAULT_DEVICE_ID",
    "DEFAULT_GLOBAL_ID",
    "DEFAULT_PZID",
    "DEFAULT_WAF_BD",
    "DEFAULT_WAF_A_TS",
    "DEFAULT_WAF_FP_ID",
    "DEFAULT_WAF_DYSIG",
]
