import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

BASE = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, value=BASE):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class AirPermitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine(clock=self.clock)
        )
        self.safety = Actor("safe-1", "safety")
        self.field = Actor("field-1", "field")
        self.viewer = Actor("viewer-1", "viewer")

    def tearDown(self):
        self.tmp.cleanup()

    def iso(self, minutes=0):
        return (self.clock.value + timedelta(minutes=minutes)).isoformat(timespec="seconds")

    def setup_area(self, area="M-09", worker_state="evacuated", sensor_state="normal"):
        incident = self.service.create(
            self.safety, "incident",
            {"area_code": area, "severity": "high", "summary": "smoke"},
        )
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.service.transition(self.safety, incident["id"], action)

        worker = self.service.create(
            self.safety, "worker", {"name": "Zhang San", "location_code": area, "team": "B"}
        )
        if worker_state == "evacuated":
            worker = self.service.transition(self.safety, worker["id"], "mark_missing")
            worker = self.service.transition(self.safety, worker["id"], "locate", {"located_at": self.iso()})
            worker = self.service.transition(
                self.safety, worker["id"], "rescue", {"incident_id": incident["id"]}
            )
        sensor = self.service.create(
            self.safety, "sensor", {"location_code": area, "gas_ppm": 10, "threshold_ppm": 80}
        )
        if sensor_state == "alarm":
            sensor = self.service.create(
                self.safety, "sensor", {"location_code": area, "gas_ppm": 200, "threshold_ppm": 80}
            )
            sensor = self.service.transition(self.safety, sensor["id"], "raise_alarm")
        elif sensor_state == "faulty":
            sensor = self.service.transition(
                self.safety, sensor["id"], "mark_faulty", {"reason": "no signal"}
            )

        vent = self.service.create(
            self.safety, "ventilation", {"name": "fan-" + area, "area_code": area, "capacity": 50}
        )
        vent = self.service.transition(self.safety, vent["id"], "stop", {"reason": "fire"})
        return incident, worker, sensor, vent

    def apply(self, vent, tested_offset=0, oxygen=20.9, methane=0.1, actor=None):
        return self.service.create(
            actor or self.safety,
            "air_permit",
            {
                "ventilation_id": vent["id"],
                "tested_at": self.iso(tested_offset),
                "oxygen_pct": oxygen,
                "methane_pct": methane,
            },
        )

    def close(self, incident):
        return self.service.transition(self.safety, incident["id"], "close", {"summary": "all clear"})

    # --- 放行 ---

    def test_permit_approved_when_all_checks_pass(self):
        _, _, _, vent = self.setup_area()
        permit = self.apply(vent)
        self.assertEqual(permit["status"], "approved")
        self.assertEqual(permit["data"]["decision"], "approved")
        self.assertTrue(all(item["ok"] for item in permit["data"]["checks"]))
        names = [item["name"] for item in permit["data"]["checks"]]
        self.assertIn("氧气不低于19.5%", names)
        self.assertIn("甲烷低于1%", names)
        self.assertIn("检测时间在30分钟内", names)

    def test_restore_requires_approved_permit(self):
        _, _, _, vent = self.setup_area()
        with self.assertRaises(ValidationError):
            self.service.transition(self.field, vent["id"], "restore", {})
        permit = self.apply(vent)
        vent = self.service.transition(
            self.field, vent["id"], "restore", {"permit_id": permit["id"]}
        )
        self.assertEqual(vent["status"], "running")
        self.assertEqual(vent["data"]["approved_permit_id"], permit["id"])

    def test_full_close_with_valid_permit_succeeds(self):
        incident, _, _, vent = self.setup_area()
        permit = self.apply(vent)
        self.service.transition(self.field, vent["id"], "restore", {"permit_id": permit["id"]})
        self.assertEqual(self.close(incident)["status"], "closed")

    # --- 逐项驳回原因 ---

    def test_low_oxygen_denies_with_itemized_reason(self):
        _, _, _, vent = self.setup_area()
        permit = self.apply(vent, oxygen=18.0)
        self.assertEqual(permit["status"], "denied")
        oxygen_check = next(c for c in permit["data"]["checks"] if c["name"] == "氧气不低于19.5%")
        self.assertFalse(oxygen_check["ok"])
        self.assertIn("18.0", oxygen_check["detail"])

    def test_oxygen_boundary_19_5_approves(self):
        _, _, _, vent = self.setup_area()
        self.assertEqual(self.apply(vent, oxygen=19.5)["status"], "approved")

    def test_methane_at_one_percent_denies(self):
        _, _, _, vent = self.setup_area()
        permit = self.apply(vent, methane=1.0)
        self.assertEqual(permit["status"], "denied")
        methane_check = next(c for c in permit["data"]["checks"] if c["name"] == "甲烷低于1%")
        self.assertFalse(methane_check["ok"])

    def test_test_before_shutdown_denies(self):
        _, _, _, vent = self.setup_area()
        permit = self.apply(vent, tested_offset=-30)
        self.assertEqual(permit["status"], "denied")
        check = next(c for c in permit["data"]["checks"] if c["name"] == "检测时间不早于停机")
        self.assertFalse(check["ok"])

    def test_test_older_than_30_minutes_denies_and_must_redetect(self):
        _, _, _, vent = self.setup_area()
        permit = self.apply(vent, tested_offset=-31)
        self.assertEqual(permit["status"], "denied")
        check = next(c for c in permit["data"]["checks"] if c["name"] == "检测时间在30分钟内")
        self.assertFalse(check["ok"])
        # 即使记录为approved（绕过评估），启机时也要按30分钟有效期拦截
        # 这里直接验证：时间推进40分钟后，当时有效的许可不能再启机
        fresh = self.apply(vent, tested_offset=0)
        self.assertEqual(fresh["status"], "approved")
        self.clock.advance(minutes=40)
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.field, vent["id"], "restore", {"permit_id": fresh["id"]})
        self.assertIn("retest required", str(ctx.exception))

    def test_worker_still_underground_denies(self):
        _, _, _, vent = self.setup_area(worker_state="active")
        permit = self.apply(vent)
        self.assertEqual(permit["status"], "denied")
        worker_checks = [c for c in permit["data"]["checks"] if "人员" in c["name"]]
        self.assertTrue(worker_checks)
        self.assertFalse(any(c["ok"] for c in worker_checks))

    def test_missing_worker_denies(self):
        incident, worker, _, vent = self.setup_area(worker_state="active")
        self.service.transition(self.safety, worker["id"], "mark_missing")
        permit = self.apply(vent)
        self.assertEqual(permit["status"], "denied")
        self.assertTrue(any("失联" in c["detail"] for c in permit["data"]["checks"]))

    def test_alarming_sensor_denies(self):
        _, _, _, vent = self.setup_area(sensor_state="alarm")
        permit = self.apply(vent)
        self.assertEqual(permit["status"], "denied")
        sensor_checks = [c for c in permit["data"]["checks"] if c["name"].startswith("区域传感器")]
        self.assertTrue(any(not c["ok"] for c in sensor_checks))

    def test_faulty_sensor_denies(self):
        _, _, _, vent = self.setup_area(sensor_state="faulty")
        permit = self.apply(vent)
        self.assertEqual(permit["status"], "denied")
        self.assertTrue(any("故障" in c["detail"] for c in permit["data"]["checks"]))

    # --- 启机与关闭约束 ---

    def test_denied_permit_cannot_start_fan(self):
        _, _, _, vent = self.setup_area()
        permit = self.apply(vent, oxygen=16.0)
        with self.assertRaises(ConflictError):
            self.service.transition(self.field, vent["id"], "restore", {"permit_id": permit["id"]})

    def test_permit_for_other_unit_rejected(self):
        _, _, _, vent = self.setup_area(area="M-09")
        _, _, _, other = self.setup_area(area="M-10")
        permit = self.apply(other)
        with self.assertRaises(ValidationError):
            self.service.transition(self.field, vent["id"], "restore", {"permit_id": permit["id"]})

    def test_permit_for_previous_shutdown_rejected(self):
        _, _, _, vent = self.setup_area()
        permit = self.apply(vent)
        # 风机重新运行后再次停运：旧许可针对上一次停机，不能用
        vent = self.service.transition(self.field, vent["id"], "restore", {"permit_id": permit["id"]})
        vent = self.service.transition(self.safety, vent["id"], "stop", {"reason": "recheck"})
        with self.assertRaises(ConflictError):
            self.service.transition(self.field, vent["id"], "restore", {"permit_id": permit["id"]})
        new_permit = self.apply(vent)
        self.assertEqual(new_permit["data"]["stop_seq"], 2)
        vent = self.service.transition(self.field, vent["id"], "restore", {"permit_id": new_permit["id"]})
        self.assertEqual(vent["status"], "running")

    def test_close_blocked_when_stopped_area_has_no_valid_permit(self):
        incident, _, _, vent = self.setup_area()
        # 未经过送风许可流程，风机仍停运
        with self.assertRaises(ConflictError):
            self.close(incident)

    def test_close_blocked_for_previously_stopped_area_without_valid_permit(self):
        incident, _, _, vent = self.setup_area()
        # 历史遗留数据：设备已恢复运行，但当前停机批次没有有效放行许可。
        # 事件关闭仍要求每个曾停运区域的本次停机批次都有放行许可。
        self.service.repository.update_entity(
            vent["id"], vent["version"], "running", dict(vent["data"])
        )
        with self.assertRaises(ConflictError) as ctx:
            self.close(incident)
        self.assertIn("air permit", str(ctx.exception))
        # 重新走“停运→检测→放行→启机”流程后可以关闭
        vent = self.service.get(vent["id"])
        vent = self.service.transition(self.safety, vent["id"], "stop", {"reason": "retest"})
        permit = self.apply(vent)
        self.service.transition(self.field, vent["id"], "restore", {"permit_id": permit["id"]})
        self.assertEqual(self.close(incident)["status"], "closed")

    def test_permit_only_for_stopped_unit(self):
        vent = self.service.create(
            self.safety, "ventilation", {"name": "fan-x", "area_code": "M-99", "capacity": 10}
        )
        with self.assertRaises(ValidationError):
            self.apply(vent)

    def test_unknown_ventilation_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.safety,
                "air_permit",
                {"ventilation_id": "nope", "tested_at": self.iso(), "oxygen_pct": 20.9, "methane_pct": 0.1},
            )

    def test_only_safety_can_issue_permit(self):
        _, _, _, vent = self.setup_area()
        with self.assertRaises(PermissionDenied):
            self.apply(vent, actor=self.viewer)

    def test_field_can_start_fan_but_not_issue_permit(self):
        _, _, _, vent = self.setup_area()
        with self.assertRaises(PermissionDenied):
            self.apply(vent, actor=self.field)

    def test_bad_test_time_format_rejected(self):
        _, _, _, vent = self.setup_area()
        with self.assertRaises(ValidationError):
            self.service.create(
                self.safety,
                "air_permit",
                {"ventilation_id": vent["id"], "tested_at": "yesterday", "oxygen_pct": 20.9, "methane_pct": 0.1},
            )

    def test_non_numeric_readings_rejected(self):
        _, _, _, vent = self.setup_area()
        with self.assertRaises(ValidationError):
            self.service.create(
                self.safety,
                "air_permit",
                {"ventilation_id": vent["id"], "tested_at": self.iso(), "oxygen_pct": "high", "methane_pct": 0.1},
            )

    def test_degraded_fan_restore_does_not_need_permit(self):
        vent = self.service.create(
            self.safety, "ventilation", {"name": "fan-d", "area_code": "M-50", "capacity": 10}
        )
        vent = self.service.transition(self.safety, vent["id"], "degrade", {"reason": "filter"})
        vent = self.service.transition(self.safety, vent["id"], "bypass_restore", {})
        self.assertEqual(vent["status"], "running")


if __name__ == "__main__":
    unittest.main()
