#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
估价公式计算子进程池（解决 GUI 卡顿的关键）

背景：
  `formulas.ExcelModel.calculate()` 本身不快（单商品 0.5~5s），但真正卡 GUI 的
  不是公式求值，而是 `schedula` 的图调度（单次 calculate 里 `<genexpr>` 被调用
  2200 万次 + `builtins.all` 1.1 万次）——这段纯 Python CPU 密集代码长期持有 GIL，
  把 PySide6 主线程饿死，导致界面卡死。

  线程池救不了 GIL（多线程照样被同一个 GIL 卡住）。这里的做法是把计算下沉到
  `spawn` 子进程：每个子进程有自己的 Python 解释器 + 独立 GIL，主进程 GUI 线程
  不再被饥饿，心跳从「秒级」回落到「毫秒级」。

设计：
  1. `multiprocessing.Pool(initializer=_init_worker)` —— 每个 worker 进程启动时
     各自 `ExcelModel().loads(cache).finish()` 一次，模型存进程级全局，之后每笔
     只 `calculate()` 不重建。
  2. 主进程 `calculate_price` → `FormulaProcessPool.calculate()` → `Pool.apply`，
     把 (商品文本, 平台单元格映射) 序列化过去，子进程算完只把 `prices` 字典
     序列化回来（结果很小，pickle 开销可忽略）。
  3. 跨进程天然隔离：不同商品、不同平台的 calculate 在独立 GIL 里真并行。

注意（Windows spawn 约束）：
  - `_init_worker` / `_calc_one` 必须是模块顶层函数（可 pickle 按引用传递），
    因此不能写成闭包或 lambda。
  - 子进程会重新 import 本模块，所以本模块的 import 必须是轻量的（不 import
    PySide6 / config_manager / services 等重依赖），否则拖慢 worker 启动。
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import sys
from typing import Dict, Optional

# ★ 冻结的 windowed exe（console=False）里 sys.stdout / sys.stderr 为 None。
#   formulas 编译估价模型时用 tqdm 进度条往 stderr 写 → None.write 崩溃
#   （AttributeError: 'NoneType' object has no attribute 'write'）。
#   主入口 main.py 顶部已做统一兜底（UTF-8 devnull），这里再兜一次作为防御：
#   - 用 UTF-8 + errors=replace，避免 open(os.devnull) 默认 GBK 编码
#     在写 ✓ 等字符时抛 UnicodeEncodeError（曾导致 handleError 打印 traceback 崩溃）。
#   主进程和 spawn 出来的 worker 都会 import 本模块，两个进程都能被兜住。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)

# worker 进程数：每个 worker 都要独立编译一份估价模型（formulas 的模型内存占用高，
# 实测单份编译即吃数百 MB，N 份 = N× 内存）。默认取 1：既能靠「独立 GIL」彻底解决
# GUI 卡顿（旧线程方案受 GIL 限制，3 线程其实串行，1 进程吞吐不降反稳），又不额外
# 放大内存（与旧版「主进程单份模型」占用一致）。内存充裕、想提升吞吐时可设环境变量
# JX_FORMULA_WORKERS=2/3（每 +1 个 worker ≈ 多一份模型内存）。
def _default_workers() -> int:
    try:
        n = int(os.environ.get("JX_FORMULA_WORKERS", "") or 0)
        if n > 0:
            return n
    except ValueError:
        pass
    return 1

WORKERS = _default_workers()

# ---------------- 子进程全局状态（每个 worker 独立一份） ----------------
_model = None          # formulas.ExcelModel 实例
_base = None           # 缓存文件名（拼节点 key 用）
_build_error = None    # 子进程构建失败时存异常，calc 时抛给主进程


def _init_worker(cache_path: str) -> None:
    """Pool worker 启动时执行一次：加载 + 编译估价表模型。"""
    global _model, _base, _build_error
    try:
        from formulas import ExcelModel
        _model = ExcelModel().loads(cache_path).finish()
        _base = os.path.basename(cache_path)
        logger.info(f"[formula_process] worker 模型编译完成: {_base}")
    except Exception as e:  # noqa: BLE001
        _model = None
        _build_error = e
        logger.error(f"[formula_process] worker 模型编译失败: {e}", exc_info=True)


def _key(sheet: str, cell: str) -> str:
    # 与 formula_engine 原实现一致：中文表名无大小写，整体加单引号兼容含点文件名
    return f"'[{_base}]{sheet.upper()}'!{cell}"


def _unwrap_scalar(v):
    """把 formulas 返回的 [[620.0]] / numpy 数组解包成标量。"""
    if hasattr(v, 'tolist'):
        v = v.tolist()
    while isinstance(v, (list, tuple)):
        if len(v) == 0:
            return None
        if len(v) == 1:
            v = v[0]
        else:
            break
    return v


def _calc_one(args):
    """单商品估价（子进程内执行）。返回 {platform: price}。

    异常会冒泡到主进程的 `Pool.apply`，由 FormulaProcessPool.calculate 捕获。
    """
    product_info, cell_map = args
    if _model is None:
        raise RuntimeError(f"公式引擎子进程构建失败: {_build_error}")

    input_key = _key('收价文本', 'A1')
    output_keys = [_key('收价文本', cell) for cell in cell_map.values() if cell]

    sol = _model.calculate(
        inputs={input_key: product_info},
        outputs=output_keys,
    )

    prices: Dict[str, float] = {}
    for platform, cell in cell_map.items():
        if not cell:
            continue
        try:
            raw = sol[_key('收价文本', cell)]
            v = _unwrap_scalar(getattr(raw, 'value', raw))
        except Exception:  # noqa: BLE001
            v = None
        if v is None or v == '' or (isinstance(v, str) and v.startswith('#')):
            continue
        try:
            price = float(v)
        except (TypeError, ValueError):
            continue
        if price > 0:
            prices[platform] = round(price, 2)

    return prices


class FormulaProcessPool:
    """估价公式子进程池（spawn 隔离 GIL）。

    生命周期：
      start()      — 派生 WORKERS 个 worker，各自后台编译模型
      calculate()  — 阻塞提交一笔估价，返回 {platform: price}
      close()      — 终止并回收所有 worker（进程退出 / 换表时调用）
    """

    def __init__(self, cache_path: str, n_workers: int = WORKERS):
        self.cache_path = cache_path
        self.n_workers = n_workers
        self._pool = None

    def start(self) -> None:
        ctx = multiprocessing.get_context('spawn')
        self._pool = ctx.Pool(
            self.n_workers,
            initializer=_init_worker,
            initargs=(self.cache_path,),
        )
        logger.info(
            f"[formula_process] 已派生 {self.n_workers} 个估价 worker 进程"
        )

    def calculate(self, product_info, cell_map) -> Dict[str, float]:
        """阻塞提交一笔估价。异常向上抛出，由调用方转成 {success:False}。"""
        pool = self._pool
        if pool is None:
            raise RuntimeError("公式引擎子进程池未启动")
        return pool.apply(_calc_one, ((product_info, cell_map),))

    def close(self) -> None:
        pool = self._pool
        self._pool = None
        if pool is None:
            return
        try:
            # ★ 优雅关闭（close + join）而非 terminate：terminate 硬杀会让
            # 正在 pool.apply() 阻塞的线程永久卡死（apply 既不返回也不抛异常）。
            # close() 拒绝新任务、等已提交任务自然完成，join() 回收 worker 进程。
            pool.close()
            pool.join()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[formula_process] 关闭 worker 池异常: {e}")
