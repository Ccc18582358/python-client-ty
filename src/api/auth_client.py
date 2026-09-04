#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
远端登录接口客户端

接口（恒星对接文档 - 登录管理）：
  GET  {base_url}/open/captcha?uuid={uuid}   -> image bytes
  POST {base_url}/open/login                  -> {code, msg, data: {token, expire}}

base_url / timeout 走 api_config（dev/prod 切换）
"""

import sys
from pathlib import Path
from typing import Optional, Tuple

# 让本文件能从 src/api/ 找到 src/config/api_config.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from config.api_config import api_config  # noqa: E402


def _url(path: str) -> str:
    return f"{api_config.base_url}{path}"


def get_captcha(uuid_str: str) -> Tuple[bool, object]:
    """获取图形验证码

    Returns:
        (True, image_bytes)  成功
        (False, error_msg)   失败
    """
    try:
        resp = requests.get(
            _url("/open/captcha"),
            params={"uuid": uuid_str},
            timeout=api_config.timeout,
        )
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        # 优先当图片读；后端有时回 JSON {code, msg, data: base64}
        ct = resp.headers.get("Content-Type", "")
        if "image" in ct or len(resp.content) > 100:
            return True, resp.content
        # 退化：尝试按 JSON 解析
        try:
            j = resp.json()
            if j.get("code") == 0 and j.get("data"):
                import base64
                b64 = j["data"]
                if isinstance(b64, str):
                    return True, base64.b64decode(b64)
        except Exception:
            pass
        return False, f"未识别的响应 (Content-Type={ct}, {len(resp.content)} bytes)"
    except requests.Timeout:
        return False, f"接口超时（>{api_config.timeout}s）"
    except requests.ConnectionError as e:
        return False, f"无法连接 {api_config.base_url}：{e.__class__.__name__}"
    except Exception as e:
        return False, f"请求异常：{e}"


def login(username: str, password: str, captcha: str, uuid_str: str,
          is_app: bool = False) -> Tuple[bool, str, Optional[dict]]:
    """登录

    Returns:
        (True, "ok", {"token", "expire_ms", "username"})  成功
        (False, err_msg, None)                             失败
    """
    payload = {
        "username": username,
        "password": password,
        "captcha": captcha,
        "uuid": uuid_str,
        "isApp": is_app,
    }
    try:
        resp = requests.post(
            _url("/open/login"),
            json=payload,
            timeout=api_config.timeout,
        )
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}", None
        try:
            j = resp.json()
        except Exception:
            return False, "响应不是合法 JSON", None

        if j.get("code") != 0:
            return False, j.get("msg") or "登录失败", None

        data = j.get("data") or {}
        token = data.get("token")
        expire = data.get("expire")  # 毫秒
        if not token:
            return False, "响应缺少 token", None
        return True, "ok", {
            "token": token,
            "expire_ms": expire,
            "username": username,
        }
    except requests.Timeout:
        return False, f"接口超时（>{api_config.timeout}s）", None
    except requests.ConnectionError as e:
        return False, f"无法连接 {api_config.base_url}：{e.__class__.__name__}", None
    except Exception as e:
        return False, f"请求异常：{e}", None


def current_env_label() -> str:
    return f"{api_config.name}（{api_config.base_url}）"
