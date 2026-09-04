"""滑动窗口预算：限制 captcha / 风控 触发主动换 IP 的速率。

设计要点：
- budget 仅控制"主动 force_refresh"路径；TTL 自然到期不走 budget，永远允许
- 滑动窗口：每次 try_acquire 把窗口外的时间戳踢掉，再数
- pop_last：扣费失败时回滚本次计入
- 模块级：每个 platform 独立实例，由 caller 持有引用
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ProxySwitchBudget:
    """滑动窗口预算器。

    Args:
        max_count: 窗口内允许的最大主动切 IP 次数
        window_seconds: 窗口长度（秒）
    """

    max_count: int = 3
    window_seconds: float = 120.0
    _ticks: List[float] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def try_acquire(self, now: Optional[float] = None) -> bool:
        """申请一次主动切 IP 配额。

        Args:
            now: 时间戳（可选，默认 time.time()）；仅供测试注入

        Returns:
            True  配额允许（已计入）
            False 配额已满（不计入）
        """
        now = now if now is not None else time.time()
        with self._lock:
            cutoff = now - self.window_seconds
            self._ticks = [t for t in self._ticks if t > cutoff]
            if len(self._ticks) >= self.max_count:
                return False
            self._ticks.append(now)
            return True

    def pop_last(self) -> None:
        """回滚最近一次成功计入（扣费失败时使用，确保不占预算）。"""
        with self._lock:
            if self._ticks:
                self._ticks.pop()

    def remaining_block_sec(self, now: Optional[float] = None) -> float:
        """剩余多少秒配额才解禁。0 表示已经允许。"""
        now = now if now is not None else time.time()
        with self._lock:
            cutoff = now - self.window_seconds
            self._ticks = [t for t in self._ticks if t > cutoff]
            if len(self._ticks) < self.max_count:
                return 0.0
            return self._ticks[0] + self.window_seconds - now

    def status(self) -> dict:
        """当前预算使用情况（给 UI / 日志）。"""
        now = time.time()
        with self._lock:
            cutoff = now - self.window_seconds
            self._ticks = [t for t in self._ticks if t > cutoff]
            return {
                "used_in_window": len(self._ticks),
                "max_per_window": self.max_count,
                "window_seconds": self.window_seconds,
            }

    def reset(self) -> None:
        """清空预算（重新登录或调试用）。"""
        with self._lock:
            self._ticks = []


__all__ = ["ProxySwitchBudget"]
