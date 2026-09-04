#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API 环境配置

数据源（按优先级覆盖，高 → 低）：
1. <app_dir>/config/active.json      ★ 主入口：用户编辑这一个文件即可切换环境
2. ~/.jingxi_client/config.json       运行时用户配置（程序内 set_active 改这里）
3. APP_ENV 环境变量                   兼容旧版
4. 兜底 dev

active.json schema（最小可用）：
{
  "active_env": "dev"          // 必填，值 "dev" / "prod"
  // 可选覆盖（一般不用写，写了会覆盖对应 env 文件里的值）：
  // "base_url": "https://x.com",
  // "timeout": 8,
  // "brand_name": "xxx",
  // "logo_path": "logo/xxx.jpg"
}

dev.json / prod.json schema：
{
  "name": "开发环境",
  "base_url": "http://192.168.110.11:8082",
  "timeout": 5,
  "brand_name": "账号估价助手",
  "logo_path": ""
}

打包约定（PyInstaller）：
- 不把 config/ 打进 exe（.spec 里 datas 删掉 ('config', 'config')）
- 构建后用 build.py 把 config/ 整个目录拷到 dist/config/
- 用户想换环境就直接编辑 <exe_dir>/config/active.json
"""

import json
import os
import sys
from pathlib import Path

# 让本文件无论从哪个 cwd 运行都能 import config.config（src/config/config.py）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


_DEFAULT_DEV = {
    "name": "开发环境",
    "base_url": "http://192.168.110.11:8082",
    "timeout": 5,
    "brand_name": "账号估价助手",
    "logo_path": "",
}

_DEFAULT_PROD = {
    "name": "生产环境",
    "base_url": "https://api.nyyyds.com",
    "timeout": 10,
    "brand_name": "账号估价助手",
    "logo_path": "",
}

# 用户运行时还可手动切环境（~/.jingxi_client/config.json）
_RUNTIME_ACTIVE_KEY = "api_active_env"
_RUNTIME_BASE_URL_KEY = "api_base_url"
_RUNTIME_TIMEOUT_KEY = "api_timeout"

# 合法的环境名
_VALID_ENVS = ("dev", "prod")


def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def _config_dir():
    return os.path.join(_app_dir(), "config")


def _env_path(env_name: str) -> str:
    return os.path.join(_config_dir(), f"{env_name}.json")


class APIConfig:
    """API 环境配置（单例）"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        # 运行时配置：复用项目里的 Config（~/.jingxi_client/config.json）
        from config.config import Config
        self._runtime = Config()
        self._refresh()

    def _read_active_file(self) -> dict:
        """读 <app_dir>/config/active.json；不存在或解析失败返回 {}"""
        path = os.path.join(_config_dir(), "active.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"[api_config] 读取 {path} 失败: {e}")
            return {}

    def _resolve_active(self) -> str:
        """解析当前环境名（active.json > runtime > APP_ENV > dev）"""
        # 1. 最高优先级：<app_dir>/config/active.json
        active_data = self._read_active_file()
        active = (active_data.get("active_env") or "").strip().lower()
        if active in _VALID_ENVS:
            return active

        # 2. 运行时配置：~/.jingxi_client/config.json
        rt = self._runtime.get(_RUNTIME_ACTIVE_KEY)
        if rt in _VALID_ENVS:
            return rt

        # 3. 环境变量
        env_var = os.environ.get("APP_ENV", "").strip().lower()
        if env_var in _VALID_ENVS:
            return env_var

        return "dev"

    def _load_env_file(self, env_name: str) -> dict:
        """读 config/{env_name}.json，缺失则用内置 default"""
        defaults = _DEFAULT_DEV if env_name == "dev" else _DEFAULT_PROD
        path = _env_path(env_name)
        if not os.path.exists(path):
            return json.loads(json.dumps(defaults))
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 缺失字段用 default 兜底
            merged = json.loads(json.dumps(defaults))
            if isinstance(data, dict):
                for k, v in data.items():
                    if v is not None and v != "":
                        merged[k] = v
            return merged
        except Exception as e:
            print(f"[api_config] 读取 {path} 失败，使用默认: {e}")
            return json.loads(json.dumps(defaults))

    def _refresh(self):
        self._active = self._resolve_active()
        self._env = self._load_env_file(self._active)
        # active.json 里如果有 base_url / timeout / brand_name / logo_path，覆盖之
        active_data = self._read_active_file()
        for k in ("base_url", "brand_name", "logo_path"):
            v = active_data.get(k)
            if v:
                self._env[k] = v
        timeout = active_data.get("timeout")
        if isinstance(timeout, (int, float)) and timeout > 0:
            self._env["timeout"] = int(timeout)

        # runtime 覆盖 base_url / timeout
        runtime_url = self._runtime.get(_RUNTIME_BASE_URL_KEY)
        if runtime_url:
            self._env["base_url"] = runtime_url
        runtime_timeout = self._runtime.get(_RUNTIME_TIMEOUT_KEY)
        if isinstance(runtime_timeout, (int, float)) and runtime_timeout > 0:
            self._env["timeout"] = int(runtime_timeout)

    @property
    def active(self):
        return self._active

    @property
    def base_url(self):
        return self._env.get("base_url", "").rstrip("/")

    @property
    def timeout(self):
        return int(self._env.get("timeout", 5))

    @property
    def name(self):
        return self._env.get("name", self._active)

    @property
    def brand_name(self):
        return self._env.get("brand_name", "账号估价助手")

    @property
    def logo_path(self):
        return self._env.get("logo_path", "")

    @property
    def envs(self):
        """所有可用环境（name -> label）—— 通过扫描 config/{env}.json 推断"""
        result = {}
        for env_name in _VALID_ENVS:
            if os.path.exists(_env_path(env_name)):
                data = self._load_env_file(env_name)
                result[env_name] = data.get("name", env_name)
        return result

    def set_active(self, env_name):
        """切换环境（写入 <app_dir>/config/active.json + runtime Config）

        优先写 active.json：这样 exe 模式下下次启动仍然有效
        """
        if env_name not in _VALID_ENVS:
            raise ValueError(f"未知环境: {env_name}")

        # 1) 写 active.json（exe 模式主入口）
        active_path = os.path.join(_config_dir(), "active.json")
        try:
            # 读现有，保留 _comment / 其它字段
            existing = {}
            if os.path.exists(active_path):
                try:
                    with open(active_path, "r", encoding="utf-8") as f:
                        existing = json.load(f) or {}
                except Exception:
                    existing = {}
            existing["active_env"] = env_name
            os.makedirs(os.path.dirname(active_path), exist_ok=True)
            with open(active_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[api_config] 写 {active_path} 失败: {e}")

        # 2) 同步写 runtime（兜底：active.json 读不到时仍生效）
        self._runtime.set(_RUNTIME_ACTIVE_KEY, env_name)
        self._runtime.save()

        self._refresh()

    def set_base_url(self, url):
        """覆盖 base_url（写 active.json + runtime）"""
        # 1) 写 active.json
        active_path = os.path.join(_config_dir(), "active.json")
        try:
            existing = {}
            if os.path.exists(active_path):
                try:
                    with open(active_path, "r", encoding="utf-8") as f:
                        existing = json.load(f) or {}
                except Exception:
                    existing = {}
            existing["base_url"] = url
            os.makedirs(os.path.dirname(active_path), exist_ok=True)
            with open(active_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[api_config] 写 {active_path} 失败: {e}")

        # 2) runtime
        self._runtime.set(_RUNTIME_BASE_URL_KEY, url)
        self._runtime.save()
        self._refresh()


api_config = APIConfig()
