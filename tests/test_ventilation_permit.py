import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class VentilationPermitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "test.db"),
            RuleEngine(clock=lambda: self.now),
        )
        self.admin = Actor("admin", "admin")
        self.safety = Actor("s01", "safety")
        self.field = Actor("f01", "field")

        self.incident = self.service.create(
            self.admin, "incident",
            {"area_code": "M-01", "severity": "high", "summary": "gas"},
        )
        self.sensor = self.service.create(
            self.admin, "sensor",
            {"location_code": "M-01", "gas_ppm": 5, "threshold_ppm": 80},
        )
        self.worker = self.service.create(
            self.admin, "worker",
            {"name": "Li Wei", "location_code": "M-01", "team": "A"},
        )
        self.vent = self.service.create(
            self.admin, "ventilation",
            {"name": "fan-1", "area_code": "M-01", "capacity": 100},
        )
        # 风机在 10 分钟前停机，之后完成气体检测
        base = self.now
        self.now = base - timedelta(minutes=10)
        self.vent = self.service.transition(self.admin, self.vent["id"], "stop")
        self.now = base
        # 人员撤离后才有资格申请许可
        self.service.transition(self.admin, self.worker["id"], "mark_missing")
        self.service.transition(self.admin, self.worker["id"], "locate", {"located_at": self.iso(-20)})
        self.service.transition(self.admin, self.worker["id"], "evacuate")

    def tearDown(self):
        self.tmp.cleanup()

    def iso(self, minutes_offset=0):
        return (self.now + timedelta(minutes=minutes_offset)).isoformat()

    def apply(self, *, tested_at=None, oxygen=20.9, methane=0.1, actor=None):
        return self.service.create(
            actor or self.safety,
            "ventilation_permit",
            {
                "ventilation_id": self.vent["id"],
                "tested_at": tested_at if tested_at is not None else self.iso(-5),
                "oxygen_pct": oxygen,
                "methane_pct": methane,
            },
        )

    def result_map(self, permit):
        return {c["code"]: c["result"] for c in permit["data"]["checks"]}

    def advance(self, minutes):
        self.now += timedelta(minutes=minutes)

    # ---- 放行路径 ----
    def test_approved_permit_allows_restore(self):
        permit = self.apply()
        self.assertEqual(permit["status"], "approved")
        results = self.result_map(permit)
        self.assertEqual(set(results.values()), {"pass"})
        vent = self.service.transition(
            self.admin, self.vent["id"], "restore", {"permit_id": permit["id"]}
        )
        self.assertEqual(vent["status"], "running")
        self.assertEqual(vent["data"]["permit_id"], permit["id"])

    def test_warning_sensor_is_advisory_not_blocking(self):
        self.service.transition(self.admin, self.sensor["id"], "raise_warning")
        permit = self.apply()
        self.assertEqual(permit["status"], "approved")
        self.assertEqual(self.result_map(permit)["sensors"], "warning")

    # ---- 逐项拒绝 ----
    def test_low_oxygen_is_denied_with_itemized_reason(self):
        permit = self.apply(oxygen=19.0)
        self.assertEqual(permit["status"], "denied")
        check = next(c for c in permit["data"]["checks"] if c["code"] == "oxygen")
        self.assertEqual(check["result"], "fail")
        self.assertIn("19.5", check["label"])

    def test_oxygen_boundary_19_5_passes(self):
        permit = self.apply(oxygen=19.5)
        self.assertEqual(permit["status"], "approved")
        self.assertEqual(self.result_map(permit)["oxygen"], "pass")

    def test_methane_at_1_percent_is_denied(self):
        permit = self.apply(methane=1.0)
        self.assertEqual(permit["status"], "denied")
        self.assertEqual(self.result_map(permit)["methane"], "fail")

    def test_detection_before_shutdown_is_denied(self):
        # 检测时间早于停机时间（停机于 -10 分钟）
        permit = self.apply(tested_at=self.iso(-15))
        self.assertEqual(permit["status"], "denied")
        self.assertEqual(self.result_map(permit)["stopped_after_shutdown"], "fail")

    def test_detection_older_than_30_minutes_is_denied(self):
        # 检测在停机之后，但距当前已超过 30 分钟，需要重做
        permit = self.apply(tested_at=self.iso(-35))
        self.assertEqual(permit["status"], "denied")
        self.assertEqual(self.result_map(permit)["test_fresh"], "fail")

    def test_alarm_sensor_blocks_permit(self):
        # 直接将传感器读数抬到报警值以上再报警
        repo = self.service.repository
        repo.update_entity(
            self.sensor["id"], self.sensor["version"], "normal",
            {**self.sensor["data"], "gas_ppm": 200},
        )
        sensor = self.service.get(self.sensor["id"])
        self.service.transition(self.admin, sensor["id"], "raise_alarm")
        permit = self.apply()
        self.assertEqual(permit["status"], "denied")
        self.assertEqual(self.result_map(permit)["sensors"], "fail")

    def test_unevacuated_worker_blocks_permit(self):
        # 新增一名仍在区域内的人员
        other = self.service.create(
            self.admin, "worker",
            {"name": "Wang Er", "location_code": "M-01", "team": "B"},
        )
        permit = self.apply()
        self.assertEqual(permit["status"], "denied")
        check = next(c for c in permit["data"]["checks"] if c["code"] == "workers_evacuated")
        self.assertEqual(check["result"], "fail")
        self.assertIn(other["id"], check["detail"])
        # 其他区域的人员不影响本区域
        self.service.create(
            self.admin, "worker",
            {"name": "Zhao San", "location_code": "M-99", "team": "C"},
        )
        permit2 = self.apply()
        self.assertEqual(permit2["data"]["checks"][-1]["result"], "fail")

    # ---- 申请约束 ----
    def test_only_safety_or_admin_can_issue_permit(self):
        with self.assertRaises(PermissionDenied):
            self.apply(actor=self.field)

    def test_permit_requires_stopped_device(self):
        vent2 = self.service.create(
            self.admin, "ventilation",
            {"name": "fan-2", "area_code": "M-02", "capacity": 50},
        )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.safety, "ventilation_permit",
                {"ventilation_id": vent2["id"], "tested_at": self.iso(-1),
                 "oxygen_pct": 20.9, "methane_pct": 0.1},
            )

    def test_unknown_device_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.safety, "ventilation_permit",
                {"ventilation_id": "nope", "tested_at": self.iso(-1),
                 "oxygen_pct": 20.9, "methane_pct": 0.1},
            )

    # ---- 启风机门控 ----
    def test_restore_without_permit_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.transition(self.admin, self.vent["id"], "restore")

    def test_denied_permit_cannot_start_fan(self):
        permit = self.apply(oxygen=18.0)
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin, self.vent["id"], "restore", {"permit_id": permit["id"]}
            )

    def test_permit_for_another_device_is_rejected(self):
        vent2 = self.service.create(
            self.admin, "ventilation",
            {"name": "fan-2", "area_code": "M-02", "capacity": 50},
        )
        self.service.transition(self.admin, vent2["id"], "stop")
        permit = self.apply()
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, vent2["id"], "restore", {"permit_id": permit["id"]}
            )

    def test_permit_expires_after_30_minutes_requires_retest(self):
        permit = self.apply(tested_at=self.iso(-5))
        self.assertEqual(permit["status"], "approved")
        self.advance(30)
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                self.admin, self.vent["id"], "restore", {"permit_id": permit["id"]}
            )
        self.assertIn("retest", str(ctx.exception))
        # 重新检测、重新申请后可以启动
        new_permit = self.apply(tested_at=self.iso(-2))
        vent = self.service.transition(
            self.admin, self.vent["id"], "restore", {"permit_id": new_permit["id"]}
        )
        self.assertEqual(vent["status"], "running")

    def test_field_state_change_invalidates_approved_permit(self):
        permit = self.apply()
        # 许可通过后又有一名人员进入该区域且失联
        self.service.create(
            self.admin, "worker",
            {"name": "Late Worker", "location_code": "M-01", "team": "D"},
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin, self.vent["id"], "restore", {"permit_id": permit["id"]}
            )

    # ---- 事件关闭门控 ----
    def prepare_recoverable_incident(self):
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            self.incident = self.service.transition(
                self.admin, self.incident["id"], action
            )

    def test_close_requires_approved_permit_for_each_stopped_area(self):
        self.prepare_recoverable_incident()
        repo = self.service.repository
        # 模拟历史遗留：设备已恢复运行（保留 stopped_at 记录）但未留存通过的送风许可
        repo.update_entity(
            self.vent["id"], self.vent["version"], "running", dict(self.vent["data"])
        )
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.admin, self.incident["id"], "close", {"summary": "done"})
        self.assertIn("ventilation permit", str(ctx.exception))
        self.assertIn("M-01", str(ctx.exception))

        # 直接补录一张审批通过的许可（历史数据迁移场景），关闭校验随即通过
        stopped_data = dict(self.vent["data"])
        repo.update_entity(self.vent["id"], self.vent["version"] + 1, "stopped", stopped_data)
        stopped_vent = self.service.get(self.vent["id"])
        permit = self.apply()
        self.assertEqual(permit["status"], "approved")
        repo.update_entity(stopped_vent["id"], stopped_vent["version"], "running", stopped_data)
        closed = self.service.transition(
            self.admin, self.incident["id"], "close", {"summary": "all clear"}
        )
        self.assertEqual(closed["status"], "closed")

    def test_close_succeeds_when_all_stopped_areas_have_approved_permits(self):
        self.prepare_recoverable_incident()
        permit = self.apply()
        self.service.transition(
            self.admin, self.vent["id"], "restore", {"permit_id": permit["id"]}
        )
        closed = self.service.transition(
            self.admin, self.incident["id"], "close", {"summary": "all clear"}
        )
        self.assertEqual(closed["status"], "closed")


if __name__ == "__main__":
    unittest.main()
