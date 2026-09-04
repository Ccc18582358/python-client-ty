"""
议价编辑子组件

字段（用 Slider 0~100 整数百分比，用户友好；内部存 0.0~1.0 浮点）：
  - 可二次实名账号加价比（0~100%）
  - 平台议价比例（-10%~+50%）

实时显示公式预览
"""
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QVBoxLayout, QSlider, QSpinBox
)
from PySide6.QtCore import Signal, Qt


class PricingEditor(QWidget):
    """议价编辑子组件"""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        # 行 1：二次实名加价比（0~100 整数百分比 + 旁边显示"X%"）
        h1 = QHBoxLayout()
        h1.addWidget(QLabel("可二次实名加价比："))
        self.secondary = QSpinBox()
        self.secondary.setRange(0, 100)
        self.secondary.setSingleStep(5)
        self.secondary.setSuffix(" %")
        self.secondary.setValue(0)  # 默认 0%
        self.secondary.setFixedWidth(110)
        self.secondary.valueChanged.connect(self._on_changed)
        h1.addWidget(self.secondary)
        # 提示
        h1.addWidget(QLabel("（0~100%）"))
        h1.addStretch(1)
        v.addLayout(h1)

        # 行 2：平台议价比例（-10~+50 整数百分比 + 旁边显示"X%"）
        h2 = QHBoxLayout()
        h2.addWidget(QLabel("平台议价比例："))
        self.bargain = QSpinBox()
        self.bargain.setRange(-10, 50)
        self.bargain.setSingleStep(1)
        self.bargain.setSuffix(" %")
        self.bargain.setValue(0)  # 默认 0%
        self.bargain.setFixedWidth(110)
        self.bargain.valueChanged.connect(self._on_changed)
        h2.addWidget(self.bargain)
        h2.addWidget(QLabel("（-10%~+50%，可正可负）"))
        h2.addStretch(1)
        v.addLayout(h2)

        # 分隔
        from PySide6.QtWidgets import QFrame
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        v.addWidget(line)

        # 公式预览
        self.preview_label = QLabel("")
        self.preview_label.setStyleSheet(
            "color: #555; background: #f5f7fa; padding: 8px 12px; "
            "border-left: 3px solid #1677FF; border-radius: 2px;"
        )
        self.preview_label.setWordWrap(True)
        v.addWidget(self.preview_label)

        self._update_preview()

    def _on_changed(self):
        self._update_preview()
        self.changed.emit()

    def _update_preview(self):
        s = self.secondary.value()  # 整数百分比
        b = self.bargain.value()
        self.preview_label.setText(
            f"📌 二次实名账号：平台价 × (1 + {s}% + {b}%)  =  平台价 × <b>{1 + s/100 + b/100:.2f}</b>\n"
            f"📌 非二次实名账号：平台价 × (1 + {b}%)  =  平台价 × <b>{1 + b/100:.2f}</b>"
        )

    def get_values(self):
        """返回 dict（浮点 0~1 范围）"""
        return {
            "secondary_real_name_ratio": self.secondary.value() / 100.0,
            "platform_bargain_ratio": self.bargain.value() / 100.0,
        }

    def set_values(self, secondary=0.0, bargain=0.0):
        """设置值（接受浮点，内部转百分比整数）"""
        # 容错：None / 负数 / 越界
        sec_pct = int(round((secondary or 0) * 100))
        sec_pct = max(0, min(100, sec_pct))
        b_pct = int(round((bargain or 0) * 100))
        b_pct = max(-10, min(50, b_pct))
        self.secondary.setValue(sec_pct)
        self.bargain.setValue(b_pct)
        self._update_preview()
