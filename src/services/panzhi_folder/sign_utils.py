#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盼之代售 (panzhi.com) 签名工具。

算法来源: 参考 panzhi.py 的 handle_sign()。

用法:
    from services.panzhi_folder.sign_utils import generate_sign, generate_sign_empty

    sig = generate_sign({"action": {"gameId": "11", ...}})
    # → {"PZTimestamp": 1782436759641, "Random": "591899", "Sign": "c09be6..."}

    sig = generate_sign_empty()
    # → {"PZTimestamp": ..., "Random": ..., "Sign": ...}   (GET /api/server/time 用)
"""

import hashlib
import json
import os
import random
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional
import uuid

# ── 常量 ──
ACCESS_KEY = "3qXyB7uf"
SIGN_MAGIC = "2147483647"  # 签名字符串中的固定占位符 (2^31 - 1)

# ── API 地址 ──
API_SERVER_TIME = "https://mcs.pzds.com/api/server/time"
DEFAULT_LIST_URL = "https://api.pzds.com/api/web-client/v2/public/goodsPublic/page"
APP_DIR = Path(__file__).resolve().parent
BROWSER_SIGN_RUNNER = APP_DIR / "pz_wasm_sign_runner.js"

# ── 工具函数 ──


def generate_device_id() -> str:
    """生成随机 32 位 hex 设备 ID（不带连字符）。"""
    return uuid.uuid4().hex


def generate_random_str(length: int = 6) -> str:
    """生成指定位数的随机数字字符串。"""
    return "".join(str(random.randint(0, 9)) for _ in range(length))


def generate_global_id() -> str:
    """生成随机 globalId（等同于 deviceId，也是 32 位 hex）。"""
    return uuid.uuid4().hex


def generate_pzid() -> str:
    """生成随机 pzid（9 位数字字符串）。"""
    return "".join(str(random.randint(1, 9)) for _ in range(9))


def generate_sign(
    data: Optional[Dict[str, Any]] = None,
    method: str = "POST",
) -> Dict[str, Any]:
    """生成盼之 API 签名（MD5 算法，兼容旧版 handle_sign）。

    Args:
        data: 请求体 dict（POST 时必传，GET 时传 None）
        method: HTTP 方法，"POST" 或 "GET"

    Returns:
        {"PZTimestamp": int, "Random": str, "Sign": str}
        注意 key 为大写，调用方可自行转为小写 headers。
    """
    random_str = generate_random_str(6)
    timestamp = int(time.time() * 1000)

    # 优先走仓里复制的浏览器签名 runner，和页面里的 v17 签名保持一致。
    if BROWSER_SIGN_RUNNER.exists():
        payload = {
            "body": data if data is not None else {},
            "method": method,
            "timestamp": timestamp,
            "random": int(random_str),
            "mode": "browser",
        }
        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as fh:
                json.dump(payload, fh, ensure_ascii=False)
                temp_path = fh.name
            completed = subprocess.run(
                ["node", str(BROWSER_SIGN_RUNNER), "--input", temp_path],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
                # ★ 冻结的 GUI 程序（console=False）里子进程是控制台程序（node.exe），
                #   不设此标志会弹出一个一闪而过的黑窗。Windows 下用 CREATE_NO_WINDOW 隐藏。
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                result = json.loads(completed.stdout)
                sign_value = result.get("sign") or result.get("Sign")
                if sign_value:
                    sig = {
                        "PZTimestamp": int(result.get("Timestamp") or timestamp),
                        "Random": str(result.get("Random") or random_str),
                        "Sign": str(sign_value),
                    }
                    # v17 签名必须带上版本号，否则服务端校验失败
                    version = result.get("version") or result.get("X-Sign-Version")
                    if version:
                        sig["X-Sign-Version"] = str(version)
                    return sig
        except Exception:
            # 回退到本地 MD5 实现，保证 runner 异常时还能工作。
            pass
        finally:
            if temp_path:
                try:
                    Path(temp_path).unlink(missing_ok=True)
                except Exception:
                    pass

    if method == "POST" and data is not None:
        # POST 带 body: 拼 body JSON 做签名
        post_data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        encoded_string = urllib.parse.quote(post_data_json, safe="")
        data_string = (
            f"PZTimestamp={timestamp}"
            f"&Random={random_str}"
            f"&{SIGN_MAGIC}={encoded_string}"
            f"&accessKey={ACCESS_KEY}"
        )
    elif method == "GET" and data is not None:
        # GET 带 query string: data 是 query string 如 "&goodsNo=xxx"
        data_string = (
            f"PZTimestamp={timestamp}"
            f"&Random={random_str}"
            f"{data}"
            f"&accessKey={ACCESS_KEY}"
        )
    else:
        # GET 无参数（如 /api/server/time）
        data_string = (
            f"PZTimestamp={timestamp}"
            f"&Random={random_str}"
            f"&accessKey={ACCESS_KEY}"
        )

    md5_str = hashlib.md5(data_string.encode("utf-8")).hexdigest()

    return {
        "PZTimestamp": timestamp,
        "Random": random_str,
        "Sign": md5_str,
    }


def generate_sign_empty() -> Dict[str, Any]:
    """生成空 body / GET 请求的签名（用于 /api/server/time 等）。"""
    return generate_sign(data=None, method="GET")


def build_sign_headers(signature: Dict[str, Any]) -> Dict[str, str]:
    """把签名 dict 转成小写 headers（适配 v2 公共接口风格）。

    Returns:
        {"pztimestamp": str, "random": str, "sign": str}
    """
    return {
        "pztimestamp": str(signature["PZTimestamp"]),
        "random": str(signature["Random"]),
        "sign": str(signature["Sign"]),
    }


__all__ = [
    "ACCESS_KEY",
    "SIGN_MAGIC",
    "API_SERVER_TIME",
    "DEFAULT_LIST_URL",
    "generate_device_id",
    "generate_random_str",
    "generate_global_id",
    "generate_pzid",
    "generate_sign",
    "generate_sign_empty",
    "build_sign_headers",
]
