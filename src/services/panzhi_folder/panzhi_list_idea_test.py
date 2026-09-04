#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IDEA 直跑版的盼之列表页测试脚本。

用法:
1. 先把当前浏览器会话导出成 JSON 快照文件。
2. 修改下面的 `BROWSER_CONTEXT_PATH` 为你的快照路径。
3. 在 IDEA 里直接运行这个脚本。

这个脚本不会打开浏览器，只会用本地 Python 请求列表页。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict


ROOT_DIR = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from services.jingxi_spider_panzhi import PanzhiAccountCrawler  # noqa: E402


# ==================== 这里按你的实际快照文件修改 ====================
BROWSER_CONTEXT_PATH = r"C:\PythonProduct\python-client\src\services\panzhi_folder\browser_snapshot.json"

# 盼之火影忍者
GAME_ID = "11"
GAME_NAME = "火影忍者"

# 列表页参数
PAGE = 1
PAGE_SIZE = 10

# 如需指定代理，填这里；不需要就留空
PROXY = ""


def load_browser_context(path: str) -> Dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"browser snapshot not found: {file_path}")
    return json.loads(file_path.read_text(encoding="utf-8"))


def main() -> int:
    browser_context = load_browser_context(BROWSER_CONTEXT_PATH)

    crawler = PanzhiAccountCrawler(
        game_id=GAME_ID,
        game_name=GAME_NAME,
        page_size=PAGE_SIZE,
        proxy=PROXY or None,
        browser_context=browser_context,
    )

    response_json, err = crawler._fetch_list_page(PAGE)
    print("error =", err)
    print("debug =", json.dumps(crawler._last_list_fetch_debug, ensure_ascii=False, indent=2))

    if not isinstance(response_json, dict):
        print("response =", getattr(response_json, "text", response_json))
        return 2

    data_block = response_json.get("data") or {}
    records = data_block.get("records") or data_block.get("list") or []
    print("success =", response_json.get("success"))
    print("code =", response_json.get("code"))
    print("info =", response_json.get("info"))
    print("record_count =", len(records) if isinstance(records, list) else 0)
    if isinstance(records, list) and records:
        print("first_record =", json.dumps(records[0], ensure_ascii=False, indent=2))
    return 0 if err is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
