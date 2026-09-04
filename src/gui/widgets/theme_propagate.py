#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主题切换时的递归应用工具

主窗口在切换深色/浅色时，只调了 page.on_theme_changed()。
但 page 里嵌套的子组件（DataTable / EmptyState / LoadingOverlay 等）
不会被自动通知。导致：切换主题后，部分硬编码颜色的子组件没换色。

这个 helper 遍历整个 widget tree，找到所有实现了 apply_theme() 的子组件
统一调用。
"""

from PySide6.QtWidgets import QWidget


def propagate_theme(root: QWidget, _seen: set = None) -> int:
    """递归遍历 root 的所有子组件，调 apply_theme()

    返回实际被调用的次数
    """
    if _seen is None:
        _seen = set()
    count = 0
    if id(root) in _seen:
        return 0
    _seen.add(id(root))

    if hasattr(root, "apply_theme") and callable(root.apply_theme):
        try:
            root.apply_theme()
            count += 1
        except Exception:
            pass

    for child in root.findChildren(QWidget):
        if id(child) in _seen:
            continue
        _seen.add(id(child))
        if hasattr(child, "apply_theme") and callable(child.apply_theme):
            try:
                child.apply_theme()
                count += 1
            except Exception:
                pass
    return count
