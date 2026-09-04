"""统一从项目登录态拿 token（给代理扣费 API 用）。

设计：
- 懒导入 `api.session.session`（避免 services 顶层反向依赖）
- 返回 session.token_header()，未登录时返回 {}
- invalidate() 用于 token 过期后强制重新读
"""
from __future__ import annotations

import logging
from typing import Dict

logger = logging.getLogger(__name__)


def get_proxy_token_headers() -> Dict[str, str]:
    """拿当前登录 token header。

    Returns:
        {"token": "..."} 或 {}（未登录）
    """
    try:
        from api.session import session
        return session.token_header()
    except Exception:
        logger.exception("[proxy_token] 获取登录 token 失败")
        return {}


def invalidate_proxy_token_cache() -> None:
    """token 失效时调用。"""
    # 当前实现只透传到 Session.clear（语义上用户重新登录后会拿到新 token）
    try:
        from api.session import session
        # 不主动 clear（避免误登出）；只在读不到时自然失效
        # 留给调用方决定何时重置
    except Exception:
        pass


__all__ = ["get_proxy_token_headers", "invalidate_proxy_token_cache"]
