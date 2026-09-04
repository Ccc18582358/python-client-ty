#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库管理器（版本2）
"""

import sqlite3
import os
import sys
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class DBManagerV2:
    """数据库管理器（版本2）"""
    
    def __init__(self):
        """初始化"""
        # 数据库文件路径 - 使用固定的用户数据目录
        if getattr(sys, 'frozen', False):
            # 打包后的可执行文件
            app_dir = os.path.dirname(sys.executable)
        else:
            # 开发环境
            app_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        
        # 确保目录存在
        data_dir = os.path.join(app_dir, "data")
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
        
        self.db_path = os.path.join(data_dir, "price_monitor_v2.db")
        
        # 迁移旧数据库（如果存在）—— 仅在显式调用时执行
        # self._migrate_old_database(app_dir)  # 已禁用

        # ★ 先补列（老库升级），再建表/建索引——否则老表缺列会导致索引创建失败
        self._add_missing_columns()
        self._create_table()
        self._repair_7881_low_prices()
        # 一次性开启 WAL 模式 + busy_timeout
        # WAL: 多 reader + 1 writer 并发，Workder/主线程同读不冲突
        # busy_timeout: write-write 锁时等 5s 再抛错（默认是立即抛 OperationalError）
        try:
            _conn = sqlite3.connect(self.db_path, timeout=5.0)
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=5000")
            _conn.close()
        except Exception as e:
            # WAL 设不上不影响主流程（可能文件被独占打开）
            import logging as _lg
            _lg.getLogger(__name__).warning(f"[db] WAL/busy_timeout 初始化失败: {e}")
        # ★ 不再在 __init__ 里自动清理旧数据（每次 new DBManagerV2() 都会触发删除，导致严重数据丢失）
        # 清理逻辑已移出构造函数，需要时手动调用 _cleanup_old_records()
        # self._cleanup_old_records()

    def _add_missing_columns(self):
        """老库升级：检测每个表缺什么列，自动补齐（不丢数据）"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            # scan_tasks 应有列
            cursor.execute("PRAGMA table_info(scan_tasks)")
            scan_cols = {row[1] for row in cursor.fetchall()}
            scan_expected = {
                "task_name": "TEXT",
                "platforms": "TEXT",
                "games": "TEXT",
                "enabled": "INTEGER DEFAULT 1",
                "schedule_mode": "TEXT DEFAULT 'always'",
                "window_start": "TEXT",
                "window_end": "TEXT",
                "interval_minutes": "INTEGER DEFAULT 10",
                "scan_mode": "TEXT DEFAULT 'latest'",
                "scan_limit": "INTEGER DEFAULT 50",
                "secondary_real_name_ratio": "REAL DEFAULT 0",
                "platform_bargain_ratio": "REAL DEFAULT 0",
                "status": "TEXT DEFAULT 'stopped'",
                "last_run_at": "TIMESTAMP",
                "next_run_at": "TIMESTAMP",
                "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            }
            for col, typedef in scan_expected.items():
                if col not in scan_cols:
                    try:
                        cursor.execute(f"ALTER TABLE scan_tasks ADD COLUMN {col} {typedef}")
                        print(f"[db_manager_v2] 补列 scan_tasks.{col}")
                    except Exception as e:
                        print(f"[db_manager_v2] 补列 {col} 失败: {e}")

            # products 应有列
            cursor.execute("PRAGMA table_info(products)")
            prod_cols = {row[1] for row in cursor.fetchall()}
            prod_expected = {
                "task_id": "INTEGER",
                "product_id": "TEXT",
                "product_info": "TEXT",
                "original_price": "REAL",
                "url": "TEXT",
                "game_type": "TEXT",
                "crawled_platform": "TEXT",
                "supports_realname": "INTEGER DEFAULT 0",
                "estimated": "INTEGER DEFAULT 0",
                "estimated_at": "TIMESTAMP",
                "estimated_error": "TEXT",
                "prices_json": "TEXT",
                "final_price": "REAL",
                "pricing_formula": "TEXT",
                "is_deal": "INTEGER DEFAULT 0",
                "retry_count": "INTEGER DEFAULT 0",  # 估价重试次数
            }
            for col, typedef in prod_expected.items():
                if col not in prod_cols:
                    try:
                        cursor.execute(f"ALTER TABLE products ADD COLUMN {col} {typedef}")
                        print(f"[db_manager_v2] 补列 products.{col}")
                    except Exception as e:
                        print(f"[db_manager_v2] 补列 {col} 失败: {e}")

            conn.commit()
        finally:
            conn.close()

    def _migrate_old_database(self, app_dir):
        """旧版保留方法：显式调用才会执行清空（默认禁用）

        生产环境绝不能丢数据——此方法只用于开发者本地一次性重置
        """
        old_db_path = os.path.join(app_dir, "data", "price_monitor_v2.db")
        if os.path.exists(old_db_path):
            try:
                os.remove(old_db_path)
                print(f"[db_manager_v2] 已删除老库: {old_db_path}（旧数据全部清空）")
            except Exception as e:
                print(f"[db_manager_v2] 删除老库失败: {e}")

    def _create_table(self):
        """建表（scan_tasks / products）—— CREATE IF NOT EXISTS，不再 DROP

        保留用户数据。schema 变更走 _add_missing_columns()
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # ===== 1. 扫号任务表 =====
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS scan_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_name TEXT,
            platforms TEXT,                       -- JSON list
            games TEXT,                           -- JSON list
            enabled INTEGER DEFAULT 1,

            -- 调度（什么时候跑）
            schedule_mode TEXT DEFAULT 'always',  -- 'always' / 'window'
            window_start TEXT,                    -- 'HH:MM'，仅 window
            window_end TEXT,                      -- 'HH:MM'，仅 window
            interval_minutes INTEGER DEFAULT 10,  -- 10~1440

            -- 扫描（扫多少）
            scan_mode TEXT DEFAULT 'latest',      -- 'full' / 'latest'
            scan_limit INTEGER DEFAULT 50,        -- 1~1000，仅 latest

            -- 议价
            secondary_real_name_ratio REAL DEFAULT 0,
            platform_bargain_ratio REAL DEFAULT 0,

            -- 状态
            status TEXT DEFAULT 'stopped',
            last_run_at TIMESTAMP,
            next_run_at TIMESTAMP,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        # ===== 2. 商品表（重构后 schema） =====
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,                       -- 关联任务
            product_id TEXT,                       -- 外部商品 ID（去重 key）
            product_info TEXT,
            original_price REAL,
            url TEXT,
            game_type TEXT,
            crawled_platform TEXT,                 -- 这次回调来自哪个平台

            supports_realname INTEGER DEFAULT 0,
            estimated INTEGER DEFAULT 0,          -- 0=待估价, 1=已估价
            estimated_at TIMESTAMP,
            estimated_error TEXT,

            prices_json TEXT,                      -- Excel 算 4 平台价
            final_price REAL,                      -- 议价后价
            pricing_formula TEXT,                  -- 'secondary' / 'platform_only'
            is_deal INTEGER DEFAULT 0,             -- final_price > original_price

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        # ===== 3. 索引（性能） =====
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_dedup ON products(product_id, estimated_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending ON products(estimated) WHERE estimated = 0")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_id ON products(task_id)")

        conn.commit()
        conn.close()
        # 表已就绪（CREATE IF NOT EXISTS 幂等）

    def _repair_7881_low_prices(self):
        """Repair legacy 7881 prices that were accidentally divided by 100.

        The bug only affected 7881 normalization. Keep this narrow so real
        low-price rows from other platforms are not changed.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM products
                WHERE crawled_platform = '7881'
                  AND original_price > 0
                  AND original_price < 100
                """
            )
            affected = int(cursor.fetchone()[0] or 0)
            if affected <= 0:
                return

            cursor.execute(
                """
                UPDATE products
                SET original_price = ROUND(original_price * 100, 2),
                    is_deal = CASE
                        WHEN final_price > ROUND(original_price * 100, 2)
                             AND ROUND(original_price * 100, 2) > 0
                             AND final_price > 0
                        THEN 1
                        ELSE 0
                    END
                WHERE crawled_platform = '7881'
                  AND original_price > 0
                  AND original_price < 100
                """
            )
            conn.commit()
            logger.warning(
                "[db] repaired %s legacy 7881 low-price rows (multiplied original_price by 100)",
                affected,
            )
        except Exception:
            conn.rollback()
            logger.exception("[db] failed to repair legacy 7881 low-price rows")
        finally:
            conn.close()
    
    def save_product(self, product_info, original_price, prices, url="", game_type=""):
        """保存商品信息（兼容旧签名 — 落到新 schema，估价为 0）

        实际业务用 save_product_pending + update_product_estimated 两阶段
        """
        # 旧调用方：当成"立即落库 + 估价"两步合一的便捷方法
        db_id = self.save_product_pending(
            product_info=product_info,
            original_price=original_price,
            url=url,
            game_type=game_type,
            product_id=url or f"legacy_{int(time.time()*1000)}",
            task_id=0,
            crawled_platform="",
            supports_realname=False,
        )
        if prices:
            self.update_product_estimated(
                product_db_id=db_id,
                prices=prices,
                final_price=0,
                pricing_formula="",
                is_deal=False,
            )
        return db_id

    # ==================== 新：扫号回调两阶段 ====================

    def save_product_pending(self, *,
        product_info: str, original_price: float, url: str, game_type: str,
        product_id: str, task_id: int, crawled_platform: str,
        supports_realname: bool = False,
    ) -> Optional[int]:
        """阶段 1：外部回调落库（estimated=0，待 Worker 估价）

        去重策略（同 product_id）：
          1. 同 product_id + 同 original_price → **跳过，不入库，不修改任何记录**
          2. 同 product_id + 异 original_price → **更新原行**：
               - 更新 original_price = 新价
               - 更新 created_at = CURRENT_TIMESTAMP
               - reset estimated = 0 (清掉 estimated_at / estimated_error / prices_json / final_price / pricing_formula / is_deal)
               - worker 后续会重新估价
             返回原行 id
          3. 新 product_id（库里没有）→ INSERT 新行，返回新行 id

        Returns:
            None  → 同 product_id + 同价（跳过，不入库）
            int   → 数据库行 id（异价更新 或 新增）
        """
        if not product_id:
            # 没有 product_id 的（极端情况）→ 直接新插
            return self._insert_new_pending(
                product_info=product_info, original_price=original_price, url=url,
                game_type=game_type, product_id=product_id, task_id=task_id,
                crawled_platform=crawled_platform, supports_realname=supports_realname,
            )

        # 查最近一条同 product_id 的记录（不论 estimated 状态）
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, original_price FROM products WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (product_id,)
        )
        row = cursor.fetchone()

        if row is not None:
            old_price = float(row['original_price'] or 0)
            new_price = float(original_price or 0)
            if abs(old_price - new_price) < 0.001:
                # 情况 1：同 product_id + 同价 → 跳过，不入库，不修改
                conn.close()
                logger.info(
                    f"[save_product_pending] 跳过同价: product_id={product_id} price={new_price} (库内原 id={row['id']})"
                )
                return None

            # 情况 2：同 product_id + 异价 → 更新原行
            cursor.execute(
                """
                UPDATE products
                SET original_price = ?,
                    created_at = CURRENT_TIMESTAMP,
                    estimated = 0,
                    estimated_at = NULL,
                    estimated_error = NULL,
                    prices_json = '{}',
                    final_price = 0,
                    pricing_formula = '',
                    is_deal = 0
                WHERE id = ?
                """,
                (new_price, row['id'])
            )
            conn.commit()
            conn.close()
            logger.info(
                f"[save_product_pending] 异价更新: product_id={product_id} "
                f"原 ¥{old_price:.2f} → 新 ¥{new_price:.2f}, id={row['id']}"
            )
            return int(row['id'])

        # 情况 3：新 product_id
        conn.close()
        return self._insert_new_pending(
            product_info=product_info, original_price=original_price, url=url,
            game_type=game_type, product_id=product_id, task_id=task_id,
            crawled_platform=crawled_platform, supports_realname=supports_realname,
        )

    def _insert_new_pending(self, *,
        product_info: str, original_price: float, url: str, game_type: str,
        product_id: str, task_id: int, crawled_platform: str,
        supports_realname: bool = False,
    ) -> int:
        """新插一行（save_product_pending 内部 helper）"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            '''
            INSERT INTO products
            (task_id, product_id, product_info, original_price, url, game_type,
             crawled_platform, supports_realname, estimated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            ''',
            (task_id, product_id, product_info, original_price, url, game_type,
             crawled_platform, 1 if supports_realname else 0)
        )
        new_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return new_id

    def update_product_estimated(self, *,
        product_db_id: int,
        prices: dict,
        final_price: float,
        pricing_formula: str,
        is_deal: bool,
        estimated_error: str = None,
        force_estimated: int = None,
    ) -> bool:
        """阶段 2：Worker 估价完成后更新

        同时 retry_count += 1（用于跟踪重试次数）

        Args:
            force_estimated: 强制设置 estimated 值。
                - None（默认）：保持原行为 estimated=1
                - 2：用于"重试超限标永久失败"路径（避免被 worker 再扫到）
        """
        import json as _json
        try:
            prices_json = _json.dumps(prices or {}, ensure_ascii=False)
        except Exception:
            prices_json = "{}"
        estimated_value = 1 if force_estimated is None else int(force_estimated)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            '''
            UPDATE products SET
                estimated = ?,
                estimated_at = CURRENT_TIMESTAMP,
                estimated_error = ?,
                prices_json = ?,
                final_price = ?,
                pricing_formula = ?,
                is_deal = ?,
                retry_count = retry_count + 1
            WHERE id = ?
            ''',
            (estimated_value, estimated_error, prices_json, final_price, pricing_formula,
             1 if is_deal else 0, product_db_id)
        )
        conn.commit()
        conn.close()
        return True

    def get_pending_products(self, limit: int = 50):
        """查待估价的商品（Worker 兜底轮询用）

        包含两种行：
          1. estimated=0  （首次待估价）
          2. estimated=1 + final_price=0 + 无 error + retry_count<3
             （"无结果"行重试，最多 3 次）

        Returns:
            list of row dicts
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT * FROM products
            WHERE estimated = 0
               OR (estimated = 1
                   AND final_price = 0
                   AND (estimated_error IS NULL OR estimated_error = '')
                   AND retry_count < 3)
            ORDER BY id ASC
            LIMIT ?
            ''',
            (limit,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def count_pending_products(self) -> dict:
        """按状态统计商品数量（用于估价进度卡）

        Returns:
            {
                'pending': int,      # estimated=0（首次待估价）
                'retrying': int,     # estimated=1+final_price=0+无 error+retry<3（重试中）
                'completed': int,    # estimated=1+final_price>0（已估价成功）
                'error': int,        # estimated=2（永久失败）
                'total': int         # products 总数
            }
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT
                       SUM(CASE WHEN estimated = 0 THEN 1 ELSE 0 END) AS pending,
                       SUM(CASE WHEN estimated = 1
                                 AND final_price = 0
                                 AND (estimated_error IS NULL OR estimated_error = '')
                                 AND retry_count < 3
                               THEN 1 ELSE 0 END) AS retrying,
                       SUM(CASE WHEN estimated = 1 AND final_price > 0 THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN estimated = 2 THEN 1 ELSE 0 END) AS error,
                       COUNT(*) AS total
                   FROM products'''
            )
            row = cursor.fetchone()
            return {
                "pending":   int(row[0] or 0),
                "retrying":  int(row[1] or 0),
                "completed": int(row[2] or 0),
                "error":     int(row[3] or 0),
                "total":     int(row[4] or 0),
            }
        finally:
            conn.close()

    def get_last_estimated(self, product_id: str):
        """按 product_id 查最近一条已估价记录（去重用）

        Returns:
            dict 或 None
        """
        if not product_id:
            return None
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT * FROM products
            WHERE product_id = ? AND estimated = 1
            ORDER BY estimated_at DESC
            LIMIT 1
            ''',
            (product_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_product_by_id_new(self, product_db_id: int):
        """按主键 id 取商品（Worker 队列用）"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM products WHERE id = ?", (product_db_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def _row_to_prices(self, row) -> dict:
        """把 sqlite row 还原成 prices dict

        优先读 prices_json（动态可配），fallback 4 列老 schema
        """
        import json as _json
        # 1. 优先 prices_json
        pj = None
        try:
            pj = row['prices_json'] if 'prices_json' in row.keys() else None
        except (AttributeError, KeyError, IndexError):
            pj = None
        if pj:
            try:
                data = _json.loads(pj)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        # 2. fallback 老 4 列
        prices = {}
        try:
            if row['panzhi_price']:
                prices['盼之'] = row['panzhi_price']
            if row['pangxie_price']:
                prices['螃蟹'] = row['pangxie_price']
            if row['qibaibayi_price']:
                prices['7881'] = row['qibaibayi_price']
            if row['kejinshou_price']:
                prices['氪金兽'] = row['kejinshou_price']
        except (KeyError, IndexError):
            pass
        return prices
    
    def get_product_by_id(self, product_id):
        """根据ID获取商品记录"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM products WHERE id = ?", (product_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    
    def delete_product(self, product_id):
        """删除商品记录"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()
        conn.close()
    
    def delete_products(self, product_ids):
        """批量删除商品记录"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(product_ids))
        cursor.execute(f"DELETE FROM products WHERE id IN ({placeholders})", product_ids)
        conn.commit()
        conn.close()

    def clear_estimate_error(self, product_db_id: int):
        """清除估价错误状态，重置 estimated=0 以便 worker 重新估价"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE products SET estimated=0, estimated_error=NULL, retry_count=0 WHERE id=?",
            (product_db_id,)
        )
        conn.commit()
        conn.close()
    
    def get_deal_products(self, page=1, page_size=10, game_type=None, crawled_platform=None, min_price=None, max_price=None, min_save=None, max_save=None, start_date=None, end_date=None):
        """获取捡漏商品（支持分页和筛选）"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        conditions = []
        params = []

        # 新 schema：用 is_deal=1 表示捡漏
        conditions.append("is_deal = 1")

        if game_type and game_type != "全部":
            conditions.append("game_type = ?")
            params.append(game_type)

        # 平台筛选
        if crawled_platform and crawled_platform != "全部":
            conditions.append("crawled_platform = ?")
            params.append(crawled_platform)

        if min_price is not None:
            conditions.append("original_price >= ?")
            params.append(min_price)
        if max_price is not None:
            conditions.append("original_price <= ?")
            params.append(max_price)

        # min_save/max_save 改用 final_price（原 save_amount 字段已删）
        if min_save is not None:
            conditions.append("final_price >= ?")
            params.append(min_save)
        if max_save is not None:
            conditions.append("final_price <= ?")
            params.append(max_save)

        if start_date:
            if len(start_date) == 10 and start_date.count('-') == 2:
                start_date = start_date + ' 00:00:00'
            conditions.append("created_at >= ?")
            params.append(start_date)
        if end_date:
            if len(end_date) == 10 and end_date.count('-') == 2:
                end_date = end_date + ' 23:59:59'
            conditions.append("created_at <= ?")
            params.append(end_date)

        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        
        offset = (page - 1) * page_size
        
        query = f'''
        SELECT * FROM products 
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        '''
        cursor.execute(query, params + [page_size, offset])
        
        products = [dict(row) for row in cursor.fetchall()]
        
        count_query = f'''
        SELECT COUNT(*) FROM products 
        {where_clause}
        '''
        cursor.execute(count_query, params)
        total = cursor.fetchone()[0]
        
        conn.close()
        return products, total
    
    def get_scan_tasks(self, page=1, page_size=10):
        """获取扫号任务（支持分页）"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        offset = (page - 1) * page_size
        
        cursor.execute(
            '''
            SELECT * FROM scan_tasks 
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            ''',
            (page_size, offset)
        )
        rows = cursor.fetchall()
        
        cursor.execute("SELECT COUNT(*) FROM scan_tasks")
        total = cursor.fetchone()[0]
        
        conn.close()
        
        tasks = []
        for row in rows:
            task = dict(row)
            task['platforms'] = self._decode_task_field(task['platforms'])
            task['games'] = self._decode_task_field(task['games'])
            tasks.append(task)

        return tasks, total
    
    def save_scan_task(self, task_name, platforms, games, is_enabled=1):
        """保存扫号任务"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute(
            '''
            INSERT INTO scan_tasks (task_name, platforms, games, enabled)
            VALUES (?, ?, ?, ?)
            ''',
            (task_name, platforms, games, is_enabled)
        )
        
        task_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return task_id
    
    # ==================== 扫号任务相关方法 ====================
    
    def create_scan_task(self, task_name, platforms, games, enabled=True, *,
        schedule_mode='always', window_start=None, window_end=None,
        interval_minutes=10, scan_mode='latest', scan_limit=50,
        secondary_real_name_ratio=0.0, platform_bargain_ratio=0.0,
    ):
        """创建扫号任务（新版 11 字段）"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        import json
        platforms_json = json.dumps(platforms, ensure_ascii=False)
        games_json = json.dumps(games, ensure_ascii=False)

        cursor.execute(
            '''
            INSERT INTO scan_tasks
            (task_name, platforms, games, enabled,
             schedule_mode, window_start, window_end, interval_minutes,
             scan_mode, scan_limit,
             secondary_real_name_ratio, platform_bargain_ratio)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (task_name, platforms_json, games_json, 1 if enabled else 0,
             schedule_mode, window_start, window_end, interval_minutes,
             scan_mode, scan_limit,
             secondary_real_name_ratio, platform_bargain_ratio)
        )

        task_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return task_id

    def update_scan_task(self, task_id, *,
        task_name=None, platforms=None, games=None, enabled=None, status=None,
        schedule_mode=None, window_start=None, window_end=None,
        interval_minutes=None, scan_mode=None, scan_limit=None,
        secondary_real_name_ratio=None, platform_bargain_ratio=None,
        last_run_at=None,
    ):
        """更新扫号任务（支持所有新字段的 kwargs）"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        import json
        updates = []
        params = []

        if task_name is not None:
            updates.append("task_name = ?")
            params.append(task_name)
        if platforms is not None:
            updates.append("platforms = ?")
            params.append(json.dumps(platforms, ensure_ascii=False) if isinstance(platforms, list) else platforms)
        if games is not None:
            updates.append("games = ?")
            params.append(json.dumps(games, ensure_ascii=False) if isinstance(games, list) else games)
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if enabled else 0)
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if schedule_mode is not None:
            updates.append("schedule_mode = ?")
            params.append(schedule_mode)
        if window_start is not None:
            updates.append("window_start = ?")
            params.append(window_start)
        if window_end is not None:
            updates.append("window_end = ?")
            params.append(window_end)
        if interval_minutes is not None:
            updates.append("interval_minutes = ?")
            params.append(interval_minutes)
        if scan_mode is not None:
            updates.append("scan_mode = ?")
            params.append(scan_mode)
        if scan_limit is not None:
            updates.append("scan_limit = ?")
            params.append(scan_limit)
        if secondary_real_name_ratio is not None:
            updates.append("secondary_real_name_ratio = ?")
            params.append(secondary_real_name_ratio)
        if platform_bargain_ratio is not None:
            updates.append("platform_bargain_ratio = ?")
            params.append(platform_bargain_ratio)
        if last_run_at is not None:
            updates.append("last_run_at = ?")
            params.append(last_run_at)

        updates.append("updated_at = CURRENT_TIMESTAMP")

        if updates:
            query = f"UPDATE scan_tasks SET {', '.join(updates)} WHERE id = ?"
            params.append(task_id)
            cursor.execute(query, params)
            conn.commit()

        conn.close()
    
    def delete_scan_task(self, task_id):
        """删除扫号任务"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scan_tasks WHERE id = ?", (task_id,))
        conn.commit()
        conn.close()
    
    def get_scan_task(self, task_id):
        """获取单个扫号任务"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM scan_tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        
        conn.close()
        
        if row:
            task = dict(row)
            task['platforms'] = self._decode_task_field(task['platforms'])
            task['games'] = self._decode_task_field(task['games'])
            return task
        return None

    @staticmethod
    def _decode_task_field(value):
        """统一解析扫号任务的 platforms/games 字段

        存储约定：既支持 JSON 数组（推荐），也兼容旧版的逗号分隔字符串。
        返回值：解析后的 list，或解析失败时的原字符串。
        """
        if not value:
            return ''
        if isinstance(value, list):
            return value
        if not isinstance(value, str):
            return value
        # 优先尝试 JSON
        import json
        s = value.strip()
        if s.startswith('[') and s.endswith(']'):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
        # 兜底：按逗号分隔（兼容旧数据）
        return [item.strip() for item in s.split(',') if item.strip()]
    
    def get_all_scan_tasks(self):
        """获取所有扫号任务"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM scan_tasks ORDER BY created_at DESC")
        rows = cursor.fetchall()
        
        conn.close()
        
        tasks = []
        for row in rows:
            task = dict(row)
            task['platforms'] = self._decode_task_field(task['platforms'])
            task['games'] = self._decode_task_field(task['games'])
            tasks.append(task)
        
        return tasks
    
    def get_enabled_scan_tasks(self):
        """获取所有启用的扫号任务"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM scan_tasks WHERE enabled = 1 ORDER BY created_at DESC")
        rows = cursor.fetchall()
        
        conn.close()
        
        tasks = []
        for row in rows:
            task = dict(row)
            task['platforms'] = self._decode_task_field(task['platforms'])
            task['games'] = self._decode_task_field(task['games'])
            tasks.append(task)
        
        return tasks
    
    def _cleanup_old_records(self):
        """清理7天前的历史记录"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 计算7天前的日期
        seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
        
        # 删除7天前的记录
        cursor.execute(
            '''
            DELETE FROM products 
            WHERE created_at < ?
            ''',
            (seven_days_ago,)
        )
        
        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()
        
        if deleted_count > 0:
            print(f"已清理 {deleted_count} 条7天前的历史记录")
    
    def get_all_products(self, page=1, page_size=10, game_type=None, crawled_platform=None, min_price=None, max_price=None, start_date=None, end_date=None):
        """获取所有历史记录（支持分页和筛选）"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 构建查询条件
        conditions = []
        params = []

        # 游戏类型筛选
        if game_type and game_type != "全部":
            conditions.append("game_type = ?")
            params.append(game_type)

        # 平台筛选
        if crawled_platform and crawled_platform != "全部":
            conditions.append("crawled_platform = ?")
            params.append(crawled_platform)

        # 价格区间筛选
        if min_price is not None:
            conditions.append("original_price >= ?")
            params.append(min_price)
        if max_price is not None:
            conditions.append("original_price <= ?")
            params.append(max_price)

        # 时间筛选
        # 兼容 'yyyy-MM-dd' 短格式: 拼成完整 datetime，否则按字典序比较会漏掉当天数据
        # (例: '2026-06-15' <= '2026-06-15 12:34:56' 是 False，导致 6-15 当天全部漏掉)
        if start_date:
            if len(start_date) == 10 and start_date.count('-') == 2:
                start_date = start_date + ' 00:00:00'
            conditions.append("created_at >= ?")
            params.append(start_date)
        if end_date:
            if len(end_date) == 10 and end_date.count('-') == 2:
                end_date = end_date + ' 23:59:59'
            conditions.append("created_at <= ?")
            params.append(end_date)
        
        # 构建WHERE子句
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        
        # 计算偏移量
        offset = (page - 1) * page_size
        
        # 查询数据
        query = f'''
        SELECT * FROM products 
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        '''
        cursor.execute(query, params + [page_size, offset])
        
        products = [dict(row) for row in cursor.fetchall()]
        
        # 查询总记录数
        count_query = f'''
        SELECT COUNT(*) FROM products 
        {where_clause}
        '''
        cursor.execute(count_query, params)
        total = cursor.fetchone()[0]
        
        conn.close()
        return products, total
