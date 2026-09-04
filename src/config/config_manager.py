#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置管理器

分层架构：
  - config.json         主题/字体/logo/品牌名  （写在哪都行）
  - game_platform.json  游戏+平台（业务配置，动态可配） ← 本文件核心
  - ui_prefs.json       UI 偏好（记住账号密码、列宽等）

数据模型（game_platform.json）：
  {
    "version": 1,
    "games": [
      {"id": "huoying", "name": "火影忍者", "alias": ["火影"], "enabled": true, "sort_order": 1}
    ],
    "platforms": [
      {
        "id": "panzhi",
        "name": "盼之",
        "enabled": true,
        "sort_order": 1,
        "base_url": "https://www.panzhi.com",
        "games": [
          {"name": "火影忍者", "game_id": "10032", "sheet_name": "火影忍者", "cell": "H3"}
        ]
      }
    ]
  }

对外 API（保持向后兼容 + 新增）：
  - 读取：get_xxx / get_xxx_names / get_enabled_xxx
  - 写：add_xxx / update_xxx / delete_xxx / toggle_xxx
  - 落盘：save_game_platform()
  - 信号：configChanged（配置变更时 emit，UI 监听后自动刷新）
"""

import os
import sys
import json
from pathlib import Path
from PySide6.QtCore import QObject, Signal


class ConfigManager(QObject):
    """全局配置管理器（单例）

    配置分层：
      - self.config        读自 config.json（主题/logo/品牌名）
      - self.gp_config     读自 game_platform.json（业务：游戏+平台）

    信号：
      configChanged(change_type: str)  str ∈ {'games', 'platforms', 'all'}
    """
    _instance = None

    # 信号：配置变更通知 UI 自动刷新
    # change_type: 'games' / 'platforms' / 'all'
    configChanged = Signal(str)

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # QObject.__init__ 必须调一次（单例模式下用 _initialized 标志防重复）
        if getattr(self, "_initialized", False):
            return
        super().__init__()

        # ===== 路径 =====
        if getattr(sys, 'frozen', False):
            self.app_dir = os.path.dirname(sys.executable)
        else:
            self.app_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        self.config_dir = os.path.join(self.app_dir, "config")
        if not os.path.exists(self.config_dir):
            os.makedirs(self.config_dir)

        self.config_path = os.path.join(self.config_dir, "config.json")
        self.gp_config_path = os.path.join(self.config_dir, "game_platform.json")

        # ===== 默认业务配置（用户视角只有两数组） =====
        # 程序内部用 _expand_to_full 自动展开成完整数据
        # 落盘永远存简化版（用户改 json 也只改两个数组）
        self.default_gp_config = {
            "games": ["火影忍者", "王者荣耀", "和平精英", "英雄联盟", "其他"],
            "platforms": ["盼之", "螃蟹", "7881", "氪金兽"]
        }

        # 拼音 id 映射表（让 id 可读，遇到没在表里的就用 ascii fallback）
        self._PINYIN_ID_MAP = {
            "盼之": "panzhi",
            "螃蟹": "pangxie",
            "氪金兽": "kejinshou",
            "7881": "7881",
            "火影忍者": "huoyingrenzhe",
            "王者荣耀": "wangzherongyao",
            "和平精英": "hepingjingying",
            "英雄联盟": "yingxionglianmeng",
            "其他": "qita",
        }

        # ===== 加载两个配置文件 =====
        self.config = self._load_config()       # config.json
        self.gp_config = self._load_gp_config()  # game_platform.json（内部用完整版，文件用简化版）

        self._initialized = True
    
    def _load_config(self):
        """加载 config.json（主题/字体/logo/品牌名）"""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    return json.load(f) or {}
            except Exception as e:
                print(f"加载 config.json 失败: {e}")
                return {}
        return {}

    def _load_gp_config(self) -> dict:
        """加载 game_platform.json（游戏+平台业务配置）

        支持三种格式（自动适配）：
          1. 简化版（用户视角）：{"games": [str], "platforms": [str]}
          2. 完整版（新）：{"games": [dict], "platforms": [dict]}
          3. 完整版（老 v1 字段）：{"version": 1, "games": [dict with id/name/enabled], "platforms": [dict with games list]}

        输出：内部统一用完整版（含 id/alias/sort_order/平台映射等）
        落盘永远存简化版（保持用户视角最简）
        """
        if not os.path.exists(self.gp_config_path):
            self._save_gp_config(self.default_gp_config)
            return self._expand_to_full(self.default_gp_config)

        try:
            with open(self.gp_config_path, 'r', encoding='utf-8') as f:
                data = json.load(f) or {}
        except Exception as e:
            print(f"加载 game_platform.json 失败: {e}，恢复默认")
            try:
                import shutil
                shutil.copy(self.gp_config_path, self.gp_config_path + ".bak")
            except Exception:
                pass
            self._save_gp_config(self.default_gp_config)
            return self._expand_to_full(self.default_gp_config)

        # 检测格式
        games_raw = data.get("games", [])
        if not games_raw:
            full = self._expand_to_full(self.default_gp_config)
            full["game_cell_map"] = self.default_gp_config.get("game_cell_map", {})
            return full
        if isinstance(games_raw[0], str):
            # 简化版 → 展开
            full = self._expand_to_full({"games": games_raw, "platforms": data.get("platforms", [])})
            full["game_cell_map"] = data.get("game_cell_map", {})
            return full
        else:
            # 完整版（兼容老 v1）→ 字段补全
            full = self._normalize_full(data)
            full["game_cell_map"] = data.get("game_cell_map", {})
            return full

    def _save_gp_config(self, data: dict = None) -> bool:
        """保存 game_platform.json（永远存简化版）

        data: 可以是完整版或简化版 —— 都会被自动转成简化版落盘
        """
        if data is None:
            data = self.gp_config
        try:
            # 转简化版
            simplified = self._to_simplified(data)
            tmp_path = self.gp_config_path + ".tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(simplified, f, ensure_ascii=False, indent=2)
            if os.path.exists(self.gp_config_path):
                os.remove(self.gp_config_path)
            os.rename(tmp_path, self.gp_config_path)
            return True
        except Exception as e:
            print(f"保存 game_platform.json 失败: {e}")
            return False

    # ====================================================================
    # 简化版 <-> 完整版 转换（核心：用户只维护两数组，其他自动）
    # ====================================================================
    def _pinyin_id(self, name: str) -> str:
        """根据名字生成 id（拼音表 → ascii fallback）"""
        if name in self._PINYIN_ID_MAP:
            return self._PINYIN_ID_MAP[name]
        # fallback：保留 ASCII 字母数字，转小写
        out = ""
        for ch in name:
            if ch.isascii() and ch.isalnum():
                out += ch
        return out.lower() or f"item{abs(hash(name)) % 100000}"

    def _col_letter(self, n: int) -> str:
        """0→A, 1→B, ..., 7→H, 8→I, ... 用于生成 Excel 列"""
        s = ""
        n += 1
        while n > 0:
            n, r = divmod(n - 1, 26)
            s = chr(65 + r) + s
        return s

    def _expand_to_full(self, minimal: dict) -> dict:
        """把最小配置展开成完整版。

        支持两种 platforms 格式：
          - list[str]:          笛卡尔积(所有平台×所有游戏)
          - dict[str→list[str]]: 每个平台指定自己的游戏列表
            {"螃蟹": ["火影忍者","金铲铲之战"], "盼之": ["火影忍者"]}
        """
        game_names = minimal.get("games", []) or []
        platforms_raw = minimal.get("platforms", []) or []

        # 1. 展开 games
        games_full = []
        for i, gname in enumerate(game_names):
            gid = self._pinyin_id(gname)
            games_full.append({
                "id": gid,
                "name": gname,
                "alias": [],
                "enabled": True,
                "sort_order": i + 1,
            })

        # 2. 判断 platforms 格式
        if isinstance(platforms_raw, dict):
            # 新格式: {平台名: [游戏名列表]}
            platform_entries = list(platforms_raw.items())
        else:
            # 老格式: [平台名, ...] → 每个平台=所有游戏
            platform_entries = [(pname, game_names) for pname in platforms_raw]

        # 3. 展开 platforms
        platforms_full = []
        for pi, (pname, p_game_names) in enumerate(platform_entries):
            pid = self._pinyin_id(pname)
            col = self._col_letter(7 + pi)
            platform_games = []
            for gi, g in enumerate(games_full):
                if g["name"] not in p_game_names:
                    continue  # 跳过该平台不支持的游戏
                platform_games.append({
                    "name": g["name"],
                    "game_id": f"g{gi}p{pi}",
                    "sheet_name": g["name"] if g["name"] != "其他" else "收价文本",
                    "cell": f"{col}3",
                })
            platforms_full.append({
                "id": pid,
                "name": pname,
                "enabled": True,
                "sort_order": pi + 1,
                "base_url": "",
                "games": platform_games,
            })

        return {
            "version": minimal.get("version", 1),
            "games": games_full,
            "platforms": platforms_full,
        }

    def _to_simplified(self, data: dict) -> dict:
        """把完整版/简化版转成简化版（落盘用）

        输出: platforms 用 dict 格式(每个平台指定自己的游戏列表)
        """
        def extract_names(items):
            result = []
            for x in items:
                if isinstance(x, str):
                    result.append(x)
                elif isinstance(x, dict):
                    result.append(x.get("name", ""))
            return [n for n in result if n]

        # platforms 输出为 dict 格式: {平台名: [游戏名列表]}
        platforms_dict = {}
        for p in data.get("platforms", []):
            if isinstance(p, dict):
                pname = p.get("name", "")
                p_games = [g.get("name", "") for g in p.get("games", []) if g.get("name")]
                if pname:
                    platforms_dict[pname] = p_games

        return {
            "games": extract_names(data.get("games", [])),
            "platforms": platforms_dict,
            "game_cell_map": data.get("game_cell_map", {}),
        }

    def _normalize_full(self, data: dict) -> dict:
        """老完整版补字段（id/alias/sort_order/base_url）"""
        games = data.get("games", [])
        for i, g in enumerate(games):
            if "id" not in g:
                g["id"] = self._pinyin_id(g.get("name", f"g{i}"))
            if "alias" not in g:
                g["alias"] = []
            if "enabled" not in g:
                g["enabled"] = True
            if "sort_order" not in g:
                g["sort_order"] = i + 1

        platforms = data.get("platforms", [])
        for i, p in enumerate(platforms):
            if "id" not in p:
                p["id"] = self._pinyin_id(p.get("name", f"p{i}"))
            if "enabled" not in p:
                p["enabled"] = True
            if "sort_order" not in p:
                p["sort_order"] = i + 1
            if "base_url" not in p:
                p["base_url"] = ""
            if "games" not in p:
                p["games"] = []
            # 补每个 game 的 game_id/sheet_name/cell
            for gi, pg in enumerate(p["games"]):
                if "game_id" not in pg:
                    pg["game_id"] = f"g{gi}p{i}"
                if "sheet_name" not in pg:
                    pg["sheet_name"] = pg.get("name", "")
                if "cell" not in pg:
                    pg["cell"] = f"{self._col_letter(7 + i)}3"
        return data

    def _save_config(self, config=None) -> bool:
        """保存 config.json"""
        if config is None:
            config = self.config
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"保存 config.json 失败: {e}")
            return False

    def _emit_change(self, change_type: str):
        """统一发信号（write 方法调）"""
        try:
            self.configChanged.emit(change_type)
        except Exception:
            pass

    # ====================================================================
    # 业务配置读取（从 self.gp_config = game_platform.json 读）
    # ====================================================================
    def get_platforms(self):
        """获取所有平台（按 sort_order 排序）"""
        plats = self.gp_config.get("platforms", [])
        return sorted(plats, key=lambda p: p.get("sort_order", 999))

    def get_platform_names(self):
        """获取所有平台名称列表（按 sort_order 排序）"""
        return [p["name"] for p in self.get_platforms()]

    def get_enabled_platforms(self):
        """获取启用的平台"""
        return [p for p in self.get_platforms() if p.get("enabled", True)]

    def get_enabled_platform_names(self):
        """获取启用的平台名称列表"""
        return [p["name"] for p in self.get_enabled_platforms()]

    def get_games(self):
        """获取所有游戏（按 sort_order 排序）"""
        gs = self.gp_config.get("games", [])
        return sorted(gs, key=lambda g: g.get("sort_order", 999))

    def get_enabled_games(self):
        """获取启用的游戏"""
        return [g for g in self.get_games() if g.get("enabled", True)]

    def get_game_names(self):
        """获取所有游戏名称列表（按 sort_order 排序）"""
        return [g["name"] for g in self.get_games()]

    def get_enabled_game_names(self):
        """获取启用的游戏名称列表"""
        return [g["name"] for g in self.get_enabled_games()]

    def get_platform_game_names(self, platform_name: str) -> list:
        """获取指定平台支持的游戏名称列表。

        Args:
            platform_name: 平台显示名(如 "螃蟹"、"氪金兽")

        Returns:
            该平台下的游戏名列表(按 sort_order 排序)
        """
        for p in self.get_platforms():
            if p["name"] == platform_name:
                return [g["name"] for g in p.get("games", [])]
        return []

    def get_game_id(self, game_id: str) -> dict:
        """通过 game.id 找游戏 dict（找不到返回 None）"""
        for g in self.get_games():
            if g["id"] == game_id:
                return g
        return None

    def get_platform_id(self, platform_id: str) -> dict:
        """通过 platform.id 找平台 dict（找不到返回 None）"""
        for p in self.get_platforms():
            if p["id"] == platform_id:
                return p
        return None

    def get_platform_game_id(self, platform_id, game_name):
        """获取平台对应游戏的 game_id"""
        for p in self.get_platforms():
            if p["id"] == platform_id:
                for g in p.get("games", []):
                    if g["name"] == game_name:
                        return g.get("game_id", "")
        return ""

    def get_platform_game_mapping(self, platform_id: str, game_name: str) -> dict:
        """获取平台×游戏的完整映射（game_id / sheet_name / cell）

        Returns:
            dict 或 {} 找不到时
        """
        for p in self.get_platforms():
            if p["id"] == platform_id:
                for g in p.get("games", []):
                    if g["name"] == game_name:
                        return g
        return {}

    def get_game_sheet_name(self, game_name: str) -> str:
        """获取游戏对应的 Excel sheet 名（取第一个启用平台里这个游戏的 sheet_name）"""
        for p in self.get_enabled_platforms():
            for g in p.get("games", []):
                if g["name"] == game_name:
                    return g.get("sheet_name", "收价文本")
        return "收价文本"

    def get_platform_cell_for_game(self, platform_id: str, game_name: str) -> str:
        """获取平台×游戏的 Excel 单元格位置（如 H3）"""
        for p in self.get_platforms():
            if p["id"] == platform_id:
                for g in p.get("games", []):
                    if g["name"] == game_name:
                        return g.get("cell", "")
        return ""

    def get_cell_map_for_game(self, game_name: str) -> dict:
        """获取游戏→{平台→cell} 映射（从 gp_config.game_cell_map 读）

        配置示例（game_platform.json）：
          "game_cell_map": {
              "火影忍者": {
                  "盼之":   "H3",
                  "螃蟹":   "I3",
                  "7881":   "J3",
                  "氪金兽": "K3"
              }
          }

        Args:
            game_name: 游戏名（如 "火影忍者"）

        Returns:
            {平台名: cell位置, ...} 字典，没配则返回 {}
        """
        return self.gp_config.get("game_cell_map", {}).get(game_name, {})

    # ====================================================================
    # 业务配置写入（全部加信号广播 + 落盘）
    # ====================================================================
    def _platforms_to_dict(self) -> dict:
        """将当前完整版 platforms 转为 {平台名: [游戏名列表]} 字典。"""
        result = {}
        for p in self.gp_config.get("platforms", []):
            if isinstance(p, dict):
                pname = p.get("name", "")
                pgames = [g.get("name", "") for g in p.get("games", []) if g.get("name")]
                if pname:
                    result[pname] = pgames
        return result

    def _game_names_list(self) -> list:
        """当前 games 的名字列表。"""
        return [g["name"] for g in self.gp_config.get("games", [])]

    def save_game_platform(self) -> bool:
        """显式落盘 game_platform.json（用于 update_platforms/update_games 后）"""
        ok = self._save_gp_config(self.gp_config)
        if ok:
            self._emit_change("all")
        return ok

    def update_platforms(self, platforms):
        """整组替换平台配置"""
        self.gp_config["platforms"] = platforms
        self._save_gp_config()
        self._emit_change("platforms")

    def update_games(self, games):
        """整组替换游戏配置"""
        self.gp_config["games"] = games
        self._save_gp_config()
        self._emit_change("games")

    def add_platform(self, name: str) -> bool:
        """添加平台（按名字，id/cell/game_id 自动生成）

        Args:
            name: 平台显示名
        Returns:
            True 成功，False 已存在同名
        """
        platforms = self.gp_config.get("platforms", [])
        if any(p["name"] == name for p in platforms):
            return False
        plat_dict = self._platforms_to_dict()
        plat_dict[name] = self._game_names_list()
        self.gp_config = self._expand_to_full({
            "games": self._game_names_list(),
            "platforms": plat_dict,
        })
        self._save_gp_config()
        self._emit_change("platforms")
        return True

    def update_platform(self, old_name: str, new_name: str = None, enabled: bool = None) -> bool:
        """更新平台（按名字）

        Args:
            old_name: 原名（用来定位）
            new_name: 新名（None = 不改）
            enabled: 启用状态（None = 不改）
        """
        platforms = self.gp_config.get("platforms", [])
        target = None
        for p in platforms:
            if p["name"] == old_name:
                target = p
                break
        if target is None:
            return False
        plat_dict = self._platforms_to_dict()
        if new_name and new_name != old_name:
            if new_name in plat_dict:
                return False
            plat_dict[new_name] = plat_dict.pop(old_name)
        self.gp_config = self._expand_to_full({
            "games": self._game_names_list(),
            "platforms": plat_dict,
        })
        if enabled is not None:
            for p in self.gp_config["platforms"]:
                if p["name"] == (new_name or old_name):
                    p["enabled"] = bool(enabled)
                    break
        self._save_gp_config()
        self._emit_change("platforms")
        return True

    def add_game(self, name: str) -> bool:
        """添加游戏（按名字，id/sort_order 自动）"""
        games = self.gp_config.get("games", [])
        if any(g["name"] == name for g in games):
            return False
        new_names = [g["name"] for g in games] + [name]
        self.gp_config = self._expand_to_full({
            "games": new_names,
            "platforms": self._platforms_to_dict(),
        })
        self._save_gp_config()
        self._emit_change("games")
        return True

    def update_game(self, old_name: str, new_name: str = None, enabled: bool = None) -> bool:
        """更新游戏（按名字）"""
        games = self.gp_config.get("games", [])
        target = None
        for g in games:
            if g["name"] == old_name:
                target = g
                break
        if target is None:
            return False
        new_game_names = [g["name"] for g in games]
        if new_name and new_name != old_name:
            if new_name in new_game_names:
                return False
            idx = new_game_names.index(old_name)
            new_game_names[idx] = new_name
        self.gp_config = self._expand_to_full({
            "games": new_game_names,
            "platforms": self._platforms_to_dict(),
        })
        if enabled is not None:
            for g in self.gp_config["games"]:
                if g["name"] == (new_name or old_name):
                    g["enabled"] = bool(enabled)
                    break
        self._save_gp_config()
        self._emit_change("games")
        return True

    def delete_platform(self, name: str) -> bool:
        """删除平台（按名字）"""
        plat_dict = self._platforms_to_dict()
        if name not in plat_dict:
            return False
        del plat_dict[name]
        self.gp_config = self._expand_to_full({
            "games": self._game_names_list(),
            "platforms": plat_dict,
        })
        self._save_gp_config()
        self._emit_change("platforms")
        return True

    def delete_game(self, name: str) -> bool:
        """删除游戏（按名字，自动从所有平台里删引用）"""
        games = self.gp_config.get("games", [])
        if not any(g["name"] == name for g in games):
            return False
        new_names = [g["name"] for g in games if g["name"] != name]
        # 同时从平台游戏列表中移除
        plat_dict = self._platforms_to_dict()
        for pname in plat_dict:
            plat_dict[pname] = [g for g in plat_dict[pname] if g != name]
        self.gp_config = self._expand_to_full({
            "games": new_names,
            "platforms": plat_dict,
        })
        self._save_gp_config()
        self._emit_change("games")
        return True

    def toggle_platform(self, name: str, enabled: bool) -> bool:
        """切换平台启用（按名字）"""
        for p in self.gp_config.get("platforms", []):
            if p["name"] == name:
                p["enabled"] = bool(enabled)
                self._save_gp_config()
                self._emit_change("platforms")
                return True
        return False

    def toggle_game(self, name: str, enabled: bool) -> bool:
        """切换游戏启用（按名字）"""
        for g in self.gp_config.get("games", []):
            if g["name"] == name:
                g["enabled"] = bool(enabled)
                self._save_gp_config()
                self._emit_change("games")
                return True
        return False

    def reorder_games(self, new_order_names: list) -> bool:
        """按名字列表重排游戏

        Args:
            new_order_names: 完整的新顺序名字列表（必须包含所有现有游戏）
        """
        cur = [g["name"] for g in self.get_games()]
        if set(new_order_names) != set(cur):
            return False
        self.gp_config = self._expand_to_full({
            "games": new_order_names,
            "platforms": self._platforms_to_dict(),
        })
        self._save_gp_config()
        self._emit_change("games")
        return True

    def reorder_platforms(self, new_order_names: list) -> bool:
        """按名字列表重排平台"""
        plat_dict = self._platforms_to_dict()
        if set(new_order_names) != set(plat_dict.keys()):
            return False
        # Python 3.7+ dict 保持插入顺序
        reordered = {k: plat_dict[k] for k in new_order_names}
        self.gp_config = self._expand_to_full({
            "games": self._game_names_list(),
            "platforms": reordered,
        })
        self._save_gp_config()
        self._emit_change("platforms")
        return True

    def reset_to_default(self) -> bool:
        """重置 game_platform.json 到默认（简化版两数组）"""
        self.gp_config = self._expand_to_full(self.default_gp_config)
        ok = self._save_gp_config(self.gp_config)
        if ok:
            self._emit_change("all")
        return ok

    def reload_gp_config(self) -> bool:
        """从文件重新加载（外部改 json 后调）"""
        self.gp_config = self._load_gp_config()
        self._emit_change("all")
        return True

    # 兼容旧 API（老代码可能用 platform_id）
    def remove_platform(self, name_or_id):
        return self.delete_platform(name_or_id)

    def remove_game(self, name_or_id):
        return self.delete_game(name_or_id)

    # ---- 品牌配置（委托给 api_config，按当前 env 读取）----
    def get_branding(self) -> dict:
        """品牌配置从当前 env 配置文件（config/dev.json 或 prod.json）读取"""
        # 延迟 import 避免循环依赖
        from config.api_config import api_config
        return {
            "logo_path": api_config.logo_path or "",
            "brand_name": api_config.brand_name or "账号估价助手",
        }

    def get_brand_name(self) -> str:
        from config.api_config import api_config
        return api_config.brand_name or "账号估价助手"

    def get_logo_path(self) -> str:
        """返回 logo 绝对路径。优先绝对路径；否则相对 project_root 解析。

        project_root 推断：
            - dev 模式 = python-client/（即 app_dir）
            - frozen 模式 = sys._MEIPASS（PyInstaller 解压目录）或 exe 所在目录
        """
        from config.api_config import api_config
        raw = api_config.logo_path or ""
        if not raw:
            return ""
        p = Path(raw)
        if p.is_absolute() and p.exists():
            return str(p)

        # 收集所有可能的搜索根（按优先级）
        search_roots = []
        if getattr(sys, "frozen", False):
            # PyInstaller 单文件模式：资源解压到 _MEIPASS
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                search_roots.append(Path(meipass))
            # 单目录模式 / 兜底：exe 所在目录
            search_roots.append(Path(sys.executable).parent)
        else:
            search_roots.append(Path(self.app_dir))

        for base in search_roots:
            candidate = (base / raw).resolve()
            if candidate.exists():
                return str(candidate)

        # 兜底：原样返回（交给 QPixmap 处理）
        return raw

    def update_branding(self, logo_path: str = None, brand_name: str = None):
        """更新品牌配置并落盘到当前 env 配置文件（dev.json / prod.json）"""
        from config.api_config import api_config
        path = os.path.join(self.app_dir, "config", f"{api_config.active}.json")
        try:
            data = {}
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            if logo_path is not None:
                data["logo_path"] = logo_path
            if brand_name is not None:
                data["brand_name"] = brand_name
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"[config_manager] update_branding 失败: {e}")
            return False


config_manager = ConfigManager()
