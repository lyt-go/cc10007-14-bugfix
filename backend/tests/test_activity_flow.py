"""活动评估 / 对比分析修复的回归测试（只用标准库 unittest，无需额外依赖）。

运行：cd backend && .venv/bin/python -m unittest tests.test_activity_flow -v
"""
from __future__ import annotations

import copy
import json
import threading
import unittest
from typing import Any

from fastapi.testclient import TestClient

from app.main import app
from app.services.statistics import two_proportion_z_test
from app.store import store

client = TestClient(app)


def reset() -> None:
    store.reset()


def act(entry_id: int, action: str, **values: Any) -> dict[str, Any]:
    body = {"values": {"action": action, **values}}
    response = client.post(f"/api/activity/{entry_id}/actions", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def comp(entry_id: int, action: str, **values: Any) -> dict[str, Any]:
    body = {"values": {"action": action, **values}}
    response = client.post(f"/api/comparison/{entry_id}/actions", json=body)
    assert response.status_code == 200, response.text
    return response.json()


class StateMachineTest(unittest.TestCase):
    def setUp(reset_self) -> None:
        reset()

    def test_draft_cannot_start_evaluation(self) -> None:
        """ACTI-0001 是草稿：不能直接进入评估。"""
        entry = client.get("/api/activity/1").json()
        assert entry["status"] == "草稿"
        result = act(1, "启动评估")
        assert result["ok"] is False, result
        assert "草稿" in result["message"]
        assert client.get("/api/activity/1").json()["status"] == "草稿"

    def test_draft_cannot_finalize(self) -> None:
        result = act(1, "结项评估")
        assert result["ok"] is False
        assert client.get("/api/activity/1").json()["status"] == "草稿"

    def test_happy_path_draft_to_closed(self) -> None:
        assert act(1, "提交方案评审")["ok"] is True
        assert act(1, "启动评估")["ok"] is True
        assert act(1, "结项评估")["ok"] is False  # 对比组还是草稿
        # 执行对比（需要先审批）
        assert comp(1, "执行对比")["ok"] is False
        assert comp(1, "提交对比审批")["ok"] is True
        executed = comp(1, "执行对比")
        assert executed["ok"] is True, executed
        assert executed["entry"]["status"] == "已执行"
        # 执行对比同批把活动回写成评估中
        assert client.get("/api/activity/1").json()["status"] == "评估中"
        closed = act(1, "结项评估")
        assert closed["ok"] is True, closed
        assert closed["entry"]["status"] == "已结项"
        assert closed["assessment"]["显著性结论"] == "显著"
        assert closed["assessment"]["执行编号"] == executed["entry"]["最新执行编号"]


class DraftComparisonReferenceTest(unittest.TestCase):
    def setUp(reset_self) -> None:
        reset()

    def test_draft_comparison_not_accepted_and_audited(self) -> None:
        """草稿 COMP-0001 不能支撑结项；被拒记录进入审计明细。"""
        # ACTI-0003 引用的 COMP-0003 已执行，先验证可用
        assert client.get("/api/comparison/3").json()["status"] == "已执行"
        result = act(3, "结项评估")
        assert result["ok"] is True, result
        assessment = result["assessment"]
        assert assessment["执行编号"] == "EXEC-0001"
        assert assessment["显著性结论"] == "显著"

        # 把 ACTI-0001 推到评估中（审批 + 启动），其对照 COMP-0001 仍是草稿
        assert act(1, "提交方案评审")["ok"] is True
        assert act(1, "启动评估")["ok"] is True
        rejected = act(1, "结项评估")
        assert rejected["ok"] is False
        assert "草稿" in rejected["message"]
        lookup = client.get("/api/activity/results", params={"code": "ACTI-0001"}).json()
        assert lookup["可引用执行"] is False
        assert lookup["结项评估明细"] == []
        codes = [item["对比编号"] for item in lookup["被拒绝的对比引用"]]
        assert "COMP-0001" in codes
        audit_types = {item["审计类型"] for item in lookup["审计明细"]}
        assert "拒绝草稿对比" in audit_types


class SnapshotRecomputeTest(unittest.TestCase):
    def setUp(reset_self) -> None:
        reset()

    def test_conclusion_uses_execution_snapshot_not_current_fields(self) -> None:
        """结论必须由执行回执里的快照重算；对比组后续字段变化不影响既有结论。"""
        assert act(3, "结项评估")["ok"] is True
        assessment = client.get("/api/activity/assessment", params={"code": "ACTI-0003"}).json()["items"][0]
        snapshot = assessment["数据快照"]
        expected = two_proportion_z_test(
            control_sample=snapshot["对照组样本量"],
            control_converted=snapshot["对照组转化数"],
            experiment_sample=snapshot["实验组样本量"],
            experiment_converted=snapshot["实验组转化数"],
        )
        assert assessment["z值"] == expected["z_score"]
        assert assessment["p值"] == expected["p_value"]

        # 篡改对比组当前观测字段——结项明细与回查结论都不应改变
        comparison = store.find("comparison", 3)
        comparison["实验组转化数"] = 10
        lookup = client.get("/api/activity/results", params={"code": "ACTI-0003"}).json()
        assert lookup["结项评估明细"][0]["p值"] == expected["p_value"]
        assert lookup["结项评估明细"][0]["数据快照"] == snapshot


class BatchSubmitTest(unittest.TestCase):
    def setUp(reset_self) -> None:
        reset()

    def test_batch_all_or_nothing(self) -> None:
        """多选整组：有一条不可结项则整组不落库。"""
        # ACTI-0003 可结项，ACTI-0001 不可（草稿）
        body = {"values": {"action": "结项评估", "ids": [3, 1]}}
        response = client.post("/api/activity/batch", json=body)
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is False
        assert payload["assessments"] == []
        assert client.get("/api/activity/3").json()["结项状态"] == "未结项"
        assert client.get("/api/activity/1").json()["结项状态"] == "未结项"
        assert payload["checks"][0]["ok"] is True   # 逐条核对结果保留
        assert payload["checks"][1]["ok"] is False

    def test_batch_success(self) -> None:
        assert act(3, "提交方案评审")  # 已经评估中，会被拒；改用 ACTI-0003 与 ACTI-0002?
        reset()
        # 准备两条可结项记录：ACTI-0003 直接可结项；ACTI-0001 走完整流程
        assert act(1, "提交方案评审")["ok"] is True
        assert act(1, "启动评估")["ok"] is True
        assert comp(1, "提交对比审批")["ok"] is True
        assert comp(1, "执行对比")["ok"] is True
        body = {"values": {"action": "结项评估", "ids": [1, 3]}}
        payload = client.post("/api/activity/batch", json=body).json()
        assert payload["ok"] is True, payload
        assert len(payload["assessments"]) == 2
        assert client.get("/api/activity/1").json()["status"] == "已结项"
        assert client.get("/api/activity/3").json()["status"] == "已结项"

    def test_duplicate_finalize_rejected(self) -> None:
        assert act(3, "结项评估")["ok"] is True
        again = act(3, "结项评估")
        assert again["ok"] is False
        assert "重复" in again["message"] or "已结项" in again["message"]


class HistoricalScopeTest(unittest.TestCase):
    def setUp(reset_self) -> None:
        reset()

    def test_confirmed_history_keeps_alarm_time(self) -> None:
        """已确认的历史结项保持原告警时点状态。"""
        before = client.get("/api/activity/4").json()
        assert before["结项状态"] == "已确认"
        assert before["历史告警时点"] == "2026-08-15T09:30:00+08:00"
        items = client.get("/api/activity/assessment", params={"code": "ACTI-0004"}).json()["items"]
        assert len(items) == 1
        assert items[0]["明细编号"] == "ASMT-H0001"
        assert items[0]["告警时点"] == "2026-08-15T09:30:00+08:00"
        assert items[0]["数据口径"] == "历史结项口径"

        # 重新执行 COMP-0003 不应改写历史结项
        # COMP-0003 已执行 -> 再次执行会生成新回执，但 ACTI-0004 已确认，保持原状
        comp(3, "执行对比")
        after = client.get("/api/activity/4").json()
        assert after["status"] == "已结项"
        assert after["历史告警时点"] == before["历史告警时点"]
        assert after["结项明细编号"] == before["结项明细编号"]
        assert act(4, "结项评估")["ok"] is False

    def test_new_execution_reopens_review_for_current_conclusion(self) -> None:
        """当前结项遇到新执行回执：旧结论不被静默改写，转为待复核并可重新结项。"""
        assert act(3, "结项评估")["ok"] is True
        first = client.get("/api/activity/3").json()
        assert first["结项状态"] == "已确认"
        assert first["引用执行编号"] == "EXEC-0001"

        second = comp(3, "执行对比", 实验组转化数=170)
        assert second["ok"] is True
        assert second["entry"]["最新执行编号"] == "EXEC-0003"

        pending = client.get("/api/activity/3").json()
        assert pending["status"] == "已结项"
        assert pending["结项状态"] == "待复核"
        assert pending["引用执行编号"] == "EXEC-0001"  # 旧结论保留

        # 不指定引用直接重结项会被要求先复核；指定旧引用同样被拒
        stale = act(3, "结项评估", expected_execution="EXEC-0001")
        assert stale["ok"] is False
        refreshed = act(3, "结项评估")
        assert refreshed["ok"] is False
        assert "expected_execution" in refreshed["message"]
        refreshed = act(3, "结项评估", expected_execution="EXEC-0003")
        assert refreshed["ok"] is True, refreshed
        assert refreshed["assessment"]["执行编号"] == "EXEC-0003"
        assert refreshed["assessment"]["替代明细"][0]["执行编号"] == "EXEC-0001"

        active = client.get("/api/activity/assessment", params={"code": "ACTI-0003"}).json()["items"]
        assert [item["执行编号"] for item in active] == ["EXEC-0003"]


class LookupContractTest(unittest.TestCase):
    def setUp(reset_self) -> None:
        reset()

    def test_result_lookup_shape(self) -> None:
        """COMP-0001 结果回查样例：字段齐全，且能说明结论依据哪组执行。"""
        payload = client.get("/api/activity/results", params={"code": "ACTI-0001"}).json()
        for key in ("活动方案编号", "活动状态", "对照方案", "可引用执行", "执行回执",
                    "结项评估明细", "被拒绝的对比引用", "审计明细", "口径说明"):
            assert key in payload, key
        assert payload["可引用执行"] is False
        assert payload["对比组状态"] == "草稿"
        assert payload["执行回执"] is None

    def test_assessment_dataset_is_registered_from_pending_detail(self) -> None:
        assert act(3, "结项评估")["ok"] is True
        item = client.get("/api/activity/assessment", params={"code": "ACTI-0003"}).json()["items"][0]
        assert item["登记来源"] == "对比分析待处理明细"
        assert item["待处理明细编号"] == "PEND-0003"
        assert item["对比编号"] == "COMP-0003"
        assert item["执行编号"] == "EXEC-0001"
        assert item["活动方案编号"] == "ACTI-0003"
        assert item["结项状态"] == "已确认"

    def test_export_contains_conclusion_fields(self) -> None:
        assert act(3, "结项评估")["ok"] is True
        payload = client.get("/api/activity/export").json()
        row = [item for item in payload["items"] if item["方案编号"] == "ACTI-0003"][0]
        assert row["结项明细编号"].startswith("ASMT-")
        assert row["引用执行编号"] == "EXEC-0001"

    def test_execute_does_not_skip_activity_states(self) -> None:
        """执行对比不能把还没进入评估的方案跳级成评估中。"""
        # ACTI-0002 引用 COMP-0001，处于方案评审中：审批+执行后应保持原状态
        assert comp(1, "提交对比审批")["ok"] is True
        assert comp(1, "执行对比")["ok"] is True
        row = client.get("/api/activity/2").json()
        assert row["status"] == "方案评审中"
        assert row["引用执行编号"] is None
        assert act(2, "结项评估")["ok"] is False

    def test_batch_requires_explicit_review_when_reopened(self) -> None:
        assert act(3, "结项评估")["ok"] is True
        assert comp(3, "执行对比", 实验组转化数=170)["ok"] is True
        body = {"values": {"action": "结项评估", "ids": [3]}}
        payload = client.post("/api/activity/batch", json=body).json()
        assert payload["ok"] is False
        assert "expected_execution" in payload["checks"][0]["message"]
        body = {"values": {"action": "结项评估", "ids": [3], "expected_execution": "EXEC-0003"}}
        payload = client.post("/api/activity/batch", json=body).json()
        assert payload["ok"] is True, payload


class ExportBoundaryTest(unittest.TestCase):
    def setUp(reset_self) -> None:
        reset()

    def test_export_not_shadowed_by_path_param(self) -> None:
        """静态路径先于 /{entry_id} 注册：导出不返回 422。"""
        for path in ("/api/activity/export", "/api/comparison/export",
                     "/api/activity/assessment", "/api/comparison/executions"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code, response.text)

    def test_comparison_status_written_back_to_list(self) -> None:
        """结项后对比分析列表能看到回写状态。"""
        assert act(3, "结项评估")["ok"] is True
        rows = client.get("/api/comparison", params={"status": "已执行"}).json()["items"]
        target = [row for row in rows if row["对比编号"] == "COMP-0003"][0]
        assert target["评估状态"] == "已复核"
        assert target["回写活动编号"] == "ACTI-0003"
        assert target["结项明细编号"].startswith("ASMT-")


class ConcurrencyTest(unittest.TestCase):
    def setUp(reset_self) -> None:
        reset()

    def test_concurrent_finalize_creates_one_dataset(self) -> None:
        """并发结项只生成一份可复核明细。"""
        results: list[bool] = []
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            payload = act(3, "结项评估")
            results.append(bool(payload["ok"]))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert results.count(True) == 1, results
        items = client.get("/api/activity/assessment", params={"code": "ACTI-0003"}).json()["items"]
        assert len(items) == 1
        rows = store.rows("activity_assessment")
        codes = [row["明细编号"] for row in rows if row["活动方案编号"] == "ACTI-0003"]
        assert len(codes) == 1

    def test_concurrent_execute_and_finalize(self) -> None:
        """对比执行回执与结项请求并发：页面可执行动作与历史记录位置稳定。"""
        assert act(1, "提交方案评审")["ok"] is True
        assert act(1, "启动评估")["ok"] is True
        assert comp(1, "提交对比审批")["ok"] is True
        barrier = threading.Barrier(6)
        outputs: list[dict[str, Any]] = []

        def finalize() -> None:
            barrier.wait()
            outputs.append(act(1, "结项评估"))

        def execute() -> None:
            barrier.wait()
            outputs.append(comp(1, "执行对比"))

        threads = [threading.Thread(target=finalize) for _ in range(3)]
        threads += [threading.Thread(target=execute) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        entry = client.get("/api/activity/1").json()
        # 要么结项成功且引用执行，要么保持评估中；不允许结项引用未执行对比
        if entry["status"] == "已结项":
            assert entry["引用执行编号"]
        else:
            assert entry["status"] in ("评估中", "方案评审中")
        # 不允许出现同一执行的两份明细
        rows = [row for row in store.rows("activity_assessment") if row["活动方案编号"] == "ACTI-0001"]
        keys = [(row["执行编号"]) for row in rows]
        assert len(keys) == len(set(keys))


if __name__ == "__main__":
    unittest.main()
