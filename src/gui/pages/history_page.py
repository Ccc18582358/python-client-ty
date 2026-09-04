#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史记录页面

用 PySide6 + qfluentwidgets 重写。结构：
    标题
    筛选卡（游戏 / 价格区间 / 时间范围 / 筛选 / 重置）
    工具栏（批量删除）
    表格卡（TableWidget + ElPagination）
"""

import sys
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from PySide6.QtCore import Qt, QDate
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMessageBox,
    QTableWidgetItem, QAbstractItemView, QHeaderView, QSizePolicy,
    QDateEdit, QFrame,
)

from qfluentwidgets import (
    CardWidget, LineEdit, ComboBox,
    PrimaryPushButton, PushButton, FluentIcon as FIF, MessageBox,
    IconWidget, StrongBodyLabel, CaptionLabel,
)

from gui.widgets.pagination import ElPagination
from gui.widgets.data_table import DataTable
from gui.widgets.filter_state import FilterStateMixin
from gui.widgets.header_center import center_table_header
from gui.widgets.date_range import default_date_range
from gui.widgets.estimation_progress import EstimationProgressCard
from gui.theme import text_primary, notify
from api.session import session
from database.db_manager_v2 import DBManagerV2
from src.config.config_manager import config_manager


class HistoryPage(QWidget, FilterStateMixin):
    """历史记录页面"""

    FILTER_STATE_KEY = "history"

    def __init__(self, parent=None, db_manager=None):
        super().__init__(parent)
        # FluentWindow.addSubInterface 需要 objectName
        self.setObjectName("HistoryPage")

        self.db_manager = db_manager
        self.selected_items = set()
        self.current_page = 1
        self.page_size = 20
        self.total_count = 0
        self.current_game = "全部"
        self.current_platform = "全部"
        self.current_min_price = None
        self.current_max_price = None
        self.current_start_date = None
        self.current_end_date = None

        self._create_page()
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
        # 紧凑标题的颜色用全局 QSS 跟主题，不用手动刷
        if hasattr(self, "_progress_card"):
            self._progress_card.on_theme_changed()

    def _on_config_changed(self, change_type: str):
        """游戏/平台配置变更时刷新（仅游戏下拉）"""
        if change_type not in ('games', 'platforms', 'all'):
            return
        if hasattr(self, 'game_combo'):
            cur = self.current_game
            self.game_combo.clear()
            self.game_combo.addItems(["全部"] + config_manager.get_game_names())
            idx = self.game_combo.findText(cur)
            if idx >= 0:
                self.game_combo.setCurrentIndex(idx)
        self._load_data()

    # --------------------------------------------------------------- UI 构建
    def _create_page(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(10)

        # 标题区（精简版：单行图标 + 标题，~32px 高）
        self._title_widget = self._build_compact_title()
        outer.addWidget(self._title_widget)

        # ★ 估价进度卡（单行紧凑版）
        self._progress_card = EstimationProgressCard(self, db_manager=self.db_manager)
        outer.addWidget(self._progress_card)

        # 筛选条（单行版本）
        outer.addWidget(self._build_filter_card())
        # 工具栏
        outer.addLayout(self._build_toolbar())
        # 表格 ← 核心，占 stretch=1
        outer.addWidget(self._build_table_card(), stretch=1)

    def _build_compact_title(self) -> QWidget:
        """紧凑版标题：图标 + 标题 + 副标题（单行），高度 ~32px

        替代原 PageTitle（22pt + 40x40 图标 = ~60px），省 28px
        """
        w = QWidget(self)
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        h.setAlignment(Qt.AlignVCenter)

        icon = IconWidget(FIF.HISTORY, w)
        icon.setFixedSize(22, 22)
        h.addWidget(icon)

        title = StrongBodyLabel("历史记录", w)
        f = QFont("Microsoft YaHei UI", 16, QFont.DemiBold)
        title.setFont(f)
        title.setStyleSheet("color: #1D2129; background: transparent;")
        h.addWidget(title)

        sub = CaptionLabel("扫号任务的所有商品归档，可按游戏/价格/时间筛选", w)
        sub.setStyleSheet("color: #86909C; font-size: 11px; background: transparent;")
        h.addWidget(sub)
        h.addStretch(1)

        # 引用给主题切换用
        self.title_label = title
        self.subtitle_label = sub
        return w

    def _build_filter_card(self) -> CardWidget:
        """筛选卡：单行紧凑布局

        目标高度：~50px（原来 3 行 ~140px）

        单行布局：
        [游戏▼] [平台▼] [原价: [min] - [max]] [起始日期▼] 至 [结束日期▼]    [筛选] [重置]
        """
        card = CardWidget(self)
        outer = QHBoxLayout(card)
        outer.setContentsMargins(12, 6, 12, 6)
        outer.setSpacing(8)
        outer.setAlignment(Qt.AlignVCenter)

        # ---- 游戏类型 ----
        outer.addWidget(self._make_field_label("游戏"))
        self.game_combo = ComboBox()
        self.game_combo.addItems(["全部"] + config_manager.get_game_names())
        self.game_combo.setMinimumWidth(90)
        self.game_combo.setMaximumWidth(140)
        outer.addWidget(self.game_combo)

        # 分隔
        outer.addWidget(self._make_vsep())

        # ---- 平台 ----
        outer.addWidget(self._make_field_label("平台"))
        self.platform_combo = ComboBox()
        self.platform_combo.addItems(["全部"] + config_manager.get_platform_names())
        self.platform_combo.setMinimumWidth(80)
        self.platform_combo.setMaximumWidth(130)
        outer.addWidget(self.platform_combo)

        outer.addWidget(self._make_vsep())

        # ---- 原价区间 ----
        outer.addWidget(self._make_field_label("原价"))
        self.min_price_edit = LineEdit()
        self.min_price_edit.setPlaceholderText("最低")
        self.min_price_edit.setMinimumWidth(60)
        self.min_price_edit.setMaximumWidth(90)
        outer.addWidget(self.min_price_edit)
        outer.addWidget(self._make_field_label("-"))
        self.max_price_edit = LineEdit()
        self.max_price_edit.setPlaceholderText("最高")
        self.max_price_edit.setMinimumWidth(60)
        self.max_price_edit.setMaximumWidth(90)
        outer.addWidget(self.max_price_edit)

        outer.addWidget(self._make_vsep())

        # ---- 时间范围 ----
        outer.addWidget(self._make_field_label("时间"))
        self.start_date_picker = QDateEdit()
        self.start_date_picker.setCalendarPopup(True)
        self.start_date_picker.setDisplayFormat("yyyy-MM-dd")
        self.start_date_picker.setMinimumWidth(110)
        self.start_date_picker.setSpecialValueText(" 起始")
        _default_start, _ = default_date_range()
        self.start_date_picker.setDate(_default_start)
        outer.addWidget(self.start_date_picker)
        outer.addWidget(self._make_field_label("至"))
        self.end_date_picker = QDateEdit()
        self.end_date_picker.setCalendarPopup(True)
        self.end_date_picker.setDisplayFormat("yyyy-MM-dd")
        self.end_date_picker.setMinimumWidth(110)
        self.end_date_picker.setSpecialValueText(" 结束")
        _, _default_end = default_date_range()
        self.end_date_picker.setDate(_default_end)
        outer.addWidget(self.end_date_picker)

        # 弹性空间 + 按钮（右对齐）
        outer.addStretch(1)

        self.reset_btn = PushButton("重置")
        self.reset_btn.setIcon(FIF.SYNC)
        self.reset_btn.clicked.connect(self._reset_filter)
        outer.addWidget(self.reset_btn)

        self.filter_btn = PrimaryPushButton("筛选")
        self.filter_btn.setIcon(FIF.FILTER)
        self.filter_btn.clicked.connect(self._apply_filter)
        outer.addWidget(self.filter_btn)

        return card

    def _make_field_label(self, text: str) -> QLabel:
        """统一字段标签样式"""
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #86909C; font-size: 11px; background: transparent;")
        lbl.setAlignment(Qt.AlignVCenter)
        return lbl

    def _make_vsep(self) -> QFrame:
        """垂直分隔线"""
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedWidth(1)
        sep.setFixedHeight(16)
        sep.setStyleSheet("background: #E5E7EB; border: none;")
        return sep

    def _build_toolbar(self) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        self.batch_delete_btn = PushButton("批量删除")
        self.batch_delete_btn.setIcon(FIF.DELETE)
        self.batch_delete_btn.clicked.connect(self._batch_delete)
        h.addWidget(self.batch_delete_btn)

        # 右侧：总条数（动态）
        self._total_label = CaptionLabel("共 0 条")
        self._total_label.setStyleSheet(
            "color: #86909C; font-size: 11px; background: transparent;"
        )
        self._total_label.setAlignment(Qt.AlignVCenter)
        h.addStretch(1)
        h.addWidget(self._total_label)

        return h

    def _build_table_card(self) -> CardWidget:
        card = CardWidget(self)
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 8, 12, 8)
        v.setSpacing(4)

        # ===== 10 列固定：选择 / ID / 平台名称 / 游戏 / 原价 / 平台估价 / 溢价 / 状态 / 时间 / 操作 =====
        self._columns = ["选择", "ID", "平台名称", "游戏", "原价", "平台估价", "溢价", "状态", "时间", "操作"]
        self.table = DataTable(card, columns=10, headers=self._columns)

        # 用 ☐/☑ 字符模拟 checkbox，所以禁止表格原生高亮选择
        self.table.setSelectionMode(QAbstractItemView.NoSelection)

        # 减 cell 内边距（默认 padding 让 80px 列实际显示 ~64px，价格会被截断）
        self.table.setStyleSheet(
            "QTableWidget::item{padding:0 4px;}"
            "QHeaderView::section{padding:4px 6px;}"
        )

        # 列宽（加宽价格列以显示完整）
        header = self.table.horizontalHeader()
        self.table.setColumnWidth(0, 50)    # 选择
        self.table.setColumnWidth(1, 60)    # ID
        self.table.setColumnWidth(2, 80)    # 平台名称
        self.table.setColumnWidth(3, 100)   # 游戏
        self.table.setColumnWidth(4, 110)   # 原价
        self.table.setColumnWidth(5, 110)   # 平台估价
        self.table.setColumnWidth(6, 100)   # 溢价
        self.table.setColumnWidth(7, 90)    # 状态
        self.table.setColumnWidth(8, 140)   # 时间
        self.table.setColumnWidth(9, 180)   # 操作
        for col in range(9):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
        header.setSectionResizeMode(9, QHeaderView.Stretch)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        center_table_header(self.table)

        # 事件
        self.table.cellClicked.connect(self._on_table_click)
        self.table.cellDoubleClicked.connect(self._on_double_click)

        v.addWidget(self.table, stretch=1)

        # 分页
        self.pagination = ElPagination(
            card,
            on_prev=self._prev_page,
            on_next=self._next_page,
            on_go=self._go_to_page,
        )
        v.addWidget(self.pagination)

        return card

    # --------------------------------------------------------------- 业务方法
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
            "start_date": self.current_start_date,
            "end_date": self.current_end_date,
        }

    def _load_filter_state(self, state: dict):
        self.current_game = state.get("game", "全部")
        self.current_platform = state.get("platform", "全部")
        self.current_min_price = state.get("min_price")
        self.current_max_price = state.get("max_price")
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
        # 同步日期 picker：兼容"上次选了 None = 7 天默认"的情况
        SENTINEL = QDate(1900, 1, 1)
        if self.current_start_date:
            sd = QDate.fromString(self.current_start_date, "yyyy-MM-dd")
            if sd.isValid():
                self.start_date_picker.setDate(sd)
        else:
            self.start_date_picker.setDate(SENTINEL)
        if self.current_end_date:
            ed = QDate.fromString(self.current_end_date, "yyyy-MM-dd")
            if ed.isValid():
                self.end_date_picker.setDate(ed)
        else:
            self.end_date_picker.setDate(SENTINEL)

    def _load_data(self):
        # 显示加载态
        self.table.show_loading("加载中...")
        self.table.setRowCount(0)
        self.selected_items.clear()

        products = []
        try:
            game_type = None if self.current_game == "全部" else self.current_game
            platform = None if self.current_platform == "全部" else self.current_platform

            products, total = self.db_manager.get_all_products(
                page=self.current_page,
                page_size=self.page_size,
                game_type=game_type,
                crawled_platform=platform,
                min_price=self.current_min_price,
                max_price=self.current_max_price,
                start_date=self.current_start_date,
                end_date=self.current_end_date,
            )

            self.total_count = total
            total_pages = ElPagination.calc_total_pages(total, self.page_size)
            self.pagination.update_info(self.current_page, total_pages, total)

            # 同步更新顶部工具栏的总条数
            if hasattr(self, '_total_label') and self._total_label is not None:
                self._total_label.setText(f"共 {total:,} 条")

            # ===== 行填充（9 列固定）=====
            self.table.setRowCount(len(products))
            from PySide6.QtGui import QColor
            # ★ 复用 self.db_manager 而不是每次 new DBManagerV2()
            for r, p in enumerate(products):
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
                # 8. 时间（DB 存 UTC，显示时 +8 小时）
                raw_time = p.get('created_at', '') or ''
                try:
                    dt = datetime.strptime(str(raw_time)[:19], '%Y-%m-%d %H:%M:%S') + timedelta(hours=8)
                    time_display = dt.strftime('%Y-%m-%d %H:%M')
                except Exception:
                    time_display = str(raw_time)[:16]
                item = QTableWidgetItem(time_display)
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
        finally:
            # ★ 无论是否异常，都必须关掉 loading 遮罩，否则 UI 永久卡死
            self.table.show_content(
                len(products),
                empty_title="暂无历史记录",
                empty_desc="暂无历史数据。扫号任务运行时，结果会自动归档到这里。",
                empty_action_text="",
            )

    # ---------------- 状态 cellWidget ----------------
    def _compute_status(self, estimated: int, final_price, platform_price, error, retry_count: int = 0) -> tuple:
        """状态判定（严格 3 态）

        规则：
          - final_price>0                                → 已估价   (done)
          - estimated=0                                  → 待估价   (pending)
          - estimated=1 + final=0 + 有 error             → 估价失败  (failed)
          - estimated=1 + final=0 + 无 error + retry<3  → 重试中    (retrying)
          - estimated=1 + final=0 + 无 error + retry>=3 → 估价失败  (failed)
              ↑ 数据问题导致 Excel 永远算不出价，应标失败而不是永远"待估价"

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
            # 减 padding 让文字完整
            retry_btn.setStyleSheet("PushButton{padding:2px 4px;min-width:0px;}")
            retry_btn.clicked.connect(lambda: self._retry_estimate(product_id))
            h.addWidget(retry_btn)

        if url:
            # 用 "打开" 文字（更紧凑），点击后直接在默认浏览器新标签页打开
            copy_btn = PushButton("打开", w)
            copy_btn.setFixedSize(48, 24)
            copy_btn.setCursor(Qt.PointingHandCursor)
            copy_btn.setStyleSheet("PushButton{padding:2px 4px;min-width:0px;}")
            copy_btn.setToolTip(f"打开链接\n{url}")  # hover 显示完整 URL
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
        """右键菜单：浏览器打开（history 没有 url 字段，跳过）"""
        pass

    def _copy_id_for_row(self, row: int):
        """右键菜单：复制 ID（带反馈）"""
        from qfluentwidgets import InfoBar, InfoBarPosition
        from PySide6.QtGui import QGuiApplication
        if 0 <= row < self.table.rowCount():
            id_item = self.table.item(row, 1)
            if id_item is not None:
                pid = id_item.text()
                QGuiApplication.clipboard().setText(pid)
                InfoBar.success(
                    title="已复制", content=f"记录 ID: {pid}",
                    parent=self, position=InfoBarPosition.TOP_RIGHT, duration=1500,
                )

    def _on_table_click(self, row: int, col: int):
        """col==0 切选中；其它列忽略"""
        if col != 0:
            return
        id_item = self.table.item(row, 1)
        if id_item is None:
            return
        product_id = id_item.text()
        if not product_id:
            return

        checkbox_item = self.table.item(row, 0)
        if product_id in self.selected_items:
            self.selected_items.discard(product_id)
            checkbox_item.setText("☐")
        else:
            self.selected_items.add(product_id)
            checkbox_item.setText("☑")

    def _on_double_click(self, row: int, col: int):
        id_item = self.table.item(row, 1)
        if id_item is None:
            return
        product_id = id_item.text()
        if not product_id:
            return
        product = self.db_manager.get_product_by_id(product_id)
        if product and product.get('url'):
            webbrowser.open(product['url'])

    def _apply_filter(self):
        self.current_game = self.game_combo.currentText() or "全部"
        self.current_platform = self.platform_combo.currentText() or "全部"

        # 价格区间
        min_val = self.min_price_edit.text().strip()
        max_val = self.max_price_edit.text().strip()
        self.current_min_price = None
        self.current_max_price = None
        if min_val:
            try:
                self.current_min_price = float(min_val)
            except ValueError:
                pass
        if max_val:
            try:
                self.current_max_price = float(max_val)
            except ValueError:
                pass

        # 日期：QDate(1900,1,1) 是 sentinel 表示"未选"
        SENTINEL = QDate(1900, 1, 1)
        sd = self.start_date_picker.date()
        ed = self.end_date_picker.date()
        self.current_start_date = sd.toString("yyyy-MM-dd") if sd.isValid() and sd != SENTINEL else None
        self.current_end_date = ed.toString("yyyy-MM-dd") if ed.isValid() and ed != SENTINEL else None

        self.current_page = 1
        self.save_filter_state()
        self._load_data()

    def _reset_filter(self):
        self.current_game = "全部"
        self.current_platform = "全部"
        self.current_min_price = None
        self.current_max_price = None
        # 重置回到 7 天前 ~ 今天（与首次进入页面一致）
        _default_start, _default_end = default_date_range()
        self.current_start_date = _default_start.toString("yyyy-MM-dd")
        self.current_end_date = _default_end.toString("yyyy-MM-dd")
        self.current_page = 1

        self.game_combo.setCurrentIndex(0)
        self.platform_combo.setCurrentIndex(0)
        self.min_price_edit.setText("")
        self.max_price_edit.setText("")
        self.start_date_picker.setDate(_default_start)
        self.end_date_picker.setDate(_default_end)

        self.save_filter_state()
        self._load_data()

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
            notify(
                self,
                "提示",
                f"页码超出范围，请输入 1-{max_page} 之间的数字",
                level="warning",
            )

    def _batch_delete(self):
        if not self.selected_items:
            notify(self, "提示", "请先选择要删除的记录", level="warning")
            return

        count = len(self.selected_items)
        confirm = MessageBox(
            "确认",
            f"确定要删除选中的 {count} 条记录吗？",
            self,
        )
        if confirm.exec():
            self.db_manager.delete_products(list(self.selected_items))
            self.selected_items.clear()
            self._load_data()
            notify(self, "删除成功", f"已删除 {count} 条记录", level="success")
