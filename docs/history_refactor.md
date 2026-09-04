# 历史记录改造文档（v2）

> **状态**：📝 文档已出，代码未动  
> **目标版本**：v2.x  
> **更新日期**：2026-06-15

---

## 1. 背景 & 现状问题

### 1.1 现状

历史记录页（`history_page.py`）当前列：

| 列 | 来源 | 显示 |
|----|------|------|
| 选择 | - | ☐/☑ |
| ID | `products.id` | 数字 |
| 游戏 | `game_type` | 文本 |
| **价格** | `original_price` | 原价 |
| 螃蟹 / 盼之 / 7881 / 氪金兽 | `prices_json[p]` | 4 列 platform 估价（动态列） |
| 溢价 | `save_amount` | - |
| 时间 | `created_at` | yyyy-MM-dd HH:mm |

### 1.2 问题

1. ❌ 4 列动态 platform 不合理——商品落库时 `crawled_platform` 决定它只属于一个平台，**4 列里 3 列永远是空**（用户根本看不到这 platform 的估价）
2. ❌ "价格"列显示原价，用户期望看**估价后价**（final_price）
3. ❌ 没有"状态"列——分不清"已估价 / 待估价 / 估价失败"
4. ❌ 估价失败没标识 + 没重试入口

---

## 2. 需求（已与用户确认）

### 2.1 用户原话

> "回调一条这样的数据（task_id/platform/results[]），你落库历史记录就要为螃蟹，火影忍者，原价 xxx，平台估价 等等。表头应该是这样的：
> **选择 │ ID │ 游戏 │ 原价 │ 平台估价 │ 溢价 │ 状态 │ 时间 │ 操作**"

### 2.2 列结构（9 列，固定）

| # | 列名 | 来源 | 宽度 |
|---|------|------|------|
| 0 | 选择 | - | 50 |
| 1 | ID | `products.id` | 60 |
| 2 | 游戏 | `game_type` | 120 |
| 3 | 原价 | `original_price` | 90 |
| 4 | **平台估价** | `prices_json[crawled_platform]` | 90 |
| 5 | 溢价 | `final_price - original_price` | 70 |
| 6 | **状态** | `estimated` + `estimated_error` | 130 |
| 7 | 时间 | `created_at` | 140 |
| 8 | **操作** | - | 100 |

> 📝 **平台估价 = prices_json[crawled_platform]**，因为这条商品只属于 `crawled_platform` 这一个平台

### 2.3 详细显示规则

| 列 | 待估价 | 已估价 | 估价失败 |
|----|--------|--------|----------|
| **原价** | `¥100.00` | `¥100.00` | `¥100.00` |
| **平台估价** | **完全空** | `361` | **完全空** |
| **溢价** | **完全空** | `+¥50`（绿）/ `-¥30`（红）/ `¥0`（灰） | **完全空** |
| **状态** | 🔵 蓝色标签 "待估价" | 🟢 绿色标签 "已估价" | 🟠 橙色标签 "估价失败" |
| **操作** | - | - | **重试** 按钮 |

### 2.4 状态判定

| 状态 | 判定 | 显示 |
|------|------|------|
| 待估价 | `estimated=0 AND estimated_error IS NULL` | 蓝色 "待估价" |
| 已估价 | `estimated=1 AND estimated_error IS NULL` | 绿色 "已估价" |
| 估价失败 | `estimated=0 AND estimated_error IS NOT NULL` | 橙色 "估价失败" + 重试 |

---

## 3. 数据示例

### 3.1 回调 payload（你给的）

```json
{
  "task_id": 123,
  "platform": "螃蟹",
  "results": [
    {
      "product_id": "pangxie_12345",
      "product_info": "火影忍者 100 级号 装备齐全",
      "original_price": 100.0,
      "url": "https://example.com/product/12345",
      "game_type": "火影忍者",
      "supports_realname": true
    }
  ]
}
```

### 3.2 落库后 DB 状态

```sql
INSERT INTO products
  (task_id, product_id, product_info, original_price, url, game_type,
   crawled_platform, supports_realname, estimated)
VALUES
  (123, 'pangxie_12345', '火影忍者 100 级号 装备齐全', 100.0,
   'https://...', '火影忍者', '螃蟹', 1, 0);
-- estimated=0（待估价）
```

### 3.3 Worker 估价后

```sql
UPDATE products SET
  prices_json = '{"螃蟹": 361, "盼之": 350, ...}',  -- 4 平台都算了
  final_price = 380.5,                              -- 螃蟹列议价后价
  pricing_formula = 'secondary',
  is_deal = 0,
  estimated = 1,
  estimated_at = CURRENT_TIMESTAMP
WHERE id = 42;
```

> 📝 **`prices_json` 含 4 平台估价**，但 history 表只显示 `crawled_platform`（螃蟹）那一列的值

### 3.4 在 history 表的显示

| 选择 | ID | 游戏 | 原价 | 平台估价 | 溢价 | 状态 | 时间 | 操作 |
|------|----|----|------|---------|------|------|------|------|
| ☐ | 42 | 火影忍者 | ¥100.00 | **361** | +¥280.50 | 🟢 已估价 | 2026-06-15 16:30 | - |

---

## 4. 设计方案

### 4.1 列结构（新版）

```
┌────┬────┬──────────┬────────┬──────────┬───────┬────────────┬──────────┬────────┐
│选择│ ID │   游戏    │  原价  │ 平台估价  │ 溢价  │    状态     │   时间   │  操作  │
├────┼────┼──────────┼────────┼──────────┼───────┼────────────┼──────────┼────────┤
│ ☐  │ 42 │ 火影忍者  │¥100.00 │   361    │+¥280  │ 🟢 已估价  │16:30:25  │   -    │
│ ☐  │ 43 │ 王者荣耀  │¥150.00 │          │       │ 🔵 待估价  │16:31:02  │   -    │
│ ☐  │ 44 │ 火影忍者  │¥500.00 │          │       │ 🟠 估价失败│16:31:18  │ [重试] │
└────┴────┴──────────┴────────┴──────────┴───────┴────────────┴──────────┴────────┘
```

### 4.2 关键设计

#### 4.2.1 "完全空"的实现

QTableWidgetItem 不能为 None，setText("") 才能让 cell 完全空（**注意：cell 边框还在，只是没文字**）：

```python
# "完全空" = QTableWidgetItem("")，cell 边框 + 高度都在
item = QTableWidgetItem("")  # 视觉效果：白底 + 边框
self.table.setItem(r, COL_PLATFORM_PRICE, item)
```

#### 4.2.2 状态标签（cellWidget QLabel）

不用纯文字，用 cellWidget QLabel 加底色：

```python
from PySide6.QtWidgets import QLabel

status_label = QLabel("待估价")
status_label.setAlignment(Qt.AlignCenter)
status_label.setStyleSheet("""
    QLabel {
        background: #e6f7ff;
        color: #1890ff;
        border-radius: 4px;
        padding: 2px 8px;
        font-weight: bold;
    }
""")
table.setCellWidget(row, COL_STATUS, status_label)
```

**配色**：

| 状态 | 背景 | 文字 |
|------|------|------|
| 待估价 | `#e6f7ff`（浅蓝）| `#1890ff`（蓝）|
| 已估价 | `#f6ffed`（浅绿）| `#52c41a`（绿）|
| 估价失败 | `#fff7e6`（浅橙）| `#fa8c16`（橙）|

#### 4.2.3 操作列 cellWidget（重试按钮）

```python
from qfluentwidgets import PushButton

# 待估价 / 已估价：操作列空
# 估价失败：操作列 = [重试] 按钮
op_widget = QWidget()
op_layout = QHBoxLayout(op_widget)
op_layout.setContentsMargins(4, 2, 4, 2)
if status == "failed":
    retry_btn = PushButton("重试")
    retry_btn.setFixedSize(50, 24)
    retry_btn.clicked.connect(lambda: self._retry_estimate(product_id))
    op_layout.addWidget(retry_btn)
    op_layout.addStretch(1)
else:
    op_layout.addStretch(1)  # 占位，cell 仍然有边框
table.setCellWidget(row, COL_OPERATION, op_widget)
```

#### 4.2.4 溢价

| 状态 | 显示 |
|------|------|
| 待估价 | 完全空 |
| 已估价 + diff > 0 | 绿色 `+¥50` |
| 已估价 + diff < 0 | 红色 `-¥30` |
| 已估价 + diff = 0 | 灰色 `¥0` |
| 估价失败 | 完全空 |

```python
if estimated == 1 and final_price and original_price:
    diff = final_price - original_price
    if diff > 0:
        text, color = f"+¥{diff:.0f}", "#52c41a"
    elif diff < 0:
        text, color = f"-¥{abs(diff):.0f}", "#f5222d"
    else:
        text, color = "¥0", "#999999"
    item = QTableWidgetItem(text)
    item.setForeground(QColor(color))
```

#### 4.2.5 实时刷新（监听 signal）

```python
# main_window 转发 worker 的 productEstimated signal
product_estimated_signal.connect(history_page._on_product_estimated)

# history_page 收到后：整表重渲染（30s 内节流）
def _on_product_estimated(self, *args):
    if not hasattr(self, '_throttle_ts') or time.time() - self._throttle_ts > 2:
        self._throttle_ts = time.time()
        self._load_data()
```

---

## 5. 涉及改动清单

### 5.1 后端

| 文件 | 改动 | 说明 |
|------|------|------|
| `database/db_manager_v2.py` | ➕ 加 `clear_estimate_error(product_db_id)` | 重试前清 error |
| `services/estimation_worker.py` | ➕ 加 `retry_estimate(product_db_id)` | 重新入队 |
| `gui/main_window.py` | ➕ 加转发 signal | productEstimated → product_estimated_signal |
| `api/api_server.py` | ❌ **不用改**（已不启用）| 保留代码作废 |

### 5.2 前端

| 文件 | 改动 | 说明 |
|------|------|------|
| `gui/pages/history_page.py` | 🔧 **大改** | 列结构、行填充、状态 cellWidget、操作 cellWidget |

### 5.3 history_page.py 详细改动

#### 改动 A：列定义（删除动态 platform 列）

```python
# 旧（动态列）
self._platform_names = config_manager.get_platform_names()
self._columns = ["选择", "ID", "游戏", "价格"] + self._platform_names + ["溢价", "时间"]

# 新（固定 9 列）
self._columns = ["选择", "ID", "游戏", "原价", "平台估价", "溢价", "状态", "时间", "操作"]
self.table = DataTable(parent, columns=9, headers=self._columns)
self.table.setColumnWidth(0, 50)    # 选择
self.table.setColumnWidth(1, 60)    # ID
self.table.setColumnWidth(2, 120)   # 游戏
self.table.setColumnWidth(3, 90)    # 原价
self.table.setColumnWidth(4, 90)    # 平台估价
self.table.setColumnWidth(5, 70)    # 溢价
self.table.setColumnWidth(6, 130)   # 状态
self.table.setColumnWidth(7, 140)   # 时间
self.table.setColumnWidth(8, 100)   # 操作
```

**删除**：
- ❌ `_platform_names` 字段
- ❌ `_build_table_columns()` 动态列生成
- ❌ `_rebuild_table_columns()` 动态列重生成
- ❌ `_on_config_changed()` 里跟 platform 相关的部分
- ❌ `config_manager.configChanged` 监听（平台列删了，没必要）

#### 改动 B：行填充逻辑

```python
def _load_data(self):
    self.table.setRowCount(0)
    self.selected_items.clear()

    products, total = self.db_manager.get_all_products(
        page=self.current_page, page_size=self.page_size,
        game_type=..., min_price=..., max_price=..., start_date=..., end_date=...,
    )
    self.total_count = total
    self.pagination.update_info(self.current_page, ..., total)

    self.table.setRowCount(len(products))
    dbm = DBManagerV2()

    for r, p in enumerate(products):
        # 0. 选择
        self.table.setItem(r, 0, QTableWidgetItem("☐"))
        # 1. ID
        self.table.setItem(r, 1, QTableWidgetItem(str(p.get('id', ''))))
        # 2. 游戏
        item = QTableWidgetItem(p.get('game_type', ''))
        item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(r, 2, item)

        original_price = float(p.get('original_price', 0) or 0)
        estimated = int(p.get('estimated', 0) or 0)
        final_price = float(p.get('final_price', 0) or 0)
        error = p.get('estimated_error')
        crawled_platform = p.get('crawled_platform', '')

        # 3. 原价
        item = QTableWidgetItem(f"¥{original_price:.2f}" if original_price else "")
        item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(r, 3, item)

        # 4. 平台估价 = prices_json[crawled_platform]
        prices = dbm._row_to_prices(p) or {}
        platform_price = prices.get(crawled_platform) if crawled_platform else None
        if platform_price is None or platform_price == 0:
            self.table.setItem(r, 4, QTableWidgetItem(""))  # 完全空
        else:
            item = QTableWidgetItem(f"{float(platform_price):.0f}")
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(r, 4, item)

        # 5. 溢价
        if estimated == 1 and final_price and original_price:
            diff = final_price - original_price
            if diff > 0:
                text, color = f"+¥{diff:.0f}", "#52c41a"
            elif diff < 0:
                text, color = f"-¥{abs(diff):.0f}", "#f5222d"
            else:
                text, color = "¥0", "#999999"
            item = QTableWidgetItem(text)
            item.setForeground(QColor(color))
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(r, 5, item)
        else:
            self.table.setItem(r, 5, QTableWidgetItem(""))  # 完全空

        # 6. 状态（cellWidget）
        status_text, status_enum = self._compute_status(estimated, error)
        status_widget = self._build_status_widget(status_text, status_enum)
        self.table.setCellWidget(r, 6, status_widget)

        # 7. 时间
        item = QTableWidgetItem((p.get('created_at', '') or '')[:16])
        item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(r, 7, item)

        # 8. 操作（cellWidget）
        op_widget = self._build_op_widget(status_enum, p['id'])
        self.table.setCellWidget(r, 8, op_widget)

    # 切换空态/内容
    self.table.show_content(len(products), empty_title="暂无历史记录", empty_desc="...")
```

#### 改动 C：状态判定函数

```python
def _compute_status(self, estimated: int, error) -> tuple[str, str]:
    if estimated == 1:
        return "已估价", "done"
    if error:
        return "估价失败", "failed"
    return "待估价", "pending"
```

#### 改动 D：状态 cellWidget 构造

```python
def _build_status_widget(self, text: str, status: str) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(4, 2, 4, 2)
    h.setSpacing(0)

    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    label.setFixedHeight(24)
    colors = {
        "pending": ("#e6f7ff", "#1890ff"),
        "done":    ("#f6ffed", "#52c41a"),
        "failed":  ("#fff7e6", "#fa8c16"),
    }
    bg, fg = colors[status]
    label.setStyleSheet(
        f"QLabel{{background:{bg};color:{fg};border-radius:4px;"
        f"padding:2px 10px;font-weight:bold;}}"
    )
    h.addWidget(label)
    h.addStretch(1)
    return w
```

#### 改动 E：操作 cellWidget 构造

```python
def _build_op_widget(self, status: str, product_id: int) -> QWidget:
    """操作列：估价失败时显示"重试"按钮；其他状态留空"""
    from qfluentwidgets import PushButton
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(4, 2, 4, 2)
    h.setSpacing(0)

    if status == "failed":
        retry_btn = PushButton("重试", w)
        retry_btn.setFixedSize(50, 24)
        retry_btn.setCursor(Qt.PointingHandCursor)
        retry_btn.clicked.connect(lambda: self._retry_estimate(product_id))
        h.addWidget(retry_btn)
    h.addStretch(1)
    return w
```

#### 改动 F：重试逻辑

```python
def _retry_estimate(self, product_db_id: int):
    """把估价失败的商品重新入队"""
    from services.estimation_worker import get_estimation_worker
    worker = get_estimation_worker()
    if not worker:
        notify(self, "错误", "估价 Worker 未启动", level="error")
        return

    # 1. 清掉 estimated_error
    self.db_manager.clear_estimate_error(product_db_id)
    # 2. 重新入队
    worker.enqueue([product_db_id])
    # 3. 乐观刷新
    notify(self, "提示", f"商品 #{product_db_id} 已重新入队", level="info")
    self._load_data()
```

#### 改动 G：实时刷新

```python
def __init__(self, ...):
    ...
    # 监听估价完成 signal
    try:
        from gui.main_window import product_estimated_signal
        product_estimated_signal.connect(self._on_product_estimated)
    except Exception:
        pass

def _on_product_estimated(self, product_db_id: int, final_price: float, is_deal: bool):
    """Worker 估价完成：节流后整表刷新"""
    import time
    if not hasattr(self, '_throttle_ts'):
        self._throttle_ts = 0
    if time.time() - self._throttle_ts < 2:
        return
    self._throttle_ts = time.time()
    self._load_data()
```

### 5.4 main_window.py 改动

```python
# 顶层（模块级）— 全局 signal，让 page 能订阅
from PySide6.QtCore import QObject, Signal

class _EstimateSignals(QObject):
    product_estimated = Signal(int, float, bool)
product_estimated_signal = _EstimateSignals().product_estimated

# 在 _start_background_services 里，监听 worker signal → 转发
if self.estimation_worker:
    self.estimation_worker.productEstimated.connect(product_estimated_signal.emit)
```

### 5.5 estimation_worker.py 改动

```python
def retry_estimate(self, product_db_id: int) -> None:
    """重试：清 error + 重新入队"""
    self.db.clear_estimate_error(product_db_id)
    self.enqueue([product_db_id])
```

### 5.6 db_manager_v2.py 改动

```python
def clear_estimate_error(self, product_db_id: int) -> bool:
    """清掉估价失败的 error 记录（准备重试）"""
    conn = sqlite3.connect(self.db_path)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE products SET estimated_error = NULL WHERE id = ? AND estimated = 0",
        (product_db_id,)
    )
    conn.commit()
    conn.close()
    return cursor.rowcount > 0
```

---

## 6. 数据流

```
爬虫 demo_crawler
  ↓ submit_scan_results(task_id, platform, results)
  ↓
scanner_service 落库 estimated=0
  ├─ products.crawled_platform = '螃蟹'（每条只属于一个 platform）
  ↓
Worker 异步估价
  ├─ 成功 → update_product_estimated(estimated=1, final_price=X, prices_json={4平台})
  │        → emit productEstimated(product_db_id, final_price, is_deal)
  │        → main_window 转发 product_estimated_signal
  │        → history_page._on_product_estimated → _load_data
  │
  └─ 失败 → update_product_estimated(estimated=0, estimated_error="...")
           → history_page 显示橙色"估价失败" + [重试] 按钮
           → 用户点重试 → estimation_worker.retry_estimate → 重新入队
```

---

## 7. 异常处理

| 场景 | 表现 | 兜底 |
|------|------|------|
| Worker 未启动 | history 全是"待估价" | 30s 兜底刷新 + 提示 |
| Excel 计算异常 | 该行 estimated_error 有值 | 状态"估价失败" + 重试按钮 |
| 重试后仍失败 | 再次"估价失败" | 可无限重试 |
| 平台估价 prices_json 缺该 platform | 列完全空 | 视为待估价 |
| crawled_platform 为空 | 列完全空 | 视为待估价 |

---

## 8. 验收标准

### 8.1 列结构

- [ ] 9 列：选择 / ID / 游戏 / **原价** / **平台估价** / **溢价** / **状态** / 时间 / **操作**
- [ ] 列宽：50 / 60 / 120 / 90 / 90 / 70 / 130 / 140 / 100
- [ ] **删除动态 platform 列**（不再根据 config_manager 平台名动态生成）

### 8.2 待估价行

- [ ] 原价：¥xxx.xx
- [ ] 平台估价：**完全空**（cell 是 ""）
- [ ] 溢价：**完全空**
- [ ] 状态：蓝色标签"待估价"
- [ ] 操作：空（但 cell 边框在）

### 8.3 已估价行

- [ ] 原价：¥xxx.xx
- [ ] 平台估价：prices_json[crawled_platform] 的值（整数无小数）
- [ ] 溢价：`+¥X` 绿 / `-¥X` 红 / `¥0` 灰
- [ ] 状态：绿色标签"已估价"
- [ ] 操作：空

### 8.4 估价失败行

- [ ] 原价：¥xxx.xx
- [ ] 平台估价：完全空
- [ ] 溢价：完全空
- [ ] 状态：橙色标签"估价失败"
- [ ] 操作：**[重试] 按钮**（50×24）

### 8.5 重试功能

- [ ] 点重试：estimated_error 清掉 + 重新入队 + 状态变"待估价"
- [ ] 几秒后变"已估价"（Worker 处理成功）或仍"估价失败"（Worker 仍失败）

### 8.6 实时性

- [ ] Worker 估价完成后 ≤ 2s，history 表对应行状态更新
- [ ] 30s 兜底：保证最终一致

### 8.7 性能

- [ ] 1000 行数据不卡
- [ ] 翻页 / 筛选 / 搜索：照常工作

---

## 9. 测试用例

### 9.1 手动测试

1. **空库**：开 history → 全空
2. **落库 1 条待估价**：`submit_scan_results` 入 1 条 → 平台估价空 + 状态"待估价"
3. **Worker 估价成功**：手动触发 worker → 平台估价填充 + 状态"已估价"
4. **Worker 估价失败**：mock Excel 抛异常 → 状态"估价失败" + [重试] 按钮
5. **点重试**：error 清掉 → 状态"待估价" → 几秒后"已估价"

### 9.2 自动化（可选）

```python
# tests/test_history_page.py
def test_pending_row_shows_empty_platform_price(): ...
def test_estimated_row_shows_crawled_platform_price(): ...
def test_failed_row_has_retry_button(): ...
def test_retry_resets_status_to_pending(): ...
```

---

## 10. 实施顺序

| Step | 内容 | 估时 | 风险 |
|------|------|------|------|
| 1 | db_manager_v2.py 加 `clear_estimate_error()` | 5min | 低 |
| 2 | estimation_worker.py 加 `retry_estimate()` | 5min | 低 |
| 3 | main_window.py 加 product_estimated_signal | 10min | 低 |
| 4 | history_page.py：删除动态 platform 列 | 5min | 低 |
| 5 | history_page.py：列定义 + 行填充（按上面 B） | 60min | 中 |
| 6 | history_page.py：状态 cellWidget（按 D） | 20min | 中 |
| 7 | history_page.py：操作 cellWidget + 重试（按 E + F） | 20min | 低 |
| 8 | history_page.py：实时刷新（按 G） | 15min | 中 |
| 9 | 全流程手动测试 | 30min | - |

**总计**：~3h

---

## 11. 待用户最后确认

1. ✅ **列名**：9 列名 = `选择 / ID / 游戏 / 原价 / 平台估价 / 溢价 / 状态 / 时间 / 操作` 确认
2. ✅ **平台估价只显示价格**（不显示 platform 名）
3. ✅ **平台估价只显示** `prices_json[crawled_platform]` 这一列的值（其他 3 个 platform 隐藏）
4. ⚠️ **状态文字**："待估价" / "已估价" / "估价失败"（与之前确认一致）
5. ⚠️ **重试按钮**：50×24 大小，在"操作"列
6. ⚠️ **"完全空"**：cell 边框还在，只是不显示文字

> 📝 **如确认 OK，回复"OK"或"开始改"，我按本方案改代码**
