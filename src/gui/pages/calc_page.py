#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
价格计算页面（PySide6 + qfluentwidgets 版）

==== 纯 Python 公式引擎（无 COM 线程亲和） ====
估价走 FormulaPriceEngine（formulas 库），不依赖 Office，可在任意线程调用。
手动单条估价仍在主线程同步执行（~2s，用 WaitCursor + 状态文案反馈），
批量估价由 EstimationWorker 在后台线程并发跑，不阻塞 UI。
"""

import os
import sys
from pathlib import Path

# 把 src/ 注入 sys.path，让 api / database / config / excel 顶级包可解析
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit,
    QFileDialog, QMessageBox, QTableWidgetItem,
    QAbstractItemView, QHeaderView, QApplication,
)
from qfluentwidgets import (
    CardWidget, TableWidget, LineEdit, ComboBox,
    PrimaryPushButton, PushButton, FluentIcon as FIF, MessageBox,
)

from api.session import session  # noqa: F401  登录态注入点，保留以便后续按需读取
from database.db_manager_v2 import DBManagerV2  # 用作 db_manager 字段的类型注解
from config.config_manager import config_manager
from excel.formula_engine import get_formula_engine
from gui.theme import (
    text_primary, text_secondary, text_regular,
    COLOR_SUCCESS, COLOR_DANGER, COLOR_INFO,
)
from gui.widgets.page_title import PageTitle


class CalcPage(QWidget):
    """价格计算页面：选 Excel → 输入商品 → 后台线程估价 → 表格展示 → 落库"""

    # 后台线程算完回主线程的信号（跨线程自动 queued）
    _calc_done = Signal(object, str, str)   # (result, product_info, game_type)
    _calc_error = Signal(str)

    def __init__(self, parent=None, db_manager=None):
        super().__init__(parent)
        # FluentWindow.addSubInterface 需要 objectName
        self.setObjectName("CalcPage")

        # ---- 业务状态 ----
        self.db_manager: DBManagerV2 = db_manager
        self.calculator = None         # FormulaPriceEngine 实例（懒加载）
        self.excel_path = None         # 当前选中的 Excel 文件路径
        self.current_result = None     # 最近一次成功估价的快照（落库用）
        self.is_calculating = False    # 防重入

        self._calc_done.connect(self._on_calc_done)
        self._calc_error.connect(self._on_calc_error)

        self._build_ui()
        # 启动时尝试加载默认 Excel —— 找不到不报错，等用户手动选
        self._check_excel_file()

        # 监听业务配置变更（游戏下拉自动跟新）
        try:
            config_manager.configChanged.connect(self._on_config_changed)
        except Exception:
            pass

    def on_theme_changed(self):
        """主窗口切换主题时回调，刷新动态色文字"""
        self._refresh_dynamic_colors()

    def _on_config_changed(self, change_type: str):
        """游戏配置变了 → 刷新游戏下拉"""
        if change_type not in ('games', 'all'):
            return
        if hasattr(self, 'game_select'):
            cur = self.game_select.currentText()
            self.game_select.clear()
            self.game_select.addItems(config_manager.get_enabled_game_names())
            # 尽量恢复选中
            idx = self.game_select.findText(cur)
            if idx >= 0:
                self.game_select.setCurrentIndex(idx)
            else:
                self.game_select.setCurrentIndex(0)

    def _refresh_dynamic_colors(self):
        """把页面里所有用 _set_status_xxx 设的状态文字色重新应用"""
        # 标题、excel label、status label 都用动态色
        if hasattr(self, "status_label") and self.status_label.text():
            # 状态色根据当前 text 决定
            t = self.status_label.text()
            if "成功" in t or "完成" in t:
                self._set_status_style(COLOR_SUCCESS)
            elif "失败" in t or "错误" in t:
                self._set_status_style(COLOR_DANGER)
            elif "计算中" in t:
                self._set_status_style(COLOR_INFO)
            else:
                self._set_status_style(text_regular())
        if hasattr(self, "_title_widget"):
            self._title_widget.apply_title_color()

    # ================== UI 构建 ==================
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        # 标题区（大图标 + 标题 + 副标题）
        self._title_widget = PageTitle(
            icon=FIF.SHOPPING_CART,
            title="价格计算",
            subtitle="从 Excel 导入数据或粘贴商品描述，自动计算各平台最低价",
        )
        self.title_label = self._title_widget.title_label
        self.subtitle_label = self._title_widget.subtitle_label
        root.addWidget(self._title_widget)

        # 商品信息卡片（高度自适应）
        root.addWidget(self._build_input_card())

        # 计算结果卡片（限制最大高度，避免拉伸过大）
        result_card = self._build_result_card()
        root.addWidget(result_card)
        # 设置最大高度，让结果卡按内容自适应，最多占父容器一半
        result_card.setMaximumHeight(280)

    def _build_input_card(self) -> CardWidget:
        """商品信息卡：游戏类型选择 + Excel 选择 + 商品文本框 + 操作按钮"""
        card = CardWidget(self)
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(12)

        # ---- 行1：游戏类型 + 选 Excel + 文件名 ----
        row1 = QHBoxLayout()
        row1.setSpacing(8)

        row1.addWidget(QLabel("游戏类型："))

        self.game_select = ComboBox()
        self.game_select.addItems(config_manager.get_enabled_game_names())
        self.game_select.setMinimumWidth(160)
        row1.addWidget(self.game_select)

        row1.addSpacing(16)

        self.select_excel_btn = PrimaryPushButton(FIF.FOLDER, "选择Excel")
        self.select_excel_btn.clicked.connect(self._select_excel)
        row1.addWidget(self.select_excel_btn)

        self.excel_label = QLabel("未选择")
        # 不显式 setStyleSheet，让全局 QSS 接管（深色模式自动变白）
        row1.addWidget(self.excel_label)

        row1.addStretch(1)
        v.addLayout(row1)

        # ---- 行2：商品信息文本框 ----
        v.addWidget(QLabel("商品信息："))
        self.product_text = QTextEdit()
        self.product_text.setFixedHeight(120)
        self.product_text.setPlaceholderText("粘贴商品描述...")
        v.addWidget(self.product_text)

        # ---- 行3：操作按钮 + 状态 ----
        row3 = QHBoxLayout()
        row3.setSpacing(12)

        self.calc_btn = PrimaryPushButton(FIF.PLAY, "开始计算")
        self.calc_btn.clicked.connect(self._start_calculation)
        row3.addWidget(self.calc_btn)

        self.clear_btn = PushButton(FIF.DELETE, "清空")
        self.clear_btn.clicked.connect(self._clear)
        row3.addWidget(self.clear_btn)

        self.status_label = QLabel("")
        # 不显式 setStyleSheet，让全局 QSS 接管
        row3.addWidget(self.status_label)

        row3.addStretch(1)
        v.addLayout(row3)

        return card

    def _build_result_card(self) -> CardWidget:
        """计算结果卡：4 列表格 + 保存按钮"""
        card = CardWidget(self)
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(12)

        # ---- 4 列表格 ----
        self.result_table = TableWidget(card)
        self.result_table.setColumnCount(4)
        self.result_table.setHorizontalHeaderLabels(["平台", "价格", "溢价", "溢价率"])
        self.result_table.setRowCount(0)

        # qfluentwidgets 特性：边框 + 圆角
        self.result_table.setBorderVisible(True)
        self.result_table.setBorderRadius(8)

        # 视觉清理：隐藏行号 + 关网格 + 行只读 + 整行选择
        self.result_table.verticalHeader().hide()
        self.result_table.setShowGrid(False)
        self.result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectRows)

        # 4 列等宽拉伸（基础列宽 100，weight 由 Stretch 模式接管）
        header = self.result_table.horizontalHeader()
        for i in range(4):
            self.result_table.setColumnWidth(i, 100)
            header.setSectionResizeMode(i, QHeaderView.Stretch)

        # 行高 40
        self.result_table.verticalHeader().setDefaultSectionSize(40)

        v.addWidget(self.result_table, 1)

        # ---- 保存按钮 ----
        btn_row = QHBoxLayout()
        self.save_btn = PrimaryPushButton(FIF.SAVE, "保存到历史记录")
        self.save_btn.clicked.connect(self._save_to_history)
        btn_row.addWidget(self.save_btn)
        btn_row.addStretch(1)
        v.addLayout(btn_row)

        return card

    # ================== 业务方法（保留原逻辑） ==================
    def _app_root(self) -> str:
        """获取 exe/项目根目录：frozen 模式 = sys.executable 同级，开发模式 = 项目根"""
        if getattr(sys, 'frozen', False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

    def _check_excel_file(self):
        """启动时加载 Excel：唯一路径 = exe 根目录 + QSettings 存的文件名

        找不到 → 静默等用户手选（不兜底，不查 d:\\word）
        """
        from PySide6.QtCore import QSettings
        qs = QSettings("PriceMonitor", "PriceMonitorClient")

        app_root = self._app_root()
        saved_name = qs.value("excel/filename", "", type=str)
        if not saved_name:
            return  # 没上传过，等用户点"选择 Excel"

        candidate = os.path.join(app_root, saved_name)
        if os.path.exists(candidate):
            self.excel_path = candidate
            self.excel_label.setText(saved_name)
            self.excel_label.setStyleSheet(f"color: {text_primary()};")
            self._init_excel_calculator()

    def _select_excel(self):
        """文件选择对话框：上传 = 复制到 exe 根目录 + 记录文件名

        唯一路径规则：
        - frozen: sys.executable 同级
        - dev: 项目根目录
        QSettings 只存"文件名"（不存绝对路径），重启后 exe 根目录 + 文件名 拼出
        """
        from PySide6.QtCore import QSettings
        import shutil
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择Excel文件",
            "",
            "Excel 文件 (*.xlsx *.xls);;所有文件 (*.*)",
        )
        if not file_path:
            return

        app_root = self._app_root()
        try:
            dst_name = os.path.basename(file_path)
            dst_path = os.path.join(app_root, dst_name)
            # 已经在根目录就不复制；否则复制过去
            if os.path.normcase(os.path.abspath(file_path)) != os.path.normcase(os.path.abspath(dst_path)):
                shutil.copy2(file_path, dst_path)
                print(f"[calc_page] 已复制 Excel 到根目录: {dst_path}")

            # 持久化：只存"根目录下的文件名"
            qs = QSettings("PriceMonitor", "PriceMonitorClient")
            qs.setValue("excel/filename", dst_name)
            qs.sync()

            self.excel_path = dst_path
            self.excel_label.setText(dst_name)
            self.excel_label.setStyleSheet(f"color: {text_primary()};")
            self._init_excel_calculator()
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "上传失败", f"复制 Excel 到根目录失败：\n{e}")

    def _init_excel_calculator(self):
        """实例化公式引擎 —— 走全局单例（纯 Python，无需主线程 / Office）"""
        try:
            # 走单例：同 path 不重建，异 path 自动关旧建新
            self.calculator = get_formula_engine(self.excel_path)
            if self.calculator is None:
                self.status_label.setText(f"Excel 路径无效: {self.excel_path}")
                self._set_status_style(COLOR_DANGER)
            else:
                self.status_label.setText("公式引擎初始化成功")
                self._set_status_style(COLOR_SUCCESS)
                # ★ 关键：引擎单例创建成功后，重启估价 Worker
                #   避免"启动时没引擎 → Worker=None → 导入 Excel → 全部待估价"
                self._restart_estimation_worker()
        except Exception as e:
            self.status_label.setText(f"公式引擎初始化失败: {str(e)}")
            self._set_status_style(COLOR_DANGER)
            self.calculator = None

    def _restart_estimation_worker(self):
        """引擎就绪后重启估价 Worker（如果之前未启动或 calc 换了）。"""
        try:
            from services.estimation_worker import init_estimation_worker
            main_win = self.window()
            if main_win is None or not hasattr(main_win, 'estimationStarted'):
                return

            worker = init_estimation_worker(self.db_manager, self.calculator)
            # 连接信号（Qt 自动去重同 signal-slot 对）
            worker.estimationStarted.connect(main_win.estimationStarted)
            worker.estimationFinished.connect(main_win.estimationFinished)
            # 更新 MainWindow 引用
            main_win.estimation_worker = worker
            main_win.excel_calc = self.calculator
            print("[CalcPage] EstimationWorker 已重启 + 信号已接")
        except Exception as e:
            print(f"[CalcPage] Worker 重启失败: {e}")

    def _start_calculation(self):
        """触发一次估价

        ★ 必须主线程同步执行 ★
        Excel COM 对象创建时绑定到主线程，跨线程调用会触发
        `pywintypes.com_error: Invalid call`。
        """
        if self.is_calculating:
            self._notify("提示", "正在计算中，请稍候...")
            return

        game_type = self.game_select.currentText().strip()
        if not game_type:
            self._notify("提示", "请选择游戏类型")
            return

        product_info = self.product_text.toPlainText().strip()
        if not product_info:
            self._notify("提示", "请输入商品信息")
            return

        if not self.calculator:
            self._notify("提示", "请先选择 Excel 文件")
            return

        # 进入计算态：禁重入 + 提示文案 + 等待光标
        self.is_calculating = True
        self.status_label.setText("计算中...")
        self._set_status_style(COLOR_INFO)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()  # 让 "计算中..." 立即上屏

        # ★ 后台线程计算：纯 Python 引擎线程安全，绝不阻塞主线程
        #   （首次点击若引擎还在后台编译，会在线程里等编译完成，UI 仍可操作）
        import threading
        threading.Thread(
            target=self._calc_worker,
            args=(product_info, game_type),
            daemon=True,
        ).start()

    def _calc_worker(self, product_info, game_type):
        """后台线程：调公式引擎算价，算完/出错发信号回主线程刷新 UI"""
        try:
            result = self.calculator.calculate_price(
                product_info, game_type, product_id="manual",
            )
            self._calc_done.emit(result, product_info, game_type)
        except Exception as e:
            self._calc_error.emit(str(e))

    @Slot(object, str, str)
    def _on_calc_done(self, result, product_info, game_type):
        QApplication.restoreOverrideCursor()
        self.is_calculating = False
        self._show_result(result, product_info, game_type)

    @Slot(str)
    def _on_calc_error(self, msg):
        QApplication.restoreOverrideCursor()
        self.is_calculating = False
        self._show_error(msg)

    def _show_result(self, result, product_info, game_type):
        """把 calculator 返回值渲染成表格 + 状态文案

        结果数据结构（与原版保持一致，不要改）：
            self.current_result = {
                'product_info': str,
                'game_type':    str,
                'original_price': float,
                'prices':       dict[platform_name -> price],
            }
        """
        # 清空旧结果
        self.result_table.setRowCount(0)

        if not result.get('success'):
            msg = result.get('message', '未知错误')
            self.status_label.setText(f"失败: {msg}")
            self._set_status_style(COLOR_DANGER)
            return

        prices = result.get('prices', {})
        original_price = result.get('original_price', 0)

        # 保留原数据结构 —— 落库时直接读
        self.current_result = {
            'product_info': product_info,
            'game_type': game_type,
            'original_price': original_price,
            'prices': prices,
        }

        # 渲染表格
        self.result_table.setRowCount(len(prices))
        for row, (platform, price) in enumerate(prices.items()):
            # 溢价 = 估价高出原价的金额，估价 < 原价时为 0（不显示负数）
            premium = max(0.0, price - original_price)
            percent = (premium / original_price * 100) if original_price > 0 else 0

            cells = [
                platform,
                f"¥{price:.2f}",
                f"¥{premium:.2f}",
                f"{percent:.1f}%",
            ]
            for col, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                item.setTextAlignment(Qt.AlignCenter)
                self.result_table.setItem(row, col, item)

        self.status_label.setText("计算完成")
        self._set_status_style(COLOR_SUCCESS)

    def _show_error(self, error_msg):
        """计算异常的统一处理：状态变红 + 弹窗"""
        self.status_label.setText(f"失败: {error_msg}")
        self._set_status_style(COLOR_DANGER)
        self._notify("错误", f"计算失败: {error_msg}")

    def _clear(self):
        """清空输入文本 + 结果表格 + 状态 + 缓存"""
        self.product_text.clear()
        self.result_table.setRowCount(0)
        self._restore_status()
        self.current_result = None

    def _restore_status(self):
        """把状态栏恢复成中性提示色"""
        self.status_label.setText("")
        self._set_status_style(text_regular())

    def _set_status_style(self, color: str):
        """统一应用状态栏颜色，便于主题切换时统一刷新"""
        self.status_label.setStyleSheet(f"color: {color};")

    def _save_to_history(self):
        """把当前 calc 结果落库

        注意：db_manager.save_product 的调用签名不允许修改，参数顺序 / 关键字必须严格一致：
            (product_info, original_price, prices, url="", game_type="")
        """
        if not self.current_result:
            self._notify("提示", "没有可保存的结果")
            return

        if self.db_manager is None:
            self._notify("提示", "数据库未初始化")
            return

        result = self.current_result
        self.db_manager.save_product(
            product_info=result['product_info'],
            original_price=result['original_price'],
            prices=result['prices'],
            url="",
            game_type=result['game_type'],
        )
        self._notify("成功", "已保存到历史记录")

    def on_show(self):
        """页面切到前台时被 main_window 调用 —— 估价页无需刷新数据，留空占位"""
        pass

    # ================== 私有工具 ==================
    def _notify(self, title: str, content: str):
        """统一的信息弹窗（基于 qfluentwidgets.MessageBox）

        MessageBox 默认带 Yes/Cancel 两个按钮，对纯通知场景把 Cancel 隐掉，
        只留 "确定" 一个按钮，避免视觉噪音。
        """
        box = MessageBox(title, content, self)
        box.yesButton.setText("确定")
        # 不同 qfluentwidgets 版本属性名可能略有差异，做下保护
        if hasattr(box, "cancelButton"):
            box.cancelButton.hide()
        box.exec()


# ---- 兼容性 import 占位 ----
# QMessageBox / LineEdit 由上级规范要求保留在 import 列表里，此处显式 ref 一下，
# 防止 IDE / linter 误判为未使用而被自动删除
_ = (QMessageBox, LineEdit)
