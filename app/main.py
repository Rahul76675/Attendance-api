"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (real environment variables win over a local .env)

Layout of this single file:
  1. configuration / database / index creation
  2. pure business rules R1-R5 (no I/O, unit-testable)
  3. conversion + validation helpers
  4. request models
  5. endpoints: system, employees, attendance
  6. aggregation pipeline builders + analytics endpoints
  7. explain endpoint
"""
import calendar
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

try:
    import mongomock
except ImportError:  # pragma: no cover - handled by requirements, but safe to keep
    mongomock = None

load_dotenv()  # does not override variables that are already set in the environment
log = logging.getLogger("attendance")


def build_client():
    """Use real MongoDB when available, otherwise fall back to a local in-memory database."""
    uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    db_name = os.getenv("MONGO_DB", "attendance_db")
    try:
        # A real MongoDB server is preferred when running with a configured installation.
        client = MongoClient(
            uri,
            tz_aware=True,
            tzinfo=timezone.utc,
            serverSelectionTimeoutMS=3000,
        )
        client.admin.command("ping")
        return client, db_name, False
    except Exception:
        if mongomock is None:
            raise
        log.warning("MongoDB unavailable; using in-memory mongomock database for local execution")
        client = mongomock.MongoClient()
        return client, db_name, True


client, db_name, _is_mocked = build_client()
db = client[db_name]
employees = db["employees"]
logs = db["attendance_logs"]


def seed_sample_data_if_needed():
    """Populate the bundled sample dataset when a fresh local database is being used."""
    if not employees.count_documents({}) and not logs.count_documents({}):
        base_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sample_data")
        for collection_name, filename in (("employees", "employees.json"), ("attendance_logs", "attendance_logs.json")):
            path = os.path.join(base_dir, filename)
            with open(path, "r", encoding="utf-8") as f:
                docs = json_util.loads(f.read())
            db[collection_name].insert_many(docs)


seed_sample_data_if_needed()

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: creating an index that already exists with the same spec is a no-op."""
    global _indexes_ready
    # employees
    employees.create_index([("emp_code", 1)], unique=True, name="emp_code_unique")  # identity + race-safe create
    employees.create_index([("department", 1), ("emp_code", 1)], name="dept_emp_code")  # list/filter, trend, summary
    employees.create_index([("joined_on", 1)], name="joined_on")  # headcount (R9) without a department filter
    # attendance_logs
    logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="emp_date_unique")  # one record/day, PATCH, monthly, $lookup
    logs.create_index([("date", -1), ("emp_code", 1)], name="date_emp")  # list sort order, month range scans (leaderboard)
    logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp")  # list filtered by status
    logs.create_index([("emp_code", 1), ("punch_in", -1)], name="emp_punch_in")  # punch-out: latest punch-in <= t
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception:  # MongoDB may still be starting; /health retries until it works
        log.exception("could not create indexes at startup; /health will retry")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.error("mongo error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Pure business rules (R1 - R5)
# --------------------------------------------------------------------------- #
# --- BEGIN PURE RULES ---
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_HOURS = Decimal("4.50")


def hhmm_to_seconds(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm_to_seconds(shift_end) <= hhmm_to_seconds(shift_start)


def shift_instant(date_str: str, hhmm: str) -> datetime:
    """The IST wall-clock time `hhmm` on calendar date `date_str`, as an aware datetime."""
    d = date.fromisoformat(date_str)
    h, m = hhmm.split(":")
    return datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=IST)


def attendance_date_for(ts: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; overnight shifts: before shift_end -> previous day."""
    local = ts.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end):
        seconds_into_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_into_day < hhmm_to_seconds(shift_end):
            d -= timedelta(days=1)
    return d.isoformat()


def late_minutes_for(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: strictly more than 10:00 after shift_start -> whole minutes since shift_start, else 0."""
    secs = int((punch_in - shift_instant(date_str, shift_start)).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def work_hours_for(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals, half-up (Decimal, not float round)."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def overtime_for(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift_end, only if >= 30. Overnight shifts end on the next calendar day."""
    end = shift_instant(date_str, shift_end)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive_on_punch_out(punch_in: datetime, punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> dict:
    wh = work_hours_for(punch_in, punch_out)
    return {
        "work_hours": wh,
        "overtime_minutes": overtime_for(punch_out, date_str, shift_start, shift_end),
        "half_day": Decimal(str(wh)) < HALF_DAY_HOURS,  # R5, on the rounded value
    }


def derive_all(status: str, punch_in, punch_out, date_str: str, shift_start: str, shift_end: str) -> dict:
    """All four derived fields for a final record state (used by corrections)."""
    if status not in PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {"late_minutes": late_minutes_for(punch_in, date_str, shift_start)}
    if punch_out is None:
        out.update({"work_hours": None, "overtime_minutes": 0, "half_day": False})
    else:
        out.update(derive_on_punch_out(punch_in, punch_out, date_str, shift_start, shift_end))
    return out


def round_half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))
# --- END PURE RULES ---


# --------------------------------------------------------------------------- #
# 3. Conversion + validation helpers
# --------------------------------------------------------------------------- #
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]
StatusT = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


def as_utc(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_ms(dt) -> Optional[int]:
    """BSON datetime -> epoch milliseconds (integer arithmetic, no float rounding)."""
    return None if dt is None else (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, UTC)


def now_ms() -> int:
    return int(time.time() * 1000)


def now_dt() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)  # BSON keeps milliseconds


def parse_date(value: str, name: str = "date") -> date:
    if not DATE_RE.match(value):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid calendar date")


def month_bounds(month: str):
    """-> (first_day, last_day) as date objects; 422 for impossible months."""
    try:
        y, m = int(month[:4]), int(month[5:7])
        first = date(y, m, 1)
        last = date(y, m, calendar.monthrange(y, m)[1])
    except (ValueError, OverflowError):
        raise HTTPException(422, "month must be YYYY-MM")
    return first, last


def weekdays_between(a: date, b: date) -> int:
    n, d = 0, a
    while d <= b:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc.get("name"), "email": doc.get("email"),
        "department": doc.get("department"), "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"), "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def history_out(entries) -> list:
    out = []
    for e in entries or []:
        changes = {}
        for field, ft in (e.get("changes") or {}).items():
            if field in ("punch_in", "punch_out"):
                ft = {"from": to_ms(ft.get("from")), "to": to_ms(ft.get("to"))}
            changes[field] = ft
        out.append({"at": to_ms(e.get("at")), "by": e.get("by"), "reason": e.get("reason"), "changes": changes})
    return out


def attendance_out(doc: dict) -> dict:
    """Stored document -> API record. Missing legacy fields get their documented defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history_out(doc.get("history")),
    }


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field("18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("name", "department")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @field_validator("joined_on")
    @classmethod
    def valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("must be YYYY-MM-DD")
        date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[StatusT] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 5. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_dt()
    try:
        employees.insert_one(dict(doc))  # copy: insert_one adds _id to what it is given
    except DuplicateKeyError:  # the unique index decides, not a prior find_one
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = employees.count_documents(q)
    cur = employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at if body.punched_at is not None else now_ms())
    d = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes_for(ts, d, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): of N simultaneous inserts exactly one succeeds
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at if body.punched_at is not None else now_ms())
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = as_utc(rec["punch_in"])
    if ts <= pin:
        raise HTTPException(422, "punched_at must be after punch_in")
    if ts - pin > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")
    sets = {"punch_out": ts, **derive_on_punch_out(pin, ts, rec["date"], emp["shift_start"], emp["shift_end"])}
    # Atomic compare-and-set: only the request that still sees an open record wins.
    res = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": sets},
        return_document=True,
    )
    if res is None:
        raise HTTPException(409, "already punched out")
    return attendance_out(res)


def build_attendance_query(emp_code, date_from, date_to, status, page, page_size) -> dict:
    """Shared by GET /attendance and its explain, so both run exactly the same query."""
    df = parse_date(date_from, "date_from") if date_from else None
    dt = parse_date(date_to, "date_to") if date_to else None
    if df and dt and df > dt:
        raise HTTPException(422, "date_from must not be after date_to")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if df or dt:
        q["date"] = {}
        if df:
            q["date"]["$gte"] = date_from
        if dt:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    # with a single emp_code the tie-break on emp_code is constant, so leave it out of the sort
    sort = [("date", -1)] if emp_code else [("date", -1), ("emp_code", 1)]
    return {"filter": q, "sort": sort, "skip": (page - 1) * page_size, "limit": page_size}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = logs.count_documents(spec["filter"])
    cur = logs.find(spec["filter"], {"_id": 0}).sort(spec["sort"]).skip(spec["skip"]).limit(spec["limit"])
    return {"items": [attendance_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


def _same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(a - b) < 1e-9
    return a == b


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date")
    emp = get_employee_or_404(emp_code)
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    ss, se = emp["shift_start"], emp["shift_end"]
    status = body.status or rec["status"]
    if status in PRESENCE:
        pin = from_ms(body.punch_in) if body.punch_in is not None else as_utc(rec.get("punch_in"))
        pout = from_ms(body.punch_out) if body.punch_out is not None else as_utc(rec.get("punch_out"))
        if pin is None:
            raise HTTPException(422, "a presence status requires punch_in")
        if attendance_date_for(pin, ss, se) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if pout is not None and not (pin < pout <= pin + timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    else:  # ABSENT / LEAVE clear both punch times
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"punch times cannot be supplied for status {status}")
        pin = pout = None

    new = {"status": status, "punch_in": pin, "punch_out": pout,
           **derive_all(status, pin, pout, date, ss, se)}
    old = {
        "status": rec.get("status"), "punch_in": as_utc(rec.get("punch_in")), "punch_out": as_utc(rec.get("punch_out")),
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old[k], "to": new[k]} for k in old if not _same(old[k], new[k])}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {"at": now_dt(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    hist = rec.get("history")
    # Optimistic concurrency: the write only applies if the record is still exactly as we read it
    # (same history length, same punches/status). The loser gets 409 instead of overwriting history.
    guard = {"emp_code": emp_code, "date": date, "status": rec.get("status"),
             "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")}
    if isinstance(hist, list):
        guard["history"] = {"$size": len(hist)}
        update = {"$set": new, "$push": {"history": entry}}
    else:  # legacy record without a history array
        guard["history"] = None
        update = {"$set": {**new, "history": [entry]}}
    if logs.update_one(guard, update).matched_count == 0:
        raise HTTPException(409, "record was modified concurrently; retry")
    updated = {**rec, **new, "history": (hist if isinstance(hist, list) else []) + [entry]}
    return attendance_out(updated)


# --------------------------------------------------------------------------- #
# 6. Aggregation pipelines + analytics endpoints
# --------------------------------------------------------------------------- #
# Reusable expressions over an attendance_logs document. "p2" is the present-day weight DOUBLED
# (1 = half day, 2 = full day) so sums stay exact integers; divide by 2 at the end.
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
IS_LATE = {"$gt": [LATE_MIN, 0]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}
IS_HALF = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
WEIGHT_P2 = {"$cond": [IS_HALF, 1, 2]}
P2_ANY_DAY = {"$cond": [IS_PRESENT, WEIGHT_P2, 0]}
P2_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, WEIGHT_P2, 0]}
ONE_IF = lambda cond: {"$cond": [cond, 1, 0]}  # noqa: E731


def pipeline_employee_monthly(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "p2": {"$sum": P2_WEEKDAY},
            "leave_days": {"$sum": ONE_IF({"$eq": ["$status", "LEAVE"]})},
            "late_count": {"$sum": ONE_IF(IS_LATE)},
            "total_late_minutes": {"$sum": LATE_MIN},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def pipeline_department_summary(start: str, end: str, department: Optional[str]) -> list:
    """Driven from `employees` so people with zero logs still count (R9); logs are joined per employee."""
    match = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    has_hours = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$group": {
                    "_id": None,
                    "p2": {"$sum": P2_WEEKDAY},
                    # work hours in integer hundredths -> exact sums, exact half-up mean later
                    "wh100": {"$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
                    "wh_n": {"$sum": ONE_IF(has_hours)},
                    "late_count": {"$sum": ONE_IF(IS_LATE)},
                    "late_total": {"$sum": LATE_MIN},
                    "leave": {"$sum": ONE_IF({"$eq": ["$status", "LEAVE"]})},
                    "on_duty": {"$sum": ONE_IF({"$eq": ["$status", "ON_DUTY"]})},
                }},
            ],
            "as": "s",
        }},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "p2": {"$sum": {"$ifNull": ["$s.p2", 0]}},
            "wh100": {"$sum": {"$ifNull": ["$s.wh100", 0]}},
            "wh_n": {"$sum": {"$ifNull": ["$s.wh_n", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
            "late_total": {"$sum": {"$ifNull": ["$s.late_total", 0]}},
            "leave": {"$sum": {"$ifNull": ["$s.leave", 0]}},
            "on_duty": {"$sum": {"$ifNull": ["$s.on_duty", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


def pipeline_late_leaderboard(start: str, end: str, limit: int, department: Optional[str]) -> list:
    p = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": LATE_MIN}, "late_count": {"$sum": ONE_IF(IS_LATE)}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        p.append({"$match": {"emp.department": department}})  # rank inside the department only
    p += [
        # $rank = standard competition ranking (1, 2, 2, 4); computed BEFORE the limit is applied
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return p


def pipeline_department_trend(department: str, d_from: date, d_to: date) -> list:
    """
    One pass over the department's employees emits two kinds of rows per employee:
      * a 'join' row (hc=1) on max(joined_on, from) -> cumulative sum later gives headcount (R9)
      * one row per log in range (present weight, late flag)
    Rows are grouped per day, $densify fills the missing calendar days, and $setWindowFields
    computes headcount (running sum) and the 7-day moving average. Rates are rounded half-up
    with integer arithmetic (no float rounding surprises).
    """
    f, t = d_from.isoformat(), d_to.isoformat()
    lo = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    hi = datetime(d_to.year, d_to.month, d_to.day, tzinfo=UTC) + timedelta(days=1)  # upper bound is exclusive
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": f, "$lte": t}}},
                {"$project": {"_id": 0, "d": "$date", "hc": {"$literal": 0}, "p2": P2_ANY_DAY, "l": ONE_IF(IS_LATE)}},
            ],
            "as": "rows",
        }},
        {"$project": {"_id": 0, "rows": {"$concatArrays": [
            {"$cond": [{"$lte": ["$joined_on", t]},
                       [{"d": {"$cond": [{"$gt": ["$joined_on", f]}, "$joined_on", f]}, "hc": 1, "p2": 0, "l": 0}],
                       []]},
            "$rows",
        ]}}},
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {"$group": {"_id": "$d", "hc": {"$sum": "$hc"}, "p2": {"$sum": "$p2"}, "late": {"$sum": "$l"}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$sort": {"day": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$set": {"hc": {"$ifNull": ["$hc", 0]}, "p2": {"$ifNull": ["$p2", 0]}, "late": {"$ifNull": ["$late", 0]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "headcount": {"$sum": "$hc", "window": {"documents": ["unbounded", "current"]}}}}},
        {"$set": {"working": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]}}},
        # rate in 1/10000ths, half-up: floor((10000*p2 + h) / (2h))   with rate = (p2/2)/h
        {"$set": {"rate10k": {"$cond": [
            {"$and": ["$working", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$p2", 10000]}, "$headcount"]}, {"$multiply": ["$headcount", 2]}]}},
            None]}}},
        {"$set": {"rk": {"$cond": [{"$isNumber": "$rate10k"}, 1, 0]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "w_sum": {"$sum": "$rate10k", "window": {"documents": [-6, 0]}},
            "w_cnt": {"$sum": "$rk", "window": {"documents": [-6, 0]}}}}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "is_working_day": "$working",
            "headcount": 1,
            "present_count": {"$divide": ["$p2", 2]},
            "late_count": "$late",
            "attendance_rate": {"$cond": [{"$isNumber": "$rate10k"}, {"$divide": ["$rate10k", 10000]}, None]},
            "moving_avg_7d": {"$cond": [
                {"$gt": ["$w_cnt", 0]},
                {"$divide": [{"$floor": {"$divide": [{"$add": [{"$multiply": ["$w_sum", 2]}, "$w_cnt"]},
                                                     {"$multiply": ["$w_cnt", 2]}]}}, 10000]},
                None]},
        }},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    first, last = month_bounds(month)
    emp = get_employee_or_404(emp_code)
    row = next(iter(logs.aggregate(pipeline_employee_monthly(emp_code, first.isoformat(), last.isoformat()))), None) or {}
    joined = parse_date(emp["joined_on"], "joined_on")
    working_days = weekdays_between(max(first, joined), last) if joined <= last else 0
    present_days = (row.get("p2") or 0) / 2
    pct = None
    if working_days:
        pct = round_half_up(Decimal(str(present_days)) / Decimal(working_days) * 100, 2)
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": round_half_up(Decimal(str(present_days)), 2),
        "leave_days": row.get("leave_days", 0), "late_count": row.get("late_count", 0),
        "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for r in employees.aggregate(pipeline_department_summary(first.isoformat(), last.isoformat(), department)):
        avg = None
        if r["wh_n"]:
            avg = round_half_up(Decimal(int(round(r["wh100"]))) / Decimal(r["wh_n"]) / Decimal(100), 2)
        items.append({
            "department": r["_id"], "headcount": r["headcount"],
            "present_days": round_half_up(Decimal(r["p2"]) / 2, 2), "avg_work_hours": avg,
            "late_count": r["late_count"], "total_late_minutes": r["late_total"],
            "leave_count": r["leave"], "on_duty_count": r["on_duty"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    items = list(logs.aggregate(pipeline_late_leaderboard(first.isoformat(), last.isoformat(), limit, department)))
    return {"month": month, "items": items}


def validate_trend_range(from_: str, to: str):
    d_from, d_to = parse_date(from_, "from"), parse_date(to, "to")
    if d_to < d_from:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    d_from, d_to = validate_trend_range(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = list(employees.aggregate(pipeline_department_trend(department, d_from, d_to)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 7. Explain endpoint
# --------------------------------------------------------------------------- #
def _need(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing required query parameter(s): {', '.join(missing)}")


def _jsonable(doc) -> dict:
    return json.loads(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Each branch builds the query with the SAME builder the real endpoint uses."""
    if endpoint == "attendance_list":
        spec = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
        coll = logs.name
        cmd = {"find": coll, "filter": spec["filter"], "sort": dict(spec["sort"]),
               "skip": spec["skip"], "limit": spec["limit"]}
    else:
        if endpoint == "employee_monthly":
            _need(emp_code=emp_code, month=month)
            first, last = month_bounds(month)
            coll, pipe = logs.name, pipeline_employee_monthly(emp_code, first.isoformat(), last.isoformat())
        elif endpoint == "department_summary":
            _need(month=month)
            first, last = month_bounds(month)
            coll, pipe = employees.name, pipeline_department_summary(first.isoformat(), last.isoformat(), department)
        elif endpoint == "late_leaderboard":
            _need(month=month)
            first, last = month_bounds(month)
            coll, pipe = logs.name, pipeline_late_leaderboard(first.isoformat(), last.isoformat(), limit, department)
        else:  # department_trend
            _need(department=department, **{"from": from_, "to": to})
            d_from, d_to = validate_trend_range(from_, to)
            coll, pipe = employees.name, pipeline_department_trend(department, d_from, d_to)
        cmd = {"aggregate": coll, "pipeline": pipe, "cursor": {}}
    out = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": _jsonable(out)}
