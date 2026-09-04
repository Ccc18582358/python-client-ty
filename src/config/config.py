#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置管理
"""

import json
import os
from pathlib import Path


class Config:
    """配置管理类"""
    
    def __init__(self):
        """初始化"""
        self.config_file = Path.home() / '.jingxi_client' / 'config.json'
        self.config_data = self._load_config()
    
    def _load_config(self):
        """加载配置
        
        Returns:
            配置字典
        """
        try:
            if self.config_file.exists():
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            print(f"加载配置失败: {str(e)}")
        return {}
    
    def save(self):
        """保存配置"""
        try:
            # 创建目录
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            
            # 保存配置
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config_data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"保存配置失败: {str(e)}")
            return False
    
    def get(self, key, default=None):
        """获取配置
        
        Args:
            key: 配置键
            default: 默认值
            
        Returns:
            配置值
        """
        return self.config_data.get(key, default)
    
    def set(self, key, value):
        """设置配置
        
        Args:
            key: 配置键
            value: 配置值
        """
        self.config_data[key] = value
    
    def delete(self, key):
        """删除配置
        
        Args:
            key: 配置键
        """
        if key in self.config_data:
            del self.config_data[key]
