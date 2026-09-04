"""
扫描模式编辑子组件

字段：
  - 全量扫描（无间隔，跑完即止）
  - 最新数据扫描（带扫描页数）
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QRadioButton, QButtonGroup,
    QSpinBox, QLabel
)
from PySide6.QtCore import Signal


class ScanModeEditor(QWidget):
    """扫描模式编辑子组件"""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        # 行 1：扫描方式 radio
        h1 = QHBoxLayout()
        self.full_radio = QRadioButton("全量扫描（无间隔，跑完即止）")
        self.latest_radio = QRadioButton("最新数据扫描")
        self.latest_radio.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.full_radio, 0)
        self._mode_group.addButton(self.latest_radio, 1)
        self._mode_group.buttonClicked.connect(self._on_mode_changed)
        h1.addWidget(QLabel("方式："))
        h1.addWidget(self.full_radio)
        h1.addWidget(self.latest_radio)
        h1.addStretch(1)
        v.addLayout(h1)

        # 行 2：扫描页数（仅 latest）
        # 范围提示 "（1~1000）" 放到输入框外部，更清晰
        h2 = QHBoxLayout()
        self.scan_limit_label = QLabel("扫描页数：")
        h2.addWidget(self.scan_limit_label)
        self.scan_limit = QSpinBox()
        self.scan_limit.setRange(1, 200)
        self.scan_limit.setValue(5)
        self.scan_limit.setSuffix(" 页")
        self.scan_limit.valueChanged.connect(lambda _: self.changed.emit())
        h2.addWidget(self.scan_limit)
        # 范围提示 label（外部）
        self.scan_limit_range_label = QLabel("（1~200）")
        self.scan_limit_range_label.setStyleSheet("color: gray;")
        h2.addWidget(self.scan_limit_range_label)
        h2.addStretch(1)
        v.addLayout(h2)

        self._on_mode_changed()

    def _on_mode_changed(self):
        is_latest = self.latest_radio.isChecked()
        # 全量模式下隐藏扫描页数行（label / input / 范围提示）
        self.scan_limit_label.setVisible(is_latest)
        self.scan_limit.setVisible(is_latest)
        self.scan_limit_range_label.setVisible(is_latest)
        self.changed.emit()

    def get_values(self):
        """返回 dict"""
        return {
            "scan_mode": 'latest' if self.latest_radio.isChecked() else 'full',
            "scan_limit": self.scan_limit.value() if self.latest_radio.isChecked() else 0,
        }

    def set_values(self, scan_mode='latest', scan_limit=50):
        if scan_mode == 'full':
            self.full_radio.setChecked(True)
        else:
            self.latest_radio.setChecked(True)
        self.scan_limit.setValue(int(scan_limit or 50))
        self._on_mode_changed()
