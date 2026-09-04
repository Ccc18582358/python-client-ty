#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫号任务页面 - PySide6 + qfluentwidgets 版本"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta
from typing import Dict, List
from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QDialog, QFrame,
    QFormLayout, QMessageBox, QTableWidgetItem, QAbstractItemView,
    QHeaderView, QPushButton, QCheckBox
)
from qfluentwidgets import (
    CardWidget, LineEdit, ComboBox, DatePicker,
    PrimaryPushButton, PushButton, FluentIcon as FIF, MessageBox,
    CheckBox, SwitchButton, isDarkTheme
)

from gui.widgets.pagination import ElPagination
from gui.widgets.data_table import DataTable
from gui.widgets.header_center import center_table_header
from gui.widgets.page_title import PageTitle
from qfluentwidgets import TableWidget
from gui.theme import text_primary, notify
from api.session import session
from database.db_manager_v2 import DBManagerV2
from config.config_manager import config_manager


def _section_label(text: str) -> QLabel:
    """模块级辅助：用于弹窗分节标题"""
    lbl = QLabel(text)
    lbl.setStyleSheet(
        "color: #ffffff; background: #1677FF; font-weight: bold; "
        "padding: 6px 12px; border-radius: 4px; font-size: 13px;"
    )
    lbl.setContentsMargins(0, 4, 0, 4)
    return lbl


class ChipTag(QPushButton):
    """可切换的标签芯片：圆角毛玻璃质感、点击高亮、支持单选组。

    用法:
        tag = ChipTag("火影忍者")
        tag.toggled.connect(lambda checked: print("selected", checked))
        tag.setChecked(True)   # 初始选中
        tag.isChecked()        # 是否选中

    单选组:
        ChipTag.make_radio_group([tag1, tag2, tag3])
        # 点击任意一个,自动取消同组其他芯片的选中
    """

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(64)
        self.setFixedHeight(34)
        self._hovered = False
        self._radio_group: List["ChipTag"] = []
        self._apply_style(False)

    def _apply_style(self, checked: bool):
        h = self._hovered
        if checked:
            if h:
                self.setStyleSheet("background: rgba(33,122,255,0.50); border: 1px solid rgba(33,122,255,0.90); border-radius: 17px; color: #ffffff; font-size: 13px; padding: 5px 16px; font-weight: 700;")
            else:
                self.setStyleSheet("background: rgba(33,122,255,0.40); border: 1px solid rgba(33,122,255,0.65); border-radius: 17px; color: #ffffff; font-size: 13px; padding: 5px 16px; font-weight: 700;")
        else:
            if h:
                self.setStyleSheet("background: rgba(255,255,255,0.50); border: 1px solid rgba(255,255,255,0.60); border-radius: 17px; color: #111111; font-size: 13px; padding: 5px 16px; font-weight: 700;")
            else:
                self.setStyleSheet("background: rgba(255,255,255,0.40); border: 1px solid rgba(255,255,255,0.50); border-radius: 17px; color: #1a1a1a; font-size: 13px; padding: 5px 16px; font-weight: 700;")

    def enterEvent(self, event):
        self._hovered = True
        self._apply_style(self.isChecked())
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._apply_style(self.isChecked())
        super().leaveEvent(event)

    def nextCheckState(self):
        """重写:点击时切换选中,同时取消同组其他芯片。"""
        was_checked = self.isChecked()
        # 单选组:先取消同组所有,再选中当前
        if self._radio_group and not was_checked:
            for sibling in self._radio_group:
                if sibling is not self and sibling.isChecked():
                    sibling.setChecked(False)
                    sibling._apply_style(False)
        self.setChecked(not was_checked)
        self._apply_style(self.isChecked())

    @staticmethod
    def make_radio_group(tags: List["ChipTag"]):
        """将一组 ChipTag 设为单选组(互斥)。"""
        for tag in tags:
            tag._radio_group = list(tags)


class ScanTaskPage(QWidget):
    """扫号任务页面"""

    # 列索引常量（与 setHorizontalHeaderLabels 顺序一致）
    COL_SELECT = 0
    COL_ID = 1
    COL_NAME = 2
    COL_PLATFORMS = 3
    COL_GAMES = 4
    COL_SCHEDULE = 5    # 调度（什么时候跑）
    COL_NEXT_RUN = 6    # 下次执行
    COL_STATUS = 7
    COL_CREATED = 8
    COL_OPERATION = 9

    def __init__(self, parent=None, db_manager=None):
        super().__init__(parent)
        # FluentWindow.addSubInterface 需要 objectName
        self.setObjectName("ScanTaskPage")

        self.db_manager = db_manager if db_manager is not None else DBManagerV2()
        self.selected_items = set()
        self.current_page = 1
        self.page_size = 20
        self.total_count = 0

        self._build_ui()
        self._load_data()
        # ★ 只在 __init__ 里连接一次 config 变更信号，不要每次切主题都重复 connect
        self._install_config_listener()

    def on_theme_changed(self):
        """主窗口切换主题时回调"""
        if hasattr(self, "_title_widget"):
            self._title_widget.apply_title_color()

    def _install_config_listener(self):
        """监听配置变更：游戏/平台改动时自动刷新 UI（只连一次）"""
        try:
            config_manager.configChanged.connect(self._on_config_changed)
        except Exception:
            pass

    def _on_config_changed(self, change_type: str):
        """配置变更回调（用户加/删/改 游戏或平台）"""
        if change_type not in ('games', 'platforms', 'all'):
            return
        # 刷新当前显示的任务列表（platforms/games 列）
        self._load_data()
        # 如果有打开的弹窗（新建/编辑），刷弹窗里的 checkbox
        if hasattr(self, '_edit_dialog') and self._edit_dialog and self._edit_dialog.isVisible():
            self._edit_dialog.rebuild_form_options()

    # ============================================================
    # UI 构造
    # ============================================================
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        # 标题区（大图标 + 标题 + 副标题）
        self._title_widget = PageTitle(
            icon=FIF.ROBOT,
            title="扫号任务",
            subtitle="管理自动扫号任务，配置平台和游戏规则",
        )
        self.title_label = self._title_widget.title_label
        self.subtitle_label = self._title_widget.subtitle_label
        root.addWidget(self._title_widget)

        # 工具栏
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        new_btn = PrimaryPushButton("新建任务", self)
        new_btn.setIcon(FIF.ADD)
        new_btn.clicked.connect(self._show_create_dialog)
        toolbar.addWidget(new_btn)

        batch_del_btn = PushButton("批量删除", self)
        batch_del_btn.setIcon(FIF.DELETE)
        batch_del_btn.clicked.connect(self._batch_delete)
        toolbar.addWidget(batch_del_btn)

        run_now_btn = PushButton("立即执行选中", self)
        run_now_btn.setIcon(FIF.PLAY)
        run_now_btn.clicked.connect(self._run_selected_now)
        toolbar.addWidget(run_now_btn)

        toolbar.addStretch(1)
        root.addLayout(toolbar)

        # 表格卡片
        card = CardWidget(self)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(8)

        # ============ 双 Table 设计：冻结最后一列（操作） ============
        # 左侧 table：前 9 列（可水平滚动）
        # 右侧 table：最后 1 列（操作，固定不动）
        # 共享 vertical scrollbar 和行高，呈现 Excel 冻结窗格效果

        # 表头（10 列：用于给左右 table 分头）
        self.headers = [
            "选择", "ID", "任务名称", "扫号平台", "扫号游戏",
            "调度", "下次执行", "状态", "创建时间", "操作",
        ]

        # --- 左侧 table（前 9 列：选择/ID/任务名/平台/游戏/调度/下次/状态/创建） ---
        self.left_table = TableWidget(self)
        self.left_table.setBorderVisible(True)
        self.left_table.setBorderRadius(8)
        self.left_table.setWordWrap(False)
        self.left_table.setRowCount(0)
        self.left_table.setColumnCount(self.COL_OPERATION)  # 9 列
        self.left_table.setHorizontalHeaderLabels(self.headers[:self.COL_OPERATION])
        self.left_table.verticalHeader().hide()
        self.left_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.left_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # 关闭 grid
        self.left_table.setShowGrid(False)
        # 关闭右侧边框
        self.left_table.setFrameShape(QFrame.NoFrame)

        # 左侧列宽：9 列全 Fixed（不压缩）
        # 总宽 = 50+50+200+140+130+170+110+90+150 = 1090
        # 窗口 >= 1090+130=1220 时无 hbar；窗口 < 1220 时 left_table 自动出 hbar
        left_header = self.left_table.horizontalHeader()
        left_header.setSectionResizeMode(QHeaderView.Fixed)
        self.left_table.setColumnWidth(0, 50)    # 选择
        self.left_table.setColumnWidth(1, 50)    # ID
        self.left_table.setColumnWidth(2, 200)   # 任务名
        self.left_table.setColumnWidth(3, 140)   # 扫号平台
        self.left_table.setColumnWidth(4, 130)   # 扫号游戏
        self.left_table.setColumnWidth(5, 170)   # 调度
        self.left_table.setColumnWidth(6, 110)   # 下次执行
        self.left_table.setColumnWidth(7, 90)    # 状态
        self.left_table.setColumnWidth(8, 150)   # 创建时间
        # 关闭 stretch 模式后，最后一列右侧空间用空白填充（看起来正常）
        self.left_table.setShowGrid(True)  # 重新打开 grid 让分割线更明显
        center_table_header(self.left_table)

        # --- 右侧 table（最后 1 列：操作，固定） ---
        self.right_table = TableWidget(self)
        self.right_table.setBorderVisible(True)
        self.right_table.setBorderRadius(8)
        self.right_table.setWordWrap(False)
        self.right_table.setRowCount(0)
        self.right_table.setColumnCount(1)
        self.right_table.setHorizontalHeaderLabels([self.headers[self.COL_OPERATION]])
        self.right_table.verticalHeader().hide()
        self.right_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.right_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.right_table.setShowGrid(False)
        # 关闭左侧边框（避免与左 table 双边框）
        self.right_table.setFrameShape(QFrame.NoFrame)
        # 右侧宽度固定（紧凑：编辑+删除两个按钮 + 文字显示完整）
        self.right_table.setColumnWidth(0, 130)
        self.right_table.setFixedWidth(130)
        right_header = self.right_table.horizontalHeader()
        right_header.setSectionResizeMode(0, QHeaderView.Fixed)
        center_table_header(self.right_table)
        # 隐藏右 table 的 horizontal scrollbar（只有 1 列不需要）
        self.right_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 关闭左 table 的 vertical scrollbar（共享右 table 的）
        self.left_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 共享 vertical scrollbar
        self.right_table.verticalScrollBar().valueChanged.connect(
            self.left_table.verticalScrollBar().setValue
        )
        self.left_table.verticalScrollBar().valueChanged.connect(
            self.right_table.verticalScrollBar().setValue
        )

        # 共享行高 + 同步列头高度：监听右 table 行高变化 → 应用到左 table
        self.right_table.verticalHeader().sectionResized.connect(
            lambda idx, h, _old: self.left_table.setRowHeight(idx, h)
        )

        # 容器：横向布局（左 table + 右 table 紧贴）
        table_container = QWidget()
        table_hbox = QHBoxLayout(table_container)
        table_hbox.setContentsMargins(0, 0, 0, 0)
        table_hbox.setSpacing(0)
        # 比例：左 table 占满，右 table 固定 130
        table_hbox.addWidget(self.left_table, 1)
        table_hbox.addWidget(self.right_table, 0)
        card_layout.addWidget(table_container)

        # 为了兼容老代码内部 self.table 引用：定义 self._main_table 指向 left_table
        self._main_table = self.left_table
        # 共享 cellClicked / cellDoubleClicked
        self._main_table.cellDoubleClicked.connect(self._on_double_click)
        self._main_table.cellClicked.connect(self._on_cell_clicked)

        # 翻页
        self.pagination = ElPagination(
            card,
            on_prev=self._prev_page,
            on_next=self._next_page,
            on_go=self._go_to_page,
        )
        card_layout.addWidget(self.pagination)

        root.addWidget(card, 1)

    # ============================================================
    # 业务方法
    # ============================================================
    def on_show(self):
        """主窗口切到本页面时调"""
        self._load_data()

    def refresh(self):
        """公共方法：主窗口 / 调度器通知刷新（用于自动更新"下次执行"列）"""
        # 防止调度器 tick 时用户在编辑任务弹窗——避免打断用户操作
        try:
            if hasattr(self, '_edit_dialog') and self._edit_dialog and self._edit_dialog.isVisible():
                return
        except Exception:
            pass
        self._load_data()

    def _load_data(self):
        # 清空两个 table
        self._main_table.setRowCount(0)
        self.right_table.setRowCount(0)
        self.selected_items.clear()
        # 用 dict 缓存 task_id -> 按钮引用（兼容旧字段名 _cb_items）
        self._cb_items: dict[int, QTableWidgetItem] = {}
        self._select_buttons: dict[int, QCheckBox] = {}

        tasks, total = self.db_manager.get_scan_tasks(
            page=self.current_page,
            page_size=self.page_size,
        )
        self.total_count = total
        total_pages = ElPagination.calc_total_pages(total, self.page_size)
        self.pagination.update_info(self.current_page, total_pages, total)

        for task in tasks:
            platforms = task.get('platforms', '') or ''
            if isinstance(platforms, list):
                platforms = ','.join(platforms)

            games = task.get('games', '') or ''
            if isinstance(games, list):
                games = ','.join(games)

            status_text = "运行中" if task.get('enabled', 0) == 1 else "已停止"
            task_id = int(task.get('id', 0))

            # ===== 调度列文字（精简） =====
            schedule_mode = task.get('schedule_mode', 'always') or 'always'
            scan_mode = task.get('scan_mode', 'latest') or 'latest'
            interval = int(task.get('interval_minutes', 10) or 10)
            # scan_mode 简写：full → "全量"，latest → "最新"
            sm_short = '全量' if scan_mode == 'full' else '最新'
            if schedule_mode == 'always':
                sched_text = f"全天 {sm_short} {interval}分"
            else:
                ws = task.get('window_start') or '--:--'
                we = task.get('window_end') or '--:--'
                # 窗口 08:00:22:00 → 08-22
                ws_short = ws[:5] if len(ws) >= 5 else ws
                we_short = we[:5] if len(we) >= 5 else we
                sched_text = f"窗口 {ws_short}-{we_short} {sm_short} {interval}分"

            # ===== 下次执行列 =====
            next_run = self._compute_next_run_text(task)

            # 双 table 同步插行
            row = self._main_table.rowCount()
            self._main_table.insertRow(row)
            self.right_table.insertRow(row)

            # 选择列：用 QCheckBox cellWidget（Qt 原生，跨字体跨主题都稳）
            select_cb = QCheckBox()
            select_cb.setProperty("task_id", task_id)
            select_cb.setCursor(Qt.PointingHandCursor)
            select_cb.toggled.connect(self._on_select_btn_toggled)
            # cellWidget 居中
            cb_container = QWidget()
            cb_layout = QHBoxLayout(cb_container)
            cb_layout.setContentsMargins(0, 0, 0, 0)
            cb_layout.setAlignment(Qt.AlignCenter)
            cb_layout.addWidget(select_cb)
            self._main_table.setCellWidget(row, self.COL_SELECT, cb_container)
            self._select_buttons[task_id] = select_cb

            self._main_table.setItem(row, self.COL_ID, QTableWidgetItem(str(task_id)))
            self._main_table.setItem(row, self.COL_NAME, QTableWidgetItem(str(task.get('task_name', ''))))
            self._main_table.setItem(row, self.COL_PLATFORMS, QTableWidgetItem(str(platforms)))
            self._main_table.setItem(row, self.COL_GAMES, QTableWidgetItem(str(games)))
            self._main_table.setItem(row, self.COL_SCHEDULE, QTableWidgetItem(sched_text))
            self._main_table.setItem(row, self.COL_NEXT_RUN, QTableWidgetItem(next_run))

            # 状态列：cellWidget QLabel（不被 QSS 覆盖），主题感知
            status_label = QLabel(status_text)
            status_label.setAlignment(Qt.AlignCenter)
            status_label.setFixedHeight(28)
            is_enabled = task.get('enabled', 0) == 1
            dark = isDarkTheme()
            if is_enabled:
                bg = "#1d3a23" if dark else "#f6ffed"
                fg = "#95de64" if dark else "#389e0d"
            else:
                bg = "#3a1f1f" if dark else "#fff1f0"
                fg = "#ff7875" if dark else "#cf1322"
            status_label.setStyleSheet(
                f"QLabel {{ background: {bg}; color: {fg}; "
                f"font-weight: bold; border-radius: 4px; }}"
            )
            self._main_table.setCellWidget(row, self.COL_STATUS, status_label)

            created = task.get('created_at', '') or ''
            try:
                dt = datetime.strptime(str(created)[:19], '%Y-%m-%d %H:%M:%S') + timedelta(hours=8)
                created_display = dt.strftime('%Y-%m-%d %H:%M')
            except Exception:
                created_display = str(created)[:16]
            self._main_table.setItem(row, self.COL_CREATED, QTableWidgetItem(created_display))

            # 操作列：放到右 table（冻结）
            op_widget = self._build_op_widget(task_id)
            self.right_table.setCellWidget(row, 0, op_widget)

        # 同步 vertical header 高度（列头高度）
        if self.right_table.rowCount() > 0:
            header_h = self.right_table.horizontalHeader().height()
            self.left_table.horizontalHeader().setFixedHeight(header_h)

    def _on_double_click(self, row, col):
        id_item = self._main_table.item(row, 1)
        if id_item is None:
            return
        try:
            task_id = int(id_item.text())
        except ValueError:
            return
        self._show_edit_dialog(task_id)

    def _compute_next_run_text(self, task: dict) -> str:
        """计算"下次执行"列文本（粗略）"""
        if task.get('enabled', 0) != 1:
            return "—"
        scan_mode = task.get('scan_mode', 'latest') or 'latest'
        if scan_mode == 'full':
            # 全量：last_run_at 有值就"已完成"
            if task.get('last_run_at'):
                return "已完成"
            return "待执行"
        # latest 模式：按 last_run_at + interval 估算
        last = task.get('last_run_at')
        if not last:
            return "待执行"
        try:
            s = str(last).replace('T', ' ').replace('Z', '')[:19]
            last_dt = datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
            # last_run_at 是本地时间（Python datetime.now()），不需要转时区
            interval = int(task.get('interval_minutes', 10) or 10)
            next_dt = last_dt + timedelta(minutes=interval)
            now = datetime.now()
            if next_dt <= now:
                return "待执行"
            diff_min = int((next_dt - now).total_seconds() // 60)
            if diff_min < 60:
                return f"{diff_min} 分钟后"
            return next_dt.strftime("今天 %H:%M")
        except Exception:
            return "—"

    def _run_selected_now(self):
        """立即执行选中任务（不计任何调度判定）"""
        if not self.selected_items:
            notify(self, "提示", "请先选择要执行的任务", level="warning")
            return
        from services.scan_scheduler import get_scan_scheduler
        sched = get_scan_scheduler()
        if not sched:
            notify(self, "提示", "调度器未启动", level="warning")
            return
        for task_id in list(self.selected_items):
            task = self.db_manager.get_scan_task(task_id)
            if not task:
                continue
            sched._execute(task)  # 触发执行（不计窗口/间隔）
        notify(
            self,
            "已触发",
            f"已立即执行 {len(self.selected_items)} 个任务",
            level="success",
        )

    def _on_select_btn_toggled(self, checked):
        """选择按钮 toggle 处理（cellWidget 内 QCheckBox 发出的信号）"""
        cb = self.sender()
        if not isinstance(cb, QCheckBox):
            return
        task_id = cb.property("task_id")
        if task_id is None:
            return
        if checked:
            self.selected_items.add(int(task_id))
        else:
            self.selected_items.discard(int(task_id))

    def _on_cell_clicked(self, row, col):
        """表格 cellClicked 处理：状态列点击切换启用"""
        if col == self.COL_STATUS:
            id_item = self._main_table.item(row, self.COL_ID)
            if id_item is None:
                return
            try:
                task_id = int(id_item.text())
            except ValueError:
                return
            self._toggle_status(task_id)

    def _toggle_select(self, task_id):
        """兼容方法：供其他地方手动切换选择状态（程序选中）"""
        cb = self._select_buttons.get(task_id)
        if cb is None:
            return
        cb.toggle()  # 触发 _on_select_btn_toggled

    def _build_op_widget(self, task_id: int) -> QWidget:
        """操作列：编辑 / 删除 两个带背景框的按钮（紧凑 + 文字完整）"""
        from qfluentwidgets import PushButton
        # 关键：PushButton 默认 padding 12+12 占了 24px，文字放不下
        # 用 QSS 强制 padding 减小，文字才能完整显示
        _BTN_QSS = "PushButton{padding:2px 6px;min-width:0px;background:transparent;}"
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 2, 2, 2)
        h.setSpacing(4)

        edit_btn = PushButton("编辑", w)
        edit_btn.setFixedSize(58, 26)
        edit_btn.setCursor(Qt.PointingHandCursor)
        edit_btn.setStyleSheet(_BTN_QSS)
        edit_btn.clicked.connect(lambda: self._show_edit_dialog(task_id))
        h.addWidget(edit_btn)

        del_btn = PushButton("删除", w)
        del_btn.setFixedSize(58, 26)
        del_btn.setCursor(Qt.PointingHandCursor)
        # 红色样式（危险操作）+ 减 padding
        del_btn.setStyleSheet(
            "PushButton{padding:2px 6px;min-width:0px;"
            "background:#fff1f0;color:#ff4d4f;border:1px solid #ffccc7;border-radius:4px;}"
            "PushButton:hover{background:#ffccc7;color:#cf1322;}"
        )
        del_btn.clicked.connect(lambda: self._delete_task(task_id))
        h.addWidget(del_btn)

        return w

    def _toggle_status(self, task_id):
        task = self.db_manager.get_scan_task(task_id)
        if not task:
            return
        new_enabled = 0 if task.get('enabled', 0) == 1 else 1
        try:
            self.db_manager.update_scan_task(task_id, enabled=new_enabled)
            self._load_data()
            # 通知调度器 reload，否则内存里还是老 enabled 状态
            try:
                from services.scan_scheduler import get_scan_scheduler
                sched = get_scan_scheduler()
                if sched:
                    sched.reload_tasks()
            except Exception:
                pass
            # 如果改为停止状态，通知正在运行的爬虫线程停止
            if new_enabled == 0:
                try:
                    from services.jingxi_scanner import stop_task
                    stop_task(task_id)
                except Exception:
                    pass
            notify(
                self,
                "状态已更新",
                f"任务已{'启用' if new_enabled else '停止'}",
                level="success",
            )
        except Exception as e:
            notify(self, "状态更新失败", str(e), level="error")

    def _delete_task(self, task_id):
        confirm = MessageBox("确认", "确定要删除该任务吗？", self)
        if not confirm.exec():
            return
        try:
            self.db_manager.delete_scan_task(task_id)
            self.selected_items.discard(task_id)
            self._load_data()
            # 通知调度器 reload，否则内存里还保留已删任务
            try:
                from services.scan_scheduler import get_scan_scheduler
                sched = get_scan_scheduler()
                if sched:
                    sched.reload_tasks()
            except Exception:
                pass
            notify(self, "删除成功", "任务已删除", level="success")
        except Exception as e:
            notify(self, "删除失败", str(e), level="error")

    def _batch_delete(self):
        if not self.selected_items:
            notify(self, "提示", "请先选择要删除的任务", level="warning")
            return
        count = len(self.selected_items)
        confirm = MessageBox("确认", f"确定要删除选中的 {count} 个任务吗？", self)
        if not confirm.exec():
            return
        try:
            for task_id in list(self.selected_items):
                self.db_manager.delete_scan_task(task_id)
            self.selected_items.clear()
            self._load_data()
            # 通知调度器 reload
            try:
                from services.scan_scheduler import get_scan_scheduler
                sched = get_scan_scheduler()
                if sched:
                    sched.reload_tasks()
            except Exception:
                pass
            notify(self, "删除成功", f"已删除 {count} 个任务", level="success")
        except Exception as e:
            notify(self, "批量删除失败", str(e), level="error")

    # ============================================================
    # 新建 / 编辑 弹窗
    # ============================================================
    def _show_create_dialog(self):
        self._open_task_dialog(None)

    def _show_edit_dialog(self, task_id):
        self._open_task_dialog(task_id)

    def _open_task_dialog(self, task_id):
        is_edit = task_id is not None
        task = None
        if is_edit:
            task = self.db_manager.get_scan_task(task_id)
            if not task:
                return

        dialog = QDialog(self)
        dialog.setWindowTitle("编辑扫号任务" if is_edit else "新建扫号任务")
        dialog.resize(720, 880)
        # 自适应屏幕高度：不超过可用区域 85%，避免保存按钮被屏幕切掉
        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            max_h = int(avail.height() * 0.85)
            dialog.setMaximumHeight(max_h)

        # 主题适配：弹窗默认不跟 qfluentwidgets 主题，手动加 QSS
        from qfluentwidgets import isDarkTheme
        if isDarkTheme():
            dialog.setStyleSheet("""
                QDialog { background-color: #202020; }
                QLabel { color: #e6e6e6; background: transparent; }
                QLineEdit {
                    background: #2d2d2d; color: #ffffff;
                    border: 1px solid #404040; border-radius: 4px; padding: 6px;
                }
                QSpinBox, QDoubleSpinBox, QTimeEdit {
                    background: #2d2d2d; color: #ffffff;
                    border: 1px solid #404040; border-radius: 4px;
                }
                /* ★ 关键：QSpinBox 系列不能设 padding——全局 QSS 的 padding:0 8px
                   会破坏 up/down 按钮子控件的 hit-test（表现为只有右下角能点）。
                   这里显式清零 padding，并用 subcontrol-origin/position 让按钮正确定位。 */
                QSpinBox, QDoubleSpinBox, QTimeEdit {
                    padding: 0px;
                }
                QSpinBox::up-button, QDoubleSpinBox::up-button, QTimeEdit::up-button {
                    subcontrol-origin: border;
                    subcontrol-position: top right;
                    width: 18px;
                    background: #3d3d3d;
                    border: none;
                }
                QSpinBox::down-button, QDoubleSpinBox::down-button, QTimeEdit::down-button {
                    subcontrol-origin: border;
                    subcontrol-position: bottom right;
                    width: 18px;
                    background: #3d3d3d;
                    border: none;
                }
                QCheckBox, QRadioButton { color: #e6e6e6; spacing: 8px; background: transparent; }
                QCheckBox::indicator, QRadioButton::indicator {
                    width: 16px; height: 16px;
                }
                QScrollArea, QWidget { background: transparent; }
                QScrollArea > QWidget > QWidget { background: transparent; }
                QFrame[frameShape="4"], QFrame[frameShape="5"] { color: #404040; }
            """)

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(12)

        # 滚动容器
        from PySide6.QtWidgets import QScrollArea
        scroll = QScrollArea(dialog)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll, 1)

        form_widget = QWidget()
        scroll.setWidget(form_widget)
        form = QFormLayout(form_widget)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)

        # === 基础字段 ===
        name_input = LineEdit()
        name_input.setText(str(task.get('task_name', '')) if task else '')
        name_input.setPlaceholderText("如：盼之 每日 9 点扫号")
        form.addRow("任务名称：", name_input)

        # ── 扫号平台(芯片标签,单选,每行最多5个) ──
        saved_platforms = self._saved_names(task, 'platforms')
        platform_widget, platform_chips = self._build_chip_flow(
            config_manager.get_platform_names(),
            saved_platforms,
            chips_per_row=5,
        )
        ChipTag.make_radio_group(list(platform_chips.values()))
        form.addRow("扫号平台：", platform_widget)

        # ── 扫号游戏(动态:仅显示已选平台的游戏) ──
        game_widget = QWidget()
        game_layout = QVBoxLayout(game_widget)
        game_layout.setContentsMargins(0, 0, 0, 0)
        game_layout.setSpacing(10)
        game_label = QLabel("请先选择平台")
        game_label.setStyleSheet("color: #888; font-size: 13px; padding: 6px 0;")
        game_layout.addWidget(game_label)
        game_flow_widget = QWidget()
        game_flow_layout = QVBoxLayout(game_flow_widget)
        game_flow_layout.setContentsMargins(0, 0, 0, 0)
        game_flow_layout.setSpacing(10)
        game_layout.addWidget(game_flow_widget)
        saved_games = self._saved_names(task, 'games')
        all_games = config_manager.get_game_names()
        game_chips: Dict[str, ChipTag] = {}
        # 初始空列表(编辑旧任务时由联动填充)
        game_chips = self._rebuild_game_chips(
            game_flow_widget, [], saved_games, chips_per_row=5
        )
        form.addRow("扫号游戏：", game_widget)

        # ── 平台点击联动:更新游戏列表(单选,只取当前选中平台) ──
        def _on_platform_toggled():
            selected = [name for name, chip in platform_chips.items() if chip.isChecked()]
            if not selected:
                game_label.setText("请先选择平台")
                game_label.setVisible(True)
                self._rebuild_game_chips(game_flow_widget, [], saved_games, chips_per_row=5)
                return
            game_label.setVisible(False)
            plat = selected[0]  # 单选,只有一个
            plat_games = config_manager.get_platform_game_names(plat)
            if not plat_games:
                game_label.setText("该平台暂无游戏")
                game_label.setVisible(True)
                self._rebuild_game_chips(game_flow_widget, [], saved_games, chips_per_row=5)
                return
            nonlocal game_chips
            game_chips = self._rebuild_game_chips(
                game_flow_widget, plat_games, saved_games, chips_per_row=5
            )
            ChipTag.make_radio_group(list(game_chips.values()))

        for chip in platform_chips.values():
            chip.toggled.connect(lambda _checked, c=chip: _on_platform_toggled())
        # 初始触发一次(编辑任务时还原游戏列表)
        _on_platform_toggled()

        # === 分隔线 ===
        from PySide6.QtWidgets import QFrame as _QF
        line1 = _QF()
        line1.setFrameShape(_QF.HLine)
        line1.setFrameShadow(_QF.Sunken)
        form.addRow(line1)

        # === 调度子组件 ===
        from gui.widgets.schedule_editor import ScheduleEditor
        sched_editor = ScheduleEditor()
        if task:
            sched_editor.set_values(
                schedule_mode=task.get('schedule_mode', 'always'),
                window_start=task.get('window_start'),
                window_end=task.get('window_end'),
                interval_minutes=task.get('interval_minutes', 10),
            )
        form.addRow(_section_label("── 调度（什么时候跑） ──"))
        form.addRow(sched_editor)

        # 分隔线
        line2 = _QF()
        line2.setFrameShape(_QF.HLine)
        line2.setFrameShadow(_QF.Sunken)
        form.addRow(line2)

        # === 扫描模式子组件 ===
        from gui.widgets.scan_mode_editor import ScanModeEditor
        scan_mode_editor = ScanModeEditor()
        if task:
            scan_mode_editor.set_values(
                scan_mode=task.get('scan_mode', 'latest'),
                scan_limit=task.get('scan_limit', 50),
            )
        form.addRow(_section_label("── 扫描（扫多少） ──"))
        form.addRow(scan_mode_editor)

        # 分隔线
        line3 = _QF()
        line3.setFrameShape(_QF.HLine)
        line3.setFrameShadow(_QF.Sunken)
        form.addRow(line3)

        # === 议价子组件 ===
        from gui.widgets.pricing_editor import PricingEditor
        pricing_editor = PricingEditor()
        if task:
            pricing_editor.set_values(
                secondary=task.get('secondary_real_name_ratio', 0) or 0,
                bargain=task.get('platform_bargain_ratio', 0) or 0,
            )
        form.addRow(_section_label("── 加价议价 ──"))
        form.addRow(pricing_editor)

        # === 是否启用 ===
        enable_switch = SwitchButton()
        enable_switch.setOnText("已启用")
        enable_switch.setOffText("未启用")
        if task:
            enable_switch.setChecked(task.get('enabled', 0) == 1)
        else:
            enable_switch.setChecked(True)
        form.addRow("启用此任务：", enable_switch)

        outer.addLayout(form)

        # 底部按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = PushButton("取消")
        cancel_btn.setIcon(FIF.CLOSE)
        cancel_btn.clicked.connect(dialog.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = PrimaryPushButton("保存")
        save_btn.setIcon(FIF.SAVE)
        btn_row.addWidget(save_btn)
        outer.addLayout(btn_row)

        def on_save():
            name = name_input.text().strip()
            if not name:
                notify(self, "提示", "请输入任务名称", level="warning")
                return
            selected_platforms = [k for k, chip in platform_chips.items() if chip.isChecked()]
            selected_games = [k for k, chip in game_chips.items() if chip.isChecked()]
            if not selected_platforms:
                notify(self, "提示", "请至少选择一个平台", level="warning")
                return
            if not selected_games:
                notify(self, "提示", "请至少选择一个游戏", level="warning")
                return
            # 取子组件值
            sched = sched_editor.get_values()
            scan = scan_mode_editor.get_values()
            pricing = pricing_editor.get_values()
            try:
                if is_edit:
                    self.db_manager.update_scan_task(
                        task_id=task_id,
                        task_name=name,
                        platforms=selected_platforms,
                        games=selected_games,
                        enabled=1 if enable_switch.isChecked() else 0,
                        schedule_mode=sched['schedule_mode'],
                        window_start=sched['window_start'],
                        window_end=sched['window_end'],
                        interval_minutes=sched['interval_minutes'],
                        scan_mode=scan['scan_mode'],
                        scan_limit=scan['scan_limit'],
                        secondary_real_name_ratio=pricing['secondary_real_name_ratio'],
                        platform_bargain_ratio=pricing['platform_bargain_ratio'],
                    )
                else:
                    self.db_manager.create_scan_task(
                        task_name=name,
                        platforms=selected_platforms,
                        games=selected_games,
                        enabled=enable_switch.isChecked(),
                        schedule_mode=sched['schedule_mode'],
                        window_start=sched['window_start'],
                        window_end=sched['window_end'],
                        interval_minutes=sched['interval_minutes'],
                        scan_mode=scan['scan_mode'],
                        scan_limit=scan['scan_limit'],
                        secondary_real_name_ratio=pricing['secondary_real_name_ratio'],
                        platform_bargain_ratio=pricing['platform_bargain_ratio'],
                    )
                # 通知调度器 reload
                try:
                    from services.scan_scheduler import get_scan_scheduler
                    sched_eng = get_scan_scheduler()
                    if sched_eng:
                        sched_eng.reload_tasks()
                except Exception:
                    pass
                dialog.accept()
            except Exception as e:
                notify(
                    self,
                    f"{'更新' if is_edit else '创建'}失败",
                    str(e),
                    level="error",
                )

        save_btn.clicked.connect(on_save)

        if dialog.exec() == QDialog.Accepted:
            self._load_data()
            notify(
                self,
                f"任务已{'更新' if is_edit else '创建'}",
                name_input.text().strip(),
                level="success",
            )

    @staticmethod
    def _saved_names(task, key):
        """兼容 list 和 逗号字符串两种存储格式。"""
        if not task:
            return []
        value = task.get(key, '') or ''
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [s.strip() for s in value.split(',') if s.strip()]
        return []

    @staticmethod
    def _build_chip_flow(names, saved, chips_per_row=5):
        """芯片标签流式布局。返回 (widget, {name: ChipTag})。

        Args:
            names: 标签名列表
            saved: 已选中的名列表
            chips_per_row: 每行最多芯片数
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        chips: Dict[str, ChipTag] = {}
        for i in range(0, len(names), chips_per_row):
            row_names = names[i:i + chips_per_row]
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(10)
            for name in row_names:
                tag = ChipTag(name)
                if name in (saved or []):
                    tag.setChecked(True)
                    tag._apply_style(True)
                row_layout.addWidget(tag)
                chips[name] = tag
            row_layout.addStretch(1)
            layout.addWidget(row)

        return widget, chips

    @staticmethod
    def _rebuild_game_chips(parent_widget, game_names, saved, chips_per_row=5):
        """重建游戏芯片列表(清空后重绘)。

        Args:
            parent_widget: 父容器(QWidget,内含 QVBoxLayout)
            game_names: 要显示的游戏名列表
            saved: 已选中的名集合
            chips_per_row: 每行最多芯片数

        Returns:
            {name: ChipTag} 字典
        """
        layout = parent_widget.layout()
        if layout is None:
            layout = QVBoxLayout(parent_widget)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(10)
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        chips: Dict[str, ChipTag] = {}
        for i in range(0, len(game_names), chips_per_row):
            row_names = game_names[i:i + chips_per_row]
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(10)
            for name in row_names:
                tag = ChipTag(name)
                if name in (saved or []):
                    tag.setChecked(True)
                    tag._apply_style(True)
                row_layout.addWidget(tag)
                chips[name] = tag
            row_layout.addStretch(1)
            layout.addWidget(row)

        return chips

    # ============================================================
    # 翻页
    # ============================================================
    def _prev_page(self):
        if self.current_page > 1:
            self.current_page -= 1
            self._load_data()

    def _next_page(self):
        max_page = ElPagination.calc_total_pages(self.total_count, self.page_size)
        if self.current_page < max_page:
            self.current_page += 1
            self._load_data()

    def _go_to_page(self, page):
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
