#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Excel计算器 - 最终版
"""

from __future__ import annotations  # 允许函数体内用 Optional[...] 注解（Python 3.14 严格检查）

import os
import time
import uuid
import logging
import threading
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_WINREG_SUPPRESSED = False


def _suppress_office_prompts():
    """写入注册表 + 杀僵尸进程，禁止 Office 弹窗。

    未激活的 Office 365/2016/2019 会周期性弹登录窗，
    即使 Visible=False + Interactive=False 也可能打断 COM 连接。
    """
    global _WINREG_SUPPRESSED
    if _WINREG_SUPPRESSED:
        return
    _WINREG_SUPPRESSED = True

    # 1. ★ 不再暴力杀所有 Excel（会杀掉用户正在编辑的文档，导致数据丢失）
    #    改为：仅依赖 COM 正常 Quit 关闭，僵尸进程由 close_excel_calculator() 处理
    #    如果 Excel 进程残留不是僵尸而是用户自己的窗口，taskkill /f 会破坏用户工作

    # 2. 注册表封堵所有已知 Office 弹窗（登录/激活/首次运行）
    try:
        import winreg
        for version in ('16.0', '15.0', '14.0'):
            for subkey, values in (
                # 禁用登录
                (rf'Software\Microsoft\Office\{version}\Common\SignIn',
                 {'SignInOptions': 3, 'DisableSignIn': 1, 'AcceptEULA': 1}),
                # 禁用首次运行向导
                (rf'Software\Microsoft\Office\{version}\Common\General',
                 {'ShownFirstRunOptin': 1, 'OptInDisableDownload': 1}),
                # 策略级：禁用登录
                (rf'Software\Policies\Microsoft\Office\{version}\Common\SignIn',
                 {'SignInOptions': 3, 'DisableSignIn': 1}),
                # 跳过首次运行（阻止激活弹窗）
                (rf'Software\Microsoft\Office\{version}\FirstRun',
                 {'BootedRTM': 1, 'DisableMovie': 1}),
            ):
                try:
                    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, subkey)
                    for vname, vval in values.items():
                        winreg.SetValueEx(key, vname, 0, winreg.REG_DWORD, vval)
                    winreg.CloseKey(key)
                except OSError:
                    pass
        logger.info("已封堵 Office 注册表弹窗（登录/激活/首次运行）")
    except Exception:
        pass


# ====================================================================
# Excel 进程单例：整个程序生命周期只启动一个后台 Excel 进程
# _create_excel_app() 幂等——已有一个存活实例就复用，绝不重复启动
# ★ 绝不 GetObject 连用户自己开的 Excel——只管理 subprocess 启动的后台实例
# ====================================================================
_excel_app_singleton = None       # win32com Excel.Application 对象


def _close_office_popups(target_pid: int = None):
    """关闭 Office 登录/激活弹窗（目标进程的顶层子窗口）

    Office 未激活时 COM 启动 Excel 可能弹出"登录以设置 Office"等提示窗。
    即使用注册表封堵，某些版本仍可能弹出，这里用 Windows API 自动关闭。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    windows_to_close = []

    def _enum_callback(hwnd, lParam):
        proc_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if target_pid is None or proc_id.value == target_pid:
            length = user32.GetWindowTextLengthW(hwnd)
            if 0 < length < 100:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                # 匹配 Office 登录/激活相关弹窗
                if title and any(kw in title for kw in (
                    '登录以设置 Office', '登录', '激活', 'Sign in', 'Activate',
                    'First things first', 'Accept', 'Enter your product key',
                )):
                    windows_to_close.append(hwnd)
        return True

    enum_proc = WNDENUMPROC(_enum_callback)
    user32.EnumWindows(enum_proc, 0)

    for hwnd in windows_to_close:
        logger.info(f"自动关闭 Office 弹窗: hwnd={hwnd}")
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    return len(windows_to_close) > 0
_excel_app_pid = None             # subprocess 启动的进程 pid


def _create_excel_app():
    """获取或创建唯一的 Excel Application（单例、幂等）。

    规则：
      1. _excel_app_singleton 还活着 → 直接返回
      2. 没有 → subprocess /e /automation 后台启动 → GetObject 连接
      3. 找不到 exe → DispatchEx 兜底
    """
    global _excel_app_singleton, _excel_app_pid
    import subprocess

    # ---- 已有存活实例：直接复用 ----
    if _excel_app_singleton is not None:
        try:
            _ = _excel_app_singleton.Name  # 快速探活
            logger.info("Excel 单例复用（已有实例存活）")
            return _excel_app_singleton
        except Exception:
            logger.info("Excel 单例已死，重新创建...")
            _excel_app_singleton = None
            _excel_app_pid = None

    # ---- 找 Excel.exe 路径 ----
    candidates = [
        r'C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE',
        r'C:\Program Files (x86)\Microsoft Office\root\Office16\EXCEL.EXE',
        r'C:\Program Files\Microsoft Office\Office16\EXCEL.EXE',
        r'C:\Program Files (x86)\Microsoft Office\Office16\EXCEL.EXE',
        r'C:\Program Files\Microsoft Office\Office15\EXCEL.EXE',
    ]
    import glob as _glob
    for pattern in (
        r'C:\Program Files\Microsoft Office\root\Office*\EXCEL.EXE',
        r'C:\Program Files (x86)\Microsoft Office\root\Office*\EXCEL.EXE',
    ):
        for hit in _glob.glob(pattern):
            if hit not in candidates:
                candidates.append(hit)

    excel_path = None
    for path in candidates:
        if os.path.isfile(path):
            excel_path = path
            break

    # ---- 启动 Excel 后台实例 ----
    if excel_path:
        # ★ 先杀掉残留的无窗口 Excel 僵尸（从上次异常退出留下的），避免 GetObject 连到它
        try:
            import subprocess as _sp
            result = _sp.run(
                ['tasklist', '/fi', 'IMAGENAME eq EXCEL.EXE', '/fo', 'csv', '/nh'],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().split('\n'):
                if 'EXCEL.EXE' in line:
                    parts = line.replace('"','').split(',')
                    if len(parts) >= 5 and parts[4].strip() == 'N/A':
                        try:
                            pid = int(parts[1].strip())
                            logger.info(f"清理僵尸 Excel 进程: pid={pid}")
                            _sp.run(['taskkill', '/f', '/pid', str(pid)],
                                   capture_output=True, timeout=5)
                        except Exception:
                            pass
        except Exception:
            pass

        logger.info(f"subprocess 启动 Excel 后台实例: {excel_path}")
        proc = subprocess.Popen(
            [excel_path, '/e', '/automation'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _excel_app_pid = proc.pid
        time.sleep(2)

        # ★ 用 GetObject 连接刚启动的实例。上面已清掉僵尸，ROT 里只有我们自己的
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            excel = win32com.client.GetObject(Class='Excel.Application')
            _excel_app_singleton = excel
            logger.info(f"Excel 单例创建成功（pid={_excel_app_pid}）")
            return excel
        except Exception as e:
            logger.warning(f"GetObject 连接失败: {e}")
            _excel_app_pid = None

    # ---- DispatchEx 兜底 ----
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        excel = win32com.client.DispatchEx('Excel.Application')
        _excel_app_singleton = excel
        logger.info("Excel 单例创建成功（DispatchEx 兜底）")
        return excel
    except Exception as e:
        logger.error(f"所有 Excel 启动方式均失败: {e}")
        raise


class ExcelCalculatorFinal:
    """Excel计算器 - 最终版"""
    
    def __init__(self, excel_path):
        """初始化"""
        self.excel_path = excel_path
        self.temp_path = None
        self.excel = None
        self.workbook = None
        self.lock = threading.Lock()
        self.com_initialized = False
        logger.info(f"ExcelCalculatorFinal 初始化, excel_path: {excel_path}")
        logger.info(f"Excel文件存在: {os.path.exists(excel_path)}")
        logger.info(f"Excel文件大小: {os.path.getsize(excel_path)} bytes")
        self._initialize_excel()
    
    def _initialize_excel(self):
        """初始化Excel实例"""
        try:
            logger.info("开始初始化Excel实例")
            
            # 检查模块是否可用
            import shutil
            logger.info("shutil模块导入成功")
            
            import win32com.client
            logger.info("win32com.client模块导入成功")
            
            import pythoncom
            logger.info("pythoncom模块导入成功")
            
            # 初始化COM库 - 使用多线程模式
            try:
                pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
                self.com_initialized = True
                logger.info("COM库初始化成功（多线程模式）")
            except Exception as e:
                logger.warning(f"多线程模式初始化失败，尝试单线程模式: {e}")
                try:
                    pythoncom.CoInitialize()
                    self.com_initialized = True
                    logger.info("COM库初始化成功（单线程模式）")
                except Exception as e2:
                    logger.error(f"COM库初始化失败: {e2}")
                    raise
            
            # 每个实例用独立临时文件（UUID），避免切换 Excel 时新旧实例抢同一把文件锁
            temp_dir = tempfile.gettempdir()
            self.temp_path = os.path.join(temp_dir, f"temp_calculation_{uuid.uuid4().hex[:8]}.xlsx")

            # 清理历史残留的旧临时文件（上一次切换/崩溃留下的）
            try:
                import glob as _g
                for _old in _g.glob(os.path.join(temp_dir, "temp_calculation_*.xlsx")):
                    if _old == self.temp_path:
                        continue
                    try:
                        os.remove(_old)
                        logger.info(f"清理残留临时文件: {_old}")
                    except Exception:
                        pass  # 还被别的进程锁着，下次再清
            except Exception:
                pass

            # 复制到临时目录
            shutil.copy(self.excel_path, self.temp_path)
            logger.info(f"已复制Excel文件到临时目录: {self.temp_path}")
            
            # 启动前：杀僵尸 + 注册表封堵
            _suppress_office_prompts()

            # 启动Excel应用（优先 subprocess /e 静默模式，避免弹窗卡死）
            logger.info("正在启动Excel应用...")
            self.excel = _create_excel_app()

            # ★ 启动后立即扫描关闭 Office 登录/激活弹窗
            _close_office_popups(_excel_app_pid)

            # 配置Excel（禁止弹窗——未激活的 Office 也适用）
            # ★ Visible 是核心属性，必须成功。失败就重试（最多 3 次，等 Excel 就绪）
            for _retry in range(3):
                try:
                    self.excel.Visible = False
                    break
                except Exception:
                    if _retry < 2:
                        time.sleep(1)
            # 其余属性容错处理
            _excel_props = [
                ('DisplayAlerts', False),
                ('AutomationSecurity', 3),
                ('AskToUpdateLinks', False),
                ('ScreenUpdating', False),
                ('DisplayStatusBar', False),
                ('EnableEvents', False),
                ('DisplayFormulaBar', False),
            ]
            for _prop, _val in _excel_props:
                try:
                    setattr(self.excel, _prop, _val)
                except Exception as _e:
                    logger.warning(f"设置 Excel.{_prop} 失败: {_e}")
            # Interactive=False 阻止 Excel 弹出任何对话框（登录/激活提示等）
            try:
                self.excel.Interactive = False
            except Exception:
                pass

            # ★ Office 未激活时可能连续弹出多个对话框（登录→激活→许可），
            #    循环扫 + 关，直到连续 3 次没发现新弹窗为止。
            _popup_streak = 0
            for _ in range(10):  # 最多 10 轮
                time.sleep(0.5)
                _found_before = _close_office_popups(_excel_app_pid)
                if not _found_before:
                    _popup_streak += 1
                    if _popup_streak >= 3:
                        break
                else:
                    _popup_streak = 0
            # 最后一轮后强制隐藏
            try:
                self.excel.Visible = False
            except Exception:
                pass

            logger.info("Excel配置完成")
            
            # 打开工作簿
            logger.info(f"正在打开工作簿: {os.path.abspath(self.temp_path)}")
            self.workbook = self.excel.Workbooks.Open(
                os.path.abspath(self.temp_path),
                UpdateLinks=0
            )
            logger.info("工作簿打开成功")

            # ★ Workbooks.Open 可能让 Excel 窗口重新可见，再隐藏一次
            try:
                self.excel.Visible = False
            except Exception:
                pass

            # 验证工作簿
            if self.workbook:
                logger.info(f"工作簿标题: {self.workbook.Name}")
                logger.info(f"工作表数量: {self.workbook.Sheets.Count}")
                sheet_names = [sheet.Name for sheet in self.workbook.Sheets]
                logger.info(f"工作表列表: {sheet_names}")
            
            logger.info("Excel实例初始化成功")
        except Exception as e:
            logger.error(f"Excel初始化失败: {str(e)}")
            import traceback
            logger.error(f"错误堆栈: {traceback.format_exc()}")
            # ★ 不要把 self.excel = None！否则 get_current_excel_calc() 返回 None，
            #   下次 worker tick 又会进 _initialize_excel 重复创建进程。
            #   改为：保留对单例 Excel 进程的引用，只清 workbook（可重新 Open）
            try:
                if _excel_app_singleton is not None:
                    try:
                        _ = _excel_app_singleton.Name  # 探活
                        self.excel = _excel_app_singleton
                    except Exception:
                        self.excel = None
                else:
                    self.excel = None
            except Exception:
                self.excel = None
            self.workbook = None
    
    def calculate_price(self, product_info, game_type="火影忍者", product_id=""):
        """使用Excel计算价格

        动态可配：游戏→sheet / 平台→cell 全部从 config_manager 读
        加新平台/游戏只需改 game_platform.json，无需改本文件

        COM 断连恢复：未激活 Office 可能弹窗导致 COM 断开，
        自动重连并重试一次。
        """
        with self.lock:
            # ★ 在使用 COM 前先清理可能刚弹出的 Office 登录/激活窗口
            _close_office_popups(_excel_app_pid)

            for attempt in range(2):  # 最多尝试 2 次（首次 + 1 次重连）
                try:
                    logger.info(f"开始计算价格, game_type: {game_type}, product_id={product_id}" +
                                (f" (重试)" if attempt > 0 else ""))

                    # ★ COM 已在 _initialize_excel 时初始化，重复调用会触发
                    #     "无法在设置线程模式后对其加以更改" 警告
                    if self.excel is None or self.workbook is None:
                        logger.error("Excel实例不可用，重新初始化")
                        self._initialize_excel()
                        if self.excel is None or self.workbook is None:
                            return {
                                'success': False,
                                'prices': {},
                                'message': 'Excel初始化失败'
                            }

                    # ===== 从 config_manager 读启用的平台 =====
                    try:
                        from config.config_manager import config_manager
                    except Exception:
                        config_manager = None

                    sheet_name = '收价文本'
                    logger.info(f"选择工作表: {sheet_name}")

                    sheet = self.workbook.Sheets(sheet_name)
                    logger.info(f"已选择'{sheet_name}'工作表")

                    sheet.Range('A1').Value = product_info
                    # 选中输入区外的单元格（H1），触发公式计算
                    sheet.Activate()
                    sheet.Range('H1').Select()
                    logger.info("执行Excel计算...")
                    self.excel.CalculateFull()
                    logger.info("Excel计算完成")
                    time.sleep(0.5)

                    platform_cell_map = {}
                    if config_manager is not None:
                        platform_cell_map = config_manager.get_cell_map_for_game(game_type) or {}

                    prices = {}
                    if config_manager is not None and platform_cell_map:
                        for platform_name, cell in platform_cell_map.items():
                            if not cell:
                                continue
                            try:
                                v = sheet.Range(cell).Value
                                logger.info(f"{platform_name} 价格 ({cell}): {v} [pid={product_id}]")
                                if v is not None and v != '':
                                    try:
                                        price = float(v)
                                        if price > 0:
                                            prices[platform_name] = round(price, 2)
                                    except (TypeError, ValueError):
                                        logger.warning(f"{platform_name}({cell}) 值非数字: {v}")
                            except Exception as e:
                                logger.error(f"读取{platform_name}({cell})价格失败: {e}")
                    else:
                        for name, cell in platform_cell_map.items():
                            try:
                                v = sheet.Range(cell).Value
                                if v is not None and v != '':
                                    price = float(v)
                                    if price > 0:
                                        prices[name] = round(price, 2)
                            except Exception:
                                pass

                    logger.info(f"计算结果: {prices}")

                    if prices:
                        return {'success': True, 'prices': prices, 'message': '计算成功'}
                    else:
                        logger.error(f"未找到有效价格 [product_id={product_id}]")
                        return {'success': False, 'prices': {}, 'message': '未找到有效价格'}

                except ImportError as e:
                    logger.error(f"缺少模块: {e}")
                    return {'success': False, 'prices': {}, 'message': f'缺少模块: {str(e)}'}
                except Exception as e:
                    # 判断是否为 COM 断连错误（可恢复）
                    is_disconnected = (
                        hasattr(e, 'hresult') and e.hresult in (-2147417848, -2147220992, -2147023174)
                    ) or any(kw in str(e) for kw in [
                        '断开连接', 'disconnected', '客户端已', 'RPC', '无效的类字符串'
                    ])

                    if is_disconnected and attempt == 0:
                        logger.warning(f"Excel COM 断开，尝试重连: {e}")
                        # 清理旧的 COM 引用并重新初始化
                        try:
                            if self.workbook:
                                self.workbook = None
                            if self.excel:
                                self.excel = None
                        except Exception:
                            pass
                        self._initialize_excel()
                        if self.excel is not None and self.workbook is not None:
                            logger.info("Excel 重连成功，重试计算")
                            continue  # 重试
                        else:
                            logger.error("Excel 重连失败")

                    logger.error(f"Excel计算失败: {str(e)}")
                    import traceback
                    logger.error(f"错误堆栈: {traceback.format_exc()}")
                    return {'success': False, 'prices': {}, 'message': f'计算失败: {str(e)}'}
    
    def close(self):
        """关闭Excel实例"""
        global _excel_app_singleton, _excel_app_pid
        try:
            logger.info("开始关闭Excel实例")
            if self.workbook:
                self.workbook.Close(SaveChanges=False)
                self.workbook = None
                logger.info("工作簿关闭成功")
            if self.excel:
                self.excel.Quit()
                self.excel = None
                # ★ 清理进程单例，下次 _create_excel_app() 会重新启动
                _excel_app_singleton = None
                _excel_app_pid = None
                logger.info("Excel应用关闭成功")
            if self.temp_path and os.path.exists(self.temp_path):
                try:
                    os.remove(self.temp_path)
                    logger.info(f"临时文件已删除: {self.temp_path}")
                except Exception as e:
                    logger.error(f"删除临时文件失败: {e}")
            if self.com_initialized:
                import pythoncom
                try:
                    pythoncom.CoUninitialize()
                    logger.info("COM库已反初始化")
                except:
                    pass
            logger.info("Excel实例已关闭")
        except Exception as e:
            logger.error(f"关闭Excel实例失败: {str(e)}")
    
    def is_deal(self, platform_price, original_price):
        """判断是否捡漏"""
        if original_price <= 0:
            return False, 0, 0

        if platform_price <= original_price:
            return False, 0, 0

        premium_amount = platform_price - original_price
        premium_percent = (premium_amount / original_price) * 100

        return True, premium_amount, premium_percent


# ====================================================================
# 全局单例（按 excel_path 缓存）
#
# 背景：原版每次 ExcelCalculatorFinal(excel_path) 都会：
#   1. shutil.copy 一份到 temp_calculation_<UUID>.xlsx
#   2. DispatchEx('Excel.Application')  开新进程
# 多个调用方（main_window / calc_page / 旧 api_server）就开 N 个 Excel 进程，
# 而且没人 close，进程只能等主程序退出才释放 → 任务管理器里残留几十个。
#
# 修复：
#   - get_excel_calculator(path) 按 path 缓存单例
#   - 同 path：直接返回已存在实例
#   - 异 path：关旧实例、建新实例
#   - close_excel_calculator()  在主程序退出时调
# ====================================================================
_excel_calc_lock = threading.Lock()
_excel_calc_instance: Optional[ExcelCalculatorFinal] = None
_excel_calc_path: Optional[str] = None


def get_excel_calculator(excel_path: str) -> Optional[ExcelCalculatorFinal]:
    """获取全局单例（按 excel_path 缓存）

    - excel_path 为空 / 文件不存在 → 返回 None
    - 同 path：返回已存在实例（避免重复开 Excel 进程）
    - 异 path：关旧实例、建新实例（用户换了估价表）
    - **自动检测并强制解除 sheetProtection**（带密码也行），让后续 A1 写入不被弹窗阻塞

    线程安全：内部加锁
    """
    if not excel_path or not os.path.exists(excel_path):
        return None

    with _excel_calc_lock:
        global _excel_calc_instance, _excel_calc_path

        # 上传/选择表格时先自动检测+破解保护（无保护直接返回；带密码的自动解除）
        # 只在缓存没命中时执行——避免每次 get 都重新扫 zip
        cache_hit = (
            _excel_calc_instance is not None
            and _excel_calc_path == excel_path
            and _excel_calc_instance.excel is not None
            and _excel_calc_instance.workbook is not None
        )
        if not cache_hit:
            try:
                # 优先用相对路径（同包），失败回退到绝对路径
                try:
                    from .excel_utils import auto_unprotect_if_needed
                except ImportError:
                    from excel.excel_utils import auto_unprotect_if_needed
                auto_unprotect_if_needed(excel_path)
            except Exception as e:
                # 破解失败不阻断主流程（可能文件不是 xlsx / 没权限 / 等）
                logger.warning(f"[excel_calculator] 自动破解保护失败（继续）: {e}")

        # 命中缓存：实例还活着 → 复用
        if (_excel_calc_instance is not None
                and _excel_calc_path == excel_path
                and _excel_calc_instance.excel is not None
                and _excel_calc_instance.workbook is not None):
            logger.info(f"[excel_calculator] 命中缓存，复用实例: {excel_path}")
            return _excel_calc_instance

        # ★ COM 进程还在但 workbook 丢了 → 重试打开工作簿，绝不杀进程
        if (_excel_calc_instance is not None
                and _excel_calc_path == excel_path
                and _excel_calc_instance.excel is not None):
            logger.info(f"[excel_calculator] workbook 丢失，重试打开（不重启进程）: {excel_path}")
            try:
                _excel_calc_instance._initialize_excel()
                if _excel_calc_instance.workbook is not None:
                    return _excel_calc_instance
            except Exception as e:
                logger.warning(f"[excel_calculator] 重试打开工作簿失败: {e}")

        # 彻底失败（异 path / 首次 / 实例死了）：关旧、建新
        if _excel_calc_instance is not None:
            logger.info(f"[excel_calculator] 切换 Excel 文件: {_excel_calc_path} -> {excel_path}")
            try:
                _excel_calc_instance.close()
            except Exception as e:
                logger.warning(f"[excel_calculator] 关闭旧 Excel 引擎失败: {e}")
            _excel_calc_instance = None
            _excel_calc_path = None
        try:
            _excel_calc_instance = ExcelCalculatorFinal(excel_path)
            _excel_calc_path = excel_path
        except Exception as e:
            logger.error(f"[excel_calculator] 创建 Excel 引擎失败: {e}")
            _excel_calc_instance = None
            _excel_calc_path = None
        return _excel_calc_instance


def close_excel_calculator() -> None:
    """关闭并清空全局单例（主程序退出时调）"""
    with _excel_calc_lock:
        global _excel_calc_instance, _excel_calc_path
        if _excel_calc_instance is not None:
            try:
                _excel_calc_instance.close()
            except Exception as e:
                logger.warning(f"[excel_calculator] 关闭 Excel 引擎失败: {e}")
            _excel_calc_instance = None
            _excel_calc_path = None
            logger.info("[excel_calculator] 全局单例已关闭")


def get_current_excel_calc() -> "Optional[ExcelCalculatorFinal]":
    """始终返回当前存活的全局 Excel 单例（如果单例死了返回 None）

    ★ 不要再缓存 self.excel_calc 之类的引用——可能全局实例被切换/关闭了。
    每次调用都查最新，避免使用过期的 COM 引用触发重复创建。
    """
    with _excel_calc_lock:
        if _excel_calc_instance is None:
            return None
        # 快速探活：excel 或 workbook 任一为 None 就当死了
        try:
            if _excel_calc_instance.excel is None or _excel_calc_instance.workbook is None:
                return None
            return _excel_calc_instance
        except Exception:
            return None
