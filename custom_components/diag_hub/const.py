"""常量。"""
from __future__ import annotations

DOMAIN = "diag_hub"

# 滚动窗口：最近 N 分钟的活动量
WINDOW_MINUTES = 60
# task 采样间隔（秒）：只观察，不做任何 patch
TASK_SAMPLE_SECONDS = 10
# 实体→集成 归属缓存的刷新间隔（秒）
REGISTRY_CACHE_TTL = 600
# 对外展示的条数上限（sensor 属性有 16KB 限制）
TOP_N = 15
# HTTP 接口返回条数上限
HTTP_TOP_N = 60
# 实体级热度条数上限：只给页面用（可滚动查看），sensor 属性不取这份数据
HOT_ENTITY_N = 200
# 接口 token：为空表示不校验（仅限内网测试机使用）
API_TOKEN = ""
