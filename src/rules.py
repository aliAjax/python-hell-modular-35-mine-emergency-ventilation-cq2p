from datetime import datetime, timedelta, timezone

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError

# 送风许可阈值
PERMIT_OXYGEN_MIN = 19.5          # 氧气低于19.5%不放行
PERMIT_METHANE_MAX = 1.0          # 甲烷达到1%不放行
PERMIT_VALID_MINUTES = 30         # 检测超过30分钟必须重做
PERMIT_FUTURE_TOLERANCE_SECONDS = 60
PERMIT_BLOCKED_SENSOR_STATUSES = ("warning", "alarm", "faulty")
PERMIT_PRESENT_WORKER_STATUSES = ("active", "missing", "located")

WORKER_STATUS_LABELS = {
    "active": "在井未撤离",
    "missing": "失联",
    "located": "已定位未升井",
    "evacuated": "已撤离",
    "rescued": "已获救",
    "inactive": "停用",
}
SENSOR_STATUS_LABELS = {
    "normal": "正常",
    "warning": "预警",
    "alarm": "报警",
    "faulty": "故障",
}


def _require(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def _ensure_role(actor, allowed):
    if "*" not in allowed and actor.role not in allowed:
        raise PermissionDenied("role %s is not allowed here" % actor.role)


def _all(lookup, kind):
    return lookup(kind, "*", None) or [] if lookup else []


def _find_one(lookup, kind, field, value):
    rows = lookup(kind, field, value) or [] if lookup else []
    return rows[0] if rows else None


def _number(value, field):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be numeric")


def _validate_worker(data):
    if len(str(data.get("name", "")).strip()) < 2:
        raise ValidationError("worker name is too short")


def _validate_sensor(data):
    gas = _number(data.get("gas_ppm"), "gas_ppm")
    threshold = _number(data.get("threshold_ppm"), "threshold_ppm")
    if gas < 0 or threshold <= 0:
        raise ValidationError("gas readings and thresholds must be positive")
    data["severity"] = "alarm" if gas >= threshold * 1.5 else "warning" if gas >= threshold else "normal"


def _validate_capacity(data, field):
    if _number(data.get(field), field) <= 0:
        raise ValidationError(field + " must be positive")


def _validate_passage(data):
    if _number(data.get("width_m"), "width_m") <= 0:
        raise ValidationError("width_m must be positive")
    if data.get("from_location") == data.get("to_location"):
        raise ValidationError("passage endpoints must differ")


def _validate_incident(data):
    if data.get("severity") not in ("low", "medium", "high", "critical"):
        raise ValidationError("invalid incident severity")


def _validate_task(data, lookup):
    incident = _find_one(lookup, "incident", "id", data.get("incident_id"))
    if not incident or incident["status"] in ("closed",):
        raise ValidationError("task requires an open incident")
    if data.get("task_type") not in ("evacuation", "search", "rescue", "ventilation", "medical", "repair"):
        raise ValidationError("invalid task_type")
    key = data.get("dedupe_key")
    for task in _all(lookup, "task"):
        if task["data"].get("dedupe_key") == key and task["status"] not in ("completed", "cancelled"):
            raise ConflictError("active task already exists for dedupe_key: " + str(key))


def _validate_offline(data):
    if not isinstance(data.get("payload"), dict):
        raise ValidationError("offline payload must be an object")
    try:
        datetime.fromisoformat(str(data.get("recorded_at")).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("recorded_at must be ISO-8601")


def _parse_dt(value, field):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise ValidationError(field + " must be ISO-8601")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _check(name, ok, detail):
    return {"name": name, "ok": bool(ok), "detail": detail}


def _evaluate_air_permit(data, lookup, now):
    """逐项核验送风许可，返回(状态, 补充数据)。无论放行与否都留痕。"""
    vent_id = data.get("ventilation_id")
    vent = _find_one(lookup, "ventilation", "id", vent_id)
    if not vent:
        raise ValidationError("ventilation_id does not reference a ventilation unit")
    if vent["status"] != "stopped":
        raise ValidationError("air permit can only be issued for a stopped ventilation unit")

    area = vent["data"].get("area_code")
    tested_at = _parse_dt(data.get("tested_at"), "tested_at")
    oxygen = _number(data.get("oxygen_pct"), "oxygen_pct")
    methane = _number(data.get("methane_pct"), "methane_pct")
    stopped_at = _parse_dt(vent["data"].get("stopped_at"), "stopped_at") if vent["data"].get("stopped_at") else None
    stop_seq = int(vent["data"].get("stop_seq", 1))

    checks = [
        _check(
            "检测对象",
            area is not None,
            "停运设备 %s，区域 %s" % (vent["data"].get("name", vent_id), area),
        ),
        _check(
            "检测时间不早于停机",
            stopped_at is None or tested_at >= stopped_at,
            "检测 %s，停机 %s" % (data.get("tested_at"), vent["data"].get("stopped_at")),
        ),
        _check(
            "检测时间在30分钟内",
            now - timedelta(minutes=PERMIT_VALID_MINUTES) <= tested_at
            <= now + timedelta(seconds=PERMIT_FUTURE_TOLERANCE_SECONDS),
            "检测 %s，距今 %d 分钟，有效期 %d 分钟"
            % (data.get("tested_at"), int(abs((now - tested_at).total_seconds()) // 60), PERMIT_VALID_MINUTES),
        ),
        _check(
            "氧气不低于19.5%",
            oxygen >= PERMIT_OXYGEN_MIN,
            "实测 %.1f%%，阈值 %.1f%%" % (oxygen, PERMIT_OXYGEN_MIN),
        ),
        _check(
            "甲烷低于1%",
            methane < PERMIT_METHANE_MAX,
            "实测 %.2f%%，阈值 %.2f%%" % (methane, PERMIT_METHANE_MAX),
        ),
    ]

    area_sensors = [s for s in _all(lookup, "sensor") if s["data"].get("location_code") == area]
    if area_sensors:
        for sensor in area_sensors:
            bad = sensor["status"] in PERMIT_BLOCKED_SENSOR_STATUSES
            checks.append(
                _check(
                    "区域传感器 %s" % sensor["id"],
                    not bad,
                    "状态：%s" % SENSOR_STATUS_LABELS.get(sensor["status"], sensor["status"]),
                )
            )
    else:
        checks.append(_check("区域传感器监测", True, "区域 %s 无在线传感器，以人工读数为准" % area))

    remaining = [
        w for w in _all(lookup, "worker")
        if w["data"].get("location_code") == area and w["status"] in PERMIT_PRESENT_WORKER_STATUSES
    ]
    if remaining:
        for worker in remaining:
            checks.append(
                _check(
                    "人员 %s 已撤离" % worker["data"].get("name", worker["id"]),
                    False,
                    "状态：%s" % WORKER_STATUS_LABELS.get(worker["status"], worker["status"]),
                )
            )
    else:
        checks.append(_check("区域人员全部撤离", True, "区域 %s 无未撤离/失联人员" % area))

    approved = all(item["ok"] for item in checks)
    extra = {
        "decision": "approved" if approved else "denied",
        "checks": checks,
        "area_code": area,
        "stop_seq": stop_seq,
        "oxygen_pct": oxygen,
        "methane_pct": methane,
        "evaluated_at": now.isoformat(timespec="seconds"),
    }
    return ("approved" if approved else "denied"), extra


def _validate_air_permit_restore(actor, entity, data, lookup, now):
    """启风机前必须持有针对本次停运、仍在30分钟有效期内的放行许可。"""
    permit_id = data.get("permit_id")
    if not permit_id:
        raise ValidationError("permit_id is required: an approved air permit must precede fan start")
    permit = _find_one(lookup, "air_permit", "id", permit_id)
    if not permit:
        raise ValidationError("permit_id does not reference an air permit")
    if permit["data"].get("ventilation_id") != entity["id"]:
        raise ValidationError("air permit is for another ventilation unit")
    if permit["data"].get("stop_seq") != entity["data"].get("stop_seq"):
        raise ConflictError("air permit was issued for a previous shutdown; request a new test")
    if permit["status"] != "approved":
        raise ConflictError("air permit is not approved")
    tested_at = _parse_dt(permit["data"].get("tested_at"), "tested_at")
    if tested_at < now - timedelta(minutes=PERMIT_VALID_MINUTES):
        raise ConflictError("air permit expired: test is older than %d minutes, retest required" % PERMIT_VALID_MINUTES)
    return {
        "permit_id": permit_id,
        "approved_permit_id": permit_id,
        "permit_tested_at": permit["data"].get("tested_at"),
        "restored_by": actor.user_id,
        "tested_at": permit["data"].get("tested_at"),
    }


def _stop_ventilation(actor, entity, data, lookup, now):
    return {
        "stopped_at": now.isoformat(timespec="seconds"),
        "stop_seq": int(entity["data"].get("stop_seq", 0)) + 1,
        "stopped_by": actor.user_id,
        "approved_permit_id": None,
        "permit_tested_at": None,
    }


def _sensor_alarm(actor, entity, data, lookup):
    if float(entity["data"].get("gas_ppm", 0)) < float(entity["data"].get("threshold_ppm", 1)):
        raise ValidationError("alarm requires a reading at or above threshold")
    return {"acknowledged_by": actor.user_id}


def _complete_task(actor, entity, data, lookup):
    if not str(data.get("result", "")).strip():
        raise ValidationError("result is required")
    return {"completed_by": actor.user_id}


def _close_incident(actor, entity, data, lookup):
    if [w for w in _all(lookup, "worker") if w["status"] in ("missing", "located")]:
        raise ConflictError("cannot close incident while workers are missing or located")
    active_tasks = [t for t in _all(lookup, "task") if t["status"] not in ("completed", "cancelled")]
    if active_tasks:
        raise ConflictError("cannot close incident while tasks remain active")
    stopped = [v for v in _all(lookup, "ventilation") if v["status"] != "running"]
    if stopped:
        raise ConflictError("cannot close incident until ventilation is restored")
    # 每个曾停运的区域，本次停机都必须持有与启机关联的有效放行许可
    permits = _all(lookup, "air_permit")
    for vent in _all(lookup, "ventilation"):
        stop_seq = vent["data"].get("stop_seq")
        if stop_seq and vent["data"].get("approved_permit_id"):
            continue
        if stop_seq:
            linked = [
                p for p in permits
                if p["status"] == "approved"
                and p["data"].get("ventilation_id") == vent["id"]
                and p["data"].get("stop_seq") == stop_seq
            ]
            if not linked:
                raise ConflictError(
                    "area %s has no valid approved air permit for the current shutdown" % vent["data"].get("area_code")
                )
    return {"closed_by": actor.user_id}


class RuleEngine:
    ALIASES = {
        "workers": "worker", "sensors": "sensor", "ventilations": "ventilation",
        "passages": "passage", "refuges": "refuge", "incidents": "incident",
        "tasks": "task", "offline-records": "offline_record", "offline_records": "offline_record",
        "air-permits": "air_permit", "air_permits": "air_permit",
    }
    INITIAL_STATUS = {
        "worker": "active", "sensor": "normal", "ventilation": "running",
        "passage": "open", "refuge": "available", "incident": "detected",
        "task": "proposed", "offline_record": "merged", "air_permit": "submitted",
    }
    TRANSITIONS = {
        "worker": {
            "mark_missing": (("active",), "missing"),
            "locate": (("missing",), "located"),
            "evacuate": (("missing", "located"), "evacuated"),
            "rescue": (("missing", "located"), "rescued"),
            "find_safe": (("missing",), "active"),
            "deactivate": (("active",), "inactive"),
        },
        "sensor": {
            "raise_warning": (("normal",), "warning"),
            "raise_alarm": (("normal", "warning"), "alarm"),
            "clear": (("warning", "alarm"), "normal"),
            "mark_faulty": (("normal", "warning", "alarm"), "faulty"),
            "verify_misread": (("faulty",), "normal"),
        },
        "ventilation": {
            "degrade": (("running",), "degraded"),
            "stop": (("running", "degraded"), "stopped"),
            "restore": (("stopped",), "running"),
            "bypass_restore": (("degraded",), "running"),
        },
        "passage": {
            "restrict": (("open",), "restricted"),
            "block": (("open", "restricted"), "blocked"),
            "clear": (("blocked", "restricted"), "open"),
        },
        "refuge": {
            "occupy": (("available",), "occupied"),
            "release": (("occupied",), "available"),
            "maintain": (("available",), "maintenance"),
            "reopen": (("maintenance",), "available"),
        },
        "incident": {
            "begin_evacuation": (("detected",), "evacuating"),
            "search": (("evacuating",), "searching"),
            "stabilize": (("searching",), "stabilizing"),
            "recover": (("stabilizing",), "recovering"),
            "close": (("recovering",), "closed"),
            "reopen": (("closed",), "detected"),
        },
        "task": {
            "assign": (("proposed",), "assigned"),
            "accept": (("assigned",), "in_progress"),
            "complete": (("in_progress",), "completed"),
            "cancel": (("proposed", "assigned", "in_progress"), "cancelled"),
        },
    }
    CREATE_REQUIRED = {
        "worker": ("name", "location_code", "team"),
        "sensor": ("location_code", "gas_ppm", "threshold_ppm"),
        "ventilation": ("name", "area_code", "capacity"),
        "passage": ("from_location", "to_location", "width_m"),
        "refuge": ("location_code", "capacity"),
        "incident": ("area_code", "severity", "summary"),
        "task": ("incident_id", "task_type", "target", "dedupe_key"),
        "offline_record": ("source_id", "record_id", "recorded_at", "payload"),
        "air_permit": ("ventilation_id", "tested_at", "oxygen_pct", "methane_pct"),
    }
    ACTION_REQUIRED = {
        ("worker", "rescue"): ("incident_id",),
        ("sensor", "mark_faulty"): ("reason",),
        ("ventilation", "restore"): ("permit_id",),
        ("ventilation", "degrade"): ("reason",),
        ("incident", "close"): ("summary",),
        ("task", "complete"): ("result",),
        ("task", "cancel"): ("reason",),
    }
    CREATE_ROLES = {
        "worker": ("admin", "safety", "dispatcher"),
        "sensor": ("admin", "safety", "field"),
        "ventilation": ("admin", "safety"),
        "passage": ("admin", "safety", "field"),
        "refuge": ("admin", "safety"),
        "incident": ("admin", "safety", "dispatcher"),
        "task": ("admin", "dispatcher", "safety"),
        "offline_record": ("admin", "safety", "dispatcher", "field"),
        "air_permit": ("admin", "safety"),
    }
    ROLE_ACTIONS = {
        "mark_missing": ("admin", "safety", "dispatcher"),
        "locate": ("admin", "field", "safety"),
        "evacuate": ("admin", "field", "dispatcher"),
        "rescue": ("admin", "field", "safety"),
        "find_safe": ("admin", "field", "safety"),
        "deactivate": ("admin", "safety"),
        "raise_warning": ("admin", "field", "safety"),
        "raise_alarm": ("admin", "field", "safety"),
        "clear": ("admin", "safety"),
        "mark_faulty": ("admin", "safety"),
        "verify_misread": ("admin", "safety"),
        "degrade": ("admin", "safety"),
        "stop": ("admin", "safety"),
        "restore": ("admin", "safety", "field"),
        "bypass_restore": ("admin", "safety"),
        "restrict": ("admin", "safety", "field"),
        "block": ("admin", "safety", "field"),
        "clear": ("admin", "safety", "field"),
        "occupy": ("admin", "field", "safety"),
        "release": ("admin", "field", "safety"),
        "maintain": ("admin", "safety"),
        "reopen": ("admin", "safety"),
        "begin_evacuation": ("admin", "safety", "dispatcher"),
        "search": ("admin", "safety", "dispatcher"),
        "stabilize": ("admin", "safety", "dispatcher"),
        "recover": ("admin", "safety", "dispatcher"),
        "close": ("admin", "safety"),
        "assign": ("admin", "dispatcher", "safety"),
        "accept": ("admin", "field", "dispatcher"),
        "complete": ("admin", "field", "dispatcher"),
        "cancel": ("admin", "dispatcher", "safety"),
    }
    CUSTOM_CREATE = {
        "worker": lambda a, d, l, n: _validate_worker(d),
        "sensor": lambda a, d, l, n: _validate_sensor(d),
        "ventilation": lambda a, d, l, n: _validate_capacity(d, "capacity"),
        "passage": lambda a, d, l, n: _validate_passage(d),
        "refuge": lambda a, d, l, n: _validate_capacity(d, "capacity"),
        "incident": lambda a, d, l, n: _validate_incident(d),
        "task": lambda a, d, l, n: _validate_task(d, l),
        "offline_record": lambda a, d, l, n: _validate_offline(d),
        "air_permit": lambda a, d, l, n: _evaluate_air_permit(d, l, n),
    }
    CUSTOM_TRANSITIONS = {
        ("sensor", "raise_alarm"): lambda a, e, d, l, n: _sensor_alarm(a, e, d, l),
        ("incident", "close"): lambda a, e, d, l, n: _close_incident(a, e, d, l),
        ("task", "complete"): lambda a, e, d, l, n: _complete_task(a, e, d, l),
        ("ventilation", "stop"): _stop_ventilation,
        ("ventilation", "restore"): _validate_air_permit_restore,
    }

    def __init__(self, clock=None):
        # clock 返回 aware datetime，便于测试固定“当前时间”
        self._clock = clock

    def _now(self):
        value = self._clock() if self._clock else datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        if kind == "air_permit" and data and data.get("decision") in ("approved", "denied"):
            return data["decision"]
        return self.INITIAL_STATUS[kind]

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        _ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        _require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        if custom:
            extra = custom(actor, data, lookup, self._now())
            if extra:
                status_override, fields = extra
                data.update(fields)
                data["_initial_status"] = status_override
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        _ensure_role(actor, allowed)
        _require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup, self._now()) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
