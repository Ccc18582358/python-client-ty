#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
捡漏商品页面（PySide6 + qfluentwidgets 重写版）
"""

import sys
from pathlib import Path
import webbrowser
import csv

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from PySide6.QtCore import Qt, QDate
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFileDialog,
    QMessageBox, QTableWidgetItem, QAbstractItemView, QHeaderView, QSizePolicy,
    QDateEdit,
)

from qfluentwidgets import (
    CardWidget, LineEdit, ComboBox,
    PrimaryPushButton, PushButton, FluentIcon as FIF, MessageBox,
)

from api.session import session
from database.db_manager_v2 import DBManagerV2
from gui.widgets.pagination import ElPagination
from gui.widgets.data_table import DataTable
from gui.widgets.filter_state import FilterStateMixin
from gui.widgets.header_center import center_table_header
from gui.widgets.page_title import PageTitle
from gui.widgets.date_range import default_date_range
from gui.theme import text_primary, notify
from src.config.config_manager import config_manager


class DealPage(QWidget, FilterStateMixin):
    """捡漏商品页面"""

    FILTER_STATE_KEY = "deal"

    def __init__(self, parent=None, db_manager=None):
        super().__init__(parent)
        # FluentWindow.addSubInterface 需要 objectName
        self.setObjectName("DealPage")

        self.db_manager = db_manager
        # 选中项存 product id
        self.selected_items: set = set()
        self.current_page = 1
        self.page_size = 20
        self.total_count = 0
        self.current_game = "全部"
        self.current_platform = "全部"
        self.current_min_price = None
        self.current_max_price = None
        self.current_min_save = None
        self.current_max_save = None
        self.current_start_date = None
        self.current_end_date = None
        # 当前页 product dict 列表（供双击按 row index 拿 url 用）
        self.current_rows = []

        self._build_ui()
        self.load_filter_state()  # 恢复上次的筛选条件
        self._load_data()

        # 监听业务配置变更（仅游戏增删，平台列已固定）
        try:
            config_manager.configChanged.connect(self._on_config_changed)
        except Exception:
            pass

        # 监听估价完成 signal（实时刷新）
        try:
            from gui.main_window import estimation_bus
            estimation_bus.product_estimated_signal.connect(self._on_product_estimated)
        except Exception:
            pass

    def on_theme_changed(self):
        """主窗口切换主题时回调"""
        if hasattr(self, "_title_widget"):
            self._title_widget.apply_title_color()

    def _on_config_changed(self, change_type: str):
        """游戏/平台配置变更时刷新（游戏下拉 + 数据）"""
        if change_type not in ('games', 'platforms', 'all'):
            return
        # 刷新筛选区游戏下拉
        if hasattr(self, 'game_combo'):
            cur = self.current_game
            self.game_combo.clear()
            self.game_combo.addItems(["全部"] + config_manager.get_enabled_game_names())
            idx = self.game_combo.findText(cur)
            if idx >= 0:
                self.game_combo.setCurrentIndex(idx)
        self._load_data()

    # ---------------- UI 构造 ----------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # 标题区（大图标 + 标题 + 副标题）
        self._title_widget = PageTitle(
            icon=FIF.HEART,
            title="捡漏商品",
            subtitle="查看所有跨平台有溢价的低价商品，导出或批量删除",
        )
        self.title_label = self._title_widget.title_label
        self.subtitle_label = self._title_widget.subtitle_label
        layout.addWidget(self._title_widget)

        # 筛选卡
        layout.addWidget(self._build_filter_card())

        # 工具栏
        layout.addLayout(self._build_toolbar())

        # 表格卡（占据剩余空间）
        layout.addWidget(self._build_table_card(), 1)

    def _build_filter_card(self) -> CardWidget:
        card = CardWidget(self)
        grid = QGridLayout(card)
        grid.setContentsMargins(16, 16, 16, 16)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)

        # 设置列拉伸策略：标签列不拉伸，控件列等宽拉伸
        # 列 0/2/5 是 QLabel（不拉伸），列 1/3/4/6/7 是控件（全部拉伸）
        for col in (1, 3, 4, 6, 7):
            grid.setColumnStretch(col, 1)

        # row 0: 平台名称
        grid.addWidget(QLabel("平台名称："), 0, 0)
        self.platform_combo = ComboBox(card)
        self.platform_combo.addItems(["全部"] + config_manager.get_platform_names())
        self.platform_combo.setCurrentIndex(0)
        self.platform_combo.setMinimumWidth(120)
        self.platform_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        grid.addWidget(self.platform_combo, 0, 1)

        # row 1
        grid.addWidget(QLabel("游戏类型："), 1, 0)
        self.game_combo = ComboBox(card)
        self.game_combo.addItems(["全部"] + config_manager.get_enabled_game_names())
        self.game_combo.setCurrentIndex(0)
        self.game_combo.setMinimumWidth(120)
        self.game_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        grid.addWidget(self.game_combo, 1, 1)

        grid.addWidget(QLabel("原价区间："), 1, 2)
        self.min_price_edit = LineEdit(card)
        self.min_price_edit.setPlaceholderText("最低价")
        self.min_price_edit.setMinimumWidth(90)
        self.min_price_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        grid.addWidget(self.min_price_edit, 1, 3)

        self.max_price_edit = LineEdit(card)
        self.max_price_edit.setPlaceholderText("最高价")
        self.max_price_edit.setMinimumWidth(90)
        self.max_price_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        grid.addWidget(self.max_price_edit, 1, 4)

        grid.addWidget(QLabel("溢价区间："), 1, 5)
        self.min_save_edit = LineEdit(card)
        self.min_save_edit.setPlaceholderText("最低溢价")
        self.min_save_edit.setMinimumWidth(90)
        self.min_save_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        grid.addWidget(self.min_save_edit, 1, 6)

        self.max_save_edit = LineEdit(card)
        self.max_save_edit.setPlaceholderText("最高溢价")
        self.max_save_edit.setMinimumWidth(90)
        self.max_save_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        grid.addWidget(self.max_save_edit, 1, 7)

        # row 2: 时间范围（独占一行）
        grid.addWidget(QLabel("时间范围："), 2, 0)
        self.start_date_picker = QDateEdit(card)
        self.start_date_picker.setCalendarPopup(True)
        self.start_date_picker.setDisplayFormat("yyyy-MM-dd")
        self.start_date_picker.setMinimumWidth(150)
        self.start_date_picker.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.start_date_picker.setSpecialValueText(" 起始日期")
        # 默认值：7 天前
        _default_start, _ = default_date_range()
        self.start_date_picker.setDate(_default_start)
        grid.addWidget(self.start_date_picker, 2, 1)

        self.end_date_picker = QDateEdit(card)
        self.end_date_picker.setCalendarPopup(True)
        self.end_date_picker.setDisplayFormat("yyyy-MM-dd")
        self.end_date_picker.setMinimumWidth(150)
        self.end_date_picker.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.end_date_picker.setSpecialValueText(" 结束日期")
        # 默认值：今天
        _, _default_end = default_date_range()
        self.end_date_picker.setDate(_default_end)
        grid.addWidget(self.end_date_picker, 2, 2)

        # row 3: 按钮（独立一行，右对齐）
        self.filter_btn = PrimaryPushButton("筛选", card)
        self.filter_btn.setIcon(FIF.FILTER)
        self.filter_btn.clicked.connect(self._apply_filter)
        grid.addWidget(self.filter_btn, 3, 0)

        self.reset_btn = PushButton("重置", card)
        self.reset_btn.setIcon(FIF.SYNC)
        self.reset_btn.clicked.connect(self._reset_filter)
        grid.addWidget(self.reset_btn, 3, 1)

        # row 3 后面的格子设个 stretch spacer
        spacer = QWidget(card)
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        grid.addWidget(spacer, 3, 2, 1, 5)  # 占 col 2-6

        # 让右侧剩余列保持空白（拉伸列不设，等宽就行）
        return card

    def _build_toolbar(self) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setSpacing(8)
        h.setContentsMargins(0, 0, 0, 0)

        self.export_btn = PrimaryPushButton("导出Excel", self)
        self.export_btn.setIcon(FIF.SAVE)
        self.export_btn.clicked.connect(self._export)
        h.addWidget(self.export_btn)

        self.delete_btn = PushButton("批量删除", self)
        self.delete_btn.setIcon(FIF.DELETE)
        self.delete_btn.clicked.connect(self._batch_delete)
        h.addWidget(self.delete_btn)

        h.addStretch(1)
        return h

    def _build_table_card(self) -> CardWidget:
        card = CardWidget(self)
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(8)

        # ===== 10 列固定：选择 / ID / 平台名称 / 游戏 / 原价 / 平台估价 / 溢价 / 状态 / 时间 / 操作 =====
        self._columns = ["选择", "ID", "平台名称", "游戏", "原价", "平台估价", "溢价", "状态", "时间", "操作"]
        self.table = DataTable(card, columns=10, headers=self._columns)

        self.table.setShowGrid(False)
        # 减 cell 内边距（防止价格列被默认 padding 截断）
        self.table.setStyleSheet(
            "QTableWidget::item{padding:0 4px;}"
            "QHeaderView::section{padding:4px 6px;}"
        )
        header = self.table.horizontalHeader()
        self.table.setColumnWidth(0, 50)    # 选择
        self.table.setColumnWidth(1, 60)    # ID
        self.table.setColumnWidth(2, 80)    # 平台名称
        self.table.setColumnWidth(3, 100)   # 游戏
        self.table.setColumnWidth(4, 110)   # 原价（加宽）
        self.table.setColumnWidth(5, 110)   # 平台估价（加宽）
        self.table.setColumnWidth(6, 100)   # 溢价（加宽）
        self.table.setColumnWidth(7, 90)    # 状态
        self.table.setColumnWidth(8, 140)   # 时间
        self.table.setColumnWidth(9, 180)   # 操作
        for col in range(9):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
        header.setSectionResizeMode(9, QHeaderView.Stretch)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        center_table_header(self.table)

        # 信号
        self.table.cellClicked.connect(self._on_table_click)
        self.table.cellDoubleClicked.connect(self._on_double_click)

        v.addWidget(self.table, 1)

        self.pagination = ElPagination(
            card,
            on_prev=self._prev_page,
            on_next=self._next_page,
            on_go=self._go_to_page,
        )
        v.addWidget(self.pagination)

        return card

    # ---------------- 事件回调 ----------------
    def _on_table_click(self, row: int, col: int):
        if col != 0:
            return
        if not (0 <= row < len(self.current_rows)):
            return
        product_id = self.current_rows[row].get('id')
        if product_id is None:
            return
        cell = self.table.item(row, 0)
        if product_id in self.selected_items:
            self.selected_items.remove(product_id)
            if cell is not None:
                cell.setText("☐")
        else:
            self.selected_items.add(product_id)
            if cell is not None:
                cell.setText("☑")

    def _on_double_click(self, row: int, col: int):
        if col == 0:
            return
        if not (0 <= row < len(self.current_rows)):
            return
        url = self.current_rows[row].get('url', '')
        if url:
            webbrowser.open(url)

    # ---------------- 翻页 ----------------
    def _prev_page(self):
        if self.current_page > 1:
            self.current_page -= 1
            self._load_data()

    def _next_page(self):
        max_page = ElPagination.calc_total_pages(self.total_count, self.page_size)
        if self.current_page < max_page:
            self.current_page += 1
            self._load_data()

    def _go_to_page(self, page: int):
        max_page = ElPagination.calc_total_pages(self.total_count, self.page_size)
        if 1 <= page <= max_page:
            self.current_page = page
            self._load_data()
        else:
            QMessageBox.warning(self, "提示", f"页码超出范围，请输入1-{max_page}之间的数字")

    # ---------------- 筛选 ----------------
    def _apply_filter(self):
        self.current_game = self.game_combo.currentText() or "全部"
        self.current_platform = self.platform_combo.currentText() or "全部"

        def _to_float(s: str):
            s = (s or "").strip()
            if not s:
                return None
            try:
                return float(s)
            except ValueError:
                return None

        self.current_min_price = _to_float(self.min_price_edit.text())
        self.current_max_price = _to_float(self.max_price_edit.text())
        self.current_min_save = _to_float(self.min_save_edit.text())
        self.current_max_save = _to_float(self.max_save_edit.text())

        # 日期：QDate(1900,1,1) 是 sentinel 表示"未选"
        SENTINEL = QDate(1900, 1, 1)
        sd = self.start_date_picker.date()
        ed = self.end_date_picker.date()
        self.current_start_date = sd.toString('yyyy-MM-dd') if sd.isValid() and sd != SENTINEL else None
        self.current_end_date = ed.toString('yyyy-MM-dd') if ed.isValid() and ed != SENTINEL else None

        self.current_page = 1
        self.save_filter_state()
        self._load_data()

    def _reset_filter(self):
        self.current_game = "全部"
        self.current_platform = "全部"
        self.current_min_price = None
        self.current_max_price = None
        self.current_min_save = None
        self.current_max_save = None
        # 重置回到 7 天前 ~ 今天（与首次进入页面一致）
        _default_start, _default_end = default_date_range()
        self.current_start_date = _default_start.toString("yyyy-MM-dd")
        self.current_end_date = _default_end.toString("yyyy-MM-dd")
        self.current_page = 1

        self.game_combo.setCurrentIndex(0)
        self.platform_combo.setCurrentIndex(0)
        self.min_price_edit.clear()
        self.max_price_edit.clear()
        self.min_save_edit.clear()
        self.max_save_edit.clear()
        # 日期回到 7 天前 ~ 今天
        self.start_date_picker.setDate(_default_start)
        self.end_date_picker.setDate(_default_end)

        self.save_filter_state()
        self._load_data()

    def on_show(self):
        self._load_data()

    def focus_filter(self):
        """Ctrl+F 快捷键：聚焦到游戏类型下拉框"""
        self.game_combo.setFocus()

    def _save_filter_state(self) -> dict:
        return {
            "game": self.current_game,
            "platform": self.current_platform,
            "min_price": self.current_min_price,
            "max_price": self.current_max_price,
            "min_save": self.current_min_save,
            "max_save": self.current_max_save,
            "start_date": self.current_start_date,
            "end_date": self.current_end_date,
        }

    def _load_filter_state(self, state: dict):
        self.current_game = state.get("game", "全部")
        self.current_platform = state.get("platform", "全部")
        self.current_min_price = state.get("min_price")
        self.current_max_price = state.get("max_price")
        self.current_min_save = state.get("min_save")
        self.current_max_save = state.get("max_save")
        self.current_start_date = state.get("start_date")
        self.current_end_date = state.get("end_date")
        # 同步 UI
        if self.current_game and self.current_game != "全部":
            idx = self.game_combo.findText(self.current_game)
            if idx >= 0:
                self.game_combo.setCurrentIndex(idx)
        if self.current_platform and self.current_platform != "全部":
            idx = self.platform_combo.findText(self.current_platform)
            if idx >= 0:
                self.platform_combo.setCurrentIndex(idx)
        if self.current_min_price is not None:
            self.min_price_edit.setText(str(self.current_min_price))
        if self.current_max_price is not None:
            self.max_price_edit.setText(str(self.current_max_price))
        if self.current_min_save is not None:
            self.min_save_edit.setText(str(self.current_min_save))
        if self.current_max_save is not None:
            self.max_save_edit.setText(str(self.current_max_save))

    # ---------------- 数据加载 ----------------
    def _load_data(self):
        # 显示加载态
        self.table.show_loading("加载中...")
        self.table.setRowCount(0)
        self.selected_items.clear()
        self.current_rows = []

        game_type = None if self.current_game == "全部" else self.current_game
        platform = None if self.current_platform == "全部" else self.current_platform

        products, total = self.db_manager.get_deal_products(
            page=self.current_page,
            page_size=self.page_size,
            game_type=game_type,
            crawled_platform=platform,
            min_price=self.current_min_price,
            max_price=self.current_max_price,
            min_save=self.current_min_save,
            max_save=self.current_max_save,
            start_date=self.current_start_date,
            end_date=self.current_end_date,
        )

        self.total_count = total
        total_pages = ElPagination.calc_total_pages(total, self.page_size)
        self.pagination.update_info(self.current_page, total_pages, total)

        self.current_rows = list(products)

        # ===== 行填充（9 列固定）=====
        from PySide6.QtGui import QColor
        # ★ 复用 self.db_manager 而不是每次 new DBManagerV2()（每次 new 都会开新连接 + 触发 WAL init）
        for r, p in enumerate(products):
            self.table.insertRow(r)

            original_price = float(p.get('original_price', 0) or 0)
            final_price = float(p.get('final_price', 0) or 0)
            estimated = int(p.get('estimated', 0) or 0)
            error = p.get('estimated_error')
            crawled_platform = p.get('crawled_platform', '')

            # 0. 选择
            self.table.setItem(r, 0, QTableWidgetItem("☐"))
            # 1. ID
            self.table.setItem(r, 1, QTableWidgetItem(str(p.get('id', ''))))
            # 2. 平台名称（放到游戏前面）
            item = QTableWidgetItem(crawled_platform or "")
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(r, 2, item)
            # 3. 游戏
            item = QTableWidgetItem(p.get('game_type', ''))
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(r, 3, item)
            # 4. 原价
            item = QTableWidgetItem(f"¥{original_price:.2f}" if original_price else "")
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(r, 4, item)
            # 5. 平台估价 = prices_json[crawled_platform]
            prices = self.db_manager._row_to_prices(p) or {}
            platform_price = prices.get(crawled_platform) if crawled_platform else None
            if platform_price is None or platform_price == 0:
                self.table.setItem(r, 5, QTableWidgetItem(""))  # 完全空
            else:
                item = QTableWidgetItem(f"{float(platform_price):.0f}")
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(r, 5, item)
            # 6. 溢价 = final_price - original_price（最小 0，不显示负数）
            if estimated == 1 and final_price and original_price:
                # 溢价 = 估价高出原价的金额，估价 < 原价时为 0
                diff = max(0.0, final_price - original_price)
                if diff > 0:
                    text, color = f"+¥{diff:.0f}", "#52c41a"
                else:
                    text, color = "¥0", "#999999"
                item = QTableWidgetItem(text)
                item.setForeground(QColor(color))
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(r, 6, item)
            else:
                self.table.setItem(r, 6, QTableWidgetItem(""))  # 完全空
            # 7. 状态（cellWidget）— 严格按"final_price>0 且有平台价"才算已估价
            retry_count = int(p.get('retry_count', 0) or 0)
            status_text, status_enum = self._compute_status(estimated, final_price, platform_price, error, retry_count)
            self.table.setCellWidget(r, 7, self._build_status_widget(status_text, status_enum))
            # 8. 时间
            item = QTableWidgetItem((p.get('created_at', '') or '')[:16])
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(r, 8, item)
            # 9. 操作（cellWidget）：按 status + 是否有 url 显示按钮
            self.table.setCellWidget(
                r, 9,
                self._build_op_widget(status_enum, p.get('id'), p.get('url', '') or '')
            )

        # 注册右键菜单回调
        self.table.set_on_open_url(self._open_url_for_row)
        self.table.set_on_copy_id(self._copy_id_for_row)

        # 切换空态/内容
        self.table.show_content(
            len(products),
            empty_title="暂无捡漏商品",
            empty_desc="试试调整筛选条件，或新建扫号任务",
            empty_action_text="重置筛选" if self.current_game != "全部" else "",
            on_action=self._reset_filter if self.current_game != "全部" else None,
        )

    # ---------------- 状态 cellWidget ----------------
    def _compute_status(self, estimated: int, final_price, platform_price, error, retry_count: int = 0) -> tuple:
        """状态判定（严格 3 态，与 history_page 保持一致）

        规则：
          - final_price>0                                → 已估价   (done)
          - estimated=0                                  → 待估价   (pending)
          - estimated=1 + final=0 + 有 error             → 估价失败  (failed)
          - estimated=1 + final=0 + 无 error + retry<3  → 重试中    (retrying)
          - estimated=1 + final=0 + 无 error + retry>=3 → 估价失败  (failed)

        Returns:
            (状态文案, 状态枚举: pending/done/failed/retrying)
        """
        if final_price and float(final_price) > 0:
            return "已估价", "done"

        if estimated == 0:
            return "待估价", "pending"

        # estimated=1 但 final_price=0
        if error:
            return "估价失败", "failed"
        # retry >= 3 但还没标 error（数据问题，标失败而不是永远"待估价"）
        if retry_count >= 3:
            return "估价失败", "failed"
        return "重试中", "retrying"

    def _build_status_widget(self, text: str, status: str) -> QWidget:
        """状态列 cellWidget：彩色标签"""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(4, 2, 4, 2)
        h.setSpacing(0)
        label = QLabel(text)
        label.setAlignment(Qt.AlignCenter)
        label.setFixedHeight(24)
        colors = {
            "pending":   ("#e6f7ff", "#1890ff"),  # 蓝 — 待估价
            "done":      ("#f6ffed", "#52c41a"),  # 绿 — 已估价
            "failed":    ("#fff1f0", "#f5222d"),  # 红 — 估价失败
            "retrying":  ("#f9f0ff", "#722ed1"),  # 紫 — 重试中
        }
        bg, fg = colors.get(status, ("#fafafa", "#8c8c8c"))
        label.setStyleSheet(
            f"QLabel{{background:{bg};color:{fg};border-radius:4px;"
            f"padding:2px 10px;font-weight:bold;}}"
        )
        h.addWidget(label)
        h.addStretch(1)
        return w

    def _build_op_widget(self, status: str, product_id, url: str = "") -> QWidget:
        """操作列：按状态 + 是否有 url 显示按钮

        - 估价失败 + 有 url：[重试] [复制]
        - 估价失败 + 无 url：[重试]
        - 待估价/已估价 + 有 url：[复制]
        - 待估价/已估价 + 无 url：空
        """
        from qfluentwidgets import PushButton
        from qfluentwidgets import FluentIcon as FIF
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(4, 2, 4, 2)
        h.setSpacing(4)

        if status == "failed":
            retry_btn = PushButton("重试", w)
            retry_btn.setFixedSize(48, 24)
            retry_btn.setCursor(Qt.PointingHandCursor)
            retry_btn.setStyleSheet("PushButton{padding:2px 4px;min-width:0px;}")
            retry_btn.clicked.connect(lambda: self._retry_estimate(product_id))
            h.addWidget(retry_btn)

        if url:
            # 用 "打开" 2 字（紧凑），hover tooltip 显示完整 URL
            copy_btn = PushButton("打开", w)
            copy_btn.setFixedSize(48, 24)
            copy_btn.setCursor(Qt.PointingHandCursor)
            copy_btn.setStyleSheet("PushButton{padding:2px 4px;min-width:0px;}")
            copy_btn.setToolTip(f"打开链接\n{url}")
            copy_btn.clicked.connect(lambda: self._open_url(url))
            h.addWidget(copy_btn)

        h.addStretch(1)
        return w

    def _open_url(self, url: str) -> None:
        """在默认浏览器中新开标签页打开链接，带 InfoBar 反馈"""
        import webbrowser
        from qfluentwidgets import InfoBar, InfoBarPosition
        webbrowser.open_new_tab(url)
        InfoBar.success(
            title="已打开",
            content="链接已在浏览器中打开",
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=1500,
        )

    def _retry_estimate(self, product_db_id):
        """把估价失败的商品重新入队"""
        from services.estimation_worker import get_estimation_worker
        worker = get_estimation_worker()
        if not worker:
            notify(self, "错误", "估价 Worker 未启动", level="error")
            return
        self.db_manager.clear_estimate_error(product_db_id)
        worker.enqueue([product_db_id])
        notify(self, "提示", f"商品 #{product_db_id} 已重新入队", level="info")
        self._load_data()

    def _on_product_estimated(self, product_db_id: int, final_price: float, is_deal: bool):
        """Worker 估价完成 signal：节流后整表刷新"""
        import time
        if not hasattr(self, '_throttle_ts'):
            self._throttle_ts = 0
        if time.time() - self._throttle_ts < 2:
            return
        self._throttle_ts = time.time()
        self._load_data()

    def _open_url_for_row(self, row: int):
        """右键菜单：浏览器打开"""
        if 0 <= row < len(self.current_rows):
            url = self.current_rows[row].get('url', '')
            if url:
                import webbrowser
                webbrowser.open(url)

    def _copy_id_for_row(self, row: int):
        """右键菜单：复制 ID（带反馈）"""
        from qfluentwidgets import InfoBar, InfoBarPosition
        if 0 <= row < len(self.current_rows):
            pid = self.current_rows[row].get('id', '')
            if pid:
                from PySide6.QtGui import QGuiApplication
                QGuiApplication.clipboard().setText(str(pid))
                InfoBar.success(
                    title="已复制", content=f"商品 ID: {pid}",
                    parent=self, position=InfoBarPosition.TOP_RIGHT, duration=1500,
                )

    # ---------------- 导出 ----------------
    def _export(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存文件",
            f"捡漏商品_{self.current_page}.xlsx",
            "Excel文件 (*.xlsx);;CSV文件 (*.csv);;所有文件 (*.*)",
        )
        if not path:
            return

        game_type = None if self.current_game == "全部" else self.current_game

        products, _ = self.db_manager.get_deal_products(
            page=1,
            page_size=10000,
            game_type=game_type,
            min_price=self.current_min_price,
            max_price=self.current_max_price,
            min_save=self.current_min_save,
            max_save=self.current_max_save,
            start_date=self.current_start_date,
            end_date=self.current_end_date,
        )

        # ===== 动态列：平台名从 config_manager 读 =====
        platform_names = config_manager.get_platform_names()
        headers = ['ID', '游戏', '原价'] + platform_names + ['溢价', '时间']
        rows = []
        for p in products:
            prices = self.db_manager._row_to_prices(p) if isinstance(p, dict) else {}
            platform_cells = [str(prices.get(pn, '')) or '-' for pn in platform_names]
            rows.append([
                p.get('id', ''),
                p.get('game_type', ''),
                p.get('original_price', ''),
                *platform_cells,
                p.get('save_amount', ''),
                (p.get('created_at', '') or '')[:16],
            ])

        try:
            if path.endswith('.csv'):
                with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                    writer = csv.writer(f)
                    writer.writerow(headers)
                    writer.writerows(rows)
            else:
                try:
                    from openpyxl import Workbook
                    from openpyxl.styles import Font, Alignment, PatternFill

                    wb = Workbook()
                    ws = wb.active
                    ws.title = "捡漏商品"
                    ws.append(headers)
                    header_font = Font(bold=True)
                    header_fill = PatternFill(
                        start_color="E6E6E6", end_color="E6E6E6", fill_type="solid"
                    )
                    for cell in ws[1]:
                        cell.font = header_font
                        cell.fill = header_fill
                        cell.alignment = Alignment(horizontal='center')
                    for row in rows:
                        ws.append(row)
                    wb.save(path)
                except ImportError:
                    csv_path = path.replace('.xlsx', '.csv')
                    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
                        writer = csv.writer(f)
                        writer.writerow(headers)
                        writer.writerows(rows)
                    notify(self, "提示", "Excel 模块不可用，已自动改为 CSV 格式", level="warning")
                    return
            notify(self, "导出成功", f"共导出 {len(products)} 条数据", level="success")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"导出失败：{str(e)}")

    # ---------------- 批量删除 ----------------
    def _batch_delete(self):
        if not self.selected_items:
            notify(self, "提示", "请先选择要删除的商品", level="warning")
            return

        count = len(self.selected_items)
        w = MessageBox("确认", f"确定要删除选中的 {count} 个商品吗？", self)
        if w.exec():
            self.db_manager.delete_products(list(self.selected_items))
            self.selected_items.clear()
            self._load_data()
            notify(self, "删除成功", f"已删除 {count} 个商品", level="success")
