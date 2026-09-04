#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
页面标题区 Fluent 化组件

每个 page 顶部都需要的"大图标 + 标题 + 副标题"三件套。
原写法（4 个 page × 8 处硬编码）：

    self.title_label = QLabel("扫号任务")
    self.title_label.setStyleSheet(
        f"font-size: 20px; font-weight: 600; color: {text_primary()};"
    )

现写法（一行）：

    self.title_label, self.subtitle_label = make_page_title(
        icon=FIF.ROBOT,
        title="扫号任务",
        subtitle="管理自动扫号任务，查看运行状态",
    )

on_theme_changed 时调用 apply_title_color() 即可一次性更新。
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QSizePolicy

from qfluentwidgets import (
    FluentIcon as FIF, IconWidget, TitleLabel, CaptionLabel,
)

from gui.theme import text_primary, text_secondary


class PageTitle(QWidget):
    """Fluent 风格页面标题区

    布局：
    ┌────────────────────────────────────────────┐
    │ ┌──┐  扫号任务（22px 粗体）               │
    │ │📋│  管理自动扫号任务，查看运行状态（灰） │
    │ └──┘                                         │
    └────────────────────────────────────────────┘
    """

    def __init__(self, icon=FIF.SEARCH, title="", subtitle="", parent=None):
        super().__init__(parent)
        # 横向：左侧大图标 + 右侧双行文字
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)
        h.setAlignment(Qt.AlignVCenter | Qt.AlignLeading)

        # 左侧大图标（40×40）
        self.icon_label = IconWidget(icon, self)
        self.icon_label.setFixedSize(40, 40)
        h.addWidget(self.icon_label, 0, Qt.AlignVCenter)

        # 右侧双行文字
        v = QVBoxLayout()
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)

        # 标题行
        self.title_label = TitleLabel(title, self)
        # TitleLabel 默认 28px / DemiBold，看起来稍大，调到 22 跟之前一致
        f = QFont("Microsoft YaHei UI", 22, QFont.DemiBold)
        self.title_label.setFont(f)
        v.addWidget(self.title_label, 0, Qt.AlignVCenter)

        # 副标题行（可选）
        if subtitle:
            self.subtitle_label = CaptionLabel(subtitle, self)
            # CaptionLabel 自身会跟主题，font-size 由全局 QSS / qfluentwidgets 默认管
            v.addWidget(self.subtitle_label, 0, Qt.AlignVCenter)
        else:
            self.subtitle_label = None

        h.addLayout(v, 1)

        # 主题适配：调一次初始颜色
        self.apply_title_color()

    def apply_title_color(self):
        """主窗口切换主题时回调：让标题颜色跟随"""
        # TitleLabel / CaptionLabel 自己会跟随主题，无需手动 setStyleSheet
        pass

    def set_subtitle(self, text: str):
        """运行时修改副标题（动态信息）"""
        if self.subtitle_label is not None:
            self.subtitle_label.setText(text)


def make_page_title(icon=FIF.SEARCH, title="", subtitle="", parent=None):
    """工厂函数：创建 PageTitle 并返回 (title_label, subtitle_label)

    用法：
        self.title_label, self.subtitle_label = make_page_title(
            icon=FIF.ROBOT, title="扫号任务",
            subtitle="管理自动扫号任务，查看运行状态",
        )
        root.addWidget(self.title_label.parent())  # 实际是 QWidget，需要拿到父
        # 或：
        title_widget = make_page_title(...)
        title_widget.title_label / title_widget.subtitle_label
        layout.addWidget(title_widget)
    """
    return PageTitle(icon=icon, title=title, subtitle=subtitle, parent=parent)
