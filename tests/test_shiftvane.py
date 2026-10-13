import os
import pytest
from fastapi.testclient import TestClient
from datetime import datetime, timezone, timedelta
from app.main import app, db

client = TestClient(app)

@pytest.fixture(autouse=True)
def clean_db():
    # Clean collections before each test to ensure test isolation
    db["employees"].delete_many({})
    db["attendance_logs"].delete_many({})
    yield

def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

def test_create_employee_success():
    payload = {
        "emp_code": "EMP0002",
        "name": "Priya Singh",
        "email": "priya@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-02-01"
    }
    response = client.post("/employees", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["emp_code"] == "EMP0002"
    assert "created_at" in data

def test_duplicate_employee_conflict():
    payload = {
        "emp_code": "EMP0003",
        "name": "Amit Kumar",
        "email": "amit@example.com",
        "department": "HR",
        "shift_start": "10:00",
        "shift_end": "19:00",
        "joined_on": "2026-03-01"
    }
    # First creation should succeed
    res1 = client.post("/employees", json=payload)
    assert res1.status_code == 201

    # Second creation with same emp_code should trigger 409 Conflict
    res2 = client.post("/employees", json=payload)
    assert res2.status_code == 409
    assert "already exists" in res2.json()["detail"]

def test_punch_in_and_out_workflow():
    # 1. Create employee
    emp_payload = {
        "emp_code": "EMP0004",
        "name": "Neha Verma",
        "email": "neha@example.com",
        "department": "Finance",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-15"
    }
    client.post("/employees", json=emp_payload)

    # 2. Punch In
    punch_in_res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0004",
        "status": "PRESENT"
    })
    assert punch_in_res.status_code == 201
    log_data = punch_in_res.json()
    assert log_data["emp_code"] == "EMP0004"
    assert log_data["punch_out"] is None

    # 3. Try duplicate punch-in on same day (should be 409 Conflict)
    duplicate_punch = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0004",
        "status": "PRESENT"
    })
    assert duplicate_punch.status_code == 409

    # 4. Punch Out (simulate 9 hours later via epoch milliseconds)
    in_ms = log_data["punch_in"]
    out_ms = in_ms + (9 * 60 * 60 * 1000)

    punch_out_res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0004",
        "punched_at": out_ms
    })
    assert punch_out_res.status_code == 200
    out_data = punch_out_res.json()
    assert out_data["punch_out"] is not None
    assert "work_hours" in out_data
    assert out_data["work_hours"] == 9.0

def test_late_grace_period_boundaries():
    emp_payload = {
        "emp_code": "EMP0005",
        "name": "Grace Test",
        "email": "grace@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }
    client.post("/employees", json=emp_payload)

    # 09:30 IST = 04:00 UTC. 09:40 IST = 04:10 UTC (Exactly 10 mins / 600s grace limit)
    dt_10m = datetime(2026, 7, 7, 4, 10, 0, tzinfo=timezone.utc)
    res_10 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0005",
        "status": "PRESENT",
        "punched_at": int(dt_10m.timestamp() * 1000)
    })
    assert res_10.status_code == 201
    assert res_10.json()["late_minutes"] == 0

def test_explain_endpoint_route():
    res = client.get("/admin/explain/attendance_list")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "attendance_list"
    assert "explain" in data