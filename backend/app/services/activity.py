"""活动评估业务规则：状态流转、对比引用校验、结项明细生成与审计都收在这里。

修复口径：
1. 状态前置校验：草稿不能直接进入评估，评估中才能结项；每个动作只接受白名单前置状态。
2. 结项必须引用「已执行」的对比组，并且显著性结论只用该执行回执里的数据快照重算，
   不回读对比组当前字段——快照回查口径与历史结项一致。
3. 草稿 / 审批中的对比组一律拒绝引用，拒绝记录写进审计明细（含对比组快照）。
4. 结项评估明细只按（活动方案, 执行编号）生成一次；重复请求幂等返回既有明细，
   并发下由 store 事务锁 + 引用执行编号校验保证不会生成第二份。
5. 已确认的历史结项保持原告警时点和原结论，不参与重算。
"""
from __future__ import annotations

from typing import Any

from app.services.clock import now_iso
from app.services.statistics import two_proportion_z_test
from app.store import store

MODULE = "activity"
COMPARISON_MODULE = "comparison"
EXECUTION_MODULE = "comparison_execution"
ASSESSMENT_MODULE = "activity_assessment"
AUDIT_MODULE = "assessment_audit"
RECEIPT_MODULE = "action_receipt"

REQUIRED_FIELDS = ["方案编号", "活动名称", "活动类型"]
STATUS_ORDER = ["草稿", "方案评审中", "评估中", "已结项", "已取消"]
ACTION_RULES = {
    "提交方案评审": "方案评审中",
    "启动评估": "评估中",
    "结项评估": "已结项",
    "取消方案": "已取消",
}
# 动作 -> 允许的前置状态。未列出的组合一律拒绝（ACTI-0001 草稿不能启动评估）。
# 「已结项」只在结项状态为「待复核」（对比组产生了新执行回执）时允许重新结项。
ACTION_SOURCES = {
    "提交方案评审": {"草稿"},
    "启动评估": {"方案评审中"},
    "结项评估": {"评估中", "已结项"},
    "取消方案": {"草稿", "方案评审中", "评估中"},
}
EXECUTABLE_STATUS = "已执行"
HISTORICAL_SCOPE = "历史结项口径"
CURRENT_SCOPE = "当前校正口径"


def _now() -> str:
    return now_iso()


class ActivityService:
    # ------------------------------------------------------------------ 查询
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        name: str | None = None,
        activity_type: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("方案编号", ""))]
        if name:
            rows = [row for row in rows if name in str(row.get("活动名称", ""))]
        if activity_type:
            rows = [row for row in rows if activity_type in str(row.get("活动类型", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_entry_by_code(self, code: str) -> dict[str, Any] | None:
        return store.find_by(MODULE, "方案编号", code)

    def comparison_for(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        code = str(entry.get("对照方案") or "")
        if not code:
            return None
        return store.find_by(COMPARISON_MODULE, "对比编号", code)

    def latest_execution(self, comparison: dict[str, Any] | None) -> dict[str, Any] | None:
        if not comparison:
            return None
        code = comparison.get("最新执行编号")
        if not code:
            return None
        return store.find_by(EXECUTION_MODULE, "执行编号", code)

    def assessments(
        self, activity: dict[str, Any] | None = None, *, include_audit: bool = True
    ) -> list[dict[str, Any]]:
        rows = store.rows(ASSESSMENT_MODULE)
        if activity is not None:
            rows = [
                row for row in rows
                if row.get("活动方案id") == activity.get("id")
                or row.get("活动方案编号") == activity.get("方案编号")
            ]
        items = [self._normalize_assessment(row, include_audit=include_audit) for row in rows]
        items = [item for item in items if item.get("结项状态") != "已撤销"]
        return sorted(items, key=lambda item: (str(item.get("生成时间") or ""), item.get("id") or 0))

    def audit_records(self, activity_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(AUDIT_MODULE)
        if activity_code:
            rows = [row for row in rows if row.get("活动方案编号") == activity_code]
        return rows

    def result_lookup(self, entry: dict[str, Any]) -> dict[str, Any]:
        """活动结果回查：给出可用执行回执、既有结项明细与被拒绝的对比引用。"""
        comparison = self.comparison_for(entry)
        execution = self.latest_execution(comparison) if comparison else None
        assessments = self.assessments(entry)
        rejected = self._rejected_comparisons(entry, comparison)
        return {
            "活动方案id": entry.get("id"),
            "活动方案编号": entry.get("方案编号"),
            "活动状态": entry.get("status"),
            "结项状态": entry.get("结项状态"),
            "对照方案": entry.get("对照方案"),
            "引用的执行编号": entry.get("引用执行编号"),
            "结项明细编号": entry.get("结项明细编号"),
            "对比组状态": (comparison or {}).get("status"),
            "可引用执行": execution is not None,
            "执行回执": execution,
            "结项评估明细": assessments,
            "被拒绝的对比引用": rejected,
            "审计明细": self.audit_records(str(entry.get("方案编号"))),
            "口径说明": self._scope_note(entry, assessments, rejected),
        }

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            entry = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            for optional in ("目标人群", "核心指标", "对照方案", "评估周期天", "显著性阈值"):
                if optional in values:
                    entry[optional] = values[optional]
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["结项状态"] = "未结项"
            entry["引用执行编号"] = None
            entry["结项明细编号"] = None
            entry["历史告警时点"] = None
            entry["记录版本"] = 1
            store.rows(MODULE).append(entry)
        return entry, []

    # ------------------------------------------------------------------ 动作
    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """单条动作入口。返回 (记录, 说明, 结项明细)。"""
        values = values or {}
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"活动方案 {entry_id} 不存在或已归档", None
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于活动评估可执行范围", None
            current = str(entry.get("status") or "")
            allowed = ACTION_SOURCES[action]
            if current not in allowed:
                return None, (
                    f"活动方案当前状态为「{current}」，不能执行「{action}」；"
                    f"仅允许在 {'、'.join(sorted(allowed))} 状态执行"
                ), None
            if action == "结项评估":
                if entry.get("历史告警时点"):
                    return None, "该活动方案为历史已确认结项，保持原告警时点状态，不允许重新结项", None
                if current == "已结项" and entry.get("结项状态") != "待复核":
                    return None, "该活动方案已结项且结论已复核，不能重复结项", None
                return self._finalize(entry, values)
            self._apply_action(entry, action)
            return entry, f"活动方案已{action}", None

    def run_batch(
        self,
        entry_ids: list[int],
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[bool, str, list[dict[str, Any]], list[dict[str, Any]]]:
        """多选整组提交：先逐条核对，全部通过才整组提交。

        任意一条不通过时整组不落库，避免出现「一半结项一半没结项」的中间态。
        返回 (是否整组成功, 说明, 逐条结果, 生成的明细)。
        """
        values = values or {}
        try:
            ids = list(dict.fromkeys(int(item) for item in entry_ids))
        except (TypeError, ValueError):
            return False, "所选记录编号必须是整数", [], []
        if not ids:
            return False, "请至少选择一条活动方案", [], []
        if action not in ACTION_RULES:
            return False, f"动作「{action}」不属于活动评估可执行范围", [], []
        try:
            with store.transaction(
                MODULE, COMPARISON_MODULE, EXECUTION_MODULE,
                ASSESSMENT_MODULE, AUDIT_MODULE, RECEIPT_MODULE,
            ):
                checks = self._precheck_batch(ids, action, values)
                if any(not item["ok"] for item in checks):
                    # 抛给 transaction 触发整体回退（预检是只读的，回退是兜底）。
                    raise _BatchRejected(checks)
                created: list[dict[str, Any]] = []
                for entry_id in ids:
                    entry = store.find(MODULE, entry_id)
                    if entry is None:  # 预检已覆盖，这里只是防御
                        raise _BatchRejected(checks)
                    if action == "结项评估":
                        _, message, assessment = self._finalize(entry, values)
                        if assessment is None:
                            # 预检与提交之间状态被改（并发）：整组回退并说明原因。
                            raise _BatchRejected([{
                                "id": entry_id,
                                "方案编号": entry.get("方案编号"),
                                "status": entry.get("status"),
                                "ok": False,
                                "message": message,
                            }])
                        created.append(assessment)
                    else:
                        self._apply_action(entry, action)
        except _BatchRejected:
            raise
        return True, f"已对 {len(ids)} 条活动方案整组执行「{action}」", checks, created

    def _precheck_batch(
        self, ids: list[int], action: str, values: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """逐条核对并返回可复核的结论；任何一条失败都给出可读原因。"""
        checks: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry_id in ids:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                checks.append({"id": entry_id, "方案编号": None, "status": None,
                               "ok": False, "message": f"活动方案 {entry_id} 不存在"})
                continue
            code = str(entry.get("方案编号"))
            if code in seen:
                checks.append({"id": entry_id, "方案编号": code, "status": entry.get("status"),
                               "ok": False, "message": f"活动方案 {code} 重复选择"})
                continue
            seen.add(code)
            ok, message = self._precheck(entry, action, values)
            checks.append({"id": entry_id, "方案编号": code, "status": entry.get("status"),
                           "ok": ok, "message": message})
        return checks

    def _precheck(
        self, entry: dict[str, Any], action: str, values: dict[str, Any]
    ) -> tuple[bool, str]:
        """只读核对：不产生任何写入，供整组提交前逐条预检。"""
        if action not in ACTION_RULES:
            return False, f"动作「{action}」不属于活动评估可执行范围"
        current = str(entry.get("status") or "")
        if current not in ACTION_SOURCES[action]:
            return False, f"当前状态「{current}」不允许执行「{action}」"
        if action == "结项评估":
            if entry.get("历史告警时点"):
                return False, "该活动方案为历史已确认结项，保持原告警时点状态，不允许重新结项"
            if current == "已结项" and entry.get("结项状态") != "待复核":
                return False, "该活动方案已结项且结论已复核，不能重复结项"
        if action != "结项评估":
            return True, "可执行"
        comparison = self.comparison_for(entry)
        if comparison is None:
            return False, f"对照方案「{entry.get('对照方案')}」不存在，无法结项"
        execution = self.latest_execution(comparison)
        if comparison.get("status") != EXECUTABLE_STATUS or execution is None:
            return False, (
                f"对照方案「{comparison.get('对比编号')}」当前状态为「{comparison.get('status')}」，"
                "未执行对比不能支撑结项结论"
            )
        expected = str(values.get("expected_execution") or "").strip()
        if expected and expected != execution.get("执行编号"):
            return False, f"对比组已有新的执行回执 {execution.get('执行编号')}，请先复核最新结论"
        if current == "已结项":
            if not expected:
                return False, (
                    f"该活动方案已结项；最新执行回执为 {execution.get('执行编号')}，"
                    "请携带 expected_execution 复核后再重新结项"
                )
        return True, "可执行"

    # ------------------------------------------------------------------ 结项
    def _apply_action(self, entry: dict[str, Any], action: str) -> None:
        entry["status"] = ACTION_RULES[action]
        entry["pending"] = entry["status"] not in ("已结项", "已取消")
        entry["abnormal"] = False
        self._bump(entry)

    def _finalize(
        self,
        entry: dict[str, Any],
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """生成结项评估明细；结论只引用已执行对比的数据快照。"""
        current = str(entry.get("status") or "")
        request_id = str(values.get("request_id") or "").strip()
        if request_id:
            receipt = store.find_by(RECEIPT_MODULE, "请求编号", request_id)
            if receipt is not None:
                existing = store.find(ASSESSMENT_MODULE, receipt.get("明细id"))
                if existing is not None and existing.get("活动方案编号") == entry.get("方案编号"):
                    return entry, f"结项评估已处理（幂等），明细编号 {existing.get('明细编号')}", existing
                return None, f"请求编号 {request_id} 已用于其他结项，不能复用", None

        comparison = self.comparison_for(entry)
        rejected = self._rejected_comparisons(entry, comparison)
        if comparison is None:
            self._audit(entry, None, "拒绝结项", f"对照方案「{entry.get('对照方案')}」不存在")
            return None, f"对照方案「{entry.get('对照方案')}」不存在，无法结项", None
        execution = self.latest_execution(comparison)
        if comparison.get("status") != EXECUTABLE_STATUS or execution is None:
            for item in rejected:
                self._audit(
                    entry,
                    item,
                    "拒绝草稿对比",
                    f"对比组 {item.get('对比编号')} 状态为「{item.get('status')}」，"
                    "未执行对比不能支撑结项结论",
                )
            return None, (
                f"对照方案「{comparison.get('对比编号')}」当前状态为「{comparison.get('status')}」，"
                "未执行对比不能支撑结项结论"
            ), None

        expected = str(values.get("expected_execution") or "").strip()
        if expected and expected != execution.get("执行编号"):
            self._audit(
                entry,
                comparison,
                "拒绝过期引用",
                f"提交引用 {expected}，最新执行回执为 {execution.get('执行编号')}，需先复核最新结论",
            )
            return None, (
                f"对比组已有新的执行回执 {execution.get('执行编号')}，请先复核最新结论"
            ), None
        if current == "已结项":
            # 只有复核人显式确认「已看过最新回执」才允许重结项，避免并发下静默改写当前结论。
            if not expected:
                return None, (
                    f"该活动方案已结项；对比组最新执行回执为 {execution.get('执行编号')}，"
                    "请携带 expected_execution 复核后再重新结项"
                ), None
            if expected != execution.get("执行编号"):
                return None, f"复核引用的执行回执 {expected} 已不是最新回执，请重新回查", None

        # 同一活动同一执行只允许一份有效结项明细；已撤销的历史明细不再阻塞重结项。
        for existing in store.rows(ASSESSMENT_MODULE):
            if existing.get("活动方案编号") != entry.get("方案编号"):
                continue
            if existing.get("执行编号") != execution.get("执行编号"):
                continue
            if existing.get("结项状态") == "已撤销":
                continue
            return None, (
                f"活动方案 {entry.get('方案编号')} 已就执行回执 {execution.get('执行编号')} "
                f"生成结项明细 {existing.get('明细编号')}，不能重复结项"
            ), None

        snapshot = execution.get("数据快照") or {}
        check = two_proportion_z_test(
            control_sample=int(snapshot.get("对照组样本量") or 0),
            control_converted=int(snapshot.get("对照组转化数") or 0),
            experiment_sample=int(snapshot.get("实验组样本量") or 0),
            experiment_converted=int(snapshot.get("实验组转化数") or 0),
        )
        if not check["ok"]:
            self._audit(entry, comparison, "拒绝结项", f"执行回执快照不可用于检验：{check['reason']}")
            return None, (
                f"执行回执 {execution.get('执行编号')} 快照不可用于检验：{check['reason']}"
            ), None

        # 重新结项时，把被替代的旧明细标记为已撤销——历史记录保留但不再计入当前结论。
        superseded: list[dict[str, Any]] = []
        for existing in store.rows(ASSESSMENT_MODULE):
            if existing.get("活动方案编号") != entry.get("方案编号"):
                continue
            if existing.get("结项状态") == "已撤销":
                continue
            superseded.append(existing)
            existing["结项状态"] = "已撤销"
            existing["撤销原因"] = (
                f"已按执行回执 {execution.get('执行编号')} 重新结项，原明细留档不再计入当前结论"
            )

        threshold = float(execution.get("显著性阈值") or entry.get("显著性阈值") or 0.05)
        significant = check["p_value"] < threshold
        conclusion = "显著" if significant else "不显著"
        if check["lift"] is not None and check["lift"] < 0:
            conclusion = f"{conclusion}（实验组低于对照组）"

        generated_at = _now()
        assessment = {
            "id": store.next_id(ASSESSMENT_MODULE),
            "明细编号": self._next_assessment_code(),
            "登记来源": "对比分析待处理明细",
            "待处理明细编号": comparison.get("待处理明细编号"),
            "活动方案id": entry.get("id"),
            "活动方案编号": entry.get("方案编号"),
            "对比组id": comparison.get("id"),
            "对比编号": comparison.get("对比编号"),
            "执行编号": execution.get("执行编号"),
            "快照版本": execution.get("快照版本"),
            "数据快照": dict(snapshot),
            "对照组转化率": check["control_rate"],
            "实验组转化率": check["experiment_rate"],
            "提升率": check["lift"],
            "z值": check["z_score"],
            "p值": check["p_value"],
            "显著性阈值": threshold,
            "显著性结论": conclusion,
            "结项状态": "已确认",
            "数据口径": CURRENT_SCOPE,
            "告警时点": generated_at,
            "生成时间": generated_at,
            "复核人": str(values.get("reviewer") or "值班管理员"),
            "复核说明": str(values.get("remark") or "按执行回执快照重算显著性"),
            "替代明细": [
                {"明细编号": item.get("明细编号"), "执行编号": item.get("执行编号")}
                for item in superseded
            ],
            "被拒绝的对比引用": [
                {"对比编号": item.get("对比编号"), "status": item.get("status"),
                 "拒绝原因": "未执行对比不能支撑结项结论"}
                for item in rejected
            ],
            "审计明细": [],
        }
        store.rows(ASSESSMENT_MODULE).append(assessment)
        for item in rejected:
            self._audit(
                entry,
                item,
                "拒绝草稿对比",
                f"对比组 {item.get('对比编号')} 状态为「{item.get('status')}」，"
                "未纳入本次结项结论，仅留审计",
            )
        assessment["审计明细"] = [
            dict(row) for row in self.audit_records(str(entry.get("方案编号")))
        ]

        entry["status"] = "已结项"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["结项状态"] = "已确认"
        entry["引用执行编号"] = execution.get("执行编号")
        entry["结项明细编号"] = assessment["明细编号"]
        self._bump(entry)

        # 对比分析列表的状态回写：同一事务内更新，页面两侧同时可见。
        comparison["结项明细编号"] = assessment["明细编号"]
        comparison["评估状态"] = "已复核"
        comparison["回写活动编号"] = entry.get("方案编号")
        comparison["pending"] = False

        if request_id:
            store.rows(RECEIPT_MODULE).append({
                "id": store.next_id(RECEIPT_MODULE),
                "请求编号": request_id,
                "动作": "结项评估",
                "活动方案id": entry.get("id"),
                "明细id": assessment["id"],
                "记录时间": generated_at,
            })
        message = (
            f"活动方案 {entry.get('方案编号')} 已结项，明细 {assessment['明细编号']}，"
            f"引用执行 {execution.get('执行编号')}，结论 {conclusion}"
        )
        return entry, message, assessment

    # ------------------------------------------------------------------ 审计
    def _rejected_comparisons(
        self, entry: dict[str, Any], current: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """找出与该活动有关、但不满足「已执行」的对比组；草稿对比进入审计而不进入结论。"""
        rejected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for code in (entry.get("对照方案"), (current or {}).get("对比编号")):
            if not code or code in seen:
                continue
            seen.add(str(code))
            row = store.find_by(COMPARISON_MODULE, "对比编号", code)
            if row is None:
                continue
            if row.get("status") != EXECUTABLE_STATUS or not self.latest_execution(row):
                rejected.append({
                    "对比编号": row.get("对比编号"),
                    "对比名称": row.get("对比名称"),
                    "status": row.get("status"),
                    "对比指标": row.get("对比指标"),
                    "实验组筛选": row.get("实验组筛选"),
                    "对照组筛选": row.get("对照组筛选"),
                    "对比组id": row.get("id"),
                })
        return rejected

    def _audit(
        self,
        entry: dict[str, Any],
        comparison: dict[str, Any] | None,
        audit_type: str,
        message: str,
    ) -> dict[str, Any]:
        """写一条审计；同一活动 + 对比 + 类型只保留最新一条，避免刷屏。"""
        code = (comparison or {}).get("对比编号")
        for row in store.rows(AUDIT_MODULE):
            if (
                row.get("活动方案编号") == entry.get("方案编号")
                and row.get("对比编号") == code
                and row.get("审计类型") == audit_type
            ):
                row["审计说明"] = message
                row["记录时间"] = _now()
                row["数据快照"] = self._comparison_snapshot(comparison)
                return row
        row = {
            "id": store.next_id(AUDIT_MODULE),
            "审计编号": self._next_audit_code(),
            "活动方案编号": entry.get("方案编号"),
            "对比编号": code,
            "审计类型": audit_type,
            "审计说明": message,
            "数据快照": self._comparison_snapshot(comparison),
            "记录时间": _now(),
            "操作人": "服务端校验",
        }
        store.rows(AUDIT_MODULE).append(row)
        return row

    @staticmethod
    def _comparison_snapshot(comparison: dict[str, Any] | None) -> dict[str, Any]:
        if not comparison:
            return {}
        keys = ["对比编号", "对比名称", "status", "对比指标", "实验组筛选", "对照组筛选",
                "对照组样本量", "对照组转化数", "实验组样本量", "实验组转化数", "最新执行编号"]
        return {key: comparison.get(key) for key in keys if key in comparison}

    # ------------------------------------------------------------------ 口径
    @staticmethod
    def _normalize_assessment(row: dict[str, Any], *, include_audit: bool) -> dict[str, Any]:
        """历史明细兼容：缺失字段补默认值，历史口径不重算 z/p。"""
        item = dict(row)
        item.setdefault("数据口径", HISTORICAL_SCOPE if str(item.get("明细编号", "")).startswith("ASMT-H") else CURRENT_SCOPE)
        item.setdefault("被拒绝的对比引用", [])
        snapshot = item.get("数据快照")
        if not snapshot:
            snapshot = {
                "对照组样本量": item.get("对照组样本量"),
                "对照组转化数": item.get("对照组转化数"),
                "实验组样本量": item.get("实验组样本量"),
                "实验组转化数": item.get("实验组转化数"),
                "采集时点": item.get("告警时点") or item.get("生成时间"),
            }
            item["数据快照"] = snapshot
        if include_audit:
            item["审计明细"] = [
                audit for audit in store.rows(AUDIT_MODULE)
                if audit.get("活动方案编号") == item.get("活动方案编号")
            ]
        return item

    @staticmethod
    def _scope_note(
        entry: dict[str, Any],
        assessments: list[dict[str, Any]],
        rejected: list[dict[str, Any]],
    ) -> str:
        if entry.get("结项状态") == "已确认" and entry.get("status") == "已结项":
            if any(item.get("数据口径") == HISTORICAL_SCOPE for item in assessments):
                return "历史已确认结项，维持原告警时点状态与历史结项口径"
        if not assessments and rejected:
            return "尚无可用结项明细：对照方案未执行，草稿对比仅进入审计明细"
        if not assessments:
            return "尚无结项明细，当前版本可凭执行回执契约材料对照"
        return "当前结项明细按执行回执数据快照重算，可逐条回查"

    @staticmethod
    def _bump(entry: dict[str, Any]) -> None:
        entry["记录版本"] = int(entry.get("记录版本") or 0) + 1

    @staticmethod
    def _next_assessment_code() -> str:
        """按已用编号里的最大数字递增，保证连续且不重号。"""
        numbers = []
        for row in store.rows(ASSESSMENT_MODULE):
            code = str(row.get("明细编号") or "")
            suffix = code.rsplit("-", 1)[-1]
            if suffix.isdigit():
                numbers.append(int(suffix))
        return f"ASMT-{max(numbers, default=0) + 1:04d}"

    @staticmethod
    def _next_audit_code() -> str:
        numbers = []
        for row in store.rows(AUDIT_MODULE):
            code = str(row.get("审计编号") or "")
            suffix = code.rsplit("-", 1)[-1]
            if suffix.isdigit():
                numbers.append(int(suffix))
        return f"AUDT-{max(numbers, default=0) + 1:04d}"


class _BatchRejected(Exception):
    """整组预检未通过：携带逐条结果，事务回退后由调用方转成响应。"""

    def __init__(self, checks: list[dict[str, Any]]) -> None:
        super().__init__("batch rejected")
        self.checks = checks
