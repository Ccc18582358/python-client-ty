#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主程序入口

启动流程：
  1. 读 Session：本地有未过期 token → 直接进主窗口（自动登录）
  2. 否则：弹 LoginWindow；登录成功保存 token → 进主窗口；关闭则退出
  3. 主窗口关闭后：若 session 已被清掉（用户点了退出登录），回到登录页；否则退出整个程序

PySide6 模式下：
  - QApplication 全局单例，main 循环用 app.exec()
  - LoginWindow.show() 自带 QEventLoop 阻塞（登录窗自带 exec 语义）
  - MainWindow 不阻塞，靠 app.exec() 跑主消息循环
"""

import sys
import os
import multiprocessing
from pathlib import Path

# 让 import 走 python-client/ 根
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows 下 spawn 子进程（估价公式进程池）在打包成 exe 时需要，未打包时是 no-op
multiprocessing.freeze_support()

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont

from src.gui.theme import apply_theme


def run_app():
    from src.api.session import session

    app = QApplication(sys.argv)
    # 关掉主窗口不让 QApplication 自动退出 —— 我们自己控制循环
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    apply_theme("auto")

    # 全局 QSS 注入：让原生 QLabel / QLineEdit / QPushButton 等跟主题
    # （qfluentwidgets 自己的组件会自动跟，但业务里用了不少原生 Qt 控件）
    from src.gui.widgets.global_label_qss import apply_global_text_qss
    apply_global_text_qss(app)

    # 关键修复：patch qfluentwidgets FluentLabelBase 的 inline color
    # （它的 FluentLabelBase { color: white } 优先级 > 全局 QSS）
    from src.gui.widgets.fluent_color_patch import install as install_label_patch
    install_label_patch()

    while True:
        if not session.is_authenticated():
            from src.gui.login_window import LoginWindow
            login_win = LoginWindow()
            login_win.show()  # 阻塞
            if login_win.result is None:
                print("[main] 用户取消登录，退出")
                return
            print(f"[main] 登录成功：user={login_win.result.get('username')}")
        else:
            print(f"[main] 自动登录：user={session.username}")

        if not _launch_main_window(app):
            return

        # 主窗口关闭后：若 session 已被清掉（用户点了退出登录），回到登录页；否则退出整个程序
        if not session.is_authenticated():
            print("[main] 已退出登录，返回登录页")
            continue
        return


def _launch_main_window(app: QApplication) -> bool:
    """启动主窗口并阻塞到关闭。返回 False 表示需要彻底退出（异常）。"""
    from src.gui.main_window import MainWindow
    main_win = MainWindow()
    main_win.show()
    # 主窗口关闭后 app.exec() 返回
    app.exec()
    return True


if __name__ == "__main__":
    run_app()
