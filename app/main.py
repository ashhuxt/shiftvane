import os
import json
from contextlib import asynccontextmanager
from datetime import datetime, time, date, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, status, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from bson import ObjectId, json_util
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB", "shiftvane_db")

# Initialize PyMongo Client (tz_aware=True ensures datetimes are never naive)
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB_NAME]

# Collections
employees_col = db["employees"]
attendance_col = db["attendance_logs"]
audit_col = db["audit_trails"]

# ==========================================
# LIFESPAN & INDEX SETUP (Phase 4 & 7)
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Idempotent index creation for performance & race-safety (Phase 7)
    employees_col.create_index([("emp_code", ASCENDING)], unique=True)
    attendance_col.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    attendance_col.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    attendance_col.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    yield

app = FastAPI(
    title="Shiftvane Employee Attendance & Analytics API", 
    version="1.0.0",
    lifespan=lifespan
)

@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    try:
        client.admin.command('ping')
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database offline")

# ==========================================
# TIME, ROUNDING & RULE HELPERS (Phase 5)
# ==========================================
IST = timezone(timedelta(hours=5, minutes=30))

def ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)

def datetime_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def truncate_to_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)

def round_half_up(value: float, decimals: int = 2) -> float:
    d = Decimal(str(value))
    quantize_str = '0.' + '0' * decimals if decimals > 0 else '1'
    return float(d.quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP))

# ==========================================
# BUSINESS RULE HELPERS (R1 - R5)
# ==========================================

def get_attendance_date(punch_in_dt: datetime, shift_start_str: str, shift_end_str: str) -> str:
    """
    Computes the attendance date in IST (YYYY-MM-DD).
    Handles overnight shifts (where shift_end <= shift_start).
    """
    dt_ist = punch_in_dt.astimezone(IST)
    p_date = dt_ist.date()
    p_time = dt_ist.time()

    sh_start = time.fromisoformat(shift_start_str)
    sh_end = time.fromisoformat(shift_end_str)

    if sh_end <= sh_start:
        if p_time < sh_end:
            p_date = p_date - timedelta(days=1)
            
    return p_date.isoformat()

def calculate_late_minutes(punch_in_dt: datetime, attendance_date_str: str, shift_start_str: str) -> int:
    """
    R2: Grace period is 10 minutes (600 seconds). 
    If late > 10 mins, late minutes are measured from shift start.
    """
    dt_ist = punch_in_dt.astimezone(IST)
    att_date = date.fromisoformat(attendance_date_str)
    sh_start_time = time.fromisoformat(shift_start_str)
    
    shift_start_dt = datetime.combine(att_date, sh_start_time, tzinfo=IST)
    elapsed_seconds = int((dt_ist - shift_start_dt).total_seconds())
    
    if elapsed_seconds > 600:
        return elapsed_seconds // 60
    return 0

def calculate_overtime_minutes(punch_out_dt: datetime, attendance_date_str: str, shift_start_str: str, shift_end_str: str) -> int:
    """
    R3: Overtime requires at least 30 minutes (1800 seconds) past shift end.
    """
    dt_ist = punch_out_dt.astimezone(IST)
    att_date = date.fromisoformat(attendance_date_str)
    sh_end_time = time.fromisoformat(shift_end_str)
    sh_start_time = time.fromisoformat(shift_start_str)
    
    shift_end_dt = datetime.combine(att_date, sh_end_time, tzinfo=IST)
    if sh_end_time <= sh_start_time:
        shift_end_dt += timedelta(days=1)
        
    ot_seconds = int((dt_ist - shift_end_dt).total_seconds())
    if ot_seconds >= 1800:
        return ot_seconds // 60
    return 0

def calculate_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: Work hours calculated as decimal hours, rounded half-up to 2 decimals.
    """
    diff_seconds = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_seconds < 0:
        return 0.0
    hours = diff_seconds / 3600.0
    return round_half_up(hours, 2)

# ==========================================
# PYDANTIC MODELS & VALIDATION (Phase 6)
# ==========================================

class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120)
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^\d{2}:\d{2}$")
    shift_end: str = Field("18:30", pattern=r"^\d{2}:\d{2}$")
    joined_on: str # YYYY-MM-DD format

    @field_validator('joined_on')
    def validate_joined_on(cls, v):
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return v

class PunchInRequest(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    punched_at: Optional[int] = None  # Epoch milliseconds
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")

class PunchOutRequest(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    punched_at: Optional[int] = None  # Epoch milliseconds

class RegularizeRequest(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    date: str  # YYYY-MM-DD
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|WFH|ON_DUTY|HALF_DAY)$")
    reason: str = Field(..., min_length=3, max_length=200)

# ==========================================
# EMPLOYEE ENDPOINTS (Phase 8)
# ==========================================

@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(emp: EmployeeCreate):
    if emp.shift_start == emp.shift_end:
        raise HTTPException(status_code=422, detail="shift_start and shift_end cannot be identical")
    
    doc = emp.model_dump()
    doc["created_at"] = datetime_to_ms(datetime.now(timezone.utc))

    try:
        employees_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"Employee with emp_code {emp.emp_code} already exists")

    doc.pop("_id", None)
    return doc

@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100)
):
    query = {}
    if department:
        query["department"] = department

    total = employees_col.count_documents(query)
    skip = (page - 1) * page_size

    cursor = employees_col.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = list(cursor)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items
    }

# ==========================================
# PUNCH-IN ENDPOINT (Phase 9)
# ==========================================

@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(req: PunchInRequest):
    emp = employees_col.find_one({"emp_code": req.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee {req.emp_code} not found")
    
    now_dt = datetime.now(timezone.utc)
    punch_dt = ms_to_datetime(req.punched_at) if req.punched_at is not None else now_dt
    
    punch_dt = truncate_to_seconds(punch_dt)
    att_date = get_attendance_date(punch_dt, emp["shift_start"], emp["shift_end"])
    late_mins = calculate_late_minutes(punch_dt, att_date, emp["shift_start"])

    log_doc = {
        "emp_code": req.emp_code,
        "date": att_date,
        "status": req.status,
        "punch_in": datetime_to_ms(punch_dt),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        attendance_col.insert_one(log_doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"Attendance record for {req.emp_code} on {att_date} already exists")

    log_doc.pop("_id", None)
    return log_doc

# ==========================================
# PUNCH-OUT ENDPOINT (Phase 11)
# ==========================================

@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK)
def punch_out(req: PunchOutRequest):
    emp = employees_col.find_one({"emp_code": req.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee {req.emp_code} not found")

    now_dt = datetime.now(timezone.utc)
    out_dt = ms_to_datetime(req.punched_at) if req.punched_at is not None else now_dt
    
    out_dt = truncate_to_seconds(out_dt)
    out_ms = datetime_to_ms(out_dt)

    att_date = get_attendance_date(out_dt, emp["shift_start"], emp["shift_end"])
    record = attendance_col.find_one({"emp_code": req.emp_code, "date": att_date, "punch_out": None})
    
    if not record:
        record = attendance_col.find_one({"emp_code": req.emp_code, "punch_out": None}, sort=[("punch_in", DESCENDING)])
        if not record:
            raise HTTPException(status_code=404, detail=f"No active punch-in record found for employee {req.emp_code}")

    in_ms = record["punch_in"]
    in_dt = ms_to_datetime(in_ms)

    if out_dt <= in_dt:
        raise HTTPException(status_code=422, detail="punch_out time must be strictly greater than punch_in time")

    work_hrs = calculate_work_hours(in_dt, out_dt)
    ot_mins = calculate_overtime_minutes(out_dt, record["date"], emp["shift_start"], emp["shift_end"])
    half_day = work_hrs < 4.0

    update_result = attendance_col.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": out_ms,
                "work_hours": work_hrs,
                "overtime_minutes": ot_mins,
                "half_day": half_day
            }
        },
        return_document=True
    )

    if not update_result:
        raise HTTPException(status_code=409, detail="Conflict updating punch-out record")

    update_result.pop("_id", None)
    return update_result

# ==========================================
# REGULARIZATION ENDPOINT (Phase 12)
# ==========================================

@app.patch("/attendance/regularize", status_code=status.HTTP_200_OK)
def regularize_attendance(req: RegularizeRequest):
    emp = employees_col.find_one({"emp_code": req.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee {req.emp_code} not found")

    record = attendance_col.find_one({"emp_code": req.emp_code, "date": req.date})
    if not record:
        raise HTTPException(status_code=404, detail=f"Attendance record for {req.emp_code} on {req.date} not found")

    update_data = {}
    history_entry = {
        "timestamp": datetime_to_ms(datetime.now(timezone.utc)),
        "reason": req.reason,
        "old_values": {
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
            "status": record.get("status")
        }
    }

    new_in_ms = record["punch_in"]
    if req.punch_in is not None:
        in_dt = truncate_to_seconds(ms_to_datetime(req.punch_in))
        new_in_ms = datetime_to_ms(in_dt)
        update_data["punch_in"] = new_in_ms
        update_data["late_minutes"] = calculate_late_minutes(in_dt, req.date, emp["shift_start"])

    new_out_ms = record["punch_out"]
    if req.punch_out is not None:
        out_dt = truncate_to_seconds(ms_to_datetime(req.punch_out))
        new_out_ms = datetime_to_ms(out_dt)
        update_data["punch_out"] = new_out_ms
        if new_in_ms:
            work_hrs = calculate_work_hours(ms_to_datetime(new_in_ms), out_dt)
            update_data["work_hours"] = work_hrs
            update_data["overtime_minutes"] = calculate_overtime_minutes(out_dt, req.date, emp["shift_start"], emp["shift_end"])
            update_data["half_day"] = work_hrs < 4.0

    if req.status is not None:
        update_data["status"] = req.status

    updated_record = attendance_col.find_one_and_update(
        {"_id": record["_id"]},
        {
            "$set": update_data,
            "$push": {"history": history_entry}
        },
        return_document=True
    )

    updated_record.pop("_id", None)
    return {
        "message": "Attendance regularized successfully",
        "record": updated_record
    }

# ==========================================
# ATTENDANCE LIST ENDPOINT (Phase 10)
# ==========================================

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None, pattern=r"^EMP\d{4,6}$"),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100)
):
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    total = attendance_col.count_documents(query)
    skip = (page - 1) * page_size

    cursor = attendance_col.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = list(cursor)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items
    }

# ==========================================
# ATTENDANCE SUMMARY ENDPOINT (Phase 13)
# ==========================================

@app.get("/attendance/summary")
def get_attendance_summary(
    emp_code: str = Query(..., pattern=r"^EMP\d{4,6}$"),
    start_date: str = Query(..., description="YYYY-MM-DD"),
    end_date: str = Query(..., description="YYYY-MM-DD")
):
    emp = employees_col.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee {emp_code} not found")

    cursor = attendance_col.find({
        "emp_code": emp_code,
        "date": {"$gte": start_date, "$lte": end_date}
    }, {"_id": 0}).sort("date", ASCENDING)

    records = list(cursor)

    total_days = len(records)
    total_work_hours = sum(r.get("work_hours", 0) or 0 for r in records)
    total_overtime_mins = sum(r.get("overtime_minutes", 0) for r in records)
    total_late_mins = sum(r.get("late_minutes", 0) for r in records)
    total_half_days = sum(1 for r in records if r.get("half_day", False))

    return {
        "emp_code": emp_code,
        "start_date": start_date,
        "end_date": end_date,
        "records_count": total_days,
        "total_work_hours": round_half_up(total_work_hours, 2),
        "total_overtime_minutes": total_overtime_mins,
        "total_late_minutes": total_late_mins,
        "total_half_days": total_half_days,
        "attendance_logs": records
    }

# ==========================================
# ANALYTICS ENDPOINTS (Phase 13 & 14)
# ==========================================

@app.get("/analytics/employee-monthly")
def employee_monthly_analytics(
    emp_code: str = Query(..., pattern=r"^EMP\d{4,6}$"),
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$")
):
    emp = employees_col.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee {emp_code} not found")

    cursor = attendance_col.find({
        "emp_code": emp_code,
        "date": {"$regex": f"^{month}"}
    }, {"_id": 0})
    records = list(cursor)

    present_days = sum(0.5 if r.get("half_day") else 1.0 for r in records if r.get("status") in ["PRESENT", "WFH", "ON_DUTY"])
    leave_days = sum(1 for r in records if r.get("status") == "LEAVE")
    late_count = sum(1 for r in records if r.get("late_minutes", 0) > 0)
    total_late_minutes = sum(r.get("late_minutes", 0) for r in records)
    total_overtime_minutes = sum(r.get("overtime_minutes", 0) for r in records)

    return {
        "emp_code": emp_code,
        "month": month,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes
    }

@app.get("/analytics/department-summary")
def department_summary_analytics(
    department: Optional[str] = Query(None),
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$")
):
    pipeline = [
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"date": {"$regex": f"^{month}"}}}
    ]
    if department:
        pipeline.insert(0, {"$match": {"emp.department": department}})

    pipeline.append({
        "$group": {
            "_id": "$emp.department",
            "total_records": {"$sum": 1},
            "avg_work_hours": {"$avg": "$work_hours"}
        }
    })

    results = list(attendance_col.aggregate(pipeline))
    formatted = [{
        "department": r["_id"],
        "total_attendance_records": r["total_records"],
        "avg_work_hours": round_half_up(r["avg_work_hours"], 2) if r["avg_work_hours"] else 0.0
    } for r in results]

    return {"month": month, "departments": formatted}

@app.get("/analytics/leaderboard")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(10, ge=1, le=50)
):
    pipeline = [
        {"$match": {"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        {"$sort": {"total_late_minutes": DESCENDING}},
        {"$limit": limit}
    ]
    results = list(attendance_col.aggregate(pipeline))
    return {"month": month, "leaderboard": [{"emp_code": r["_id"], "total_late_minutes": r["total_late_minutes"]} for r in results]}

# ==========================================
# ADMIN EXPLAIN ENDPOINT (Phase 15)
# ==========================================

@app.get("/admin/explain/{endpoint_name}")
def explain_endpoint(
    endpoint_name: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 10
):
    """
    Admin endpoint to verify query execution plans and ensure IXSCAN index utilization.
    """
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend"
    ]
    
    if endpoint_name not in valid_endpoints:
        raise HTTPException(status_code=422, detail=f"Invalid endpoint name. Must be one of {valid_endpoints}")

    try:
        if endpoint_name == "attendance_list":
            query = {}
            if emp_code: query["emp_code"] = emp_code
            if status: query["status"] = status
            if date_from or date_to:
                query["date"] = {}
                if date_from: query["date"]["$gte"] = date_from
                if date_to: query["date"]["$lte"] = date_to
            
            explain_result = db.command(
                "explain",
                {
                    "find": "attendance_logs",
                    "filter": query,
                    "sort": {"date": -1, "emp_code": 1}
                },
                verbosity="executionStats"
            )
            collection_name = "attendance_logs"

        elif endpoint_name == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
            
            pipeline = [
                {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
                {"$group": {"_id": "$emp_code", "total": {"$sum": 1}}}
            ]
            explain_result = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
            collection_name = "attendance_logs"

        elif endpoint_name == "department_summary":
            pipeline = [
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
                {"$unwind": "$emp"}
            ]
            if department:
                pipeline.insert(0, {"$match": {"emp.department": department}})
            pipeline.append({"$group": {"_id": "$emp.department", "count": {"$sum": 1}}})
            
            explain_result = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
            collection_name = "attendance_logs"

        elif endpoint_name == "late_leaderboard":
            pipeline = [
                {"$match": {"late_minutes": {"$gt": 0}}},
                {"$group": {"_id": "$emp_code", "total_late": {"$sum": "$late_minutes"}}},
                {"$sort": {"total_late": -1}},
                {"$limit": limit}
            ]
            explain_result = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
            collection_name = "attendance_logs"

        elif endpoint_name == "department_trend":
            if not department or not date_from or not date_to:
                raise HTTPException(status_code=422, detail="department, date_from, and date_to are required for department_trend")
            
            pipeline = [
                {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
                {"$match": {"emp.department": department}},
                {"$group": {"_id": "$date", "count": {"$sum": 1}}}
            ]
            explain_result = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
            collection_name = "attendance_logs"

        safe_explain = json.loads(json_util.dumps(explain_result))
        return {
            "endpoint": endpoint_name,
            "collection": collection_name,
            "explain": safe_explain
        }

    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))