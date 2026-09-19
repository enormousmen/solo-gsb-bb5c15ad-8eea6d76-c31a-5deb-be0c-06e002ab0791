"""运行期配置，全部来自环境变量，均有默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    # SQLite 数据库文件路径；父目录不存在时会自动创建
    db_path: str = os.getenv("CONFIG_DB_PATH", "/data/config.db")
    # uvicorn 监听地址 / 端口
    host: str = os.getenv("CONFIG_HOST", "0.0.0.0")
    port: int = int(os.getenv("CONFIG_PORT", "8000") or "8000")
    # 历史查询单页上限
    history_max_limit: int = int(os.getenv("CONFIG_HISTORY_MAX_LIMIT", "200") or "200")


settings = Settings()
