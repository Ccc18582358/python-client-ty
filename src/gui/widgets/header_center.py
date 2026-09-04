#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
table header 居中工具

用法：
    from gui.widgets.header_center import center_table_header
    center_table_header(self.table)
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QHeaderView


def center_table_header(table):
    """把表格的所有列头文字设为水平居中

    qfluentwidgets.TableWidget 的列头默认左对齐，单元格我们用 setTextAlignment 居中了，
    但列头文字会显得视觉错位。统一居中。
    """
    header = table.horizontalHeader()
    if header is None:
        return
    # 遍历所有列
    for col in range(table.columnCount()):
        item = header.model().headerData(col, Qt.Horizontal, Qt.TextAlignmentRole)
        # 拿到当前列头 item（qfluentwidgets 内部用 QTableWidgetItem）
        from PySide6.QtWidgets import QTableWidgetItem
        hi = table.horizontalHeaderItem(col)
        if hi is not None:
            hi.setTextAlignment(Qt.AlignCenter)
