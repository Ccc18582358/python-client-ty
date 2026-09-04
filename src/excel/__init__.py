"""excel 包：估价公式引擎

- `formula_engine` 唯一对外入口（get_formula_engine 单例，纯 Python 免 Office）
- 计算下沉到 spawn 子进程（formula_process）：独立 GIL 隔离，GUI 主线程不再被
  schedula 的 CPU 密集调度饿死，多笔估价不再卡界面
- 进程关闭时调 close_formula_engine() 释放单例（会回收估价子进程）

旧版 `excel_calculator_final`（win32com Excel COM）已弃用，仅保留作历史参考。
"""
