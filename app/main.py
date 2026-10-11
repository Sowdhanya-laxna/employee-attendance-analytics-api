"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real environment variables win)

Layout of this single file
  1. configuration + Mongo client + index creation
  2. pure helpers (time zone, R1-R5 rules, rounding)  -- no third-party imports, unit-testable
  3. validation types and request models
  4. serializers (BSON <-> API)
  5. pipeline / query builders (shared by the real endpoints and /admin/explain)
  6. endpoints
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration, client, indexes
# --------------------------------------------------------------------------- #
load_dotenv()  # does NOT override variables that already exist in the environment
log = logging.getLogger("attendance")

# tz_aware=True: every datetime read from Mongo is an aware UTC datetime (never naive).
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

INDEXES = [
    # (collection, keys, options)
    # one employee per emp_code; also the lookup key for every join and every per-employee query
    ("employees", [("emp_code", ASCENDING)], {"name": "emp_code_unique", "unique": True}),
    # department filter (list, trend) + "joined on or before X" (headcount, R9)
    ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "dept_joined"}),
    # "joined on or before X" across all departments (department summary without a filter)
    ("employees", [("joined_on", ASCENDING)], {"name": "joined"}),
    # one record per (emp_code, date): makes punch-in race-safe; serves per-employee month/range scans
    ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], {"name": "emp_date_unique", "unique": True}),
    # GET /attendance default order (date desc, emp_code asc); month range scans for analytics
    ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_emp"}),
    # punch-out: "latest record of this employee whose punch_in <= t"
    ("attendance_logs", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_punch_in"}),
    # GET /attendance?status=...  with the same ordering
    ("attendance_logs", [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "status_date_emp"}),
]
_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent. Each index is created on its own so one failure cannot hide the others."""
    global _indexes_ready
    ok = True
    for coll, keys, opts in INDEXES:
        try:
            db[coll].create_index(keys, **opts)
        except PyMongoError as exc:
            ok = False
            log.error("index %s.%s failed: %s", coll, opts["name"], exc)
    _indexes_ready = ok


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception:  # never stop the app from starting; /health retries
        log.exception("could not create indexes at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_, exc: PyMongoError):
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Pure helpers  (BEGIN PURE HELPERS - stdlib only)
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_BELOW = Decimal("4.50")
MAX_SHIFT_SECONDS = 24 * 3600


def from_ms(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Datetime -> epoch ms (naive values are treated as UTC, which is what Mongo stores)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_hhmm(value: str) -> tuple:
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch-in
    (earlier than shift_end) belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and (local.hour, local.minute, local.second) < (*parse_hhmm(shift_end), 0):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str) -> tuple:
    """Aware IST (start, end) of the shift that belongs to attendance date `date_str`."""
    d = date_type.fromisoformat(date_str)
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return start, end


def round_half_up(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start; then floor(minutes since shift start)."""
    start, _ = shift_bounds(date_str, shift_start, shift_end)
    delta = int((punch_in - start).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600, 2 decimals, half-up (Decimal, so no binary-float or banker's-rounding surprises)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor(minutes after shift end), counted only if >= 30."""
    _, end = shift_bounds(date_str, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_derived(status: str, date_str: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                    shift_start: str, shift_end: str) -> dict:
    """All four derived fields for a record in its final state (DATA_MODEL.md section 3 table)."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, date_str, shift_start, shift_end)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": float(hours),
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, date_str, shift_start, shift_end),
        "half_day": hours < HALF_DAY_BELOW,  # R5 uses the ROUNDED value
    }


def month_range(month: str) -> tuple:
    y, m = int(month[:4]), int(month[5:7])
    return date_type(y, m, 1), date_type(y, m, calendar.monthrange(y, m)[1])


def count_working_days(first: date_type, last: date_type, joined_on: str) -> int:
    """R7: Mon-Fri between max(first, joined_on) and last (at most 31 days, not a per-document loop)."""
    start = max(first, date_type.fromisoformat(joined_on))
    days = (last - start).days + 1
    return sum(1 for i in range(max(days, 0)) if (start + timedelta(days=i)).weekday() < 5)
# (END PURE HELPERS)


# --------------------------------------------------------------------------- #
# 3. Validation types and request models
# --------------------------------------------------------------------------- #
def unprocessable(message: str, loc: tuple = ("body",)) -> HTTPException:
    """422 in FastAPI's validation-error shape (detail is a list) for rules checked inside endpoints."""
    return HTTPException(422, detail=[{"loc": list(loc), "msg": message, "type": "value_error"}])


def _valid_date(v: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v):
        raise ValueError("must be a date in YYYY-MM-DD format")
    try:
        date_type.fromisoformat(v)
    except ValueError:
        raise ValueError("not a real calendar date")
    return v


def _valid_month(v: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", v):
        raise ValueError("must be a month in YYYY-MM format")
    try:
        date_type(int(v[:4]), int(v[5:7]), 1)
    except ValueError:
        raise ValueError("not a real calendar month")
    return v


DateStr = Annotated[str, AfterValidator(_valid_date)]
MonthStr = Annotated[str, AfterValidator(_valid_month)]
# StrictInt: rejects floats, strings and booleans; the bounds reject seconds-looking and absurd values.
EpochMs = Annotated[StrictInt, Field(ge=MIN_MS, le=MAX_MS)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: DateStr

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Derived fields are not declared, so a client that sends them is simply ignored.
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 4. Serializers (BSON -> API)
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


_TIME_FIELDS = ("punch_in", "punch_out")


def serialize_history(entry: dict) -> dict:
    changes = {}
    for key, change in (entry.get("changes") or {}).items():
        conv = to_ms if key in _TIME_FIELDS else (lambda v: v)
        changes[key] = {"from": conv(change.get("from")), "to": conv(change.get("to"))}
    return {"at": to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def serialize_record(doc: dict) -> dict:
    """Missing history / half_day / counters on legacy documents become [] / False / 0."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": [serialize_history(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# 5. Query / pipeline builders  (used by the endpoints AND by /admin/explain)
# --------------------------------------------------------------------------- #
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def attendance_filter(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                      status: Optional[str]) -> dict:
    if date_from and date_to and date_from > date_to:
        raise unprocessable("date_from must not be after date_to", ("query", "date_from"))
    flt: dict = {}
    if emp_code:
        flt["emp_code"] = emp_code
    if date_from or date_to:
        flt["date"] = {}
        if date_from:
            flt["date"]["$gte"] = date_from
        if date_to:
            flt["date"]["$lte"] = date_to
    if status:
        flt["status"] = status
    return flt


# ---- small expression builders. prefix "" -> "$field" (document level); "l" -> "$$l.field" (inside $map) ----
def _f(prefix: str, name: str) -> str:
    return f"${name}" if not prefix else f"$${prefix}.{name}"


def _is_weekday(prefix: str) -> dict:
    # `date` is a plain "YYYY-MM-DD" string; parsed as UTC midnight so the weekday is the calendar weekday.
    # $dayOfWeek: 1 = Sunday ... 7 = Saturday  ->  Mon-Fri = 2..6
    return {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": _f(prefix, "date"), "format": "%Y-%m-%d"}}}, [2, 3, 4, 5, 6]]}


def _is_presence(prefix: str) -> dict:
    return {"$in": [_f(prefix, "status"), PRESENCE]}


def _present_value(prefix: str) -> dict:
    """R5-R7: present Mon-Fri record = 1, or 0.5 for a half day; weekend or non-present = 0."""
    return {"$cond": [
        {"$and": [_is_presence(prefix), _is_weekday(prefix)]},
        {"$cond": [{"$eq": [{"$ifNull": [_f(prefix, "half_day"), False]}, True]}, 0.5, 1]},
        0,
    ]}


def _late_flag(prefix: str) -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": [_f(prefix, "late_minutes"), 0]}, 0]}, 1, 0]}


def _late_min(prefix: str) -> dict:
    return {"$ifNull": [_f(prefix, "late_minutes"), 0]}


def _has_hours(prefix: str) -> dict:
    return {"$and": [_is_presence(prefix), {"$isNumber": _f(prefix, "work_hours")}]}


def _hours_cents(prefix: str) -> dict:
    """work_hours as integer hundredths, so sums are exact and the final average rounds half-up in Decimal."""
    return {"$cond": [_has_hours(prefix), {"$round": [{"$multiply": [_f(prefix, "work_hours"), 100]}, 0]}, 0]}


def _status_flag(prefix: str, status: str) -> dict:
    return {"$cond": [{"$eq": [_f(prefix, "status"), status]}, 1, 0]}


def build_monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _present_value("")},
            "leave_days": {"$sum": _status_flag("", "LEAVE")},
            "late_count": {"$sum": _late_flag("")},
            "total_late_minutes": {"$sum": _late_min("")},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def build_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    """Starts from `employees` (so people with zero logs are counted, R9) and joins each person's logs."""
    emp_match: dict = {"joined_on": {"$lte": end}}
    if department:
        emp_match["department"] = department

    def per_logs(expr_builder) -> dict:
        return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expr_builder("l")}}}

    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "work_hours": 1}},
            ],
            "as": "logs",
        }},
        {"$set": {
            "p_days": per_logs(_present_value),
            "p_late_count": per_logs(_late_flag),
            "p_late_min": per_logs(_late_min),
            "p_leave": per_logs(lambda p: _status_flag(p, "LEAVE")),
            "p_on_duty": per_logs(lambda p: _status_flag(p, "ON_DUTY")),
            "p_cents": per_logs(_hours_cents),
            "p_hours_n": per_logs(lambda p: {"$cond": [_has_hours(p), 1, 0]}),
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$p_days"},
            "late_count": {"$sum": "$p_late_count"},
            "total_late_minutes": {"$sum": "$p_late_min"},
            "leave_count": {"$sum": "$p_leave"},
            "on_duty_count": {"$sum": "$p_on_duty"},
            "hours_cents": {"$sum": "$p_cents"},
            "hours_n": {"$sum": "$p_hours_n"},
        }},
        {"$sort": {"_id": 1}},
    ]


def build_leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank INSIDE the department
    pipeline += [
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking, so ties at the cutoff are all kept
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def validate_trend_range(d_from: str, d_to: str) -> int:
    start, end = date_type.fromisoformat(d_from), date_type.fromisoformat(d_to)
    if end < start:
        raise unprocessable("'to' must not be before 'from'", ("query", "to"))
    days = (end - start).days + 1
    if days > 92:
        raise unprocessable("range must not exceed 92 days", ("query", "to"))
    return days


def build_trend_pipeline(department: str, d_from: str, d_to: str) -> list:
    """One document per calendar day is generated inside the database ($range + $unwind), the department's logs are
    pre-aggregated per date, and $setWindowFields produces the 7-row moving window. Rates are computed as exact
    integers (x10000, half-up) so no float rounding can flip a digit."""
    n_days = validate_trend_range(d_from, d_to)
    first_day = datetime.combine(date_type.fromisoformat(d_from), datetime.min.time(), tzinfo=timezone.utc)
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "emp_codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_codes",  # an array local field matches any of its elements
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": d_from, "$lte": d_to}}},
                {"$group": {
                    "_id": "$date",
                    "present2": {"$sum": {"$cond": [
                        _is_presence(""),
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]},
                        0]}},
                    "late_count": {"$sum": _late_flag("")},
                }},
            ],
            "as": "daily",
        }},
        {"$project": {
            "_id": 0, "joined": 1, "daily": 1,
            "days": {"$map": {"input": {"$range": [0, n_days]}, "as": "i", "in": {
                "$add": [first_day, {"$multiply": ["$$i", 86400000]}]}}},
        }},
        {"$unwind": "$days"},
        {"$set": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$days"}},
            "dow": {"$dayOfWeek": "$days"},
        }},
        {"$set": {
            "headcount": {"$size": {"$filter": {"input": "$joined", "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}},
            "stats": {"$arrayElemAt": [{"$filter": {"input": "$daily", "as": "x", "cond": {"$eq": ["$$x._id", "$date"]}}}, 0]},
            "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
        }},
        {"$set": {
            "present2": {"$ifNull": ["$stats.present2", 0]},
            "late_count": {"$ifNull": ["$stats.late_count", 0]},
        }},
        {"$set": {
            # attendance_rate * 10000 rounded half-up, using integers only: floor((2*p2*5000 + h) / (2h))
            "rate_x": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$floor": {"$divide": [{"$add": [{"$multiply": ["$present2", 10000]}, "$headcount"]},
                                        {"$multiply": [2, "$headcount"]}]}},
                None]},
        }},
        {"$set": {"has_rate": {"$cond": [{"$isNumber": "$rate_x"}, 1, 0]}}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "win_sum": {"$sum": "$rate_x", "window": {"documents": [-6, 0]}},
                "win_n": {"$sum": "$has_rate", "window": {"documents": [-6, 0]}},
            },
        }},
        {"$sort": {"date": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present2": 1, "late_count": 1,
                      "rate_x": 1, "win_sum": 1, "win_n": 1}},
    ]


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 6. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    if not _indexes_ready:
        ensure_indexes()  # retry if Mongo was not reachable at startup
    return {"status": "ok"}


# ---- employees ------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = utc_now()
    try:
        db.employees.insert_one(doc)  # the unique index decides the winner of a race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    flt = {"department": department} if department else {}
    total = db.employees.count_documents(flt)
    cursor = db.employees.find(flt).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# ---- attendance ------------------------------------------------------------ #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else utc_now()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **compute_derived(body.status, day, ts, None, emp["shift_start"], emp["shift_end"]),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else utc_now()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if ts <= pin:
        raise unprocessable("punched_at must be after the punch-in", ("body", "punched_at"))
    if (ts - pin).total_seconds() > MAX_SHIFT_SECONDS:
        raise unprocessable("punch-out is more than 24 hours after the punch-in", ("body", "punched_at"))
    derived = compute_derived(rec["status"], rec["date"], pin, ts, emp["shift_start"], emp["shift_end"])
    # Atomic compare-and-set: only succeeds while the record is still open and its punch_in is unchanged.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": pin},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    flt = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(flt)
    cursor = db.attendance_logs.find(flt).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_record(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for this date")

    supplied_times = body.punch_in is not None or body.punch_out is not None
    new_status = body.status or rec["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if supplied_times:
            raise unprocessable("ABSENT/LEAVE clear the punch times; do not send punch_in/punch_out")
        new_in = new_out = None
    else:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = from_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            raise unprocessable("a presence status requires a punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise unprocessable("punch_in must stay on the record's attendance date", ("body", "punch_in"))
        if supplied_times and new_out is not None:
            if new_out <= new_in or (new_out - new_in).total_seconds() > MAX_SHIFT_SECONDS:
                raise unprocessable("punch_out must be after punch_in and within 24 hours", ("body", "punch_out"))

    derived = compute_derived(new_status, date, new_in, new_out, emp["shift_start"], emp["shift_end"])
    old = {
        "status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"), "late_minutes": int(rec.get("late_minutes") or 0),
        "overtime_minutes": int(rec.get("overtime_minutes") or 0), "half_day": bool(rec.get("half_day") or False),
    }
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    changes = {k: {"from": old[k], "to": new[k]} for k in old if old[k] != new[k]}
    if not changes:
        raise unprocessable("this request changes nothing")

    entry = {"at": utc_now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n_hist = len(rec.get("history") or [])
    # Compare-and-set: succeeds only if nobody appended to history (or punched out) since we read the record.
    # $push is atomic, so an accepted entry can never be overwritten by a concurrent writer.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], f"history.{n_hist}": {"$exists": False},
         "status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")},
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return serialize_record(updated)


# ---- analytics ------------------------------------------------------------- #
def _r2(value) -> float:
    return float(round_half_up(Decimal(str(value)), 2))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    emp = get_employee_or_404(emp_code)
    first, last = month_range(month)
    rows = list(db.attendance_logs.aggregate(build_monthly_pipeline(emp_code, first.isoformat(), last.isoformat())))
    agg = rows[0] if rows else {}
    working = count_working_days(first, last, emp["joined_on"])
    present = float(agg.get("present_days", 0))
    pct = float(round_half_up(Decimal(str(present)) * 100 / Decimal(working), 2)) if working > 0 else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": present,
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    first, last = month_range(month)
    rows = db.employees.aggregate(build_summary_pipeline(first.isoformat(), last.isoformat(), department))
    items = []
    for r in rows:
        avg = None
        if r["hours_n"] > 0:
            avg = float(round_half_up(Decimal(int(r["hours_cents"])) / Decimal(int(r["hours_n"])) / 100, 2))
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthStr, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first, last = month_range(month)
    rows = db.attendance_logs.aggregate(build_leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department))
    items = [{
        "rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
        "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"]),
    } for r in rows]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, d_from: Annotated[DateStr, Query(alias="from")],
                     d_to: Annotated[DateStr, Query(alias="to")]):
    validate_trend_range(d_from, d_to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = []
    for r in db.employees.aggregate(build_trend_pipeline(department, d_from, d_to)):
        rate = None if r.get("rate_x") is None else float(Decimal(int(r["rate_x"])) / Decimal(10000))
        moving = None
        if r["win_n"] > 0:
            moving = float(round_half_up(Decimal(int(r["win_sum"])) / Decimal(int(r["win_n"])) / Decimal(10000), 4))
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": r["present2"] / 2,
            "late_count": int(r["late_count"]),
            "attendance_rate": rate,
            "moving_avg_7d": moving,
        })
    return {"department": department, "items": items}


# ---- admin: explain -------------------------------------------------------- #
ExplainName = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _json_safe(obj: Any) -> Any:
    return json.loads(json_util.dumps(obj, json_options=json_util.RELAXED_JSON_OPTIONS))


def _need(value, name: str):
    if value is None or value == "":
        raise unprocessable(f"query parameter '{name}' is required for this endpoint", ("query", name))
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainName,
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    d_from: Annotated[Optional[DateStr], Query(alias="from")] = None,
    d_to: Annotated[Optional[DateStr], Query(alias="to")] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {"find": collection, "filter": attendance_filter(emp_code, date_from, date_to, status),
                   "sort": dict(ATTENDANCE_SORT), "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint == "department_trend":
            collection = "employees"
            pipeline = build_trend_pipeline(_need(department, "department"), _need(d_from, "from"), _need(d_to, "to"))
        else:
            first, last = month_range(_need(month, "month"))
            s, e = first.isoformat(), last.isoformat()
            if endpoint == "employee_monthly":
                collection, pipeline = "attendance_logs", build_monthly_pipeline(_need(emp_code, "emp_code"), s, e)
            elif endpoint == "department_summary":
                collection, pipeline = "employees", build_summary_pipeline(s, e, department)
            else:  # late_leaderboard
                collection, pipeline = "attendance_logs", build_leaderboard_pipeline(s, e, limit, department)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": _json_safe(result)}
