#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ElPagination：qfluentwidgets 没内建翻页组件，自己写一个。
接口：
    __init__(parent, on_prev, on_next, on_go)
    update_info(current_page, total_pages, total_count)

主题适配：
- 用 qfluentwidgets 的组件（PushButton/LineEdit），自动跟随主题
- 暴露 apply_theme() 用于切换主题时强制刷新
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel,
)
from qfluentwidgets import (
    PushButton, LineEdit, PrimaryPushButton,
    MessageBox, FluentIcon as FIF,
)

from gui.theme import text_primary, text_secondary, text_regular


class ElPagination(QWidget):
    """简单翻页：上一页 / 下一页 / 跳转到 N / 共 X 条 / Y 页

    用 qfluentwidgets 组件（自动跟主题），并加 apply_theme() 兜底
    文字颜色（深色模式下部分文字仍需要显式适配）。
    """

    def __init__(self, parent=None, on_prev=None, on_next=None, on_go=None):
        super().__init__(parent)
        self._on_prev = on_prev
        self._on_next = on_next
        self._on_go = on_go
        self.current_page = 1
        self.total_pages = 1
        self.total_count = 0

        self._build_ui()

    def _build_ui(self):
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 8, 0, 0)
        h.setSpacing(8)

        # 上一页 / 下一页：qfluentwidgets.PushButton 自动跟主题
        self.prev_btn = PushButton("上一页")
        self.prev_btn.setFixedHeight(32)
        self.prev_btn.setCursor(Qt.PointingHandCursor)
        self.prev_btn.clicked.connect(self._handle_prev)
        h.addWidget(self.prev_btn)

        self.next_btn = PushButton("下一页")
        self.next_btn.setFixedHeight(32)
        self.next_btn.setCursor(Qt.PointingHandCursor)
        self.next_btn.clicked.connect(self._handle_next)
        h.addWidget(self.next_btn)

        h.addSpacing(16)

        # "跳转到" 文字标签：用 BodyLabel（跟主题）
        self.jump_label = QLabel("跳转到")
        h.addWidget(self.jump_label)

        self.page_input = LineEdit()
        self.page_input.setFixedWidth(60)
        self.page_input.setFixedHeight(32)
        self.page_input.setAlignment(Qt.AlignCenter)
        self.page_input.returnPressed.connect(self._handle_go)
        h.addWidget(self.page_input)

        self.go_btn = PrimaryPushButton("跳转")
        self.go_btn.setFixedHeight(32)
        self.go_btn.setCursor(Qt.PointingHandCursor)
        self.go_btn.clicked.connect(self._handle_go)
        h.addWidget(self.go_btn)

        h.addSpacing(16)

        self.info_label = QLabel("共 0 条 / 1 页")
        h.addWidget(self.info_label)

        h.addStretch(1)

        # 一次主题应用
        self.apply_theme()

    def apply_theme(self):
        """主题切换时回调：标签文字颜色跟随"""
        # 让全局 QSS 接管：只设 font-size（color 由 QSS * 选择器兜底）
        for lbl in (self.jump_label, self.info_label):
            lbl.setStyleSheet("background: transparent; font-size: 12px;")

    def _handle_prev(self):
        if self.current_page > 1 and self._on_prev:
            self._on_prev()

    def _handle_next(self):
        if self.current_page < self.total_pages and self._on_next:
            self._on_next()

    def _handle_go(self):
        text = self.page_input.text().strip()
        if not text.isdigit():
            MessageBox("提示", "请输入有效的页码（数字）", self).exec()
            self.page_input.setText(str(self.current_page))
            return
        page = int(text)
        if page < 1 or page > self.total_pages:
            MessageBox("提示", f"页码超出范围，请输入 1-{self.total_pages}", self).exec()
            self.page_input.setText(str(self.current_page))
            return
        if self._on_go:
            self._on_go(page)

    @staticmethod
    def calc_total_pages(total_count: int, page_size: int) -> int:
        """根据总条数和页大小计算总页数（最小为 1）"""
        if total_count <= 0:
            return 1
        return (total_count + page_size - 1) // page_size

    def update_info(self, current_page: int, total_pages: int, total_count: int):
        self.current_page = current_page
        self.total_pages = max(1, total_pages)
        self.total_count = total_count
        self.info_label.setText(f"共 {total_count} 条 / {self.total_pages} 页")
        self.page_input.setText(str(current_page))
        self.prev_btn.setEnabled(current_page > 1)
        self.next_btn.setEnabled(current_page < self.total_pages)
