"""全局代理付费墙（204/205 套餐致命错误）。

行为：
- 任何 platform ProxyManager 拿到 204/205 调 block() → 触发告警 + 设置全局标志
- 告警渠道分发：204 → GUI 弹窗，205 → 钉钉群告警
- 5 分钟内同 platform + code 不重复告警（throttle）
- 全局共享：4 个 ProxyManager 看到同一份状态
- 提供 clear() 与 status() 用于调试 / GUI 状态展示
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class PaywallState:
    """付费墙的运行时状态。"""

    blocked: bool = False
    code: int = 0
    msg: str = ""
    triggered_by_platform: str = ""
    triggered_at: float = 0.0
    notified_via: str = ""


class ProxyPaywall:
    """全局付费墙状态管理（线程安全）。"""

    _state: PaywallState = PaywallState()    # 类级实例属性 (别用 dataclasses.field, 它只对 @dataclass 生效)
    _lock: threading.RLock = threading.RLock()
    _on_gui: Optional[Callable[[str, str], None]] = None
    _on_dingtalk: Optional[Callable[[str, str], None]] = None
    _last_alert_at: Dict[str, float] = {}
    _throttle_seconds: float = 300.0  # 5 分钟内同 code+platform 不重发

    # ────────── 安装告警回调 ──────────

    @classmethod
    def install(
        cls,
        *,
        on_gui: Optional[Callable[[str, str], None]] = None,
        on_dingtalk: Optional[Callable[[str, str], None]] = None,
        throttle_seconds: float = 300.0,
    ) -> None:
        """注入告警回调。MainWindow 启动时调用一次。

        Args:
            on_gui: (title, body) → 在主线程弹 MessageBox
            on_dingtalk: (title, body) → 推送钉钉群
            throttle_seconds: 同 platform+code 告警间隔下界
        """
        with cls._lock:
            cls._on_gui = on_gui
            cls._on_dingtalk = on_dingtalk
            cls._throttle_seconds = max(0.0, float(throttle_seconds))

    # ────────── 主入口 ──────────

    @classmethod
    def block(cls, *, code: int, msg: str, platform: str) -> None:
        """触发付费墙。

        任何 ProxyManager 在神龙返回 204/205（业务付费致命）时调用。
        重复触发幂等（不会重复告警）。
        """
        with cls._lock:
            if cls._state.blocked:
                # 已有付费墙生效，不再触发新告警，但可继续记录
                logger.info(
                    "[paywall] 已有付费墙生效 (code=%s platform=%s), 新触发 %s:%s 忽略",
                    cls._state.code,
                    cls._state.triggered_by_platform,
                    platform,
                    code,
                )
                return

            # throttle：5 分钟内同 platform:code 不重复
            key = f"{platform}:{code}"
            now = time.time()
            last = cls._last_alert_at.get(key, 0.0)
            if (now - last) < cls._throttle_seconds:
                logger.info(
                    "[paywall] throttle 未过期, key=%s, 距上次 %.0fs",
                    key,
                    now - last,
                )
                # 但仍然要设置 blocked 状态（只第一次给告警）
                cls._state = PaywallState(
                    blocked=True,
                    code=code,
                    msg=msg,
                    triggered_by_platform=platform,
                    triggered_at=now,
                    notified_via="throttled",
                )
                return

            cls._state = PaywallState(
                blocked=True,
                code=code,
                msg=msg,
                triggered_by_platform=platform,
                triggered_at=now,
            )
            cls._last_alert_at[key] = now

            title = f"代理付费墙告警 ({platform})"
            body = (
                f"错误码: {code}\n"
                f"{msg}\n\n"
                f"当前所有平台已切换为直连模式，"
                f"扫号任务将在首次遇到风控/验证码时自动停止。"
            )

            notified_via = ""

            # 渠道分发
            if code == 204:
                # 套餐已过期 → GUI 弹窗（必须通知用户）
                if cls._on_gui:
                    try:
                        cls._on_gui(title, body)
                        notified_via = "gui"
                    except Exception:
                        logger.exception("[paywall] GUI 弹窗失败")
                else:
                    notified_via = "log_only"
                    logger.error("[paywall/204 GUI 未挂载] %s | %s", title, body)
            elif code == 205:
                # 提取数量上限 → 钉钉告警（不打扰用户）
                if cls._on_dingtalk:
                    try:
                        cls._on_dingtalk(title, body)
                        notified_via = "dingtalk"
                    except Exception:
                        logger.exception("[paywall] 钉钉告警失败")
                else:
                    notified_via = "log_only"
                    logger.error("[paywall/205 钉钉未挂载] %s | %s", title, body)
            else:
                # 兜底：两个渠道都试
                if cls._on_dingtalk:
                    try:
                        cls._on_dingtalk(title, body)
                        notified_via = "dingtalk"
                    except Exception:
                        logger.exception("[paywall] 钉钉兜底告警失败")
                if cls._on_gui:
                    try:
                        cls._on_gui(title, body)
                        notified_via = (notified_via + "+gui") if notified_via else "gui"
                    except Exception:
                        logger.exception("[paywall] GUI 兜底告警失败")

            cls._state.notified_via = notified_via or "log_only"
            logger.error(
                "[paywall] code=%s platform=%s msg=%s notified_via=%s",
                code,
                platform,
                msg,
                cls._state.notified_via,
            )

    # ────────── 查询 ──────────

    @classmethod
    def is_blocked(cls) -> bool:
        with cls._lock:
            return cls._state.blocked

    @classmethod
    def state(cls) -> PaywallState:
        """当前付费墙状态（线程安全的快照）。"""
        with cls._lock:
            return PaywallState(**cls._state.__dict__)

    @classmethod
    def status(cls) -> dict:
        with cls._lock:
            return {
                "blocked": cls._state.blocked,
                "code": cls._state.code,
                "msg": cls._state.msg,
                "triggered_by_platform": cls._state.triggered_by_platform,
                "triggered_at": cls._state.triggered_at,
                "notified_via": cls._state.notified_via,
            }

    # ────────── 维护 ──────────

    @classmethod
    def clear(cls) -> None:
        """手动清除付费墙（用户重新充值套餐后调用，或重启 app）。"""
        with cls._lock:
            cls._state = PaywallState()
            logger.info("[paywall] 已手动清除")


__all__ = ["ProxyPaywall", "PaywallState"]
