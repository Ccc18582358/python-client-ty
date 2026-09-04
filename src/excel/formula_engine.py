#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纯 Python 估价公式引擎（替代 Excel COM）

背景：
  原 ExcelCalculatorFinal 依赖 win32com + 本机安装 Office/WPS，且必须在
  主线程跑（COM 线程亲和），每次 CalculateFull() 阻塞 1~3s，是 GUI 卡顿的
  根因。这里用 `formulas` 库在本地解析 xlsx 公式并计算，彻底移除 Office 依赖。

实现：
  1. 一次性把估价表「裁剪 + 修复」缓存到临时目录（免 Office）：
     - 只保留估价相关工作表，删掉会导致 MemoryError 的幽灵表
       （"枪战手填表" 1048576 行、"金铲铲公式" 16384 列）
     - 清空 external_links（否则 formulas 报 D:/C: 盘符错误）
     - 去掉公式里的 [N] 跨工作簿引用、把 `--` 双负号换成等价的 `1*`
       （formulas 会把 `--` 折叠成 `+`，丢失布尔/文本转数字，导致 SUM=0）
  2. 公式计算下沉到 `spawn` 子进程池（见 formula_process）：`formulas` 的
     schedula 图调度是纯 Python CPU 密集代码，长期持有 GIL 会把 GUI 主线程饿死；
     移到子进程后每个 worker 有独立 GIL，主进程界面不再卡顿，多笔估价真并行。
  3. 每次估价：把商品文本写进 `收价文本!A1`，读各平台价格单元格。

线程安全 & 性能：
  calc 在独立子进程执行，无 GIL 争抢，主线程永不阻塞；跨进程用 pickle 传
  (商品文本, 单元格映射) → 返回 {平台: 价格}，数据量极小，开销可忽略。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import tempfile
from typing import Optional

from excel.formula_process import FormulaProcessPool

logger = logging.getLogger(__name__)

# 只保留估价相关表；其余幽灵表会导致 formulas 编译 MemoryError
KEEP_SHEETS = [
    '收价文本',
    '火影公式', '火影价格', '火影名称',
    '王者价格', '王者名称',
    '使命公式', '使命价格', '使命名称',
    '飞车公式', '飞车价格表', '飞车名称',
    '穿越公式', '穿越价格表', '穿越名称',
    '三角洲公式', '三角洲价格表', '三角洲名称',
]

# 跨工作簿外部引用前缀（如 [1]、[2]），openpyxl 读出后 formulas 无法解析
_EXTERNAL_REF = re.compile(r'\[\d+\]')

# 2.97 版「火影公式」的 A忍 VLOOKUP 误引用了外部工作簿 [3]Sheet1（悬空引用，表里并不存在
# Sheet1 这张表；3.0 版已修正为内置「火影价格」表）。就地替换成「火影价格」，否则 formulas
# 因找不到 Sheet1 而让整条火影价格链算成错误 → 收价文本!H3/I3/J3 全空。
# 注意：2.97 该引用还带 off-by-one 的区间 `$A$2:$AW$97`，但「青年达鲁伊」等最后几个
# A忍 在火影价格表第 98 行，97 行区间查不到 → VLOOKUP 返回 #N/A。故一并扩到 98 行。
_EXTERNAL_SHEET1 = re.compile(r'\[3\]Sheet1!\$A\$2:\$AW\$97', re.IGNORECASE)


def _fix_formula(text: str) -> str:
    """修复 2.97 悬空外部引用 + 去掉 [N] 前缀 + `--` → `1*`（保住类型转换）"""
    text = _EXTERNAL_SHEET1.sub('火影价格!$A$2:$AW$98', text)
    text = _EXTERNAL_REF.sub('', text)
    return text.replace('--', '1*')


def _build_trimmed(source_path: str, cache_path: str) -> None:
    """一次性：裁剪 + 修复估价表，输出到 cache_path（免 Office）。"""
    import openpyxl

    wb = openpyxl.load_workbook(source_path, data_only=False)
    # 清空外部链接，否则 formulas 会因跨盘符（D:/C:）报错
    wb._external_links = []
    for name in KEEP_SHEETS:
        ws = wb[name]
        for row in ws.iter_rows():
            for c in row:
                v = c.value
                if isinstance(v, str) and ('--' in v or '[' in v):
                    c.value = _fix_formula(v)
                elif v is not None and hasattr(v, 'text') and ('--' in v.text or '[' in v.text):
                    v.text = _fix_formula(v.text)
    for name in list(wb.sheetnames):
        if name not in KEEP_SHEETS:
            wb.remove(wb[name])
    wb.save(cache_path)
    wb.close()


class FormulaPriceEngine:
    """纯 Python 公式引擎（接口对齐 ExcelCalculatorFinal）

    只暴露 calculate_price / close，替换后上层代码无需改调用签名。
    """

    def __init__(self, excel_path: str):
        self.excel_path = str(excel_path)
        self._pool: Optional[FormulaProcessPool] = None
        self._build_error: Optional[BaseException] = None
        self._ready = threading.Event()
        # 后台预构建：不在调用方线程里做耗时裁剪/派生（避免 GUI 卡死）
        threading.Thread(target=self._build, name="formula-engine-build",
                         daemon=True).start()

    # ---------------- 构建 ----------------

    def _build(self) -> None:
        try:
            src = self.excel_path
            st = os.stat(src)
            # 缓存键：源文件 mtime + 大小（同文件复用裁剪产物，换表重新裁剪）
            cache_key = f"{st.st_mtime_ns}_{st.st_size}_{len(src)}"
            cache_path = os.path.join(tempfile.gettempdir(),
                                      f"jx_formula_{cache_key}.xlsx")
            if not os.path.exists(cache_path):
                logger.info(f"[formula_engine] 裁剪+修复估价表 -> {cache_path}")
                _build_trimmed(src, cache_path)

            # 派生子进程池：每个 worker 后台各自编译模型（不再在主进程持有模型）
            self._pool = FormulaProcessPool(cache_path)
            self._pool.start()
            logger.info("[formula_engine] 估价子进程池已启动")
        except Exception as e:  # noqa: BLE001
            self._build_error = e
            logger.error(f"[formula_engine] 构建失败: {e}", exc_info=True)
        finally:
            self._ready.set()

    # ---------------- 计算 ----------------

    def calculate_price(self, product_info, game_type="火影忍者", product_id=""):
        """计算各平台估价。返回 {success, prices, message}（与原 COM 版一致）。"""
        self._ready.wait()  # 首次调用等后台构建完成（裁剪 + 派生 worker）
        if self._build_error is not None:
            return {
                'success': False, 'prices': {},
                'message': f'公式引擎构建失败: {self._build_error}',
            }
        # 快照 pool 本地引用：close() 可能并发置空 self._pool（进程退出竞态）
        pool = self._pool
        if pool is None:
            return {'success': False, 'prices': {}, 'message': '公式引擎未就绪'}

        try:
            from config.config_manager import config_manager
            cell_map = config_manager.get_cell_map_for_game(game_type) or {}
        except Exception as e:  # noqa: BLE001
            logger.error(f"[formula_engine] 读取平台单元格配置失败: {e}")
            cell_map = {}

        if not cell_map:
            return {
                'success': False, 'prices': {},
                'message': f'未配置游戏「{game_type}」的平台单元格',
            }

        logger.info(
            f"[formula_engine] 计算价格 game_type={game_type} "
            f"product_id={product_id}"
        )
        try:
            prices = pool.calculate(product_info, cell_map)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[formula_engine] 计算异常: {e}", exc_info=True)
            return {'success': False, 'prices': {}, 'message': f'计算失败: {e}'}

        logger.info(f"[formula_engine] 计算结果: {prices}")
        if prices:
            return {'success': True, 'prices': prices, 'message': '计算成功'}
        return {'success': False, 'prices': {}, 'message': '未找到有效价格'}

    def close(self) -> None:
        """终止估价子进程池（换表 / 程序退出时回收 worker）"""
        pool = self._pool
        self._pool = None
        if pool is not None:
            try:
                pool.close()
            except Exception:  # noqa: BLE001
                pass


# ====================================================================
# 全局单例（对齐原 get_excel_calculator / get_current_excel_calc）
# ====================================================================
_engine_lock = threading.Lock()
_engine_instance: Optional[FormulaPriceEngine] = None
_engine_path: Optional[str] = None


def get_formula_engine(excel_path: str) -> Optional[FormulaPriceEngine]:
    """获取全局单例（按 excel_path 缓存）

    - excel_path 为空 / 文件不存在 → None
    - 同 path：复用（后台构建完成后直接可用）
    - 异 path：换新实例
    """
    if not excel_path or not os.path.exists(excel_path):
        return None

    with _engine_lock:
        global _engine_instance, _engine_path
        if (_engine_instance is not None and _engine_path == excel_path):
            return _engine_instance
        logger.info(f"[formula_engine] 切换估价表: {_engine_path} -> {excel_path}")
        if _engine_instance is not None:
            try:
                _engine_instance.close()
            except Exception:  # noqa: BLE001
                pass
        _engine_instance = FormulaPriceEngine(excel_path)
        _engine_path = excel_path
        return _engine_instance


def get_current_formula_engine() -> Optional[FormulaPriceEngine]:
    """返回当前存活的全局引擎（未初始化则 None）"""
    with _engine_lock:
        return _engine_instance


def close_formula_engine() -> None:
    """关闭并清空全局单例（主程序退出时调）"""
    with _engine_lock:
        global _engine_instance, _engine_path
        if _engine_instance is not None:
            try:
                _engine_instance.close()
            except Exception:  # noqa: BLE001
                pass
            _engine_instance = None
            _engine_path = None
            logger.info("[formula_engine] 全局单例已关闭")
