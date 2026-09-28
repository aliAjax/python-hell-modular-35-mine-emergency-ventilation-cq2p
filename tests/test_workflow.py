import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "test.db"),
            RuleEngine(clock=lambda: self.now),
        )
        self.actor = Actor("admin", "admin")
        self.safety = Actor("s01", "safety")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data, actor=None):
        return self.service.create(actor or self.actor, kind, data)

    def act(self, entity, action, data=None, version=None, actor=None):
        return self.service.transition(actor or self.actor, entity["id"], action, data or {}, version)

    def iso(self, minutes_offset=0):
        return (self.now + timedelta(minutes=minutes_offset)).isoformat()

    def test_full_emergency_flow(self):
        incident = self.create("incident", {"area_code": "M-01", "severity": "critical", "summary": "gas leak"})
        incident = self.act(incident, "begin_evacuation")
        incident = self.act(incident, "search")
        incident = self.act(incident, "stabilize")
        incident = self.act(incident, "recover")

        worker = self.create("worker", {"name": "Li Wei", "location_code": "M-01", "team": "A"})
        worker = self.act(worker, "mark_missing")
        worker = self.act(worker, "locate", {"located_at": self.iso(-30)})
        worker = self.act(worker, "rescue", {"incident_id": incident["id"]})
        self.assertEqual(worker["status"], "rescued")

        sensor = self.create("sensor", {"location_code": "M-01", "gas_ppm": 120, "threshold_ppm": 80})
        self.assertEqual(sensor["data"]["severity"], "alarm")
        sensor = self.act(sensor, "raise_alarm")
        self.assertEqual(sensor["status"], "alarm")
        # 送风许可要求区域传感器无报警，排除险情后复位
        sensor = self.act(sensor, "clear")

        vent = self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        # 风机先于检测停机
        self.now -= timedelta(minutes=10)
        vent = self.act(vent, "stop", {})
        self.now += timedelta(minutes=10)
        self.assertEqual(vent["data"]["stopped_at"], (self.now - timedelta(minutes=10)).isoformat(timespec="seconds"))

        permit = self.create(
            "ventilation_permit",
            {
                "ventilation_id": vent["id"],
                "tested_at": self.iso(-5),
                "oxygen_pct": 20.6,
                "methane_pct": 0.2,
            },
            actor=self.safety,
        )
        self.assertEqual(permit["status"], "approved")
        self.assertTrue(all(c["result"] == "pass" for c in permit["data"]["checks"]))

        vent = self.act(vent, "restore", {"permit_id": permit["id"]})
        self.assertEqual(vent["status"], "running")
        self.assertEqual(vent["data"]["permit_id"], permit["id"])

        task = self.create("task", {"incident_id": incident["id"], "task_type": "rescue", "target": "worker-1", "dedupe_key": "rescue-1"})
        task = self.act(task, "assign", {"team": "A"})
        task = self.act(task, "accept", {})
        task = self.act(task, "complete", {"result": "worker recovered"})
        self.assertEqual(task["status"], "completed")

        incident = self.act(incident, "close", {"summary": "all clear"})
        self.assertEqual(incident["status"], "closed")

    def test_offline_merge_is_idempotent(self):
        record = {"source_id": "field-a", "record_id": "42", "recorded_at": "2026-09-27T10:00:00Z", "payload": {"type": "gas", "value": 12}}
        first = self.service.merge_offline(self.actor, [record])
        second = self.service.merge_offline(self.actor, [record])
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(len(self.service.list("offline_record")), 1)


if __name__ == "__main__":
    unittest.main()
