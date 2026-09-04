#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主题管理：包装 qfluentwidgets.setTheme，支持 light / dark / auto 三种模式。

主题持久化：用 QSettings 写入 Windows 注册表 / ~/.config，
重启时自动恢复上次用户选择。

提供随主题切换动态变化的色板，业务页面应优先用这些色板而不是硬编码颜色。
"""

from qfluentwidgets import setTheme, Theme, qconfig
from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor


_QSETTINGS = QSettings("PriceMonitor", "PriceMonitorClient")


def apply_theme(mode: str = "auto") -> None:
    """应用主题模式：'light' | 'dark' | 'auto'

    默认从 QSettings 恢复上次用户选择
    """
    # 默认值第一次启动时为 'auto'，可被传入参数覆盖
    if mode == "default-from-settings":
        mode = _QSETTINGS.value("ui/theme_mode", "light", type=str)
    if mode == "dark":
        setTheme(Theme.DARK)
        _QSETTINGS.setValue("ui/theme_mode", "dark")
    elif mode == "auto":
        setTheme(Theme.AUTO)
        _QSETTINGS.setValue("ui/theme_mode", "auto")
    else:
        setTheme(Theme.LIGHT)
        _QSETTINGS.setValue("ui/theme_mode", "light")
    _QSETTINGS.sync()


def current_theme() -> str:
    """返回当前主题：'Light' / 'Dark'"""
    return "Dark" if qconfig.theme == Theme.DARK else "Light"


def toggle_theme() -> str:
    """在 light / dark 之间切换，返回切换后的主题名（同时持久化）"""
    if qconfig.theme == Theme.DARK:
        new_mode = "light"
        setTheme(Theme.LIGHT)
    else:
        new_mode = "dark"
        setTheme(Theme.DARK)
    _QSETTINGS.setValue("ui/theme_mode", new_mode)
    _QSETTINGS.sync()
    return "Dark" if new_mode == "dark" else "Light"


def load_saved_theme() -> str:
    """启动时调：返回上次保存的主题模式（'light' / 'dark' / 'auto'），默认 'light'"""
    return _QSETTINGS.value("ui/theme_mode", "light", type=str)


def set_primary_color(hex_color: str = "#1677FF") -> None:
    """设置 Fluent 主题主色（默认 Windows 11 蓝）"""
    qconfig.set(qconfig.themeColor, QColor(hex_color))


def set_global_font(family: str = "Microsoft YaHei UI", size: int = 10) -> None:
    """设置全局默认字体（在 QApplication 创建后调用）"""
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is not None:
        app.setFont(QFont(family, size))


def notify(parent, title: str, content: str = "", level: str = "info", duration: int = 2000):
    """全局 InfoBar 通知

    level: info / success / warning / error
    """
    from qfluentwidgets import InfoBar, InfoBarPosition
    level = level.lower()
    if level == "error":
        return InfoBar.error(
            title=title, content=content,
            parent=parent, position=InfoBarPosition.TOP_RIGHT,
            duration=duration,
        )
    if level == "warning":
        return InfoBar.warning(
            title=title, content=content,
            parent=parent, position=InfoBarPosition.TOP_RIGHT,
            duration=duration,
        )
    if level == "success":
        return InfoBar.success(
            title=title, content=content,
            parent=parent, position=InfoBarPosition.TOP_RIGHT,
            duration=duration,
        )
    return InfoBar.info(
        title=title, content=content,
        parent=parent, position=InfoBarPosition.TOP_RIGHT,
        duration=duration,
    )


# ============================================================
# 动态色板：随主题切换而变化
# ============================================================

# 状态色（success / warning / danger / info）两个主题都可用
COLOR_SUCCESS = "#67C23A"
COLOR_WARNING = "#E6A23C"
COLOR_DANGER = "#F56C6C"
COLOR_INFO = "#409EFF"


def text_primary() -> str:
    """主要文字色：深色模式纯白 / 浅色模式深色"""
    return "#FFFFFF" if qconfig.theme == Theme.DARK else "#303133"


def text_secondary() -> str:
    """次要文字色（描述、占位符）"""
    return "#A3A6AD" if qconfig.theme == Theme.DARK else "#909399"


def text_regular() -> str:
    """常规文字色（默认 body 文字）"""
    return "#C9CDD4" if qconfig.theme == Theme.DARK else "#606266"
