#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
估价进度卡（v3 - 单行极简版）

布局：
┌────────────────────────────────────────────────────────────────────────┐
│ ⚡ 估价进度   待估价 12  ·  进行中 2  ·  完成 5      ▰▰▱▱▱▱▱▱▱▱  35%    │
│ ⏳ 螃蟹账号#5  螃蟹·火影   ⏳ 盼之账号#3  盼之·火影   [展开]              │
└────────────────────────────────────────────────────────────────────────┘

目标高度：72px（标题行 24 + estimating 列表 28 + padding）

vs v2（紧凑版）~140px
vs v1（初版）~230px
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QSizePolicy,
)

from qfluentwidgets import (
    CardWidget, FluentIcon as FIF, IconWidget, CaptionLabel,
    StrongBodyLabel, BodyLabel, isDarkTheme, ProgressBar, IndeterminateProgressRing,
    TransparentToolButton,
)


PRIMARY = "#1677FF"
SUCCESS = "#00B42A"
WARN = "#FF7D00"
TEXT_MAIN = "#1D2129"
TEXT_SUB = "#4E5969"
TEXT_MUTED = "#86909C"
BORDER = "#E5E7EB"

# estimating 列表最大显示数
MAX_VISIBLE_ESTIMATING = 4


def _is_dark() -> bool:
    return isDarkTheme()


class EstimationProgressCard(CardWidget):
    """v3 - 单行紧凑卡

    设计要点：
    - 标题行：图标 + 标题 + 3 个数字（待估价/进行中/完成） + 进度条 + 百分比
    - estimating 列表：1 行，4 个产品逗号分隔
    - 整体高度 ~72px（vs 表格的 60% 占比）
    """

    def __init__(self, parent=None, db_manager=None):
        super().__init__(parent)
        self.db_manager = db_manager
        # estimating: db_id -> {"product_id", "platform", "game", "row_widget"}
        self._estimating: dict = {}
        # estimating 列表行（v3 用单条 row 而不是多行）
        self._estimating_row: QFrame = None

        self._build_ui()
        self._init_subscriptions()
        self._init_timer()

    # ----------------------------------------------------------- UI 构建
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 8, 14, 8)
        outer.setSpacing(6)

        # ---- 单行：图标 + 标题 + stats + 进度条 + 百分比 ----
        row1 = QHBoxLayout()
        row1.setSpacing(10)
        row1.setContentsMargins(0, 0, 0, 0)

        # 图标
        icon = IconWidget(FIF.ROBOT, self)
        icon.setFixedSize(18, 18)
        row1.addWidget(icon)

        # 标题
        self._title = StrongBodyLabel("估价进度")
        self._title.setStyleSheet(
            f"color: {TEXT_MAIN}; font-size: 13px; font-weight: 600; background: transparent;"
        )
        self._title.setAlignment(Qt.AlignVCenter)
        row1.addWidget(self._title)

        # 分隔
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.VLine)
        sep1.setFixedWidth(1)
        sep1.setStyleSheet(f"background: {BORDER}; border: none;")
        sep1.setFixedHeight(14)
        row1.addWidget(sep1)

        # Stats：待估价 / 进行中 / 完成 / 失败
        self._stat_pending = self._make_stat_chip("待估价", "0", PRIMARY)
        self._stat_inprogress = self._make_stat_chip("进行中", "0", WARN)
        self._stat_completed = self._make_stat_chip("完成", "0", SUCCESS)
        self._stat_failed = self._make_stat_chip("失败", "0", "#F5222D")
        row1.addWidget(self._stat_pending)
        row1.addSpacing(4)
        row1.addWidget(self._stat_inprogress)
        row1.addSpacing(4)
        row1.addWidget(self._stat_completed)
        row1.addSpacing(4)
        row1.addWidget(self._stat_failed)

        row1.addStretch(1)

        # 进度条
        self._progress_bar = ProgressBar(self)
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setFixedHeight(6)
        self._progress_bar.setMinimumWidth(120)
        self._progress_bar.setMaximumWidth(220)
        row1.addWidget(self._progress_bar)

        # 百分比
        self._progress_text = CaptionLabel("0%")
        self._progress_text.setStyleSheet(
            f"color: {TEXT_SUB}; font-size: 11px; background: transparent; "
            f"font-family: 'Consolas', 'Microsoft YaHei UI', monospace; min-width: 38px;"
        )
        self._progress_text.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
        self._progress_text.setMinimumWidth(38)
        row1.addWidget(self._progress_text)

        outer.addLayout(row1)

        # ---- 第 2 行：estimating 列表（单行，逗号分隔） ----
        self._estimating_container = QFrame(self)
        self._estimating_container.setObjectName("EstimatingList")
        self._estimating_container.setStyleSheet(
            f"QFrame#EstimatingList {{ background: rgba(22,119,255,0.04); "
            f"border-radius: 4px; border: none; }}"
        )
        el = QHBoxLayout(self._estimating_container)
        el.setContentsMargins(8, 4, 8, 4)
        el.setSpacing(6)

        self._est_icon = IndeterminateProgressRing(self._estimating_container)
        self._est_icon.setFixedSize(12, 12)
        self._est_icon.setStrokeWidth(3)
        el.addWidget(self._est_icon)

        self._est_label = CaptionLabel("正在估价：")
        self._est_label.setStyleSheet(
            f"color: {TEXT_SUB}; font-size: 11px; background: transparent;"
        )
        self._est_label.setAlignment(Qt.AlignVCenter)
        el.addWidget(self._est_label)

        self._est_items_label = CaptionLabel("⏸  当前没有正在估价的产品")
        self._est_items_label.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 11px; background: transparent;"
        )
        self._est_items_label.setAlignment(Qt.AlignVCenter)
        el.addWidget(self._est_items_label, 1)

        outer.addWidget(self._estimating_container)

    def _make_stat_chip(self, label: str, value: str, color: str) -> QFrame:
        """制作一个 inline 统计 chip：label + 大数字

        单行紧凑：XX 999
        """
        chip = QFrame()
        chip.setStyleSheet("background: transparent; border: none;")
        h = QHBoxLayout(chip)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        h.setAlignment(Qt.AlignVCenter)

        lbl = CaptionLabel(label + ":")
        lbl.setStyleSheet(f"color: {TEXT_SUB}; font-size: 11px; background: transparent;")
        lbl.setAlignment(Qt.AlignVCenter)
        h.addWidget(lbl)

        val = StrongBodyLabel(value)
        f = QFont()
        f.setPointSize(13)
        f.setBold(True)
        val.setFont(f)
        val.setStyleSheet(f"color: {color}; background: transparent;")
        val.setAlignment(Qt.AlignVCenter)
        h.addWidget(val)

        # 把 val 存到 chip 上方便后续更新
        chip.value_label = val
        return chip

    # ----------------------------------------------------------- 订阅信号
    def _init_subscriptions(self):
        try:
            from gui.main_window import estimation_bus
            estimation_bus.estimationStarted.connect(self._on_estimation_started)
            estimation_bus.estimationFinished.connect(self._on_estimation_finished)
        except Exception as e:
            print(f"[EstimationProgressCard] 订阅信号失败: {e}")

    # ----------------------------------------------------------- 定时轮询
    def _init_timer(self):
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._refresh_counts)
        self._timer.start()
        QTimer.singleShot(0, self._refresh_counts)

    def _refresh_counts(self):
        """刷新统计数字

        3 个 chip 的语义：
          - 待估价 (蓝)  = 首次待估价 (estimated=0)
          - 进行中 (橙)  = 重试队列 (estimated=1+retry<3) + 信号在 flight
          - 已完成 (绿)  = 累计已算到价 (final_price>0)

        进度条：已完成 / (待估价 + 进行中 + 已完成) = 累计完成率
        """
        if self.db_manager is None:
            return
        try:
            counts = self.db_manager.count_pending_products()
        except Exception as e:
            print(f"[EstimationProgressCard] 读 DB 失败: {e}")
            return

        # ★ 修复：之前 3 个 chip 全错
        # 旧：
        #   pending = pending + retrying  ← 把重试队列也当"待估价"显示
        #   in_progress = len(_estimating) ← 只统计信号在 flight（限流后基本是 0）
        #   completed = completed - base   ← 减 init 时基线，结果永远 0
        # 新：
        pending = counts["pending"]                          # 首次待估价
        in_progress = counts["retrying"] + len(self._estimating)  # 重试队列 + 在 flight
        completed = counts["completed"]                      # 累计已算到价
        failed = counts["error"]                             # 永久失败（estimated=2）

        # 更新 chip 数字
        self._stat_pending.value_label.setText(f"{pending:,}" if pending >= 1000 else str(pending))
        self._stat_inprogress.value_label.setText(f"{in_progress:,}" if in_progress >= 1000 else str(in_progress))
        self._stat_completed.value_label.setText(f"{completed:,}" if completed >= 1000 else str(completed))
        self._stat_failed.value_label.setText(f"{failed:,}" if failed >= 1000 else str(failed))

        # 进度条：累计完成率
        total = pending + in_progress + completed
        if total > 0:
            pct = int(completed * 100 / total)
            self._progress_bar.setValue(pct)
            self._progress_text.setText(f"{pct}%")
        else:
            self._progress_bar.setValue(0)
            self._progress_text.setText("0%")

        # estimating 列表
        self._refresh_estimating_label()

    def _refresh_estimating_label(self):
        n = len(self._estimating)
        if n == 0:
            self._est_items_label.setText("⏸  当前没有正在估价的产品")
            self._est_items_label.setStyleSheet(
                f"color: {TEXT_MUTED}; font-size: 11px; background: transparent;"
            )
            return

        # 按 db_id 排序（先来的先显示）
        sorted_dbs = sorted(self._estimating.keys(), key=lambda d: self._estimating[d]["start_ts"])
        items = []
        for db_id in sorted_dbs[:MAX_VISIBLE_ESTIMATING]:
            info = self._estimating[db_id]
            text = info["product_id"] or f"#{db_id}"
            if info["platform"] or info["game"]:
                text += f" ({info['platform']}"
                if info["game"] and info["game"] != info["platform"]:
                    text += f"·{info['game']}"
                text += ")"
            items.append(text)

        suffix = f"  ... 等 {n} 条" if n > MAX_VISIBLE_ESTIMATING else ""
        self._est_items_label.setText("  ·  ".join(items) + suffix)
        self._est_items_label.setStyleSheet(
            f"color: {TEXT_MAIN}; font-size: 11px; background: transparent;"
        )

    # ----------------------------------------------------------- 信号处理
    def _on_estimation_started(self, db_id: int, product_id: str, platform: str, game_type: str):
        import time
        db_id = int(db_id)
        if db_id in self._estimating:
            return
        self._estimating[db_id] = {
            "product_id": product_id,
            "platform": platform,
            "game": game_type,
            "start_ts": time.time(),
        }
        self._refresh_counts()

    def _on_estimation_finished(self, db_id: int, product_id: str, platform: str, game_type: str, success: bool):
        db_id = int(db_id)
        self._estimating.pop(db_id, None)
        self._refresh_counts()

    # ----------------------------------------------------------- 主题切换
    def on_theme_changed(self):
        for child in self.findChildren(QWidget):
            child.style().unpolish(child)
            child.style().polish(child)
        self.update()
