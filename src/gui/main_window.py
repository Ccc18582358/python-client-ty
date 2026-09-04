#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扫号器主窗口 - FluentWindow + NavigationInterface。
4 个页面靠 addSubInterface 挂到左侧导航上，底部用 addUserCard 放用户名 + 主题切换。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ★ 日志系统必须最先初始化（在任何其他模块创建 logger 之前）
from utils.logger import setup_logging
_LOG_PATH = setup_logging()

from PySide6.QtCore import Qt, Slot, Signal, QObject
from PySide6.QtGui import QIcon, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication

from qfluentwidgets import (
    FluentWindow, FluentIcon as FIF, NavigationItemPosition,
    setTheme, Theme, MessageBox, InfoBar, InfoBarPosition,
)

from api.session import session
from database.db_manager_v2 import DBManagerV2
from gui.theme import apply_theme, toggle_theme, set_primary_color, set_global_font
from gui.pages import ScanTaskPage, DealPage, HistoryPage, CalcPage


# ============================================================
# 模块级信号总线（让无 MainWindow 引用的页面也能订阅估价事件）
# ============================================================
class _GlobalSignalBus(QObject):
    """全局信号总线（单例）

    4 元参数：db_id(int), product_id(str), platform(str), game_type(str)
    5 元参数：db_id(int), product_id(str), platform(str), game_type(str), success(bool)
    3 元参数：db_id(int), final_price(float), is_deal(bool)  —— 旧签名兼容
    2 元参数：title(str), body(str)  —— 付费墙告警
    """
    estimationStarted = Signal(int, str, str, str)
    estimationFinished = Signal(int, str, str, str, bool)
    product_estimated_signal = Signal(int, float, bool)
    paywallRequested = Signal(str, str)  # title, body
    proxyNotOpened = Signal(int, str)    # task_id, platform — 代理未开通弹窗


# 模块级单例：所有页面 / 控件都 import 这个
estimation_bus = _GlobalSignalBus()


class MainWindow(FluentWindow):
    """主窗口"""

    # 实例级信号（每个 MainWindow 实例有自己的 connect 槽）
    # 4 元参数：db_id(int), product_id(str), platform(str), game_type(str)
    estimationStarted = Signal(int, str, str, str)
    # 5 元参数：db_id(int), product_id(str), platform(str), game_type(str), success(bool)
    estimationFinished = Signal(int, str, str, str, bool)

    def __init__(self):
        super().__init__()
        # 品牌主色 + 主题：从 QSettings 恢复上次用户选择
        set_primary_color("#1677FF")
        apply_theme("default-from-settings")
        set_global_font("Microsoft YaHei UI", 10)

        self.db_manager = DBManagerV2()
        # ★ 每次启动 GUI 时清理一次 7 天前的旧数据（只在启动时做这一次）
        self.db_manager._cleanup_old_records()
        self.pages = {}

        # ★ 把 MainWindow 实例信号转发到全局总线 estimation_bus
        #   让所有页面（无需持 MainWindow 引用）都能订阅
        self.estimationStarted.connect(estimation_bus.estimationStarted)
        self.estimationFinished.connect(estimation_bus.estimationFinished)
        # ★ 同时把 5 元 estimationFinished 翻译成 3 元 product_estimated_signal（兼容旧页面）
        self.estimationFinished.connect(self._emit_product_estimated_signal)

        # 启动后台服务 + 初始化 UI
        self._post_init()

    @Slot(int, str, str, str, bool)
    def _emit_product_estimated_signal(self, db_id, product_id, platform, game_type, success):
        """把 5 元 estimationFinished 翻译成 3 元 product_estimated_signal（总线）

        反查 DB 拿 final_price + is_deal（按 db_id），并发出旧签名信号到 bus
        """
        try:
            p = self.db_manager.get_product_by_id_new(int(db_id))
            if p:
                estimation_bus.product_estimated_signal.emit(
                    int(db_id),
                    float(p.get("final_price", 0) or 0),
                    bool(p.get("is_deal", 0)),
                )
        except Exception:
            pass

    def _post_init(self):
        """__init__ 末尾统一调用：启动后台服务 + 初始化 UI"""
        # ===== 启动后台服务（Excel 引擎 + 估价 Worker + 调度器）=====
        self._start_background_services()

        self._init_window()
        self._init_pages()
        self._init_navigation()
        self._init_shortcuts()
        # ★ 挂付费墙告警总线（GUI 部分；钉钉告警在 install_paywall_notifier 时挂）
        estimation_bus.paywallRequested.connect(self._on_paywall)

        try:
            from services.proxy_alert_notifier import send_dingtalk_paywall
            from services.proxy_manager import install_paywall_notifier
            install_paywall_notifier(
                on_gui=lambda title, body: estimation_bus.paywallRequested.emit(title, body),
                # on_dingtalk=send_dingtalk_paywall,    ##钉钉告警
                throttle_seconds=300.0,
            )
        except Exception:
            # 启动期失败不阻断主流程（付费墙告警未挂载会降级到 log）
            pass

        # ★ 代理未开通回调（checkOpen 失败 → worker 线程 → 信号 → 主线程弹窗）
        try:
            from services.proxy_manager import set_check_open_failed_callback
            set_check_open_failed_callback(
                lambda task_id, platform: estimation_bus.proxyNotOpened.emit(task_id, platform)
            )
        except Exception:
            pass
        estimation_bus.proxyNotOpened.connect(self._on_proxy_not_opened)

        # ★ 代理开通校验不再在启动期同步请求
        # 改为延迟校验：首次取代理 IP 时由 ProxyManager 自动调 checkOpen
        # 避免启动时用户未登录 → token 为空 → 401 误报弹窗阻塞主窗口

    def _start_background_services(self):
        """启动 Excel 引擎、异步估价 Worker、调度器"""
        # 1) 公式引擎（纯 Python，用于 Worker 估价，免 Office）
        try:
            from excel.formula_engine import get_formula_engine
            from pathlib import Path
            from PySide6.QtCore import QSettings

            # 唯一路径规则：exe 根目录 + QSettings 存的文件名
            if getattr(sys, 'frozen', False):
                app_root = Path(sys.executable).parent
            else:
                app_root = Path(__file__).resolve().parent.parent.parent

            qs = QSettings("PriceMonitor", "PriceMonitorClient")
            saved_name = qs.value("excel/filename", "", type=str)
            if not saved_name:
                raise FileNotFoundError("用户还没上传 Excel 估价表")
            excel_path = str(app_root / saved_name)
            if not Path(excel_path).exists():
                raise FileNotFoundError(f"exe 根目录下找不到 {saved_name}")

            # 走全局单例：避免 main_window / calc_page 各建一个引擎
            self.excel_calc = get_formula_engine(excel_path)
            if self.excel_calc is None:
                raise FileNotFoundError(f"公式引擎初始化失败: {excel_path}")
            print(f"[MainWindow] 公式引擎已就绪（后台编译中）: {excel_path}")
        except Exception as e:
            print(f"[MainWindow] 公式引擎启动失败: {e}（估价将不可用）")
            self.excel_calc = None

        # 2) 异步估价 Worker（引擎没起来也能起，估价时再处理）
        try:
            from services.estimation_worker import init_estimation_worker
            if self.excel_calc is not None:
                self.estimation_worker = init_estimation_worker(self.db_manager, self.excel_calc)
                # ★ worker 直接在后台线程算价（不再回主线程调 calc），
                #   算完发 started/finished 信号 → 转发到全局总线
                self.estimation_worker.estimationStarted.connect(self.estimationStarted)
                self.estimation_worker.estimationFinished.connect(self.estimationFinished)
                print("[MainWindow] EstimationWorker 已启动（后台并发估价）")
            else:
                # 引擎没起来时不启动 Worker（避免一直写 estimated_error）
                # 用户配好 Excel 文件后重启 app 即可
                self.estimation_worker = None
                print("[MainWindow] 公式引擎未就绪，跳过 Worker 启动")
        except Exception as e:
            print(f"[MainWindow] Worker 启动失败: {e}")
            self.estimation_worker = None

        # 3) 调度器
        try:
            from services.scan_scheduler import init_scan_scheduler
            self.scan_scheduler = init_scan_scheduler(self.db_manager)
            print("[MainWindow] ScanScheduler 已启动")
        except Exception as e:
            print(f"[MainWindow] Scheduler 启动失败: {e}")
            self.scan_scheduler = None

        # 3.5) 导入爬虫（让 register_scanner 自动生效）
        # ============================================================
        # ★★★ 爬虫集成点 ★★★
        # 螃蟹(鲸汐 pxb7.com) 真实爬虫
        # 如需切换回演示模式，注释掉下面这行，取消注释 demo_scanner
        # 详见 docs/CRAWLER_INTEGRATION.md
        # ============================================================
        import logging as _logging
        _init_log = _logging.getLogger(__name__)

        try:
            import services.jingxi_scanner  # noqa: F401
            _init_log.info("[MainWindow] ✓ 螃蟹爬虫已注册")
        except Exception as e:
            _init_log.error("[MainWindow] ✗ 螃蟹爬虫模块导入失败: %s", e, exc_info=True)

        try:
            import services.jingxi_panzhi_scanner  # noqa: F401
            _init_log.info("[MainWindow] ✓ 盼之爬虫已注册")
        except Exception as e:
            _init_log.error("[MainWindow] ✗ 盼之爬虫模块导入失败: %s", e, exc_info=True)

        try:
            import services.jingxi_kejinshou_scanner  # noqa: F401
            _init_log.info("[MainWindow] ✓ 氪金兽爬虫已注册")
        except Exception as e:
            _init_log.error("[MainWindow] ✗ 氪金兽爬虫模块导入失败: %s", e, exc_info=True)

        try:
            import services.jingxi_7881_scanner  # noqa: F401
            _init_log.info("[MainWindow] ✓ 7881爬虫已注册")
        except Exception as e:
            _init_log.error("[MainWindow] ✗ 7881爬虫模块导入失败: %s", e, exc_info=True)

        # 汇总已注册爬虫
        from services.scanner_registry import get_scanners
        _scanner_names = [f.__name__ for f in get_scanners()]
        _init_log.info(
            "[MainWindow] 爬虫注册完成: 共 %d 个（%s）",
            len(_scanner_names), ", ".join(_scanner_names) if _scanner_names else "无",
        )

    def closeEvent(self, event):
        """关闭时停掉后台服务 + 释放公式引擎 + 退出事件循环"""
        try:
            from services.estimation_worker import get_estimation_worker
            w = get_estimation_worker()
            if w:
                w.stop()
        except Exception:
            pass
        try:
            from services.scan_scheduler import get_scan_scheduler
            s = get_scan_scheduler()
            if s:
                s.stop()
        except Exception:
            pass
        # 释放公式引擎（无 COM 进程，仅清空单例引用）
        try:
            from excel.formula_engine import close_formula_engine
            close_formula_engine()
        except Exception:
            pass
        super().closeEvent(event)
        # 必须手动退出事件循环：setQuitOnLastWindowClosed(False) 下 close() 不会触发 app.exec() 返回
        QApplication.quit()

    # ---- 窗口基础设置 ----
    def _init_window(self):
        self.setWindowTitle("账号估价助手")

        # 自适应桌面分辨率：宽 80%（限 1024-1680），高 80%（限 640-880）
        screen = QApplication.primaryScreen()
        rect = screen.availableGeometry()
        width = max(min(int(rect.width() * 0.80), 1680), 1024)
        height = max(min(int(rect.height() * 0.80), 880), 640)
        self.resize(width, height)
        x = (rect.width() - width) // 2
        y = max((rect.height() - height) // 2, 0)
        self.move(x, y)

        # 最小尺寸保护：避免窗口被拖得太小导致布局错乱
        self.setMinimumSize(960, 600)

        # 导航栏宽度（参考设计 220px）
        self.navigationInterface.setExpandWidth(220)

    # ---- 页面实例化 ----
    def _init_pages(self):
        self.pages["scan"] = ScanTaskPage(self, self.db_manager)
        self.pages["deal"] = DealPage(self, self.db_manager)
        self.pages["history"] = HistoryPage(self, self.db_manager)
        self.pages["calc"] = CalcPage(self, self.db_manager)

        # 调度器每 tick 完 → 通知扫号任务页刷新"下次执行"列
        try:
            if self.scan_scheduler is not None:
                self.scan_scheduler.tickFinished.connect(self._on_scheduler_tick)
        except Exception:
            pass

    def _on_scheduler_tick(self):
        """调度器每 tick 后回调：刷新扫号任务页"""
        try:
            page = self.pages.get("scan")
            if page is not None:
                page.refresh()
        except Exception:
            pass

    # ---- 左侧导航 ----
    def _init_navigation(self):
        # 顶部用户卡使用 Fluent 默认头像，避免展示客户定制 Logo。
        username = session.username or "未登录"
        self.navigationInterface.addUserCard(
            routeKey="user",
            avatar="",
            title=username,
            subtitle="已登录",
            onClick=self._on_user_card_click,
        )

        # 4 个主页面
        self.addSubInterface(self.pages["scan"], FIF.SEARCH, "扫描任务")
        self.addSubInterface(self.pages["deal"], FIF.HEART, "优选商品")
        self.addSubInterface(self.pages["history"], FIF.HISTORY, "历史记录")
        self.addSubInterface(self.pages["calc"], FIF.EDUCATION, "价格计算")

        # 底部：业务配置（游戏/平台管理）+ 主题切换 + 退出登录
        self.navigationInterface.addItem(
            routeKey="business_config",
            icon=FIF.SETTING,
            text="业务配置",
            onClick=self._on_business_config,
            position=NavigationItemPosition.BOTTOM,
        )
        self.navigationInterface.addItem(
            routeKey="theme",
            icon=FIF.CONSTRACT,
            text="深色模式",
            onClick=self._on_toggle_theme,
            position=NavigationItemPosition.BOTTOM,
        )
        self.navigationInterface.addItem(
            routeKey="logout",
            icon=FIF.POWER_BUTTON,
            text="退出登录",
            onClick=self._on_logout,
            position=NavigationItemPosition.BOTTOM,
        )

        # 默认显示扫号任务
        self.switchTo(self.pages["scan"])

    # ---- 顶部标题栏：放一个手动主题切换按钮（可选，已通过 BOTTOM 主题项实现） ----
    def _on_user_card_click(self):
        # 点击用户卡切换到「扫号任务」首页（占位，无独立 profile 页）
        self.switchTo(self.pages["scan"])

    def _on_business_config(self):
        """打开业务配置管理窗口（游戏/平台增删改）"""
        from gui.widgets.game_platform_manager import GamePlatformManager
        dlg = GamePlatformManager(self.window())
        dlg.exec()

    def _on_toggle_theme(self):
        new_mode = toggle_theme()
        # 同步左侧 BOTTOM 项文字（兼容 treeWidget / navigation 接口）
        try:
            tree = self.navigationInterface.treeWidget
            for i in range(tree.topLevelItemCount()):
                top_item = tree.topLevelItem(i)
                if top_item and top_item.text(1) in ("深色模式", "浅色模式"):
                    top_item.setText(1, "浅色模式" if new_mode == "Dark" else "深色模式")
                    break
        except Exception:
            # treeWidget 不存在（qfluentwidgets 高版本）—— 跳过
            pass

        # 关键修复：重新注入全局 QSS（让原生 QLabel/QLineEdit/QPushButton 切换）
        # 强制 unpolish → setStyleSheet → polish 让所有 widget 立即应用新 QSS
        app = QApplication.instance()
        from gui.widgets.global_label_qss import apply_global_text_qss
        apply_global_text_qss(app)

        # 关键修复：patch qfluentwidgets FluentLabelBase（inline color 优先 > QSS）
        from gui.widgets.fluent_color_patch import (
            _patch_all_existing, _patch_all_table_items
        )
        _patch_all_existing()
        _patch_all_table_items()

        # 通知所有页面刷新主题相关样式
        for page in self.pages.values():
            if hasattr(page, "on_theme_changed"):
                try:
                    page.on_theme_changed()
                except Exception:
                    pass
        # 关键修复：递归遍历每个 page 的整个 widget tree，
        # 调用所有子组件的 apply_theme()，让硬编码颜色也跟着切
        try:
            from gui.widgets.theme_propagate import propagate_theme
            for page in self.pages.values():
                propagate_theme(page)
        except Exception:
            pass

    def _on_logout(self):
        if not MessageBox(
            "退出登录", "确定要退出当前账号吗？", self
        ).exec():
            return
        # 重置代理模块单例（重新登录后会拿到新 token）
        try:
            from services.proxy_manager import reset_managers
            reset_managers()
        except Exception:
            pass
        session.clear()
        self.close()
        # 必须手动退出事件循环：setQuitOnLastWindowClosed(False) 之下
        # close() 不会触发 app.exec() 返回，得显式 quit
        QApplication.quit()

    @Slot(str, str)
    def _on_paywall(self, title: str, body: str):
        """付费墙 GUI 弹窗槽。

        由 ProxyPaywall 通过 estimation_bus.paywallRequested 投到这里；
        204 套餐过期 → 这里弹窗；205 余额耗尽走钉钉不弹窗。
        """
        try:
            MessageBox(title, body, self).exec()
        except Exception:
            # fallback: qfluentwidgets MessageBox 失败时用顶层 QMessageBox
            from qfluentwidgets import MessageBox as _MB
            _MB(title, body, self).exec()

    @Slot(int, str)
    def _on_proxy_not_opened(self, task_id: int, platform: str):
        """代理未开通 → 用户选择弹窗槽。

        由 ProxyManager._ensure_open_checked 通过回调 → signal 投递到这里。
        两个选项:
          - 取消任务: 停止当前扫号任务
          - 使用本机IP执行: 爬虫以直连继续（_not_opened 已生效）
        """
        from PySide6.QtWidgets import QMessageBox

        title = "代理服务未开通"
        body = (
            f"代理服务（{platform}平台）尚未开通。\n\n"
            f"当前扫号任务 #{task_id} 正在运行。\n"
            f"请选择如何处理："
        )

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setText(body)
        msg_box.setIcon(QMessageBox.Warning)

        btn_cancel = msg_box.addButton("取消任务", QMessageBox.DestructiveRole)
        btn_local = msg_box.addButton("使用本机IP执行", QMessageBox.AcceptRole)
        msg_box.setDefaultButton(btn_local)
        msg_box.setEscapeButton(btn_local)

        msg_box.exec()

        if msg_box.clickedButton() is btn_cancel:
            self._stop_all_scanners_for_task(task_id)
            logger.info(
                "[MainWindow] 用户取消任务 task_id=%s platform=%s",
                task_id, platform,
            )
        else:
            logger.info(
                "[MainWindow] 用户选择本机IP执行 task_id=%s platform=%s",
                task_id, platform,
            )

    def _stop_all_scanners_for_task(self, task_id: int):
        """停止所有爬虫平台中正在运行的指定任务，并更新 DB 状态 + UI。"""
        # 1. 发停止信号给所有爬虫线程
        scanner_modules = [
            "services.jingxi_scanner",
            "services.jingxi_7881_scanner",
            "services.jingxi_panzhi_scanner",
            "services.jingxi_kejinshou_scanner",
        ]
        for mod_name in scanner_modules:
            try:
                import importlib
                mod = importlib.import_module(mod_name)
                if hasattr(mod, "stop_task"):
                    mod.stop_task(task_id)
            except Exception:
                logger.exception(
                    "[MainWindow] 调用 %s.stop_task(%s) 失败", mod_name, task_id
                )

        # 2. 更新 DB：禁用任务
        try:
            self.db_manager.update_scan_task(task_id, enabled=0)
        except Exception:
            logger.exception("[MainWindow] 禁用任务 #%s 失败", task_id)

        # 3. 重载调度器（移除已禁用的任务）
        try:
            if hasattr(self, "scan_scheduler") and self.scan_scheduler:
                self.scan_scheduler.reload_tasks()
        except Exception:
            logger.exception("[MainWindow] 重载调度器失败")

    # ---- 全局快捷键 ----
    def _init_shortcuts(self):
        """注册全局快捷键

        F5          → 刷新当前页
        Ctrl+F      → 聚焦筛选
        Ctrl+1/2/3/4 → 切换 4 个页面
        """
        # F5 - 刷新当前页
        QShortcut(QKeySequence("F5"), self, activated=self._shortcut_refresh)

        # Ctrl+F - 聚焦筛选（不同页面有不同的筛选入口）
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._shortcut_focus_filter)

        # Ctrl+1..4 切换页面
        for i, key in enumerate(["scan", "deal", "history", "calc"], start=1):
            sc = QShortcut(QKeySequence(f"Ctrl+{i}"), self)
            sc.activated.connect(lambda k=key: self.switchTo(self.pages[k]))

    def _current_page(self):
        """返回当前显示的子页面（通过 stackedWidget 当前位置）"""
        try:
            stack = self.stackedWidget
            current = stack.currentWidget()
            for key, page in self.pages.items():
                if page is current:
                    return key, page
        except Exception:
            pass
        return None, None

    def _shortcut_refresh(self):
        _, page = self._current_page()
        if page is not None and hasattr(page, "_load_data"):
            page._load_data()
            notify(self, "已刷新", "", level="success", duration=800)

    def _shortcut_focus_filter(self):
        """Ctrl+F 聚焦到筛选控件：各页面按需实现 focus_filter()"""
        _, page = self._current_page()
        if page is not None and hasattr(page, "focus_filter"):
            page.focus_filter()

    def show_info(self, title: str, content: str = "", error: bool = False):
        """全局 InfoBar 通知（页面可调）"""
        if error:
            InfoBar.error(
                title=title, content=content,
                parent=self, position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
            )
        else:
            InfoBar.success(
                title=title, content=content,
                parent=self, position=InfoBarPosition.TOP_RIGHT,
                duration=2000,
            )
