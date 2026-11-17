"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

并发口径：
- 所有写操作都必须持有 ``store.lock``（可重入锁），动作服务里用 ``with store.lock`` 或
  ``store.transaction(...)`` 包住一整批状态变更，保证「对比组 + 活动方案 + 结项明细 + 审计」
  要么一起生效、要么一起回退。
- ``transaction`` 只深拷贝涉及的模块；异常时按备份整体还原，避免留下半条记录。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = self._build_tables()

    @staticmethod
    def _build_tables() -> dict[str, list[dict[str, Any]]]:
        # deepcopy：示例数据里有嵌套结构（执行记录、观测数据），浅拷贝会让不同实例互相串数据。
        return {name: [copy.deepcopy(row) for row in rows] for name, rows in SEED_ROWS.items()}

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def reset(self) -> None:
        """把仓库恢复到示例数据初始状态；测试与本地复现用。"""
        with self._lock:
            self._tables = self._build_tables()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int | str) -> dict[str, Any] | None:
        try:
            wanted = int(entry_id)
        except (TypeError, ValueError):
            return None
        for row in self.rows(module):
            if int(row.get("id", 0)) == wanted:
                return row
        return None

    def find_by(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def next_id(self, module: str) -> int:
        """在调用方已持有锁的前提下分配主键，避免并发下出现重复 id。"""
        with self._lock:
            return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    @contextmanager
    def transaction(self, *modules: str) -> Iterator[None]:
        """对指定模块做一次原子变更；异常时整体回退后继续抛出。"""
        with self._lock:
            backup = {name: copy.deepcopy(self.rows(name)) for name in modules}
            try:
                yield
            except BaseException:
                for name, rows in backup.items():
                    self._tables[name] = rows
                raise

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
