#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API服务器
"""

import time
import threading
import sys
import os
from flask import Flask, request, jsonify
from flask_cors import CORS

from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# 延迟导入，避免启动时失败
# from excel.excel_calculator_final import ExcelCalculatorFinal
from database.db_manager_v2 import DBManagerV2

app = Flask(__name__)
CORS(app)

# 全局变量
calculator = None
db_manager = DBManagerV2()
calculator_lock = threading.Lock()

# 并发限制
MAX_CONCURRENT_REQUESTS = 5
current_requests = 0
request_lock = threading.Lock()

def get_excel_path():
    """获取 Excel 文件路径——唯一来源 = exe 根目录 + QSettings 存的文件名

    唯一路径规则：
    - frozen: sys.executable 同级
    - dev: 项目根目录
    QSettings 只存"文件名"（不存绝对路径），重启后 exe 根目录 + 文件名 拼出
    """
    from PySide6.QtCore import QSettings
    qs = QSettings("PriceMonitor", "PriceMonitorClient")
    saved_name = qs.value("excel/filename", "", type=str)
    if not saved_name:
        return None

    if getattr(sys, 'frozen', False):
        app_root = Path(sys.executable).parent
    else:
        app_root = Path(__file__).resolve().parent.parent.parent

    candidate = app_root / saved_name
    return str(candidate) if candidate.exists() else None

def get_calculator():
    """获取或初始化计算器（延迟初始化）

    走 formula_engine 的全局单例（纯 Python，免 Office）。
    """
    global calculator
    with calculator_lock:
        if calculator is None:
            excel_path = get_excel_path()
            if excel_path and os.path.exists(excel_path):
                try:
                    # 走全局单例（按 path 缓存）
                    from excel.formula_engine import get_formula_engine
                    calculator = get_formula_engine(excel_path)
                    if calculator is not None:
                        print(f"公式引擎初始化成功: {excel_path}")
                    else:
                        print(f"公式引擎初始化失败: get_formula_engine 返回 None")
                except Exception as e:
                    print(f"公式引擎初始化失败: {e}")
                    import traceback
                    print(f"错误堆栈: {traceback.format_exc()}")
                    calculator = None
            else:
                print(f"未找到Excel文件: {excel_path}")
        return calculator

def rate_limit(func):
    def rate_limit_wrapper(*args, **kwargs):
        global current_requests
        with request_lock:
            if current_requests >= MAX_CONCURRENT_REQUESTS:
                return jsonify({
                    "code": 429,
                    "msg": "请求过于频繁，请稍后再试",
                    "data": None
                })
            current_requests += 1
        
        try:
            return func(*args, **kwargs)
        finally:
            with request_lock:
                current_requests -= 1
    rate_limit_wrapper.__name__ = func.__name__
    return rate_limit_wrapper

@app.route('/api/scanner/callback', methods=['POST'])
@rate_limit
def scanner_callback():
    """外部 Python 扫号接口回调（异步估价）

    Payload:
        {
            "task_id": 123,                   # 扫号任务 ID
            "platform": "螃蟹",               # 这次回调来自哪个平台
            "results": [
                {
                    "product_id": "pangxie_xxx",  # 外部商品 ID（去重 key）
                    "product_info": "...",
                    "original_price": 100.0,
                    "url": "...",
                    "game_type": "...",
                    "supports_realname": true
                },
                ...
            ]
        }

    Returns:
        200 {code:0, data:{queued: N}} 立即返回
        异步：Worker 调 Excel + 议价公式 + 捡漏判断 → 更新 DB
    """
    try:
        payload = request.get_json() or {}
        task_id = payload.get('task_id')
        platform = payload.get('platform')
        results = payload.get('results', [])

        # 1. 校验
        if not task_id:
            return jsonify({"code": 400, "msg": "task_id 必填", "data": None})
        if not platform:
            return jsonify({"code": 400, "msg": "platform 必填", "data": None})
        task = db_manager.get_scan_task(int(task_id))
        if not task:
            return jsonify({"code": 404, "msg": f"task {task_id} 不存在", "data": None})
        if not results:
            return jsonify({"code": 0, "msg": "no results", "data": {"queued": 0}})

        # 2. 落库 estimated=0
        pending_ids = []
        for r in results:
            try:
                db_id = db_manager.save_product_pending(
                    product_info=r.get('product_info', '') or '',
                    original_price=float(r.get('original_price', 0) or 0),
                    url=r.get('url', '') or '',
                    game_type=r.get('game_type', '') or '',
                    product_id=r.get('product_id', '') or '',
                    task_id=int(task_id),
                    crawled_platform=platform,
                    supports_realname=bool(r.get('supports_realname', False)),
                )
                pending_ids.append(db_id)
            except Exception as e:
                print(f"[scanner_callback] 落库单条失败: {e}")

        # 3. 推入 Worker 队列（异步估价）
        try:
            from services.estimation_worker import get_estimation_worker
            worker = get_estimation_worker()
            if worker:
                worker.enqueue(pending_ids)
        except Exception as e:
            print(f"[scanner_callback] 推队列失败（落库已成功）: {e}")

        return jsonify({
            "code": 0,
            "msg": "accepted",
            "data": {"queued": len(pending_ids)}
        })
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": f"server error: {str(e)}",
            "data": None
        })

@app.route('/api/deals', methods=['GET'])
@rate_limit
def get_deals():
    """获取捡漏商品列表"""
    try:
        page = int(request.args.get('page', 1))
        page_size = int(request.args.get('page_size', 10))
        
        products, total = db_manager.get_deal_products(page, page_size)

        deals = []
        for product in products:
            # 通用 prices：优先 prices_json，fallback 4 列老 schema
            prices = db_manager._row_to_prices(product)
            deals.append({
                "id": product['id'],
                "original_price": product['original_price'],
                "prices": prices,  # 通用 {平台名: 价格}
                "url": product['url'],
                "created_at": product['created_at']
            })
        
        return jsonify({
            "code": 0,
            "msg": "success",
            "data": {
                "deals": deals,
                "total": total,
                "page": page,
                "page_size": page_size
            }
        })
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": f"获取捡漏商品失败: {str(e)}",
            "data": None
        })

@app.route('/api/history', methods=['GET'])
@rate_limit
def get_history():
    """获取历史记录"""
    try:
        page = int(request.args.get('page', 1))
        page_size = int(request.args.get('page_size', 10))
        game_type = request.args.get('game_type')
        
        products, total = db_manager.get_products(page, page_size, game_type=game_type)

        history = []
        for product in products:
            # 通用 prices：优先 prices_json，fallback 4 列老 schema
            prices = db_manager._row_to_prices(product)
            history.append({
                "id": product['id'],
                "info": product['info'],
                "original_price": product['original_price'],
                "prices": prices,  # 通用 {平台名: 价格}
                "url": product['url'],
                "game_type": product['game_type'],
                "created_at": product['created_at']
            })
        
        return jsonify({
            "code": 0,
            "msg": "success",
            "data": {
                "history": history,
                "total": total,
                "page": page,
                "page_size": page_size
            }
        })
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": f"获取历史记录失败: {str(e)}",
            "data": None
        })

@app.route('/api/health', methods=['GET'])
def health_check():
    """健康检查接口"""
    calc = get_calculator()
    return jsonify({
        "code": 0,
        "msg": "success",
        "data": {
            "status": "healthy",
            "excel_ready": calc is not None
        }
    })

def start_api_server():
    """启动API服务器"""
    print("正在启动API服务器...")
    try:
        # 使用单线程模式，避免COM线程安全问题
        app.run(host='0.0.0.0', port=5000, debug=False, threaded=False)
    except Exception as e:
        print(f"API服务器启动失败: {e}")
        import traceback
        print(f"错误堆栈: {traceback.format_exc()}")

if __name__ == '__main__':
    start_api_server()
