#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
空状态 / 加载态组件：
- EmptyState: 表格无数据时的占位（图标 + 提示语 + 可选按钮）
- LoadingOverlay: 表格上方的半透明加载遮罩（IndeterminateProgressRing）

主题适配：
- 所有硬编码颜色已替换为 gui.theme 的动态色
- 切换主题时调用 apply_theme() 即可刷新
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from PySide6.QtCore import Qt, QPropertyAnimation
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy,
    QGraphicsOpacityEffect,
)
from qfluentwidgets import (
    FluentIcon as FIF, PrimaryPushButton, PushButton,
    IndeterminateProgressRing, IconWidget, BodyLabel, CaptionLabel,
)
from qfluentwidgets import qconfig, Theme

from gui.theme import text_primary, text_secondary, text_regular


class EmptyState(QWidget):
    """表格无数据时的占位视图

    用法：
        empty = EmptyState(
            icon=FIF.SEARCH,
            title="暂无数据",
            desc="请调整筛选条件或新建任务",
            action_text="重置筛选",
            on_action=self._reset_filter,  # 回调直接传
        )
        self.table_empty_holder.addWidget(empty)
    """

    def __init__(self, icon=FIF.SEARCH, title="暂无数据", desc="",
                 action_text="", on_action=None, parent=None):
        super().__init__(parent)
        # 把回调直接存在实例上，data_table 用 .on_action 读取
        self._on_action = on_action
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 32, 0, 32)
        v.setSpacing(8)
        v.setAlignment(Qt.AlignCenter)

        # 图标（大号、半透明）
        self.icon_label = IconWidget(icon, self)
        self.icon_label.setFixedSize(56, 56)
        eff = QGraphicsOpacityEffect(self.icon_label)
        eff.setOpacity(0.4)
        self.icon_label.setGraphicsEffect(eff)
        v.addWidget(self.icon_label, 0, Qt.AlignHCenter)

        # 标题
        self.title_label = BodyLabel(title, self)
        f = QFont("Microsoft YaHei UI", 13, QFont.DemiBold)
        self.title_label.setFont(f)
        v.addWidget(self.title_label, 0, Qt.AlignHCenter)

        # 描述
        self.desc_label = None
        if desc:
            self.desc_label = CaptionLabel(desc, self)
            v.addWidget(self.desc_label, 0, Qt.AlignHCenter)

        # 操作按钮
        self.action_btn = None
        if action_text:
            v.addSpacing(8)
            self.action_btn = PushButton(action_text, self)
            self.action_btn.setIcon(FIF.SYNC)
            self.action_btn.clicked.connect(self._do_action)
            v.addWidget(self.action_btn, 0, Qt.AlignHCenter)

        # 应用一次主题
        self.apply_theme()

    def _do_action(self):
        if self._on_action is not None:
            self._on_action()

    def set_action(self, on_action):
        """重设回调（data_table 在 show_content 时按需更新）"""
        self._on_action = on_action

    def apply_theme(self):
        """主题切换时回调：让 desc_label 颜色跟随"""
        # CaptionLabel 自身会跟主题，无需手动 setStyleSheet
        pass


class LoadingOverlay(QWidget):
    """表格加载中的半透明遮罩

    用法：
        self.loading = LoadingOverlay(self.table)
        self.loading.show_loading()
        # ... 加载完成
        self.loading.hide_loading()
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        if parent is not None:
            self.setGeometry(parent.rect())

        # 全透明背景层
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)

        # 居中进度环
        h = QHBoxLayout(self)
        h.setAlignment(Qt.AlignCenter)
        self.ring = IndeterminateProgressRing(self)
        self.ring.setFixedSize(40, 40)
        h.addWidget(self.ring)

        # 文字
        v = QVBoxLayout()
        v.setAlignment(Qt.AlignCenter)
        v.setSpacing(6)
        v.addWidget(self.ring, 0, Qt.AlignCenter)

        self.text_label = BodyLabel("加载中...", self)
        v.addWidget(self.text_label, 0, Qt.AlignCenter)

        h.addLayout(v)

        # 默认隐藏
        self.hide()

        # 应用一次主题
        self.apply_theme()

    def show_loading(self, text: str = "加载中..."):
        """显示加载遮罩"""
        if self.parent() is not None:
            self.setGeometry(self.parent().rect())
        self.text_label.setText(text)
        self.raise_()
        self.show()

    def hide_loading(self):
        """隐藏加载遮罩"""
        self.hide()

    def apply_theme(self):
        """主题切换时回调：背景半透明白 vs 半透明黑，文字颜色跟随"""
        is_dark = qconfig.theme == Theme.DARK
        if is_dark:
            # 深色模式：半透明深灰背景
            self.setStyleSheet("background: rgba(0, 0, 0, 0.45);")
        else:
            # 浅色模式：半透明白背景
            self.setStyleSheet("background: rgba(255, 255, 255, 0.65);")
        # text_label 是 BodyLabel 自身会跟主题，无需手动 setStyleSheet
        # 但 background 必须保持透明，否则被遮罩背景盖掉
        self.text_label.setStyleSheet("background: transparent;")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 跟随父控件大小
        if self.parent() is not None:
            self.setGeometry(self.parent().rect())
