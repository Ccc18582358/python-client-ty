#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FilterStateMixin：给页面提供筛选条件持久化能力

业务页面可以重写这两个方法实现自己的状态存取：
- _save_filter_state() -> dict
- _load_filter_state(state: dict) -> None
"""

import json
from pathlib import Path


class FilterStateMixin:
    """筛选条件持久化 mixin

    - 自动找 ui_prefs.json（复用 config_manager.config_dir）
    - 给每个页面分配独立 key（如 "deal_filter", "history_filter"）
    - save/load 由业务页面决定存什么字段
    """

    FILTER_STATE_KEY = ""  # 子类必须设置，例如 "deal_filter"

    def _prefs_path(self) -> Path:
        try:
            from config.config_manager import config_manager
            return Path(config_manager.config_dir) / "ui_prefs.json"
        except Exception:
            return Path.home() / ".jingxi_client" / "ui_prefs.json"

    def _read_prefs(self) -> dict:
        try:
            path = self._prefs_path()
            if not path.exists():
                return {}
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_prefs(self, prefs: dict) -> None:
        try:
            path = self._prefs_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            # 保留原有其它字段
            data = self._read_prefs()
            data.update(prefs)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def save_filter_state(self):
        """把当前筛选条件写入 ui_prefs.json（覆盖本页面 key）"""
        if not self.FILTER_STATE_KEY:
            return
        state = self._save_filter_state() if hasattr(self, "_save_filter_state") else {}
        if not state:
            return
        self._write_prefs({f"filter_{self.FILTER_STATE_KEY}": state})

    def load_filter_state(self):
        """从 ui_prefs.json 读取本页面筛选条件并应用"""
        if not self.FILTER_STATE_KEY:
            return
        prefs = self._read_prefs()
        state = prefs.get(f"filter_{self.FILTER_STATE_KEY}", {})
        if state and hasattr(self, "_load_filter_state"):
            self._load_filter_state(state)
