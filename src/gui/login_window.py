#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
登录窗口 - 左右双栏 Fluent 设计

调用约定（与 main.py 保持一致）：
    win = LoginWindow()
    win.show()        # 阻塞
    if win.result:    # {token, username, expire_ms}
        ...

业务逻辑（完全不动）：
- 验证码：调用 auth_client.get_captcha(uuid) —— 复用现有接口 / 禁止本地生成
- 登录：调用 auth_client.login(username, password, captcha, uuid)
- 会话：session.set(token, expire_ms, username)

视觉：
- 1000 × 640 固定大小
- 左栏 40% 蓝色渐变（#1677FF → #69B1FF）品牌区
- 右栏 60% 浅灰底（#F5F7FA）+ 居中 480px 圆角白卡
- 卡片圆角 20px、柔和阴影、Fluent 控件
- 启动淡入 300ms，控件 hover 由 qfluentwidgets 接管
"""

import json
import os
import sys
import uuid as uuidlib
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import (
    Qt, QRect, QThreadPool, QRunnable, Signal, QObject, QEvent,
    QEventLoop, QPropertyAnimation, QTimer, QSize,
)
from PySide6.QtGui import (
    QColor, QPainter, QLinearGradient, QPixmap, QFont, QIcon, QImage,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QGraphicsDropShadowEffect, QSizePolicy,
)
from qframelesswindow import FramelessWindow
from qfluentwidgets import (
    FluentTitleBar, LineEdit, PrimaryPushButton, CaptionLabel, BodyLabel,
    StrongBodyLabel, FluentIcon as FIF, MessageBox, setTheme, Theme, CheckBox,
    ToolButton,
)
from qfluentwidgets.components.widgets.line_edit import PasswordLineEdit

from api.auth_client import get_captcha, login, current_env_label  # noqa: E402
from api.session import session  # noqa: E402
from config.config_manager import config_manager  # noqa: E402


# ---- 全局配色（与设计规范严格一致） ----
PRIMARY = "#1677FF"          # 主色
PRIMARY_HOVER = "#4096FF"    # 渐变终点
BG_PAGE = "#F5F7FA"          # 右栏背景
TEXT_MAIN = "#1D2129"        # 主文字
TEXT_SUB = "#4E5969"         # 副文字
TEXT_MUTED = "#86909C"       # 弱化文字
BORDER = "#E5E7EB"           # 边框
DANGER = "#F53F3F"           # 错误
WHITE = "#FFFFFF"

# 窗体尺寸
WINDOW_W, WINDOW_H = 1000, 640
LEFT_W = int(WINDOW_W * 0.4)            # 400
RIGHT_W = WINDOW_W - LEFT_W             # 600
CARD_W = 440
CARD_H = 500
INPUT_H = 48
BUTTON_H = 42
CAPTCHA_H = 40
TITLE_BAR_H = 32                        # FluentTitleBar 高度


# ============================================================
# 品牌名加载：统一从当前环境配置读取
# ============================================================
def _brand_name() -> str:
    """从 config_manager 读取品牌名（logo 旁的文字）"""
    return config_manager.get_brand_name()


def _make_brand_mark(parent, size: int, inverted: bool) -> QLabel:
    """创建不依赖客户素材的通用应用标识。"""
    mark = QLabel("估", parent)
    mark.setFixedSize(size, size)
    mark.setAlignment(Qt.AlignCenter)
    mark.setFont(QFont("Microsoft YaHei UI", max(size // 2, 14), QFont.Bold))
    if inverted:
        mark.setStyleSheet(
            "color: #1677FF; background: rgba(255, 255, 255, 0.95); border-radius: 12px;"
        )
    else:
        mark.setStyleSheet(
            "color: #1677FF; background: rgba(22, 119, 255, 0.08); border-radius: 9px;"
        )
    return mark


# ============================================================
# 左栏：渐变品牌背景
# ============================================================
class BrandPanel(QWidget):
    """左栏：蓝色渐变背景 + Logo / 名称 / 描述 / 版本"""

    def __init__(self, parent=None):
        super().__init__(parent)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()
        # 对角渐变（左上 #1677FF → 右下 #69B1FF）
        grad = QLinearGradient(0, 0, w, h)
        grad.setColorAt(0.0, QColor(PRIMARY))
        grad.setColorAt(1.0, QColor(PRIMARY_HOVER))
        p.fillRect(0, 0, w, h, grad)
        p.end()


# ============================================================
# 右栏：浅灰底 + 居中卡片（外部容器）
# ============================================================
class RightPanel(QWidget):
    """右栏：浅灰底 + 居中的圆角白卡"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {BG_PAGE};")


# ============================================================
# 登录卡片：纯白圆角 20px + 柔和阴影
# ============================================================
class LoginCard(QFrame):
    """圆角白卡 + 阴影。内容由 LoginWindow 装配。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("LoginCard")
        self.setStyleSheet(
            f"#LoginCard {{ background: {WHITE}; border: none; border-radius: 20px; }}"
        )
        # 柔和阴影
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 24))
        self.setGraphicsEffect(shadow)


# ============================================================
# Feature icon 圆角方块：自定义 paintEvent 确保 icon 完美居中
# ============================================================
class FeatureIconBox(QWidget):
    """带半透明白底的圆角方块，内部 icon 严格几何居中"""

    def __init__(self, icon: "QIcon", size: int = 28, icon_size: int = 16, parent=None):
        super().__init__(parent)
        self._icon = icon
        self._size = size
        self._icon_size = icon_size
        self.setFixedSize(size, size)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        # 半透明白底
        p.setBrush(QColor(255, 255, 255, int(255 * 0.18)))
        p.setPen(Qt.NoPen)
        radius = self._size // 4
        p.drawRoundedRect(0, 0, self._size, self._size, radius, radius)
        # icon 严格几何居中
        if not self._icon.isNull():
            pix = self._icon.pixmap(self._icon_size, self._icon_size)
            if not pix.isNull():
                # 把 icon 染白
                tinted = QPixmap(pix.size())
                tinted.fill(Qt.transparent)
                tp = QPainter(tinted)
                tp.setRenderHint(QPainter.Antialiasing, True)
                tp.setCompositionMode(QPainter.CompositionMode_Source)
                tp.drawPixmap(0, 0, pix)
                tp.setCompositionMode(QPainter.CompositionMode_SourceIn)
                tp.fillRect(tinted.rect(), QColor(255, 255, 255))
                tp.end()
                x = (self._size - self._icon_size) // 2
                y = (self._size - self._icon_size) // 2
                p.drawPixmap(x, y, tinted)
        p.end()


# ============================================================
# 通用输入区容器：左侧图标 + 弹性 LineEdit（圆角 10px）
# ============================================================
class IconLineEdit(QWidget):
    """圆角白底输入框 + 左侧 icon + 弹性 LineEdit

    直接在 QFrame 上画 1px 边框 + 背景色，LineEdit 透明嵌入。
    比依赖 qfluentwidgets LineEdit 自带边框更可控。
    """

    def __init__(self, icon_char: str, placeholder: str, password: bool = False, parent=None):
        super().__init__(parent)
        self.setFixedHeight(INPUT_H)
        self._has_password = password
        self._icon_char = icon_char

        # 圆角背景容器（用 QSS 而不是 paintEvent，简单可靠）
        self.setStyleSheet(
            f"IconLineEdit {{ background: {WHITE}; border: 1px solid {BORDER}; border-radius: 10px; }}"
            f"IconLineEdit:hover {{ border-color: #C9CDD4; }}"
        )
        self.setAttribute(Qt.WA_StyledBackground, True)

        # 内部 layout
        self._inner = QHBoxLayout(self)
        self._inner.setContentsMargins(14, 0, 14, 0)
        self._inner.setSpacing(10)

        # 左侧图标：固定高度，垂直居中
        self._icon = QLabel(icon_char, self)
        self._icon.setFont(QFont("Segoe UI Emoji", 12))
        self._icon.setStyleSheet(f"color: {TEXT_MUTED}; background: transparent;")
        self._icon.setFixedSize(18, INPUT_H - 2)
        self._icon.setAlignment(Qt.AlignCenter)
        self._inner.addWidget(self._icon, 0, Qt.AlignVCenter)

        # LineEdit：固定高度，垂直居中
        if password:
            self._line = PasswordLineEdit(self)
        else:
            self._line = LineEdit(self)
        self._line.setPlaceholderText(placeholder)
        self._line.setStyleSheet(
            f"LineEdit, PasswordLineEdit {{ background: transparent; border: none; padding: 0; }}"
            f"LineEdit:focus, PasswordLineEdit:focus {{ background: transparent; border: none; }}"
        )
        self._line.setFont(QFont("Microsoft YaHei UI", 12))
        self._line.setFixedHeight(INPUT_H - 2)
        # 文字垂直居中（qfluentwidgets LineEdit 默认不强制居中，需显式设置）
        self._line.setAlignment(Qt.AlignVCenter | Qt.AlignLeading)
        self._inner.addWidget(self._line, 1, Qt.AlignVCenter)

        # 密码可见切换按钮（仅密码框）
        self._toggle_btn = None
        if password:
            self._toggle_btn = ToolButton(FIF.QUIET_HOURS, self)
            self._toggle_btn.setFixedSize(24, 24)
            self._toggle_btn.setStyleSheet(
                "ToolButton { background: transparent; border: none; }"
            )
            self._toggle_btn.setCursor(Qt.PointingHandCursor)
            self._toggle_btn.setToolTip("显示/隐藏密码")
            self._toggle_btn.clicked.connect(self._toggle_password_visibility)
            self._inner.addWidget(self._toggle_btn, 0, Qt.AlignVCenter)

    def _toggle_password_visibility(self):
        """切换密码可见性（PasswordLineEdit 临时换成 LineEdit 不优雅，用 mask 切换）"""
        if self._toggle_btn is None:
            return
        # qfluentwidgets PasswordLineEdit 提供 setPasswordMode(bool)？
        # 没找到公开 API，退化方案：临时换成普通 LineEdit（带密码模式）
        is_hidden = self._line.echoMode() == self._line.EchoMode.Password if hasattr(self._line, "echoMode") else True
        if is_hidden:
            self._line.setEchoMode(self._line.EchoMode.Normal)
            self._toggle_btn.setIcon(FIF.VIEW)
        else:
            self._line.setEchoMode(self._line.EchoMode.Password)
            self._toggle_btn.setIcon(FIF.QUIET_HOURS)

    def line_edit(self):
        return self._line

    def text(self) -> str:
        return self._line.text()

    def set_text(self, v: str):
        self._line.setText(v)

    def clear(self):
        self._line.clear()

    def set_focus(self):
        self._line.setFocus()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        # 聚焦时蓝色边框
        self.setStyleSheet(
            f"IconLineEdit {{ background: {WHITE}; border: 1px solid {PRIMARY}; border-radius: 10px; }}"
        )

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.setStyleSheet(
            f"IconLineEdit {{ background: {WHITE}; border: 1px solid {BORDER}; border-radius: 10px; }}"
            f"IconLineEdit:hover {{ border-color: #C9CDD4; }}"
        )


# ============================================================
# 异步任务：QRunnable + Signal 回到主线程
# ============================================================
class _CaptchaSignals(QObject):
    finished = Signal(bool, object)  # ok, payload(bytes or str)


class _LoginSignals(QObject):
    finished = Signal(bool, str, object)  # ok, msg, data(dict) or None


class _CaptchaTask(QRunnable):
    def __init__(self, uuid_str: str, signals: _CaptchaSignals):
        super().__init__()
        self.uuid_str = uuid_str
        self.signals = signals

    def run(self):
        try:
            ok, payload = get_captcha(self.uuid_str)
        except Exception as e:
            ok, payload = False, f"请求异常：{e}"
        self.signals.finished.emit(ok, payload)


class _LoginTask(QRunnable):
    def __init__(self, username: str, password: str, captcha: str, uuid_str: str, signals: _LoginSignals):
        super().__init__()
        self.username = username
        self.password = password
        self.captcha = captcha
        self.uuid_str = uuid_str
        self.signals = signals

    def run(self):
        try:
            ok, msg, data = login(self.username, self.password, self.captcha, self.uuid_str)
        except Exception as e:
            ok, msg, data = False, f"请求异常：{e}", None
        self.signals.finished.emit(ok, msg, data)


# ============================================================
# 登录窗口主体
# ============================================================
class LoginWindow(FramelessWindow):
    """登录窗 - 左右双栏 Fluent 设计

    业务逻辑保持原样：
    - 验证码走 auth_client.get_captcha
    - 登录走 auth_client.login(user, pwd, captcha, uuid)
    - session.set(token, expire_ms, username)
    - uuid 每次启动新生成
    """

    finished = Signal()

    APP_VERSION = "v1.0.0"

    def __init__(self):
        super().__init__()

        # ---- 标题栏（保留以便拖动 + min/close）----
        self.setTitleBar(FluentTitleBar(self))
      
        self.setFixedSize(WINDOW_W, WINDOW_H)
        self.setWindowIcon(QIcon())

        # 标题栏用透明背景，让蓝色渐变左栏直通到顶
        self.titleBar.setStyleSheet(
            f"FluentTitleBar {{ background: transparent; border: none; }}"
        )

        # ---- 业务状态 ----
        self.result = None
        self._uuid = uuidlib.uuid4().hex
        self._in_flight = False
        self._pool = QThreadPool.globalInstance()

        # ---- 业务信号 ----
        self._captcha_signals = _CaptchaSignals()
        self._captcha_signals.finished.connect(self._on_captcha_loaded)
        self._login_signals = _LoginSignals()
        self._login_signals.finished.connect(self._on_login_done)

        # ---- UI 持久化（仅 UI 偏好：记住账号密码）----
        self._remember_credentials = False
        self._saved_username = ""
        self._saved_password = ""
        self._load_ui_prefs()

        # ---- 居中 ----
        self._center_on_screen()

        # ---- UI ----
        self._build_ui()

        # 启动时若勾选"记住账号密码"，自动填充
        if self._remember_credentials and self._saved_username:
            self.username_field.set_text(self._saved_username)
            if self._saved_password:
                self.password_field.set_text(self._saved_password)
            self.remember_cb.setChecked(True)
            # 焦点放到验证码框（如果都填好就直接登录）
            self.captcha_field.set_focus()

        self.titleBar.raise_()
        self._install_return_key()

        # ---- 启动时拉一次验证码 ----
        QTimer.singleShot(200, self._refresh_captcha)

        # ---- 淡入动画 ----
        self.setWindowOpacity(0.0)
        self._fade_in_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_in_anim.setDuration(300)
        self._fade_in_anim.setStartValue(0.0)
        self._fade_in_anim.setEndValue(1.0)
        self._fade_in_anim.start()

    # --------------------------------------------------------------- UI 偏好持久化
    def _prefs_path(self) -> Path:
        """ui_prefs.json 放在 config_manager 同级 config 目录下（与 session.json 同级）"""
        # 用 config.config_manager 推 config 目录，避免重复硬编码
        try:
            from config.config_manager import config_manager
            return Path(config_manager.config_dir) / "ui_prefs.json"
        except Exception:
            return Path.home() / ".jingxi_client" / "ui_prefs.json"

    def _load_ui_prefs(self):
        """读 ui_prefs.json，容错：失败则用默认值（不抛）"""
        try:
            path = self._prefs_path()
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            # 兼容老字段：remember_username / auto_login
            self._remember_credentials = bool(
                data.get("remember_credentials", data.get("remember_username", False))
            )
            self._saved_username = str(data.get("last_username", "") or "")
            # 密码用 base64 简单编码（用户层面算 ok；不建议存强加密）
            import base64
            self._saved_password = ""
            try:
                enc = data.get("last_password_b64", "") or ""
                if enc:
                    self._saved_password = base64.b64decode(enc).decode("utf-8", errors="ignore")
            except Exception:
                self._saved_password = ""
        except Exception:
            # 任何异常都不影响主流程
            pass

    def _save_ui_prefs(self, username: str, password: str = ""):
        """写 ui_prefs.json，容错：失败也不抛"""
        try:
            import base64
            path = self._prefs_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            checked = (
                self.remember_cb.isChecked()
                if hasattr(self, "remember_cb")
                else self._remember_credentials
            )
            data = {
                "remember_credentials": checked,
                "last_username": username if checked else "",
                # 密码 base64 编码（仅本地明文保护；非强安全）
                "last_password_b64": (
                    base64.b64encode(password.encode("utf-8")).decode("ascii")
                    if (checked and password)
                    else ""
                ),
            }
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # --------------------------------------------------------------- 位置
    def _center_on_screen(self):
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        rect = screen.availableGeometry()
        x = (rect.width() - WINDOW_W) // 2
        y = max((rect.height() - WINDOW_H) // 2, 20)
        self.move(x, y)

    # --------------------------------------------------------------- UI 构造
    def _build_ui(self):
        # 顶层 root：水平两栏（左右）
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # === 左栏：品牌区 ===
        left = self._build_left_panel()
        left.setFixedWidth(LEFT_W)
        root.addWidget(left)

        # === 右栏：登录卡 ===
        right = self._build_right_panel()
        right.setFixedWidth(RIGHT_W)
        root.addWidget(right)

    def _build_left_panel(self) -> QWidget:
        panel = BrandPanel(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(40, 24, 40, 24)
        layout.setSpacing(0)

        # ---- 顶部：通用应用标识 + 系统名 ----
        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(_make_brand_mark(panel, 48, inverted=True), 0, Qt.AlignVCenter)

        name_box = QVBoxLayout()
        name_box.setSpacing(2)
        # 品牌名（来自 config.json -> branding.brand_name，可配置）
        name = QLabel(_brand_name(), panel)
        name.setFont(QFont("Microsoft YaHei UI", 18, QFont.Bold))
        name.setStyleSheet("color: white; background: transparent;")
        name_box.addWidget(name)
        app_name = QLabel("智能账号估价平台", panel)
        app_name.setFont(QFont("Microsoft YaHei UI", 10))
        app_name.setStyleSheet("color: rgba(255, 255, 255, 0.75); background: transparent;")
        name_box.addWidget(app_name)
        top.addLayout(name_box)
        top.addStretch(1)
        layout.addLayout(top)

        # 留白
        layout.addStretch(1)

        # ---- 中部：副标题 + 3 条特性 ----
        sub_title = QLabel("数据采集 · 智能估价", panel)
        sub_title.setFont(QFont("Microsoft YaHei UI", 14, QFont.DemiBold))
        sub_title.setStyleSheet("color: white; background: transparent;")
        sub_title.setWordWrap(True)
        layout.addWidget(sub_title)

        layout.addSpacing(20)

        # 特性列表（用 FeatureIconBox 自定义 widget 确保 icon 完美居中）
        features = [
            (FIF.SEARCH, "高效采集账号商品"),
            (FIF.HEART, "智能生成价格参考"),
            (FIF.CLOUD, "多平台统一管理"),
        ]
        for icon, text in features:
            row = QHBoxLayout()
            row.setSpacing(10)
            icon_box = FeatureIconBox(icon.icon(), size=32, icon_size=18, parent=panel)
            row.addWidget(icon_box, 0, Qt.AlignVCenter)

            lbl = QLabel(text, panel)
            lbl.setFont(QFont("Microsoft YaHei UI", 12))
            lbl.setStyleSheet("color: rgba(255, 255, 255, 0.92); background: transparent;")
            row.addWidget(lbl, 1, Qt.AlignVCenter)
            layout.addLayout(row)
            layout.addSpacing(10)

        # 留白
        layout.addStretch(2)

        # ---- 底部：版本号 ----
        version = QLabel(self.APP_VERSION, panel)
        version.setFont(QFont("Microsoft YaHei UI", 10))
        version.setStyleSheet("color: rgba(255, 255, 255, 0.65); background: transparent;")
        layout.addWidget(version, 0, Qt.AlignLeft)

        return panel

    def _build_right_panel(self) -> QWidget:
        panel = RightPanel(self)
        # 用居中 layout 放卡片（垂直水平都居中）
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addStretch(1)

        # 居中容器（水平居中卡片）
        h_center = QHBoxLayout()
        h_center.setSpacing(0)
        h_center.addStretch(1)

        # === 登录卡片 ===
        card = LoginCard(panel)
        card.setFixedSize(CARD_W, CARD_H)

        # 卡片内部布局
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(40, 32, 40, 32)
        card_layout.setSpacing(0)

        # ---- 卡片顶部：通用应用标识 + 系统名 ----
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(_make_brand_mark(card, 36, inverted=False), 0, Qt.AlignVCenter)

        # 品牌名（由当前环境配置）+ 应用名
        small_name_box = QWidget(card)
        small_name_lay = QVBoxLayout(small_name_box)
        small_name_lay.setContentsMargins(0, 0, 0, 0)
        small_name_lay.setSpacing(0)
        small_name = QLabel(_brand_name(), small_name_box)
        small_name.setFont(QFont("Microsoft YaHei UI", 14, QFont.DemiBold))
        small_name.setStyleSheet(f"color: {TEXT_MAIN}; background: transparent;")
        small_name_lay.addWidget(small_name)
        small_sub_name = QLabel("智能账号估价平台", small_name_box)
        small_sub_name.setFont(QFont("Microsoft YaHei UI", 9))
        small_sub_name.setStyleSheet(f"color: {TEXT_MUTED}; background: transparent;")
        small_name_lay.addWidget(small_sub_name)
        top.addWidget(small_name_box, 0, Qt.AlignVCenter)
        top.addStretch(1)
        card_layout.addLayout(top)

        # 副标题
        sub = QLabel("欢迎回来", card)
        sub.setFont(QFont("Microsoft YaHei UI", 11))
        sub.setStyleSheet(f"color: {TEXT_SUB}; background: transparent;")
        card_layout.addWidget(sub)
        card_layout.addSpacing(28)

        # ---- 用户名 ----
        card_layout.addWidget(self._make_label("账号", card))
        self.username_field = IconLineEdit("👤", "", parent=card)
        card_layout.addWidget(self.username_field)
        card_layout.addSpacing(14)

        # ---- 密码 ----
        card_layout.addWidget(self._make_label("密码", card))
        self.password_field = IconLineEdit("🔒", "", password=True, parent=card)
        card_layout.addWidget(self.password_field)
        card_layout.addSpacing(14)

        # ---- 验证码 ----
        card_layout.addWidget(self._make_label("验证码", card))
        captcha_row = QHBoxLayout()
        captcha_row.setSpacing(8)
        captcha_row.setContentsMargins(0, 0, 0, 0)
        self.captcha_field = IconLineEdit("🛡", "", parent=card)
        captcha_row.addWidget(self.captcha_field, 1)

        # 验证码图（用 QLabel 显示，事件过滤器处理点击）
        self._captcha_label = QLabel(card)
        self._captcha_label.setFixedSize(110, INPUT_H)
        self._captcha_label.setAlignment(Qt.AlignCenter)
        self._captcha_label.setStyleSheet(
            f"background: {BG_PAGE}; border: 1px solid {BORDER}; border-radius: 10px;"
            f"color: {TEXT_MUTED}; font-family: 'Microsoft YaHei UI'; font-size: 9pt;"
        )
        self._captcha_label.setText("加载中…")
        self._captcha_label.setCursor(Qt.PointingHandCursor)
        self._captcha_label.installEventFilter(self)
        captcha_row.addWidget(self._captcha_label, 0)

        # 刷新按钮（IconButton）
        self._refresh_btn = self._make_icon_button(FIF.SYNC, card)
        self._refresh_btn.setFixedSize(INPUT_H, INPUT_H)
        self._refresh_btn.setToolTip("换一张")
        self._refresh_btn.clicked.connect(self._refresh_captcha)
        captcha_row.addWidget(self._refresh_btn, 0)

        card_layout.addLayout(captcha_row)
        card_layout.addSpacing(8)

        # ---- 错误提示（占位高度，避免布局跳动）----
        self._error_label = QLabel("", card)
        self._error_label.setFont(QFont("Microsoft YaHei UI", 10))
        self._error_label.setStyleSheet(f"color: {DANGER}; background: transparent;")
        self._error_label.setWordWrap(True)
        self._error_label.setFixedHeight(20)
        card_layout.addWidget(self._error_label)

        card_layout.addSpacing(4)

        # ---- 记住账号密码（左对齐） ----
        opt_row = QHBoxLayout()
        opt_row.setSpacing(20)
        opt_row.setContentsMargins(0, 0, 0, 0)
        self.remember_cb = CheckBox("记住账号密码", card)
        self.remember_cb.setChecked(self._remember_credentials)
        self.remember_cb.setFont(QFont("Microsoft YaHei UI", 10))
        opt_row.addWidget(self.remember_cb, 0, Qt.AlignLeft)
        opt_row.addStretch(1)
        card_layout.addLayout(opt_row)
        card_layout.addSpacing(18)

        # ---- 登录按钮 ----
        self._login_btn = PrimaryPushButton("登 录", card)
        self._login_btn.setFixedHeight(BUTTON_H)
        # qfluentwidgets PrimaryPushButton 默认 4px 圆角，PySide6 版本没 setBorderRadius API
        # 不在 QSS 覆盖 border-radius（会把主色背景一起干掉，慎用）
        self._login_btn.setFont(QFont("Microsoft YaHei UI", 13, QFont.DemiBold))
        self._login_btn.clicked.connect(self._on_login_clicked)
        card_layout.addWidget(self._login_btn)

        # 弹性：把内容顶到上方
        card_layout.addStretch(1)

        h_center.addWidget(card)
        h_center.addStretch(1)
        outer.addLayout(h_center)
        outer.addStretch(1)

        return panel

    def _make_label(self, text: str, parent) -> QLabel:
        lbl = QLabel(text, parent)
        lbl.setFont(QFont("Microsoft YaHei UI", 11))
        lbl.setStyleSheet(f"color: {TEXT_MAIN}; background: transparent;")
        lbl.setFixedHeight(22)
        return lbl

    def _make_icon_button(self, icon, parent):
        """Fluent 风格的图标按钮（透明背景，hover 时浅灰）"""
        from qfluentwidgets import TransparentToolButton
        btn = TransparentToolButton(icon, parent)
        btn.setFixedSize(32, 32)
        btn.setCursor(Qt.PointingHandCursor)
        return btn

    # --------------------------------------------------------------- 事件
    def eventFilter(self, obj, event: QEvent) -> bool:
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            if obj is self._captcha_label:
                self._refresh_captcha()
                return True
        return super().eventFilter(obj, event)

    def _install_return_key(self):
        """回车提交"""
        self.username_field.line_edit().returnPressed.connect(self._on_login_clicked)
        self.password_field.line_edit().returnPressed.connect(self._on_login_clicked)
        self.captcha_field.line_edit().returnPressed.connect(self._on_login_clicked)

    def closeEvent(self, event):
        if self.result is None:
            box = MessageBox("退出", "确定退出账号估价助手？", self)
            box.yesButton.setText("确定")
            if hasattr(box, "cancelButton"):
                box.cancelButton.setText("取消")
            if not box.exec():
                event.ignore()
                return
        self.finished.emit()
        super().closeEvent(event)

    def show(self):
        """保持模态运行 + 关闭后返回的语义"""
        self.setWindowModality(Qt.ApplicationModal)
        super().show()
        loop = QEventLoop()
        try:
            self.finished.connect(loop.quit, Qt.UniqueConnection)
        except TypeError:
            self.finished.connect(loop.quit)
        loop.exec()

    # --------------------------------------------------------------- 验证码
    def _refresh_captcha(self):
        if self._in_flight:
            return
        self._in_flight = True
        self._captcha_label.setText("加载中…")
        self._captcha_label.setPixmap(QPixmap())
        self._captcha_signals = _CaptchaSignals()
        self._captcha_signals.finished.connect(self._on_captcha_loaded)
        self._pool.start(_CaptchaTask(self._uuid, self._captcha_signals))

    def _on_captcha_loaded(self, ok: bool, payload):
        self._in_flight = False
        if not ok:
            self._show_error(f"验证码加载失败：{payload}")
            self._captcha_label.setText("点击重试")
            self._captcha_label.setPixmap(QPixmap())
            return
        try:
            from PIL import Image
            img = Image.open(BytesIO(payload))
            # 等比缩放到目标框（保持视觉对齐）
            target_h = CAPTCHA_H - 8
            max_w = 110
            if img.height > target_h:
                ratio = target_h / img.height
                new_w = max(int(img.width * ratio), 60)
                if new_w > max_w:
                    ratio = max_w / img.width
                    new_h = max(int(img.height * ratio), 16)
                    img = img.resize((max_w, new_h), Image.LANCZOS)
                else:
                    img = img.resize((new_w, target_h), Image.LANCZOS)
            elif img.width > max_w:
                ratio = max_w / img.width
                new_h = max(int(img.height * ratio), 16)
                img = img.resize((max_w, new_h), Image.LANCZOS)
            # RGBA → QImage.Format_RGBA8888（PySide6 6.11 没有 Format_BGRA8888）
            img = img.convert("RGBA")
            rgba = img.tobytes("raw", "RGBA")
            qimg = QImage(rgba, img.size[0], img.size[1], QImage.Format_RGBA8888).copy()
            pix = QPixmap.fromImage(qimg)
            scaled = pix.scaled(self._captcha_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._captcha_label.setPixmap(scaled)
            self._captcha_label.setText("")
        except Exception as e:
            print(f"[login] captcha image decode failed: {type(e).__name__}: {e}")
            self._captcha_label.setText(f"<{len(payload)}B>")
        # 清空输入框
        self.captcha_field.clear()

    # --------------------------------------------------------------- 登录
    def _on_login_clicked(self):
        if self._in_flight:
            return
        username = self.username_field.text().strip()
        password = self.password_field.text()
        captcha = self.captcha_field.text().strip()
        if not username:
            self._show_error("请输入用户名")
            return
        if not password:
            self._show_error("请输入密码")
            return
        if not captcha:
            self._show_error("请输入验证码")
            return

        self._in_flight = True
        self._login_btn.setText("登录中…")
        self._login_btn.setEnabled(False)
        self._clear_error()
        # 暂存当前密码到 self（_on_login_done 异步回调里要用来记到 ui_prefs）
        self._pending_password = password
        self._login_signals = _LoginSignals()
        self._login_signals.finished.connect(self._on_login_done)
        self._pool.start(_LoginTask(username, password, captcha, self._uuid, self._login_signals))

    def _on_login_done(self, ok: bool, msg: str, data):
        self._in_flight = False
        self._login_btn.setText("登 录")
        self._login_btn.setEnabled(True)
        if not ok:
            self._show_error(msg)
            self._refresh_captcha()
            return
        username = data.get("username") or self.username_field.text().strip()
        # 写会话（业务不动）
        session.set(
            token=data["token"],
            expire_ms=data.get("expire_ms"),
            username=username,
        )
        # 保存 UI 偏好（独立文件，不动 session）
        # 只有勾选"记住账号密码"时才存密码
        pending_pwd = getattr(self, "_pending_password", "")
        if self.remember_cb.isChecked():
            self._save_ui_prefs(username, pending_pwd)
        else:
            self._save_ui_prefs(username, "")
        self.result = data
        self.finished.emit()
        self.close()

    # --------------------------------------------------------------- 错误
    def _show_error(self, msg: str):
        self._error_label.setText(msg)
        self._error_label.setFixedHeight(20)

    def _clear_error(self):
        self._error_label.setText("")
        self._error_label.setFixedHeight(20)


# ============================================================
# 直接 run 入口
# ============================================================
if __name__ == "__main__":
    setTheme(Theme.LIGHT)
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))

    win = LoginWindow()
    win.show()  # 阻塞，登录成功 / 关闭后返回
    if win.result:
        print("登录成功:", win.result)
    else:
        print("取消登录")
