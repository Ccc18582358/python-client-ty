#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
强制刷新 qfluentwidgets FluentLabelBase 子类的颜色
同时 monkey-patch QTableWidgetItem.__init__ 强制前景色

问题根因：
  1. qfluentwidgets 的 BodyLabel/CaptionLabel/StrongBodyLabel/TitleLabel 注入了
     inline `FluentLabelBase { color: white }`，优先级 > 全局 * QSS
  2. QTableWidgetItem 默认前景色跟随 QPalette，深色模式下实际渲染是黑色

修复方案：
  1. EventFilter 拦截所有 FluentLabelBase 的 Show 事件，强制调 setTextColor
  2. monkey-patch QTableWidgetItem.__init__：构造时强制 setForeground(text_primary)
  3. 主题切换时遍历所有 FluentLabelBase 和所有 QTableWidgetItem 刷色
"""

from PySide6.QtCore import QObject, QEvent, QTimer
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import QApplication, QTableWidgetItem
from qfluentwidgets import qconfig
from qfluentwidgets.components.widgets.label import FluentLabelBase

from gui.theme import text_primary, text_regular


def _force_set_color(widget):
    """强制把 FluentLabelBase 的 light/dark 都设成全局色"""
    color = text_primary()
    try:
        # setTextColor 会触发 setCustomStyleSheet 重写 inline qss
        widget.setTextColor(color, color)
    except Exception:
        pass


def _patch_all_existing():
    """遍历当前所有已存在的 FluentLabelBase，刷色"""
    app = QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        if isinstance(w, FluentLabelBase):
            _force_set_color(w)


# === monkey-patch QTableWidgetItem：构造时强制前景色 ===
_original_table_item_init = QTableWidgetItem.__init__


def _patched_table_item_init(self, *args, **kwargs):
    """QTableWidgetItem 构造完后立即强制前景色为当前主题色"""
    _original_table_item_init(self, *args, **kwargs)
    # 强制设前景色（避免深色模式下默认黑字）
    try:
        c = QColor(text_primary())
        self.setForeground(QBrush(c))
    except Exception:
        pass


QTableWidgetItem.__init__ = _patched_table_item_init


# === 现有 widget 表格 item 刷色（处理已创建的 item）===
def _patch_all_table_items():
    """遍历所有 QTableWidget / QTableView，把每行 cell 的前景色都刷成主题色

    关键：从 QTableWidget.item(r, c) 拿 item，再 setForeground
    QTableWidgetItem 不在 app.allWidgets() 里，必须从 parent table 拿
    """
    from PySide6.QtWidgets import QTableWidget, QTableView
    app = QApplication.instance()
    if app is None:
        return
    c = QColor(text_primary())
    brush = QBrush(c)
    for widget in app.allWidgets():
        # QTableWidget: 直接拿 item
        if isinstance(widget, QTableWidget):
            try:
                for r in range(widget.rowCount()):
                    for col in range(widget.columnCount()):
                        item = widget.item(r, col)
                        if item is not None:
                            item.setForeground(brush)
            except Exception:
                pass
        # QTableView: 通过 model 拿 item
        elif isinstance(widget, QTableView):
            try:
                m = widget.model()
                if m is None:
                    continue
                rows = m.rowCount()
                cols = m.columnCount()
                for r in range(rows):
                    for col in range(cols):
                        idx = m.index(r, col)
                        if hasattr(m, 'itemFromIndex'):
                            item = m.itemFromIndex(idx)
                            if item and hasattr(item, 'setForeground'):
                                item.setForeground(brush)
            except Exception:
                pass


class _LabelPatcher(QObject):
    """全局 event filter：拦截 childPolished / show 事件，自动给 FluentLabelBase 刷色"""

    def eventFilter(self, obj, event):
        if isinstance(obj, FluentLabelBase):
            if event.type() in (QEvent.Show, QEvent.Polish):
                # ★ 修: 改用 QTimer.singleShot 推到下个 tick,
                #   避免在 eventFilter 里同步 processEvents() 触发 KeyboardInterrupt 死循环
                #   (Ctrl+C 退出 GUI 时 processEvents 会重新注入 KeyboardInterrupt,
                #    Qt 错误框无法关闭)
                QTimer.singleShot(0, lambda o=obj: _force_set_color(o))
        return False


_patcher = None


def install():
    """安装 patch"""
    global _patcher
    app = QApplication.instance()
    if app is None:
        return
    # 1. 装 event filter：捕获所有 FluentLabelBase 创建后的事件
    if _patcher is None:
        _patcher = _LabelPatcher()
        app.installEventFilter(_patcher)
    # 2. 立即跑一次（启动时已存在的 label）
    _patch_all_existing()
    # 3. 主题切换时刷新所有（label + 表格 item）
    def _on_theme_change():
        _patch_all_existing()
        _patch_all_table_items()
    qconfig.themeChanged.connect(_on_theme_change)
