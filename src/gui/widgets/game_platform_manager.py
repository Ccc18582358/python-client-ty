#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
业务配置管理窗口（极简版）

  Tab 1：游戏管理
    一个 ListWidget，列出所有游戏名
    - 顶部：输入框 + [+ 添加] 按钮
    - 列表项右侧：× 删除按钮
    - 双击列表项：重命名

  Tab 2：平台管理
    同上结构

底层全自动：
  - id 用拼音表自动生成
  - platform × game 笛卡尔积的 game_id / sheet_name / cell 全自动
  - 删除/重命名会广播 configChanged 信号
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QWidget, QListWidget,
    QListWidgetItem, QMessageBox, QLineEdit, QAbstractItemView
)
from qfluentwidgets import (
    PrimaryPushButton, PushButton, LineEdit, MessageBox, TabWidget,
    TitleLabel, BodyLabel, StrongBodyLabel, FluentIcon as FIF
)

from config.config_manager import config_manager


# ========================================================================
# 通用：单列表编辑器（游戏/平台共用）
# ========================================================================
class NameListEditor(QWidget):
    """通用名字列表编辑器（游戏 / 平台 通用）

    组成：
      顶部 [LineEdit] [+ 添加]   ← 输入新名字 + 添加
      中间 [ListWidget]         ← 现有名字列表
      底部 [↑] [↓] [删除] [重命名]
    """

    def __init__(self, parent=None, kind: str = "game"):
        """
        Args:
            kind: 'game' 或 'platform'
        """
        super().__init__(parent)
        self.kind = kind  # 'game' / 'platform'
        self._build_ui()
        self._load()

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(10)

        # 顶部：输入 + 添加
        h = QHBoxLayout()
        self.input = LineEdit(self)
        self.input.setPlaceholderText("输入新名字后回车或点 + 添加…")
        self.input.returnPressed.connect(self._on_add)
        h.addWidget(self.input, 1)
        self.add_btn = PrimaryPushButton("+ 添加", self)
        self.add_btn.setIcon(FIF.ADD)
        self.add_btn.clicked.connect(self._on_add)
        h.addWidget(self.add_btn)
        v.addLayout(h)

        # 中间：列表
        self.list = QListWidget(self)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.list.itemChanged.connect(self._on_item_renamed)
        self.list.model().rowsMoved.connect(self._on_rows_moved)
        v.addWidget(self.list, 1)

        # 底部：操作按钮
        h2 = QHBoxLayout()
        self.up_btn = PushButton("↑ 上移", self)
        self.up_btn.setIcon(FIF.UP)
        self.up_btn.clicked.connect(self._on_move_up)
        h2.addWidget(self.up_btn)

        self.down_btn = PushButton("↓ 下移", self)
        self.down_btn.setIcon(FIF.DOWN)
        self.down_btn.clicked.connect(self._on_move_down)
        h2.addWidget(self.down_btn)

        self.rename_btn = PushButton("重命名", self)
        self.rename_btn.setIcon(FIF.EDIT)
        self.rename_btn.clicked.connect(self._on_rename)
        h2.addWidget(self.rename_btn)

        self.delete_btn = PushButton("删除", self)
        self.delete_btn.setIcon(FIF.DELETE)
        self.delete_btn.clicked.connect(self._on_delete)
        h2.addWidget(self.delete_btn)

        h2.addStretch()
        hint = BodyLabel("提示：可拖动列表项调整顺序，双击或点重命名修改", self)
        hint.setStyleSheet("color: gray;")
        h2.addWidget(hint)
        v.addLayout(h2)

    # ------------------------------------------------------------------
    # 加载 / 刷新
    # ------------------------------------------------------------------
    def _load(self):
        """从 config_manager 加载并填充列表"""
        # 临时阻断 itemChanged（避免 reload 触发误改）
        self.list.blockSignals(True)
        self.list.clear()
        if self.kind == "game":
            items = config_manager.get_game_names()
        else:
            items = config_manager.get_platform_names()
        for name in items:
            li = QListWidgetItem(name)
            li.setData(Qt.UserRole, name)  # 存原名
            self.list.addItem(li)
        self.list.blockSignals(False)

    # ------------------------------------------------------------------
    # 事件回调
    # ------------------------------------------------------------------
    def _on_add(self):
        """添加新名字"""
        name = self.input.text().strip()
        if not name:
            return
        if self.kind == "game":
            ok = config_manager.add_game(name)
        else:
            ok = config_manager.add_platform(name)
        if ok:
            self.input.clear()
            self._load()
        else:
            MessageBox("提示", f"「{name}」已存在", self).exec()

    def _on_delete(self):
        """删除选中项"""
        cur = self.list.currentItem()
        if not cur:
            return
        name = cur.data(Qt.UserRole)
        m = MessageBox(
            "确认删除",
            f"删除{'游戏' if self.kind == 'game' else '平台'}「{name}」？",
            self,
        )
        if m.exec():
            if self.kind == "game":
                config_manager.delete_game(name)
            else:
                config_manager.delete_platform(name)
            self._load()

    def _on_rename(self):
        """进入编辑模式（重命名）"""
        cur = self.list.currentItem()
        if not cur:
            return
        self.list.editItem(cur)

    def _on_item_renamed(self, item: QListWidgetItem):
        """列表项被改名（用户双击 / Edit 触发）"""
        if not item:
            return
        old = item.data(Qt.UserRole)
        new = item.text().strip()
        if not new or new == old:
            item.setText(old)  # 还原
            return
        # 改名
        if self.kind == "game":
            ok = config_manager.update_game(old, new_name=new)
        else:
            ok = config_manager.update_platform(old, new_name=new)
        if not ok:
            # 改名失败：还原
            item.setText(old)
            MessageBox("失败", f"「{new}」已存在或改名失败", self).exec()
        else:
            # 成功：刷新（保持全选状态）
            self._load()

    def _on_move_up(self):
        """上移选中项"""
        row = self.list.currentRow()
        if row <= 0:
            return
        self._reorder([row - 1, row])

    def _on_move_down(self):
        """下移选中项"""
        row = self.list.currentRow()
        if row < 0 or row >= self.list.count() - 1:
            return
        self._reorder([row, row + 1])

    def _reorder(self, swap_rows):
        """交换两行（list 内 move + 落盘 reorder）"""
        a, b = swap_rows
        if a < 0 or b < 0 or a >= self.list.count() or b >= self.list.count():
            return
        # 取出两个 item 名字
        names = [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]
        names[a], names[b] = names[b], names[a]
        if self.kind == "game":
            config_manager.reorder_games(names)
        else:
            config_manager.reorder_platforms(names)
        self._load()
        # 恢复选中
        self.list.setCurrentRow(b)

    def _on_rows_moved(self, parent, start, end, dest, row):
        """拖拽排序时由 Qt 触发"""
        # 重新拉取顺序并落盘
        names = [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]
        # 上面拿到的可能是新顺序的"用户角色"——Qt 拖拽后我们 reload 一次，user role 会被重置
        if self.kind == "game":
            config_manager.reorder_games(names)
        else:
            config_manager.reorder_platforms(names)
        self._load()


# ========================================================================
# 主窗口：双 Tab
# ========================================================================
class GamePlatformManager(QDialog):
    """业务配置管理主窗口（极简版）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("业务配置管理 - 游戏/平台")
        self.resize(640, 480)
        self._build_ui()
        # 关窗时不重写数据——所有改动都实时落盘
        self._game_editor = None
        self._platform_editor = None

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(12)

        # 标题栏
        title_row = QHBoxLayout()
        title_row.addWidget(TitleLabel("游戏 / 平台 配置管理"))
        title_row.addStretch()
        reset_btn = PushButton("恢复默认", self)
        reset_btn.setIcon(FIF.SYNC)
        reset_btn.clicked.connect(self._reset_default)
        title_row.addWidget(reset_btn)
        close_btn = PushButton("关闭", self)
        close_btn.clicked.connect(self.close)
        title_row.addWidget(close_btn)
        v.addLayout(title_row)

        hint = BodyLabel("只维护两个名字列表 —— id / 单元格位置 / sheet 名 程序自动生成", self)
        hint.setStyleSheet("color: gray;")
        v.addWidget(hint)

        # 双 Tab
        self.tabs = TabWidget(self)
        self._game_editor = NameListEditor(self, kind="game")
        self._platform_editor = NameListEditor(self, kind="platform")
        self.tabs.addTab(self._game_editor, "游戏管理")
        self.tabs.addTab(self._platform_editor, "平台管理")
        self.tabs.setCurrentIndex(0)
        v.addWidget(self.tabs, 1)

    def _reset_default(self):
        m = MessageBox(
            "恢复默认",
            "将清除所有自定义配置，恢复到出厂 4 平台 + 5 游戏。继续？",
            self,
        )
        if m.exec():
            config_manager.reset_to_default()
            self._game_editor._load()
            self._platform_editor._load()
