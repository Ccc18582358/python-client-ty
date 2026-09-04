"""
调度编辑子组件

字段：
  - 调度模式：全天 / 时间窗口
  - 窗口起止（仅 window 用）
  - 刷新间隔（10~1440 分钟）
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QRadioButton, QButtonGroup,
    QTimeEdit, QSpinBox, QLabel
)
from PySide6.QtCore import QTime, Signal


class ScheduleEditor(QWidget):
    """调度编辑子组件"""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        # 行 1：调度方式 radio
        h1 = QHBoxLayout()
        self.always_radio = QRadioButton("全天 24h")
        self.window_radio = QRadioButton("时间窗口")
        self.always_radio.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.always_radio, 0)
        self._mode_group.addButton(self.window_radio, 1)
        self._mode_group.buttonClicked.connect(self._on_mode_changed)
        h1.addWidget(QLabel("方式："))
        h1.addWidget(self.always_radio)
        h1.addWidget(self.window_radio)
        h1.addStretch(1)
        v.addLayout(h1)

        # 行 2：窗口起止
        h2 = QHBoxLayout()
        self.window_label = QLabel("窗口：")
        h2.addWidget(self.window_label)
        self.window_start = QTimeEdit()
        self.window_start.setDisplayFormat("HH:mm")
        self.window_start.setTime(QTime(8, 0))
        self.window_start.timeChanged.connect(lambda _: self.changed.emit())
        h2.addWidget(self.window_start)
        self.zhi_label = QLabel("至")
        h2.addWidget(self.zhi_label)
        self.window_end = QTimeEdit()
        self.window_end.setDisplayFormat("HH:mm")
        self.window_end.setTime(QTime(22, 0))
        self.window_end.timeChanged.connect(lambda _: self.changed.emit())
        h2.addWidget(self.window_end)
        h2.addStretch(1)
        v.addLayout(h2)

        # 行 3：刷新间隔（label 写说明，SpinBox 后缀写单位，不重复）
        h3 = QHBoxLayout()
        h3.addWidget(QLabel("刷新间隔（≥10）："))
        self.interval = QSpinBox()
        self.interval.setRange(10, 1440)
        self.interval.setValue(10)
        self.interval.setSuffix(" 分钟")
        self.interval.valueChanged.connect(lambda _: self.changed.emit())
        h3.addWidget(self.interval)
        h3.addStretch(1)
        v.addLayout(h3)

        self._on_mode_changed()

    def _on_mode_changed(self):
        is_window = self.window_radio.isChecked()
        # 全天模式下隐藏整个窗口行（label / 至 / 2 个时间框）
        self.window_label.setVisible(is_window)
        self.window_start.setVisible(is_window)
        self.zhi_label.setVisible(is_window)
        self.window_end.setVisible(is_window)
        self.changed.emit()

    def get_values(self):
        """返回 dict"""
        mode = 'window' if self.window_radio.isChecked() else 'always'
        ws = self.window_start.time().toString("HH:mm")
        we = self.window_end.time().toString("HH:mm")
        return {
            "schedule_mode": mode,
            "window_start": ws if mode == 'window' else None,
            "window_end": we if mode == 'window' else None,
            "interval_minutes": self.interval.value(),
        }

    def set_values(self, schedule_mode='always', window_start=None,
                   window_end=None, interval_minutes=10):
        if schedule_mode == 'window':
            self.window_radio.setChecked(True)
        else:
            self.always_radio.setChecked(True)
        if window_start:
            try:
                h, m = window_start.split(':')
                self.window_start.setTime(QTime(int(h), int(m)))
            except Exception:
                pass
        if window_end:
            try:
                h, m = window_end.split(':')
                self.window_end.setTime(QTime(int(h), int(m)))
            except Exception:
                pass
        self.interval.setValue(int(interval_minutes or 10))
        self._on_mode_changed()
