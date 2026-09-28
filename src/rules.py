from datetime import datetime, timedelta, timezone

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError

# 送风许可阈值
OXYGEN_FLOOR_PCT = 19.5
METHANE_CEILING_PCT = 1.0
PERMIT_VALID_MINUTES = 30


def _require(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def _ensure_role(actor, allowed):
    if "*" not in allowed and actor.role not in allowed:
        raise PermissionDenied("role %s is not allowed here" % actor.role)


def _all(lookup, kind):
    kind = RuleEngine.ALIASES.get(kind, kind)
    return lookup(kind, "*", None) or [] if lookup else []


def _find_one(lookup, kind, field, value):
    kind = RuleEngine.ALIASES.get(kind, kind)
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


def _sensor_alarm(actor, entity, data, lookup):
    if float(entity["data"].get("gas_ppm", 0)) < float(entity["data"].get("threshold_ppm", 1)):
        raise ValidationError("alarm requires a reading at or above threshold")
    return {"acknowledged_by": actor.user_id}


def _complete_task(actor, entity, data, lookup):
    if not str(data.get("result", "")).strip():
        raise ValidationError("result is required")
    return {"completed_by": actor.user_id}


def _parse_dt(value, field):
    if value in (None, ""):
        raise ValidationError("missing required field: " + field)
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError(field + " must be ISO-8601")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# 传感器非安全状态：alarm 直接阻断送风，faulty 无法确认气体安全
_BLOCKING_SENSOR_STATUSES = ("alarm", "faulty")
# 人员尚未撤离/脱险的状态
_UNEVACUATED_WORKER_STATUSES = ("active", "missing", "located")


def evaluate_ventilation_permit(vent, tested_at, oxygen_pct, methane_pct, lookup, now):
    """逐项核查送风许可，返回 (decision, checks)。任何一项 fail 即拒绝。"""
    checks = []

    def add(code, label, passed, detail, blocking=True):
        if not passed and not blocking:
            result = "warning"
        elif passed:
            result = "pass"
        else:
            result = "fail"
        checks.append({
            "code": code,
            "label": label,
            "result": result,
            "detail": detail,
        })

    stopped_at_raw = (vent.get("data") or {}).get("stopped_at")
    stopped_at = _parse_dt(stopped_at_raw, "stopped_at") if stopped_at_raw else None
    add(
        "stopped_after_shutdown",
        "检测时间不早于设备停机时间",
        stopped_at is not None and tested_at >= stopped_at,
        "停机时间 %s，检测时间 %s" % (stopped_at_raw, tested_at.isoformat()),
    )

    age_minutes = (now - tested_at).total_seconds() / 60.0
    add(
        "test_fresh",
        "检测距当前不超过 %d 分钟" % PERMIT_VALID_MINUTES,
        0 <= age_minutes <= PERMIT_VALID_MINUTES,
        "检测时间 %s，距今 %.1f 分钟" % (tested_at.isoformat(), age_minutes),
    )

    add(
        "oxygen",
        "氧气浓度不低于 %.1f%%" % OXYGEN_FLOOR_PCT,
        oxygen_pct >= OXYGEN_FLOOR_PCT,
        "氧气 %.2f%%" % oxygen_pct,
    )
    add(
        "methane",
        "甲烷浓度低于 %.1f%%" % METHANE_CEILING_PCT,
        methane_pct < METHANE_CEILING_PCT,
        "甲烷 %.2f%%" % methane_pct,
    )

    area = vent.get("data", {}).get("area_code")
    sensors = [s for s in _all(lookup, "sensor") if s["data"].get("location_code") == area]
    blocking_sensors = [s for s in sensors if s["status"] in _BLOCKING_SENSOR_STATUSES]
    warning_sensors = [s for s in sensors if s["status"] == "warning"]
    if blocking_sensors:
        result, detail = (
            "fail",
            "报警或故障传感器：%s"
            % ", ".join(s["id"] + "(" + s["status"] + ")" for s in blocking_sensors),
        )
    elif warning_sensors:
        result, detail = (
            "warning",
            "预警传感器（不阻断送风，需关注）：%s"
            % ", ".join(s["id"] for s in warning_sensors),
        )
    else:
        result, detail = "pass", "区域传感器全部正常"
    checks.append({
        "code": "sensors",
        "label": "区域传感器无报警/故障",
        "result": result,
        "detail": detail,
    })

    workers = [w for w in _all(lookup, "worker") if w["data"].get("location_code") == area]
    unevacuated = [w for w in workers if w["status"] in _UNEVACUATED_WORKER_STATUSES]
    add(
        "workers_evacuated",
        "区域人员已全部撤离",
        not unevacuated,
        "未撤离人员：%s"
        % (
            ", ".join(w["id"] + "(" + w["status"] + ")" for w in unevacuated) or "无"
        ),
    )

    decision = "approved" if all(c["result"] != "fail" for c in checks) else "denied"
    return decision, checks


def _stop_ventilation(actor, entity, data, lookup, now):
    return {"stopped_by": actor.user_id, "stopped_at": now.isoformat(timespec="seconds")}


def _restore_ventilation(actor, entity, data, lookup, now):
    """停运设备启风机必须持有通过且仍在 30 分钟有效期内的送风许可，并复查现场状态。"""
    if entity["status"] != "stopped":
        # 降级设备直接恢复，不走向停机送风许可流程
        return {"restored_by": actor.user_id}
    permit_id = data.get("permit_id")
    if not permit_id:
        raise ValidationError("permit_id is required to start a stopped fan")
    permit = _find_one(lookup, "ventilation_permit", "id", permit_id)
    if not permit:
        raise ValidationError("ventilation permit not found: " + str(permit_id))
    if permit["status"] != "approved":
        raise ConflictError("ventilation permit %s is not approved" % permit_id)
    if permit["data"].get("ventilation_id") != entity["id"]:
        raise ValidationError("ventilation permit %s belongs to another device" % permit_id)

    tested_at = _parse_dt(permit["data"].get("tested_at"), "tested_at")
    if (now - tested_at).total_seconds() > PERMIT_VALID_MINUTES * 60:
        raise ConflictError(
            "ventilation permit %s expired: detection is older than %d minutes, retest required"
            % (permit_id, PERMIT_VALID_MINUTES)
        )

    decision, checks = evaluate_ventilation_permit(
        entity,
        tested_at,
        float(permit["data"].get("oxygen_pct")),
        float(permit["data"].get("methane_pct")),
        lookup,
        now,
    )
    if decision != "approved":
        failed = [c["label"] for c in checks if c["result"] == "fail"]
        raise ConflictError("ventilation permit %s no longer satisfies: %s" % (permit_id, "；".join(failed)))
    return {"restored_by": actor.user_id, "permit_id": permit_id}


def _close_incident(actor, entity, data, lookup):
    if [w for w in _all(lookup, "worker") if w["status"] in ("missing", "located")]:
        raise ConflictError("cannot close incident while workers are missing or located")
    active_tasks = [t for t in _all(lookup, "task") if t["status"] not in ("completed", "cancelled")]
    if active_tasks:
        raise ConflictError("cannot close incident while tasks remain active")
    non_running = [v for v in _all(lookup, "ventilation") if v["status"] != "running"]
    if non_running:
        raise ConflictError("cannot close incident until ventilation is restored")
    # 每个曾停运的区域都必须持有通过的送风许可
    stopped_areas = sorted({
        v["data"].get("area_code")
        for v in _all(lookup, "ventilation")
        if v["data"].get("stopped_at")
    })
    permitted_areas = {
        p["data"].get("area_code")
        for p in _all(lookup, "ventilation_permit")
        if p["status"] == "approved"
    }
    missing = [area for area in stopped_areas if area not in permitted_areas]
    if missing:
        raise ConflictError(
            "cannot close incident without an approved ventilation permit for areas: %s"
            % ", ".join(str(a) for a in missing)
        )
    return {"closed_by": actor.user_id}


class RuleEngine:
    ALIASES = {
        "workers": "worker", "sensors": "sensor", "ventilations": "ventilation",
        "passages": "passage", "refuges": "refuge", "incidents": "incident",
        "tasks": "task", "offline-records": "offline_record", "offline_records": "offline_record",
        "ventilation-permits": "ventilation_permit", "ventilation_permits": "ventilation_permit",
        "permits": "ventilation_permit",
    }
    INITIAL_STATUS = {
        "worker": "active", "sensor": "normal", "ventilation": "running",
        "passage": "open", "refuge": "available", "incident": "detected",
        "task": "proposed", "offline_record": "merged",
        # 送风许可的初始状态由 validate_create 动态计算（approved/denied）
        "ventilation_permit": "denied",
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
            "restore": (("stopped", "degraded"), "running"),
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
        "ventilation_permit": ("ventilation_id", "tested_at", "oxygen_pct", "methane_pct"),
    }
    ACTION_REQUIRED = {
        ("worker", "rescue"): ("incident_id",),
        ("sensor", "mark_faulty"): ("reason",),
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
        "ventilation_permit": ("safety", "admin"),
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
        "restore": ("admin", "safety"),
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
        "worker": lambda a, d, l: _validate_worker(d),
        "sensor": lambda a, d, l: _validate_sensor(d),
        "ventilation": lambda a, d, l: _validate_capacity(d, "capacity"),
        "passage": lambda a, d, l: _validate_passage(d),
        "refuge": lambda a, d, l: _validate_capacity(d, "capacity"),
        "incident": lambda a, d, l: _validate_incident(d),
        "task": lambda a, d, l: _validate_task(d, l),
        "offline_record": lambda a, d, l: _validate_offline(d),
    }
    CUSTOM_TRANSITIONS = {
        ("sensor", "raise_alarm"): _sensor_alarm,
        ("incident", "close"): _close_incident,
        ("task", "complete"): _complete_task,
    }

    def __init__(self, clock=None):
        # clock 返回带时区的当前时间，便于测试控制 30 分钟时效
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.CUSTOM_TRANSITIONS = dict(self.CUSTOM_TRANSITIONS)
        self.CUSTOM_TRANSITIONS[("ventilation", "stop")] = (
            lambda a, e, d, l: _stop_ventilation(a, e, d, l, self.clock())
        )
        self.CUSTOM_TRANSITIONS[("ventilation", "restore")] = (
            lambda a, e, d, l: _restore_ventilation(a, e, d, l, self.clock())
        )
        self.CUSTOM_CREATE = dict(self.CUSTOM_CREATE)
        self.CUSTOM_CREATE["ventilation_permit"] = self._validate_permit

    def _validate_permit(self, actor, data, lookup):
        vent_id = data.get("ventilation_id")
        vent = _find_one(lookup, "ventilation", "id", vent_id)
        if not vent:
            raise ValidationError("ventilation_id must reference an existing ventilation device")
        if vent["status"] != "stopped":
            raise ValidationError("a ventilation permit can only be issued for a stopped device")
        tested_at = _parse_dt(data.get("tested_at"), "tested_at")
        oxygen_pct = _number(data.get("oxygen_pct"), "oxygen_pct")
        methane_pct = _number(data.get("methane_pct"), "methane_pct")
        if not 0 <= oxygen_pct <= 100:
            raise ValidationError("oxygen_pct must be between 0 and 100")
        if not 0 <= methane_pct <= 100:
            raise ValidationError("methane_pct must be between 0 and 100")

        decision, checks = evaluate_ventilation_permit(
            vent, tested_at, oxygen_pct, methane_pct, lookup, self.clock()
        )
        data["ventilation_id"] = vent["id"]
        data["area_code"] = vent["data"].get("area_code")
        data["oxygen_pct"] = oxygen_pct
        data["methane_pct"] = methane_pct
        data["tested_at"] = tested_at.isoformat()
        data["decision"] = decision
        data["checks"] = checks
        data["issued_by"] = actor.user_id
        data["issued_at"] = self.clock().isoformat(timespec="seconds")
        # initial_status 据此返回 approved/denied
        data["_initial_status"] = decision
        return data

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        if data and data.get("_initial_status"):
            return data.pop("_initial_status")
        return self.INITIAL_STATUS[kind]

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        _ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        _require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
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
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
