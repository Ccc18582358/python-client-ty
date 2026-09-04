#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
时间范围筛选的默认值工具

历史记录 / 捡漏商品 / 任何"按时间筛选"的页面都应该默认显示近 7 天的数据。
"""

from __future__ import annotations

from PySide6.QtCore import QDate

# 默认时间范围（天）。改这里就能改全局默认。
DEFAULT_RANGE_DAYS = 7


def default_date_range() -> tuple[QDate, QDate]:
    """默认时间范围 = (今天 - DEFAULT_RANGE_DAYS, 今天)

    Returns:
        (start_date, end_date) — 两个 QDate
    """
    today = QDate.currentDate()
    return today.addDays(-DEFAULT_RANGE_DAYS), today
