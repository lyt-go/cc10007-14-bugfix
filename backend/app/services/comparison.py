"""对比分析业务规则：状态流转、字段校验、执行回执与筛选口径都收在这里。

关键约束（与活动评估共享）：
- 只有「已执行」的对比组才能支撑活动结项结论；草稿 / 审批中一律拒绝引用。
- 「执行对比」会同时完成三件事，且在同一个事务里：写执行回执（含观测数据快照）、
  把对比组置为已执行、把引用它的活动方案状态回写成「评估中」。
- 回归历史口径：已归档/已执行的历史执行记录只读，重复执行不会改写既有快照。
"""
from __future__ import annotations

from typing import Any

from app.services.clock import now_iso
from app.services.statistics import two_proportion_z_test
from app.store import store

MODULE = "comparison"
EXECUTION_MODULE = "comparison_execution"
ACTIVITY_MODULE = "activity"
REQUIRED_FIELDS = ["对比编号", "对比名称", "实验组筛选"]
STATUS_ORDER = ["草稿", "对比审批中", "已执行", "已归档"]
ACTION_RULES = {"提交对比审批": "对比审批中", "执行对比": "已执行", "归档对比": "已归档"}
# 允许发起某动作的前置状态：未列出的前置状态一律拒绝，避免草稿被直接执行。
ACTION_SOURCES = {
    "提交对比审批": {"草稿"},
    "执行对比": {"对比审批中", "已执行"},
    "归档对比": {"已执行"},
}
# 观测字段可由调用方在执行时覆盖；未提供时回落到种子/既有快照。
OBSERVATION_FIELDS = ["对照组样本量", "对照组转化数", "实验组样本量", "实验组转化数", "显著性阈值"]


class ComparisonService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        name: str | None = None,
        experiment_filter: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("对比编号", ""))]
        if name:
            rows = [row for row in rows if name in str(row.get("对比名称", ""))]
        if experiment_filter:
            rows = [row for row in rows if experiment_filter in str(row.get("实验组筛选", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_entry_by_code(self, code: str) -> dict[str, Any] | None:
        return store.find_by(MODULE, "对比编号", code)

    def executions(self, comparison_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(EXECUTION_MODULE)
        if comparison_id is not None:
            rows = [row for row in rows if row.get("对比组id") == comparison_id]
        return rows

    def latest_execution(self, comparison: dict[str, Any]) -> dict[str, Any] | None:
        """按执行编号回查最近一次执行回执；对比组上的最新执行编号是唯一入口。"""
        code = comparison.get("最新执行编号")
        if not code:
            return None
        return store.find_by(EXECUTION_MODULE, "执行编号", code)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            entry = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["效应量"] = None
            entry["评估状态"] = "未评估"
            entry["回写活动编号"] = None
            entry["结项明细编号"] = None
            entry["最新执行编号"] = None
            store.rows(MODULE).append(entry)
        return entry, []

    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        """执行对比动作；返回 (记录, 说明)。被拒绝时记录为 None。"""
        values = values or {}
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"对比组 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于对比分析可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"
            current = str(entry.get("status") or "")
            allowed = ACTION_SOURCES[action]
            if current not in allowed:
                return None, (
                    f"对比组当前状态为「{current}」，不能执行「{action}」；"
                    f"仅允许在 {'、'.join(sorted(allowed))} 状态执行"
                )
            if action == "执行对比":
                return self._execute(entry, values)
            if action == "提交对比审批" and not self._has_observation(entry):
                return None, "对比组缺少可回查的观测数据，不能提交审批"
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = False
            return entry, f"对比组已{action}"

    @staticmethod
    def _has_observation(entry: dict[str, Any]) -> bool:
        return all(entry.get(field) is not None for field in OBSERVATION_FIELDS if field != "显著性阈值")

    def _execute(
        self, entry: dict[str, Any], values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """写执行回执 + 置已执行 + 回写活动状态，整批原子完成。"""
        with store.transaction(MODULE, EXECUTION_MODULE, ACTIVITY_MODULE):
            observation = self._resolve_observation(entry, values)
            if observation is None:
                return None, "对比组缺少可回查的观测数据，无法执行；请先补齐样本量与转化数"
            check = two_proportion_z_test(
                control_sample=int(observation["对照组样本量"]),
                control_converted=int(observation["对照组转化数"]),
                experiment_sample=int(observation["实验组样本量"]),
                experiment_converted=int(observation["实验组转化数"]),
            )
            if not check["ok"]:
                return None, f"观测数据不可用于显著性检验：{check['reason']}"

            executed_at = now_iso()
            execution = {
                "id": store.next_id(EXECUTION_MODULE),
                "执行编号": self._next_execution_code(),
                "对比组id": entry["id"],
                "对比编号": entry.get("对比编号"),
                "执行状态": "已执行",
                "执行时间": executed_at,
                "显著性阈值": float(observation["显著性阈值"]),
                "快照版本": f"v{len(self.executions(entry['id'])) + 1}",
                "数据快照": {
                    "对照组样本量": int(observation["对照组样本量"]),
                    "对照组转化数": int(observation["对照组转化数"]),
                    "实验组样本量": int(observation["实验组样本量"]),
                    "实验组转化数": int(observation["实验组转化数"]),
                    "采集时点": executed_at,
                },
                "关联活动方案编号": entry.get("回写活动编号"),
                "审计备注": "执行对比回执，活动结项结论只能引用该快照",
            }
            for field in OBSERVATION_FIELDS:
                execution[field] = int(observation[field]) if field != "显著性阈值" else float(observation[field])
            execution["效应量"] = check["lift"]
            store.rows(EXECUTION_MODULE).append(execution)

            entry["status"] = "已执行"
            entry["pending"] = False
            entry["abnormal"] = False
            entry["效应量"] = check["lift"]
            entry["最新执行编号"] = execution["执行编号"]
            entry["评估状态"] = "待复核"

            # 同批回写活动方案：只有能对上「对照方案」的活动才更新。
            written: list[tuple[str, str]] = []
            for activity in store.rows(ACTIVITY_MODULE):
                if not self._references(activity, entry):
                    continue
                if activity.get("历史告警时点"):
                    # 已确认的历史结项保持原告警时点状态，新执行不得覆盖。
                    continue
                if activity.get("status") == "已结项":
                    # 已按旧回执结项：不改写结论，标记待复核并留审计，由人工决定是否重新结项。
                    activity["结项状态"] = "待复核"
                    store.rows("assessment_audit").append({
                        "id": store.next_id("assessment_audit"),
                        "审计编号": self._next_audit_code(),
                        "活动方案编号": activity.get("方案编号"),
                        "对比编号": entry.get("对比编号"),
                        "审计类型": "新执行回执待复核",
                        "审计说明": (
                            f"对比组产生新执行回执 {execution['执行编号']}；"
                            f"既有结项 {activity.get('结项明细编号')} 仍引用 {activity.get('引用执行编号')}"
                        ),
                        "数据快照": dict(execution.get("数据快照") or {}),
                        "记录时间": executed_at,
                        "操作人": "服务端校验",
                    })
                    written.append((str(activity.get("方案编号")), "待复核"))
                    continue
                if activity.get("status") != "评估中":
                    # 还没进入评估的活动保持原状态，不能借执行对比跳级到评估中。
                    continue
                activity["pending"] = True
                activity["abnormal"] = False
                activity["引用执行编号"] = execution["执行编号"]
                written.append((str(activity.get("方案编号")), "评估中·已更新执行编号"))
            if written:
                entry["回写活动编号"] = written[0][0]
                execution["关联活动方案编号"] = written[0][0]
        message = f"对比组已执行对比，执行回执 {execution['执行编号']}"
        if written:
            detail = "、".join(f"{code}（{note}）" for code, note in written)
            message += f"，活动方案状态同批更新：{detail}"
        return entry, message

    @staticmethod
    def _references(activity: dict[str, Any], comparison: dict[str, Any]) -> bool:
        codes = {comparison.get("对比编号")}
        codes.update(
            str(execution.get("对比编号"))
            for execution in store.rows(EXECUTION_MODULE)
            if execution.get("对比组id") == comparison.get("id")
        )
        return str(activity.get("对照方案")) in codes

    def _resolve_observation(
        self, entry: dict[str, Any], values: dict[str, Any]
    ) -> dict[str, Any] | None:
        """优先用调用方提交的观测值，其次回落到已有执行快照；参数名兼容中英文。"""
        merged: dict[str, Any] = {}
        previous = self.latest_execution(entry)
        if previous:
            merged.update({field: previous.get(field) for field in OBSERVATION_FIELDS})
        aliases = {
            "对照组样本量": ("对照组样本量", "control_sample"),
            "对照组转化数": ("对照组转化数", "control_converted"),
            "实验组样本量": ("实验组样本量", "experiment_sample"),
            "实验组转化数": ("实验组转化数", "experiment_converted"),
            "显著性阈值": ("显著性阈值", "threshold"),
        }
        for field, names in aliases.items():
            for name in names:
                if name in values and values[name] is not None:
                    merged[field] = values[name]
                    break
            merged.setdefault(field, entry.get(field))
        merged["显著性阈值"] = merged.get("显著性阈值") or 0.05
        try:
            for field in OBSERVATION_FIELDS:
                if merged.get(field) is None:
                    return None
                merged[field] = float(merged[field])
        except (TypeError, ValueError):
            return None
        return merged

    def _next_execution_code(self) -> str:
        """按已用编号里的最大数字递增，保证连续且不重号。"""
        numbers = []
        for row in store.rows(EXECUTION_MODULE):
            code = str(row.get("执行编号") or "")
            suffix = code.rsplit("-", 1)[-1]
            if suffix.isdigit():
                numbers.append(int(suffix))
        return f"EXEC-{max(numbers, default=0) + 1:04d}"

    @staticmethod
    def _next_audit_code() -> str:
        numbers = []
        for row in store.rows("assessment_audit"):
            code = str(row.get("审计编号") or "")
            suffix = code.rsplit("-", 1)[-1]
            if suffix.isdigit():
                numbers.append(int(suffix))
        return f"AUDT-{max(numbers, default=0) + 1:04d}"
