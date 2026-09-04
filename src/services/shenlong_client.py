"""神龙代理 IP 接口客户端。

业务规则：
- 成功响应 JSON: {"code":200, "data":[{"ip":"...", "port":..., "prov":"...", "city":"..."}]}
- 失败响应 JSON: {"code":2xx, "msg":"..."}
- 8 个业务码:
    200 成功
    201 请求格式不正确      → 不重试
    202 单次请求数量超出最大值 → 不重试
    203 请求 KEY 异常        → 不重试，记 ERROR
    204 套餐已过期          → 全局付费墙（GUI 弹窗）
    205 套餐提取数量上限    → 全局付费墙（钉钉告警）
    206 暂无可用 IP        → backoff 重试
    207 提取地区超出服务范围 → 不重试，记 WARNING
    208 提取频率太快        → backoff 重试
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class ShenlongCode(Enum):
    """神龙返回的业务码枚举。"""

    OK = 200
    INVALID_FORMAT = 201
    COUNT_EXCEEDED = 202
    KEY_INVALID = 203
    QUOTA_EXPIRED = 204
    QUOTA_EXHAUSTED = 205
    NO_AVAILABLE_IP = 206
    REGION_OUT_OF_SCOPE = 207
    RATE_LIMITED = 208


# 决策矩阵：哪些码走"付费墙"、哪些走"配置错不重试"、哪些走"backoff 重试"
_PAYWALL_CODES = frozenset({ShenlongCode.QUOTA_EXPIRED, ShenlongCode.QUOTA_EXHAUSTED})
_BACKOFF_CODES = frozenset({ShenlongCode.NO_AVAILABLE_IP, ShenlongCode.RATE_LIMITED})
_FATAL_CODES = frozenset({
    ShenlongCode.INVALID_FORMAT,
    ShenlongCode.COUNT_EXCEEDED,
    ShenlongCode.KEY_INVALID,
    ShenlongCode.REGION_OUT_OF_SCOPE,
})


@dataclass
class ShenlongResult:
    """神龙响应解析后的统一返回结构。"""

    code: ShenlongCode
    ip: str = ""
    port: int = 0
    prov: str = ""
    city: str = ""
    raw_msg: str = ""
    http_status: int = 0
    is_paywall: bool = False
    is_retryable: bool = False
    is_terminal: bool = False  # 配置错，不重试

    @property
    def is_ok(self) -> bool:
        return self.code is ShenlongCode.OK and bool(self.ip) and self.port > 0

    @property
    def proxy_url(self) -> str:
        """格式: 'ip:port'（user/pass 由调用方拼接）"""
        return f"{self.ip}:{self.port}"


def fetch_ip(api_url: str, timeout: float = 6.0) -> ShenlongResult:
    """同步调用一次神龙取 IP 接口。

    失败/异常**不抛**，全部走 ShenlongResult 返回，由 caller 决定如何 fallback。

    Args:
        api_url: 完整的神龙 API URL（含 key、count 等 query 参数）
        timeout: HTTP 请求超时（秒）

    Returns:
        ShenlongResult，包含业务码分类与原始错误信息
    """
    try:
        resp = requests.get(api_url, timeout=timeout)
    except requests.RequestException as exc:
        logger.warning("[shenlong] 网络错误: %s", exc)
        return ShenlongResult(
            code=ShenlongCode.NO_AVAILABLE_IP,  # 临时不可用，触发 backoff
            raw_msg=f"network:{exc!s:.80}",
            is_retryable=True,
        )

    http_status = resp.status_code
    if http_status != 200:
        logger.warning("[shenlong] HTTP %s, body=%s", http_status, resp.text[:80])
        return ShenlongResult(
            code=ShenlongCode.NO_AVAILABLE_IP,
            raw_msg=f"http_{http_status}",
            http_status=http_status,
            is_retryable=True,
        )

    try:
        data = resp.json()
    except ValueError:
        logger.warning("[shenlong] 响应不是 JSON: %s", resp.text[:120])
        return ShenlongResult(
            code=ShenlongCode.NO_AVAILABLE_IP,
            raw_msg="non_json_response",
            http_status=http_status,
            is_retryable=True,
        )

    if not isinstance(data, dict):
        return ShenlongResult(
            code=ShenlongCode.NO_AVAILABLE_IP,
            raw_msg="non_object_json",
            http_status=http_status,
            is_retryable=True,
        )

    code_int = data.get("code")
    try:
        code_int = int(code_int)
    except (TypeError, ValueError):
        return ShenlongResult(
            code=ShenlongCode.INVALID_FORMAT,
            raw_msg=f"invalid_code:{code_int}:{data.get('msg', '')}",
            http_status=http_status,
            is_terminal=True,
        )
    if code_int == 200:
        items = data.get("data") or []
        if not items or not isinstance(items, list):
            return ShenlongResult(
                code=ShenlongCode.NO_AVAILABLE_IP,
                raw_msg="empty_data",
                http_status=http_status,
                is_retryable=True,
            )
        # 多个候选 IP 时随机选一个
        item = random.choice([i for i in items if isinstance(i, dict)] or [{}])
        ip = str(item.get("ip") or "").strip()
        try:
            port = int(item.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not ip or port <= 0:
            return ShenlongResult(
                code=ShenlongCode.INVALID_FORMAT,
                raw_msg="missing_ip_or_port",
                http_status=http_status,
                is_terminal=True,
            )
        return ShenlongResult(
            code=ShenlongCode.OK,
            ip=ip,
            port=port,
            prov=str(item.get("prov") or ""),
            city=str(item.get("city") or ""),
            http_status=http_status,
        )

    # 业务码 2xx
    try:
        code_enum = ShenlongCode(code_int)
    except ValueError:
        return ShenlongResult(
            code=ShenlongCode.INVALID_FORMAT,
            raw_msg=f"unknown_code:{code_int}:{data.get('msg', '')}",
            http_status=http_status,
            is_terminal=True,
        )

    msg = str(data.get("msg") or data.get("message") or "")
    is_paywall = code_enum in _PAYWALL_CODES
    is_retryable = code_enum in _BACKOFF_CODES
    is_terminal = code_enum in _FATAL_CODES

    if is_paywall:
        logger.error("[shenlong] 付费墙触发: code=%s msg=%s", code_int, msg)
    elif is_terminal:
        logger.error("[shenlong] 配置/系统错: code=%s msg=%s", code_int, msg)
    elif is_retryable:
        logger.warning("[shenlong] 临时不可用: code=%s msg=%s", code_int, msg)
    else:
        logger.info("[shenlong] 业务码 %s: %s", code_int, msg)

    return ShenlongResult(
        code=code_enum,
        raw_msg=msg,
        http_status=http_status,
        is_paywall=is_paywall,
        is_retryable=is_retryable,
        is_terminal=is_terminal,
    )


__all__ = ["ShenlongCode", "ShenlongResult", "fetch_ip"]
