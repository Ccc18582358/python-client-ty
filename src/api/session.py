#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
登录会话单例

存储位置：~/.jingxi_client/config.json 里的 auth 子键
字段：token / username / expire_ms（绝对时间戳，毫秒）/ saved_at
"""

import sys
import time
from pathlib import Path

# 让 Session 类无论从哪个 cwd 都能 import config.Config
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.config import Config  # noqa: E402


_AUTH_KEY = "auth"
_TOKEN_KEY = "auth.token"
_USERNAME_KEY = "auth.username"
_EXPIRE_KEY = "auth.expire_ms"
_SAVED_AT_KEY = "auth.saved_at"


class Session:
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
        self._config = Config()
        self.token = self._config.get(_TOKEN_KEY) or None
        self.username = self._config.get(_USERNAME_KEY) or None
        self.expire_ms = self._config.get(_EXPIRE_KEY)
        # 内存缓存
        if self.token:
            print(f"[session] 已加载 token: user={self.username}, expire_ms={self.expire_ms}")

    def is_authenticated(self):
        if not self.token:
            return False
        if not self.expire_ms:
            return False
        try:
            expire = float(self.expire_ms)
            now_ms = time.time() * 1000
            # 强制要求毫秒级时间戳（> 1e12 毫秒 = 2001-09-09 之后）
            # 防止脏数据（秒级、相对时间、错误的字段值）被误判为未过期
            if expire < 1e12:
                return False  # 时间戳格式错误，视为无效
            # 预留 30s 缓冲
            return now_ms < expire - 30_000
        except (ValueError, TypeError):
            return False

    def set(self, token, expire_ms, username):
        self.token = token
        # 兼容：登录 API 可能给秒级时间戳，自动转毫秒
        self.expire_ms = None
        if expire_ms is not None:
            try:
                v = float(expire_ms)
                if v < 1e12:  # 秒级
                    v = v * 1000
                self.expire_ms = int(v)
            except (ValueError, TypeError):
                self.expire_ms = None

        # 兜底：API 给的时间戳如果不合理（已过期 / < 当前+1天），
        # 自动延长到 7 天后。否则下次启动永远要走登录页。
        now_ms = time.time() * 1000
        one_day_ms = 24 * 60 * 60 * 1000
        seven_days_ms = 7 * one_day_ms
        if self.expire_ms is None or self.expire_ms < now_ms + one_day_ms:
            self.expire_ms = int(now_ms + seven_days_ms)
            print(f"[session] expire_ms 不合理，自动延长到 7 天后：{self.expire_ms}")

        self.username = username
        self._config.set(_TOKEN_KEY, token)
        self._config.set(_USERNAME_KEY, username)
        self._config.set(_EXPIRE_KEY, self.expire_ms)
        self._config.set(_SAVED_AT_KEY, int(time.time()))
        self._config.save()

    def clear(self):
        self.token = None
        self.expire_ms = None
        self.username = None
        for k in (_TOKEN_KEY, _USERNAME_KEY, _EXPIRE_KEY, _SAVED_AT_KEY):
            self._config.delete(k)
        self._config.save()

    def token_header(self):
        """供本地 Flask 等内部调用方使用（带 token 的 header 字典）"""
        if not self.token:
            return {}
        return {"token": self.token}


session = Session()
