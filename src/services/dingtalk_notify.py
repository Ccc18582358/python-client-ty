#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""钉钉群机器人消息推送。

用法:
    from services.dingtalk_notify import notify_deal

    notify_deal(
        product_id="LY01N",
        game_type="火影忍者",
        platform="盼之",
        original_price=3500.0,
        final_price=4200.0,
        url="https://www.pzds.com/goodsDetails/LY01N/6",
        supports_realname=True,
    )
"""

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from typing import Optional

import requests

logger = logging.getLogger("dingtalk_notify")

# ── 钉钉机器人配置 ──
DINGTALK_WEBHOOK = (
    "https://oapi.dingtalk.com/robot/send"
    "?access_token=f386c491f00c57dad8b44a8fb5d56fa56b2db5a11056cb48c42e925717f35fee"
)
DINGTALK_SECRET = (
    "SEC0fba915db94390fbbca9290e12dd3f22c064789ac4f7ff79eaf03bf4eb368f29"
)


def _make_sign() -> tuple:
    """生成钉钉签名。返回 (timestamp, sign)。"""
    ts = str(round(time.time() * 1000))
    secret = DINGTALK_SECRET
    string_to_sign = f"{ts}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code).decode("utf-8"))
    return ts, sign


def notify_deal(
    *,
    product_id: str,
    game_type: str,
    platform: str,
    original_price: float,
    final_price: float,
    url: str = "",
    supports_realname: bool = True,
    target_price: float = 0.0,
    secondary_pct: float = 0.0,
    bargain_pct: float = 0.0,
) -> bool:
    """推送捡漏商品到钉钉群。

    Returns:
        True 推送成功，False 失败。
    """
    ts, sign = _make_sign()
    full_url = f"{DINGTALK_WEBHOOK}&timestamp={ts}&sign={sign}"

    premium = round(final_price - original_price, 2)
    realname_tag = "✅ 可二次" if supports_realname else "❌ 不可二次"

    # 加价比例文案
    if supports_realname and secondary_pct > 0:
        ratio_text = f"+{secondary_pct + bargain_pct:.0f}%（二次 {secondary_pct:.0f}% + 平台 {bargain_pct:.0f}%）"
    elif bargain_pct > 0:
        ratio_text = f"+{bargain_pct:.0f}%（仅平台 {bargain_pct:.0f}%）"
    elif bargain_pct < 0:
        ratio_text = f"{bargain_pct:.0f}%（仅平台 {bargain_pct:.0f}%）"
    else:
        ratio_text = "不加价"

    text = (
        f"## 🔥 捡漏提醒\n\n"
        f"- **游戏**：{game_type}\n"
        f"- **平台**：{platform}\n"
        f"- **商品编号**：{product_id}\n"
        f"- **卖家挂价**：¥{original_price:,.2f}\n"
        f"- **平台估价**：¥{target_price:,.2f}\n"
        f"- **加价比例**：{ratio_text}\n"
        f"- **系统估价**：¥{final_price:,.2f}\n"
        f"- **溢价**：¥{premium:,.2f}\n"
        f"- **实名**：{realname_tag}\n"
        f"- **链接**：[查看商品]({url})\n"
    )

    body = {
        "msgtype": "markdown",
        "markdown": {
            "title": f"🔥 捡漏 - {game_type} {platform} ¥{original_price:,.0f}",
            "text": text,
        },
    }

    try:
        resp = requests.post(
            full_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        result = resp.json()
        if result.get("errcode") == 0:
            logger.info(f"[dingtalk] 推送成功: {product_id}")
            return True
        else:
            logger.warning(f"[dingtalk] 推送失败: {result}")
            return False
    except Exception as e:
        logger.error(f"[dingtalk] 推送异常: {e}")
        return False


def notify_text(
    *,
    title: str,
    text: str,
    at_mobiles: Optional[list] = None,
    is_at_all: bool = False,
) -> bool:
    """推送纯文本消息（钉钉 group robot text 类型）。

    用于付费墙告警等非"捡漏"场景，复用 DINGTALK_WEBHOOK/SECRET。

    Args:
        title: 消息标题（作为钉钉消息首行展示）
        text: 完整消息正文（支持换行）
        at_mobiles: @指定手机号列表
        is_at_all: 是否 @所有人

    Returns:
        True 推送成功，False 失败。
    """
    ts, sign = _make_sign()
    full_url = f"{DINGTALK_WEBHOOK}&timestamp={ts}&sign={sign}"

    body = {
        "msgtype": "text",
        "text": {
            "content": f"{title}\n\n{text}",
        },
        "at": {
            "atMobiles": at_mobiles or [],
            "isAtAll": bool(is_at_all),
        },
    }

    try:
        resp = requests.post(
            full_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        result = resp.json()
        if result.get("errcode") == 0:
            logger.info(f"[dingtalk/text] 推送成功: {title[:30]}")
            return True
        logger.warning(f"[dingtalk/text] 推送失败: {result}")
        return False
    except Exception as e:
        logger.error(f"[dingtalk/text] 推送异常: {e}")
        return False


__all__ = [
    "notify_deal",
    "notify_text",
    "_make_sign",
    "DINGTALK_WEBHOOK",
    "DINGTALK_SECRET",
]  # notify_text + 常量给同包内其他 notifier（付费墙）复用
