"""付费墙告警路由：把 ProxyPaywall 的回调翻译成钉钉/具体格式。

复用现有 `dingtalk_notify` 模块的签名常量 + notify_text 接口。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def send_dingtalk_paywall(title: str, body: str) -> None:
    """付费墙告警 → 钉钉群（text 类型，不 at）。

    由 ProxyPaywall.install(on_dingtalk=...) 注入。
    失败吞异常（不影响 ProxyManager 主流程）。
    """
    try:
        from services.dingtalk_notify import notify_text
    except Exception as exc:
        logger.warning("[paywall/dingtalk] 导入 dingtalk_notify 失败: %s", exc)
        return
    try:
        ok = notify_text(title=title, text=body)
        if ok:
            logger.info("[paywall/dingtalk] 告警已发送: %s", title)
        else:
            logger.warning("[paywall/dingtalk] 告警失败: %s", title)
    except Exception:
        logger.exception("[paywall/dingtalk] 告警异常")


def make_gui_paywall_notifier(emitter) -> callable:
    """构造 GUI 弹窗回调。

    Args:
        emitter: 一个 callable(title, body)，跨线程 emit Qt signal

    Returns:
        ProxyPaywall.install(on_gui=...) 用的函数
    """
    def _notify(title: str, body: str) -> None:
        try:
            emitter(title, body)
        except Exception:
            logger.exception("[paywall/gui] 弹窗 emit 失败")
    return _notify


__all__ = ["send_dingtalk_paywall", "make_gui_paywall_notifier"]
