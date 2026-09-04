#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全局 QSS 主题适配 — 让所有 Qt 原生组件在深色/浅色模式下都正确显示

设计原则：
1. 用 `*` 全局选择器兜底所有未指定 color 的文字
2. 用 `QLabel` 显式覆盖（qfluentwidgets BodyLabel 继承自 QLabel）
3. 优先级：业务 setStyleSheet > 全局 QSS > qfluentwidgets 默认
   → 如果业务代码写了 color，必须删掉或改成动态色
4. 颜色：
   - 深色模式：文字纯白 #FFFFFF / 次要文字 #B4B8C0 / 弱文字 #8A8D93
   - 浅色模式：文字深色 #303133 / 次要 #606266 / 弱文字 #909399
"""

from qfluentwidgets import qconfig, Theme


def global_text_qss() -> str:
    """生成全局文字色 QSS"""
    is_dark = qconfig.theme == Theme.DARK

    if is_dark:
        # === 深色模式 ===
        text_primary = "#FFFFFF"     # 纯白
        text_secondary = "#B4B8C0"   # 浅灰
        text_tertiary = "#8A8D93"    # 弱灰
        bg_card = "rgba(255, 255, 255, 0.04)"
        bg_hover = "rgba(255, 255, 255, 0.08)"
        bg_pressed = "rgba(255, 255, 255, 0.05)"
        border = "rgba(255, 255, 255, 0.1)"
        border_hover = "rgba(255, 255, 255, 0.2)"
        primary = "#1677FF"
        # 关键：所有 color 用 !important 强制覆盖 inline stylesheet
        return f"""
        /* ============ 全局兜底：!important 覆盖所有 inline qss ============ */
        * {{
            color: {text_primary} !important;
        }}
        QWidget {{
            color: {text_primary} !important;
        }}

        /* ============ 关键：右侧 stackedWidget 背景在 DARK 模式下也设深色 ============
           qfluentwidgets 的 StackedWidget palette 默认是白色，深色模式没改。
           !important 强制让右侧内容区变深色，避免白底白字。 */
        QStackedWidget, QStackedWidget::item, QStackedWidget > QWidget {{
            background-color: #1F1F1F !important;
        }}
        /* CardWidget / 类似容器背景用半透明白（叠加深色背景） */
        CardWidget, QFrame[frameShape="4"], QFrame[frameShape="5"], QFrame[frameShape="6"] {{
            background-color: rgba(255, 255, 255, 0.04) !important;
        }}

        /* ============ 标签类 ============ */
        QLabel, QCheckBox, QRadioButton, QGroupBox, QToolButton {{
            color: {text_primary} !important;
            background: transparent;
        }}

        /* ============ 输入类 ============ */
        QLineEdit, QTextEdit, QPlainTextEdit,
        QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit, QDateTimeEdit {{
            color: {text_primary} !important;
            background-color: {bg_card};
            border: 1px solid {border};
            border-radius: 6px;
            padding: 0 8px;
            selection-background-color: {primary};
            selection-color: #FFFFFF;
        }}
        QTextEdit, QPlainTextEdit {{
            padding: 4px 8px;
        }}
        /* QSpinBox 系列不能设 padding（会破坏 up/down 按钮 hit-test，只剩右下角能点） */
        QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit, QDateTimeEdit {{
            padding: 0;
        }}
        QSpinBox::up-button, QDoubleSpinBox::up-button, QTimeEdit::up-button,
        QDateTimeEdit::up-button {{
            subcontrol-origin: border;
            subcontrol-position: top right;
        }}
        QSpinBox::down-button, QDoubleSpinBox::down-button, QTimeEdit::down-button,
        QDateTimeEdit::down-button {{
            subcontrol-origin: border;
            subcontrol-position: bottom right;
        }}
        QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
        QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus,
        QTimeEdit:focus, QDateTimeEdit:focus {{
            border: 1px solid {primary};
        }}
        QLineEdit:disabled {{
            color: {text_tertiary} !important;
        }}

        /* ============ 按钮类 ============ */
        QPushButton {{
            color: {text_primary} !important;
            background-color: {bg_hover};
            border: 1px solid {border};
            border-radius: 6px;
            padding: 0 12px;
        }}
        QPushButton:hover {{
            background-color: rgba(255, 255, 255, 0.12);
        }}
        QPushButton:pressed {{
            background-color: {bg_pressed};
        }}
        QPushButton:disabled {{
            color: {text_tertiary} !important;
            background-color: rgba(255, 255, 255, 0.02);
        }}

        /* ============ 菜单 / Tooltip ============ */
        QMenu, QToolTip {{
            color: {text_primary} !important;
            background-color: #2B2B2B;
            border: 1px solid {border};
        }}
        QMenu::item:selected {{
            background-color: {primary};
            color: #FFFFFF !important;
        }}

        /* ============ 勾选框 / 单选 ============ */
        QCheckBox::indicator, QRadioButton::indicator {{
            border: 1px solid {border_hover};
            background: {bg_card};
        }}
        QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
            border: 1px solid {primary};
        }}
        QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
            background: {primary};
            border: 1px solid {primary};
        }}

        /* ============ 表格 ============ */
        QHeaderView::section {{
            color: {text_primary} !important;
            background-color: {bg_card};
            border: none;
            border-right: 1px solid rgba(255, 255, 255, 0.05);
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            padding: 4px 8px;
        }}
        QTableWidget, QTableView {{
            color: {text_primary} !important;
            background-color: transparent;
            gridline-color: rgba(255, 255, 255, 0.05);
            selection-background-color: rgba(22, 119, 255, 0.3);
            selection-color: #FFFFFF;
        }}
        QTableWidget::item, QTableView::item {{
            color: {text_primary} !important;
            background: transparent;
        }}
        QTableWidget::item:selected, QTableView::item:selected {{
            background: rgba(22, 119, 255, 0.3);
            color: #FFFFFF !important;
        }}

        /* ============ 滚动条 ============ */
        QScrollBar:vertical, QScrollBar:horizontal {{
            background: transparent;
        }}
        QScrollBar:vertical {{
            width: 10px;
            margin: 0;
        }}
        QScrollBar::handle:vertical {{
            background: rgba(255, 255, 255, 0.15);
            border-radius: 5px;
            min-height: 20px;
        }}
        QScrollBar::handle:vertical:hover {{
            background: rgba(255, 255, 255, 0.25);
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0;
        }}
        QScrollBar:horizontal {{
            height: 10px;
            margin: 0;
        }}
        QScrollBar::handle:horizontal {{
            background: rgba(255, 255, 255, 0.15);
            border-radius: 5px;
            min-width: 20px;
        }}
        QScrollBar::handle:horizontal:hover {{
            background: rgba(255, 255, 255, 0.25);
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0;
        }}

        /* ============ ComboBox ============ */
        QComboBox {{
            color: {text_primary} !important;
            background-color: {bg_card};
            border: 1px solid {border};
            border-radius: 6px;
            padding: 4px 8px;
        }}
        QComboBox:hover {{
            border: 1px solid {border_hover};
        }}
        QComboBox::drop-down {{
            border: none;
        }}
        QComboBox QAbstractItemView {{
            color: {text_primary} !important;
            background-color: #2B2B2B;
            selection-background-color: {primary};
            selection-color: #FFFFFF;
            border: 1px solid {border};
            outline: 0;
        }}

        /* ============ 文件对话框 ============ */
        QFileDialog, QFileDialog QListView, QFileDialog QTreeView {{
            color: {text_primary} !important;
            background-color: #2B2B2B;
        }}
        QFileDialog QListView, QFileDialog QTreeView {{
            background-color: #1F1F1F;
        }}
        """

    # === 浅色模式 ===
    text_primary = "#303133"
    text_secondary = "#606266"
    text_tertiary = "#909399"
    bg_card = "#FFFFFF"
    bg_hover = "#FFFFFF"
    bg_pressed = "#F5F7FA"
    border = "#DCDFE6"
    border_hover = "#C0C4CC"
    primary = "#1677FF"
    return f"""
    * {{
        color: {text_primary} !important;
    }}
    QWidget {{
        color: {text_primary} !important;
    }}

    QLabel, QCheckBox, QRadioButton, QGroupBox, QToolButton {{
        color: {text_primary} !important;
        background: transparent;
    }}

    QLineEdit, QTextEdit, QPlainTextEdit,
    QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit, QDateTimeEdit {{
        color: {text_primary} !important;
        background-color: {bg_card};
        border: 1px solid {border};
        border-radius: 6px;
        padding: 0 8px;
        selection-background-color: {primary};
        selection-color: #FFFFFF;
    }}
    QTextEdit, QPlainTextEdit {{
        padding: 4px 8px;
    }}
    /* QSpinBox 系列不能设 padding（会破坏 up/down 按钮 hit-test，只剩右下角能点） */
    QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit, QDateTimeEdit {{
        padding: 0;
    }}
    QSpinBox::up-button, QDoubleSpinBox::up-button, QTimeEdit::up-button,
    QDateTimeEdit::up-button {{
        subcontrol-origin: border;
        subcontrol-position: top right;
    }}
    QSpinBox::down-button, QDoubleSpinBox::down-button, QTimeEdit::down-button,
    QDateTimeEdit::down-button {{
        subcontrol-origin: border;
        subcontrol-position: bottom right;
    }}
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
    QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus,
    QTimeEdit:focus, QDateTimeEdit:focus {{
        border: 1px solid {primary};
    }}
    QLineEdit:disabled {{
        color: {text_tertiary} !important;
    }}

    QPushButton {{
        color: {text_secondary} !important;
        background-color: {bg_card};
        border: 1px solid {border};
        border-radius: 6px;
        padding: 0 12px;
    }}
    QPushButton:hover {{
        color: {primary} !important;
        border: 1px solid {border_hover};
    }}
    QPushButton:pressed {{
        color: {primary} !important;
        border: 1px solid {primary};
    }}
    QPushButton:disabled {{
        color: {text_tertiary} !important;
        background-color: #F5F7FA;
    }}

    QMenu {{
        color: {text_primary} !important;
        background-color: {bg_card};
        border: 1px solid #E4E7ED;
    }}
    QMenu::item:selected {{
        background-color: #ECF5FF;
        color: {primary} !important;
    }}
    QToolTip {{
        color: {text_primary} !important;
        background-color: {bg_card};
        border: 1px solid #E4E7ED;
    }}

    QCheckBox::indicator, QRadioButton::indicator {{
        border: 1px solid {border_hover};
        background: {bg_card};
    }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background: {primary};
        border: 1px solid {primary};
    }}

    QHeaderView::section {{
        color: {text_primary} !important;
        background-color: #F5F7FA;
        border: none;
        border-right: 1px solid #E4E7ED;
        border-bottom: 1px solid #E4E7ED;
        padding: 4px 8px;
    }}
    QTableWidget, QTableView {{
        color: {text_primary} !important;
        background-color: {bg_card};
        gridline-color: #EBEEF5;
        selection-background-color: #ECF5FF;
        selection-color: {primary};
    }}
    QTableWidget::item, QTableView::item {{
        color: {text_primary} !important;
        background: transparent;
    }}
    QTableWidget::item:selected, QTableView::item:selected {{
        background: #ECF5FF;
        color: {primary} !important;
    }}

    QScrollBar:vertical, QScrollBar:horizontal {{
        background: transparent;
    }}
    QScrollBar:vertical {{
        width: 10px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {border_hover};
        border-radius: 5px;
        min-height: 20px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {text_tertiary};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QScrollBar:horizontal {{
        height: 10px;
        margin: 0;
    }}
    QScrollBar::handle:horizontal {{
        background: {border_hover};
        border-radius: 5px;
        min-width: 20px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {text_tertiary};
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0;
    }}

    QComboBox {{
        color: {text_primary} !important;
        background-color: {bg_card};
        border: 1px solid {border};
        border-radius: 6px;
        padding: 4px 8px;
    }}
    QComboBox:hover {{
        border: 1px solid {border_hover};
    }}
    QComboBox::drop-down {{
        border: none;
    }}
    QComboBox QAbstractItemView {{
        color: {text_primary} !important;
        background-color: {bg_card};
        selection-background-color: #ECF5FF;
        selection-color: {primary};
        border: 1px solid #E4E7ED;
        outline: 0;
    }}

    QFileDialog, QFileDialog QListView, QFileDialog QTreeView {{
        color: {text_primary} !important;
        background-color: {bg_card};
    }}
    """


def apply_global_text_qss(app) -> None:
    """把全局 QSS 应用到 QApplication。切换主题时需再次调用以刷新。

    关键技巧：直接 setStyleSheet 即可（qfluentwidgets 主样式仍保留）。
    不需要手动 unpolish/polish（容易触发 Qt 段错误）。
    """
    # 1. 直接设新 QSS（追加在 qfluentwidgets 主样式之后）
    try:
        app.setStyleSheet(global_text_qss())
    except Exception:
        pass
    # 2. patch FluentLabelBase 的 inline 颜色（因为 inline qss 优先 > 全局 *）
    try:
        from gui.widgets.fluent_color_patch import _patch_fluent_labels
        _patch_fluent_labels()
    except Exception:
        pass
