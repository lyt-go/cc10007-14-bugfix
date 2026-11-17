"""业务规则层：在 import 阶段固定「服务实例」的入口，避免路由层各自 new 一份。"""
from __future__ import annotations

from app.services.activity import ActivityService
from app.services.comparison import ComparisonService

activity_service = ActivityService()
comparison_service = ComparisonService()

__all__ = ["activity_service", "comparison_service"]
