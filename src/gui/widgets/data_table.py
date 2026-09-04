#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DataTable：包装 qfluentwidgets.TableWidget，统一管理：
- 加载/空状态占位
- 行 hover 高亮（qfluentwidgets 默认开启）
- 选区模式
- 通用右键菜单（复制 ID/打开 URL/查看详情）
"""

from typing import Callable, Optional

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QHeaderView, QAbstractItemView, QMenu

from qfluentwidgets import TableWidget

from gui.widgets.states import EmptyState, LoadingOverlay


class DataTable(TableWidget):
    """业务级 TableWidget：内置 loading/empty 占位 + 通用右键菜单

    用法：
        self.table = DataTable(card, columns=10, headers=[...], parent=card)
        self.table.set_on_double_click(self._on_double_click)
        self.table.set_on_open_url(self._open_url_for_row)
        # 加载完成
        self.table.show_loading()
        ... 填数据 ...
        self.table.show_content(len(rows))
    """

    def __init__(self, parent=None, columns: int = 0, headers: list = None):
        super().__init__(parent)
        if columns:
            self.setColumnCount(columns)
        if headers:
            self.setHorizontalHeaderLabels(headers)

        # 通用外观
        self.setBorderVisible(True)
        self.setBorderRadius(8)
        self.setWordWrap(False)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(40)

        # 状态占位（叠加在表格上）
        self._empty_state: Optional[EmptyState] = None
        self._loading = LoadingOverlay(self)

        # 通用右键菜单回调（外部设置）
        self._on_open_url: Optional[Callable[[int], None]] = None
        self._on_row_detail: Optional[Callable[[int], None]] = None
        self._on_copy_id: Optional[Callable[[int], None]] = None

        # 默认列宽模式
        header = self.horizontalHeader()
        for col in range(self.columnCount()):
            header.setSectionResizeMode(col, QHeaderView.Interactive)

        # 启用右键菜单
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    # ---------------- 状态占位 ----------------
    def show_loading(self, text: str = "加载中..."):
        """显示加载遮罩 + 隐藏空状态"""
        if self._empty_state is not None:
            self._empty_state.hide()
        self._loading.show_loading(text)

    def hide_loading(self):
        self._loading.hide_loading()

    def show_content(self, row_count: int, empty_icon=None, empty_title="暂无数据",
                     empty_desc="", empty_action_text="", on_action=None):
        """根据数据量切换：>0 显示表格；=0 显示空状态"""
        self.hide_loading()
        if row_count > 0:
            if self._empty_state is not None:
                self._empty_state.hide()
            return
        # 空状态
        if self._empty_state is None:
            self._empty_state = EmptyState(
                icon=empty_icon or self._guess_empty_icon(),
                title=empty_title,
                desc=empty_desc,
                action_text=empty_action_text,
                on_action=on_action,
                parent=self,
            )
        else:
            # 重新设置回调（如果调用方传了新回调）
            if on_action is not None:
                self._empty_state.set_action(on_action)
        self._empty_state.setGeometry(self.rect())
        self._empty_state.raise_()
        self._empty_state.show()

    def _guess_empty_icon(self):
        """根据表头猜一个合适的空状态图标"""
        from qfluentwidgets import FluentIcon as FIF
        text = " ".join([
            self.horizontalHeaderItem(c).text() if self.horizontalHeaderItem(c) else ""
            for c in range(self.columnCount())
        ]).lower()
        if "任务" in text:
            return FIF.ADD
        if "价格" in text or "商品" in text or "捡漏" in text:
            return FIF.HEART
        if "历史" in text:
            return FIF.HISTORY
        return FIF.SEARCH

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._empty_state is not None and self._empty_state.isVisible():
            self._empty_state.setGeometry(self.rect())
        if self._loading is not None and self._loading.isVisible():
            self._loading.setGeometry(self.rect())

    # ---------------- 右键菜单回调注册 ----------------
    def set_on_open_url(self, fn: Callable[[int], None]):
        """设置"打开 URL"回调：fn(row_index)"""
        self._on_open_url = fn

    def set_on_row_detail(self, fn: Callable[[int], None]):
        """设置"查看详情"回调：fn(row_index)"""
        self._on_row_detail = fn

    def set_on_copy_id(self, fn: Callable[[int], None]):
        """设置"复制 ID"回调：fn(row_index)，默认从第 2 列读取（约定）"""
        self._on_copy_id = fn

    def apply_theme(self):
        """主题切换时回调：把状态占位一起切色"""
        if self._empty_state is not None:
            self._empty_state.apply_theme()
        if self._loading is not None:
            self._loading.apply_theme()

    def _on_context_menu(self, pos: QPoint):
        """显示右键菜单"""
        index = self.indexAt(pos)
        if not index.isValid():
            return
        row = index.row()
        menu = QMenu(self)

        # 复制行
        copy_row_action = QAction("复制整行", self)
        copy_row_action.triggered.connect(lambda: self._copy_row(row))
        menu.addAction(copy_row_action)

        # 复制 ID
        copy_id_action = QAction("复制 ID", self)
        copy_id_action.triggered.connect(lambda: self._copy_id(row))
        menu.addAction(copy_id_action)

        menu.addSeparator()

        # 在浏览器打开
        if self._on_open_url is not None:
            open_action = QAction("在浏览器打开", self)
            open_action.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_DirHomeIcon))
            open_action.triggered.connect(lambda: self._on_open_url(row))
            menu.addAction(open_action)

        # 查看详情
        if self._on_row_detail is not None:
            detail_action = QAction("查看详情", self)
            detail_action.triggered.connect(lambda: self._on_row_detail(row))
            menu.addAction(detail_action)

        menu.exec(self.viewport().mapToGlobal(pos))

    def _copy_row(self, row: int):
        """复制整行内容到剪贴板（tab 分隔，方便贴到 Excel）"""
        cells = []
        for c in range(self.columnCount()):
            item = self.item(row, c)
            cells.append(item.text() if item else "")
        text = "\t".join(cells)
        QGuiApplication.clipboard().setText(text)

    def _copy_id(self, row: int):
        """复制 ID 列（约定第 2 列，索引 1）"""
        if self._on_copy_id is not None:
            self._on_copy_id(row)
            return
        # 默认从第 2 列复制
        id_item = self.item(row, 1) if self.columnCount() > 1 else self.item(row, 0)
        if id_item is not None:
            QGuiApplication.clipboard().setText(id_item.text())
