"""
Employee Attendance & Analytics API - Production Implementation
Contract v2.0 (FastAPI + MongoDB)
All code lives in this file per assignment requirements.
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from pydantic import BaseModel, Field, StrictInt, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
VALID_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ABSENCE_STATUSES = {"ABSENT", "LEAVE"}


# --------------------------------------------------------------------------- #
# Indexes created idempotently at startup
# --------------------------------------------------------------------------- #
def init_indexes():
    # employees collection
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index("joined_on")

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helper Functions
# --------------------------------------------------------------------------- #
def truncate_instant_ms(epoch_ms: int) -> int:
    """R1: Truncate instant to whole seconds."""
    return (epoch_ms // 1000) * 1000


def ms_to_datetime_utc(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime truncated to whole seconds."""
    truncated = truncate_instant_ms(epoch_ms)
    return datetime.fromtimestamp(truncated / 1000, tz=timezone.utc)


def datetime_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to epoch milliseconds."""
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return int(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start):
    a punch-in whose IST time is strictly earlier than shift_end belongs to the shift
    that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    ist_time_str = punch_in_ist.strftime("%H:%M")
    if is_overnight and ist_time_str < shift_end:
        attendance_day = punch_in_ist - timedelta(days=1)
        return attendance_day.strftime("%Y-%m-%d")
    return punch_in_ist.strftime("%Y-%m-%d")


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, date_str: str) -> int:
    """
    R2: Late. Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    y, mon, d = map(int, date_str.split("-"))
    start_dt = datetime(y, mon, d, h, m, 0, tzinfo=IST)
    diff_sec = (punch_in_ist - start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime(punch_out_utc: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    """
    R3: Overtime.
    Whole minutes (floor) between shift_end and punch_out,
    counted only when >= 30; otherwise 0.
    Overnight shifts: shift_end falls on next calendar day.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    h, m = map(int, shift_end.split(":"))
    y, mon, d = map(int, date_str.split("-"))
    end_dt = datetime(y, mon, d, h, m, 0, tzinfo=IST)
    if shift_end <= shift_start:
        end_dt += timedelta(days=1)
    diff_sec = (punch_out_ist - end_dt).total_seconds()
    whole_mins = int(diff_sec // 60)
    return whole_mins if whole_mins >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: Work hours.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = Decimal(str(int((punch_out_utc - punch_in_utc).total_seconds())))
    hours = diff_sec / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up(val: float, decimals: int) -> float:
    """R8: Rounding of reported numbers (half-up)."""
    d = Decimal(str(val))
    return float(d.quantize(Decimal(f"1e-{decimals}"), rounding=ROUND_HALF_UP))


def get_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7: Mon-Fri working days in the month from joined_on.
    """
    year, m = map(int, month_str.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = date(year, m, 1)
    month_end = date(year, m, last_day)
    joined_on = date.fromisoformat(joined_on_str)
    start_d = max(month_start, joined_on)
    if start_d > month_end:
        return 0
    count = 0
    cur = start_d
    while cur <= month_end:
        if cur.weekday() < 5:  # Mon..Fri
            count += 1
        cur += timedelta(days=1)
    return count


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB attendance document into API response dictionary."""
    res = dict(doc)
    res.pop("_id", None)
    res.pop("id", None)
    res["punch_in"] = datetime_to_ms(res.get("punch_in"))
    res["punch_out"] = datetime_to_ms(res.get("punch_out"))
    res["late_minutes"] = res.get("late_minutes", 0)
    res["overtime_minutes"] = res.get("overtime_minutes", 0)
    res["half_day"] = res.get("half_day", False)

    raw_history = res.get("history") or []
    formatted_history = []
    for h in raw_history:
        h_entry = {
            "at": datetime_to_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {},
        }
        for k, change in h.get("changes", {}).items():
            f_val = change.get("from")
            t_val = change.get("to")
            if k in ("punch_in", "punch_out"):
                f_val = datetime_to_ms(f_val)
                t_val = datetime_to_ms(t_val)
            h_entry["changes"][k] = {"from": f_val, "to": t_val}
        formatted_history.append(h_entry)
    res["history"] = formatted_history
    return res


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB employee document into API response dictionary."""
    res = dict(doc)
    res.pop("_id", None)
    res["created_at"] = datetime_to_ms(res.get("created_at"))
    return res


# --------------------------------------------------------------------------- #
# Pydantic Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("shift_end")
    @classmethod
    def validate_shifts(cls, v: str, info):
        shift_start = info.data.get("shift_start")
        if shift_start is not None and shift_start == v:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)


# --------------------------------------------------------------------------- #
# Pipelines Builders for Analytics & Explain
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    month_start = f"{month}-01"
    month_end = f"{month}-31"
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": "$late_minutes"
                },
                "total_overtime_minutes": {
                    "$sum": "$overtime_minutes"
                },
            }
        },
    ]


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    match_emp: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {
                                                    "$in": [
                                                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                                        [2, 3, 4, 5, 6],
                                                    ]
                                                },
                                            ]
                                        },
                                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                        0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            },
                            "total_late_minutes": {
                                "$sum": "$late_minutes"
                            },
                            "leave_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                            },
                            "on_duty_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "log_summary",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": [{"$arrayElemAt": ["$log_summary.present_days", 0]}, 0]}},
                "work_hours_sum": {"$sum": {"$ifNull": [{"$arrayElemAt": ["$log_summary.work_hours_sum", 0]}, 0]}},
                "work_hours_count": {"$sum": {"$ifNull": [{"$arrayElemAt": ["$log_summary.work_hours_count", 0]}, 0]}},
                "late_count": {"$sum": {"$ifNull": [{"$arrayElemAt": ["$log_summary.late_count", 0]}, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": [{"$arrayElemAt": ["$log_summary.total_late_minutes", 0]}, 0]}},
                "leave_count": {"$sum": {"$ifNull": [{"$arrayElemAt": ["$log_summary.leave_count", 0]}, 0]}},
                "on_duty_count": {"$sum": {"$ifNull": [{"$arrayElemAt": ["$log_summary.on_duty_count", 0]}, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$round": [{"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None) -> List[Dict[str, Any]]:
    month_start = f"{month}-01"
    month_end = f"{month}-31"
    pipe: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipe.append({"$match": {"emp.department": department}})

    pipe.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
        {
            "$project": {
                "rank": 1,
                "emp_code": 1,
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipe


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    dt_from = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    dt_to = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc)
    dt_to_bound = dt_to + timedelta(days=1)

    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$match": {
                "emp.department": department
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "date": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [dt_from, dt_to_bound],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$date_dt"},
                        [2, 3, 4, 5, 6],
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MongoDB unavailable")


@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(emp) for emp in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = ms_to_datetime_utc(body.punched_at)
    else:
        now_dt = datetime.now(timezone.utc)
        punch_in_dt = now_dt.replace(microsecond=0)

    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = ms_to_datetime_utc(body.punched_at)
    else:
        now_dt = datetime.now(timezone.utc)
        punch_out_dt = now_dt.replace(microsecond=0)

    # Find the employee's most recent record that has a punch_in
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in found")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at must be after punch_in and within 24 hours",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already punched out")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_mins = compute_overtime(punch_out_dt, emp["shift_end"], record["date"], emp["shift_start"])
    half_day = bool(work_hours < 4.50)

    # Concurrency safe atomic update
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from must be <= date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_filter:
        if status_filter not in VALID_STATUSES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid status")
        q["status"] = status_filter

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    cur_status = record.get("status")
    cur_punch_in = record.get("punch_in")
    if cur_punch_in and cur_punch_in.tzinfo is None:
        cur_punch_in = cur_punch_in.replace(tzinfo=timezone.utc)
    cur_punch_out = record.get("punch_out")
    if cur_punch_out and cur_punch_out.tzinfo is None:
        cur_punch_out = cur_punch_out.replace(tzinfo=timezone.utc)
    cur_work_hours = record.get("work_hours")
    cur_late_minutes = record.get("late_minutes", 0)
    cur_overtime_minutes = record.get("overtime_minutes", 0)
    cur_half_day = record.get("half_day", False)

    new_status = body.status if body.status is not None else cur_status

    if new_status in ABSENCE_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE",
            )
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        if body.punch_in is not None:
            new_punch_in = ms_to_datetime_utc(body.punch_in)
        else:
            new_punch_in = cur_punch_in

        if new_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A presence status requires a punch_in",
            )

        calc_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"punch_in belongs to date {calc_date}, which does not match record date {date}",
            )

        if body.punch_out is not None:
            new_punch_out = ms_to_datetime_utc(body.punch_out)
        else:
            new_punch_out = cur_punch_out

        if new_punch_out is not None:
            diff_s = (new_punch_out - new_punch_in).total_seconds()
            if diff_s <= 0 or diff_s > 86400:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be after punch_in and within 24 hours",
                )
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_end"], date, emp["shift_start"])
            new_half_day = bool(new_work_hours < 4.50)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)

    # Compute changes
    changes: Dict[str, Dict[str, Any]] = {}
    if cur_status != new_status:
        changes["status"] = {"from": cur_status, "to": new_status}
    if cur_punch_in != new_punch_in:
        changes["punch_in"] = {"from": cur_punch_in, "to": new_punch_in}
    if cur_punch_out != new_punch_out:
        changes["punch_out"] = {"from": cur_punch_out, "to": new_punch_out}
    if cur_work_hours != new_work_hours:
        changes["work_hours"] = {"from": cur_work_hours, "to": new_work_hours}
    if cur_late_minutes != new_late_minutes:
        changes["late_minutes"] = {"from": cur_late_minutes, "to": new_late_minutes}
    if cur_overtime_minutes != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": cur_overtime_minutes, "to": new_overtime_minutes}
    if cur_half_day != new_half_day:
        changes["half_day"] = {"from": cur_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields were changed",
        )

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic locking on history array length to prevent lost updates under concurrency
    history_len = len(record.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": history_len},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Concurrent regularization conflict")

    return format_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    working_days = get_working_days(month, emp["joined_on"])
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to must be >= from")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range must not exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status_filter: Optional[str] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from must be <= date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status_filter:
            if status_filter not in VALID_STATUSES:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid status")
            q["status"] = status_filter

        skip = (page - 1) * page_size
        exp = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(exp)),
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="emp_code and month are required")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        exp = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(exp)),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month is required")
        pipeline = build_department_summary_pipeline(month, department)
        exp = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json.loads(json_util.dumps(exp)),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month is required")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        exp = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(exp)),
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="department, from, and to are required")
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to must be >= from")
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range must not exceed 92 days")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        exp = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(exp)),
        }

    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown endpoint")
