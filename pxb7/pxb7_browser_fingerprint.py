#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pxb7 动态浏览器指纹生成器（离线、随机化）。

为 pxb7 的阿里云 WAF SDK（`.cdp_script15.js`）在 Node vm 沙箱里补一个**每次运行都
不一样**的浏览器环境，避免同一套静态指纹被风控按「固定环境 → 爬虫」聚类。

说明
----
实测（对 SDK 的 ``L3()`` 连续采样 300 次）确认：签名 ``timestamp__1366`` 里的
``env`` 字段本质是一个 **29 位随机数**（bit0~28 全随机、bit29~31 恒为 0），并不是
canvas/webgl/UA 这些可校验的指纹。因此本模块的核心价值不在 ``env``，而在：

  1. 让 Node 沙箱里的 ``navigator / screen / WebGL / canvas`` 与请求头里的
     ``User-Agent / sec-ch-ua / sec-ch-ua-platform`` 保持一致且每次刷新，
     避免「头与沙箱环境不一致」这类低级指纹破绽；
  2. 提供稳定的**设备指纹**（``fpId`` 的 20 位 hex、``device_id``、``gio_device``），
     使同一会话多次请求共享同一设备身份，跨会话又各不相同。

所有取值都落在真实 Chrome/Windows 的合理范围内，带随机抖动。
"""

from __future__ import annotations

import hashlib
import random
import secrets
import uuid
from typing import Any, Dict, List, Tuple

# ── 真实环境取值池（只取 Chrome + Windows，与本项目抓包的 Chrome/149 对齐）────────
_CHROME_VERSIONS = [
    "149.0.0.0", "148.0.0.0", "147.0.0.0", "146.0.0.0", "145.0.0.0",
    "144.0.0.0", "143.0.0.0", "142.0.0.0", "141.0.0.0", "140.0.0.0",
]

# (width, height, availHeight, devicePixelRatio) —— 常见 Windows 桌面分辨率
_SCREENS: List[Tuple[int, int, int, int]] = [
    (1920, 1080, 1040, 1), (1920, 1080, 1040, 2),
    (2560, 1440, 1400, 1), (2560, 1440, 1400, 2),
    (1536, 864, 824, 2), (1440, 900, 852, 1),
    (1366, 768, 728, 1), (1280, 720, 680, 1),
    (3440, 1440, 1400, 1), (3840, 2160, 2080, 2),
]

# (vendor, renderer) —— ANGLE + 常见独显/核显，浏览器里真实的 WebGL 串
_WEBGL: List[Tuple[str, str]] = [
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]

_LANGS = ["zh-CN", "zh", "en", "en-US"]


def _canvas_hash() -> str:
    """随机 canvas 指纹（模拟 toDataURL 的 base64 尾串，32 位 hex）。"""
    return secrets.token_hex(16)


def _random_ua() -> str:
    ver = random.choice(_CHROME_VERSIONS)
    major = ver.split(".")[0]
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver} Safari/537.36"
    ), major


def generate_browser_fingerprint() -> Dict[str, Any]:
    """生成一份随机的 Chrome/Windows 浏览器指纹（供 runner 的 browserFingerprint 用）。

    返回结构对应 ``pxb7_waf_runner.js`` 里 ``createContext`` 读取的字段：
      navigator / screen / webgl / performanceMemory / canvasHash。
    """
    ua, major = _random_ua()
    width, height, avail_height, dpr = random.choice(_SCREENS)
    vendor, renderer = random.choice(_WEBGL)
    cores = random.choice([8, 12, 16, 20])
    mem = random.choice([8, 16, 32])

    # 与 UA 保持一致的 sec-ch-ua 头（供调用方同步到请求头）
    sec_ch_ua = (
        f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not)A;Brand";v="24"'
    )

    return {
        "userAgent": ua,
        "secChUa": sec_ch_ua,
        "navigator": {
            "userAgent": ua,
            "platform": "Win32",
            "vendor": "Google Inc.",
            "webdriver": False,
            "language": "zh-CN",
            "languages": _LANGS[:],
            "hardwareConcurrency": cores,
            "deviceMemory": mem,
            "maxTouchPoints": 0,
            "cookieEnabled": True,
        },
        "screen": {
            "width": width,
            "height": height,
            "availWidth": width,
            "availHeight": avail_height,
            "colorDepth": 24,
            "pixelDepth": 24,
            "devicePixelRatio": dpr,
            "outerWidth": width,
            "outerHeight": height,
            "innerWidth": width,
            "innerHeight": avail_height,
        },
        "webgl": {
            "vendor": vendor,
            "renderer": renderer,
            "version": "WebGL 1.0 (OpenGL ES 2.0 Chromium)",
            "shadingLanguageVersion": "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)",
            "maxTextureSize": 16384,
        },
        "performanceMemory": {
            "usedJSHeapSize": random.randint(40_000_000, 200_000_000),
            "totalJSHeapSize": random.randint(120_000_000, 400_000_000),
            "jsHeapSizeLimit": 4_294_967_296,
        },
        "canvasHash": _canvas_hash(),
    }


def generate_device_identity() -> Dict[str, str]:
    """生成一份动态设备身份（应用层 + 签名里的 fpId）。

    返回：
      device_id / gio_device —— 请求头里的应用层设备标识（UUID v4）
      fpId                   —— 签名 LR 数组里的 20 位 hex 设备指纹
    同一会话应只生成一次并复用（见 pxb7_refresh_waf_state.refresh）。
    """
    return {
        "device_id": str(uuid.uuid4()),
        "gio_device": str(uuid.uuid4()),
        "fpId": secrets.token_hex(10),  # 20 hex chars，与 SDK 的 I() 一致
    }


def headers_for(fingerprint: Dict[str, Any], identity: Dict[str, str]) -> Dict[str, str]:
    """由指纹 + 设备身份拼出与 SDK 环境一致的请求头。"""
    return {
        "accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "User-Agent": fingerprint["userAgent"],
        "sec-ch-ua": fingerprint["secChUa"],
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "device_id": identity.get("device_id", ""),
        "gio_device": identity.get("gio_device", ""),
    }


__all__ = [
    "generate_browser_fingerprint",
    "generate_device_identity",
    "headers_for",
]
