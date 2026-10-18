from __future__ import annotations

import os
from decimal import Decimal
from typing import Any
from uuid import uuid4

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://policyproof_target:target-dev-only@localhost:5432/policyproof",
)
APP_MODE = os.environ.get("APP_MODE", "vulnerable").lower()

app = FastAPI(title="Policy-to-Proof Payment Lab", version="0.1.0")


class PaymentCreate(BaseModel):
    requester: str = "alice"
    payee: str = Field(min_length=1, max_length=120)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(default="USD", min_length=3, max_length=3)


class ApprovalRequest(BaseModel):
    approver: str = "bob"


class PaymentEdit(BaseModel):
    payee: str | None = Field(default=None, min_length=1, max_length=120)
    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class DemoMode(BaseModel):
    mode: str = Field(pattern="^(vulnerable|fixed)$")


def db() -> psycopg.Connection[Any]:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


@app.on_event("startup")
def initialize_database() -> None:
    with db() as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS lab AUTHORIZATION policyproof_target")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS lab.payments (
                id TEXT PRIMARY KEY,
                requester TEXT NOT NULL,
                payee TEXT NOT NULL,
                amount NUMERIC(12,2) NOT NULL,
                currency CHAR(3) NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                approved_by TEXT,
                approved_payee TEXT,
                approved_amount NUMERIC(12,2),
                approved_currency CHAR(3),
                approved_version INTEGER,
                executed_at TIMESTAMPTZ
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS lab.ledger (
                id BIGSERIAL PRIMARY KEY,
                payment_id TEXT NOT NULL REFERENCES lab.payments(id),
                payee TEXT NOT NULL,
                amount NUMERIC(12,2) NOT NULL,
                currency CHAR(3) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )"""
        )
        conn.execute("GRANT USAGE ON SCHEMA lab TO policyproof_oracle")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA lab TO policyproof_oracle")
        conn.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA lab "
            "GRANT SELECT ON TABLES TO policyproof_oracle"
        )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": APP_MODE}


@app.post("/demo/mode")
def set_demo_mode(body: DemoMode) -> dict[str, str]:
    # This control endpoint is only reachable on the isolated Docker lab network.
    global APP_MODE
    APP_MODE = body.mode
    return {"mode": APP_MODE}


@app.post("/demo/reset")
def reset_demo_state() -> dict[str, str]:
    with db() as conn:
        conn.execute("TRUNCATE lab.ledger, lab.payments RESTART IDENTITY CASCADE")
    return {"status": "reset", "mode": APP_MODE}


@app.post("/payments")
def create_payment(body: PaymentCreate) -> dict[str, Any]:
    payment_id = str(uuid4())
    with db() as conn:
        row = conn.execute(
            """INSERT INTO lab.payments
               (id, requester, payee, amount, currency, status, version)
               VALUES (%s, %s, %s, %s, %s, 'pending', 1) RETURNING *""",
            (payment_id, body.requester, body.payee, body.amount, body.currency.upper()),
        ).fetchone()
    return dict(row)


@app.post("/payments/{payment_id}/approve")
def approve_payment(payment_id: str, body: ApprovalRequest) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute(
            """UPDATE lab.payments SET status='approved', approved_by=%s,
               approved_payee=payee, approved_amount=amount,
               approved_currency=currency, approved_version=version
               WHERE id=%s AND status='pending' RETURNING *""",
            (body.approver, payment_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=409, detail="Payment is not pending or does not exist")
    return dict(row)


@app.patch("/payments/{payment_id}")
def edit_payment(payment_id: str, body: PaymentEdit) -> dict[str, Any]:
    values = body.model_dump(exclude_unset=True, exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="Provide at least one field to change")
    if "currency" in values:
        values["currency"] = values["currency"].upper()
    with db() as conn:
        current = conn.execute(
            "SELECT * FROM lab.payments WHERE id=%s", (payment_id,)
        ).fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        if current["status"] == "executed":
            raise HTTPException(status_code=409, detail="Executed payment cannot be edited")
        assignments = ", ".join(f"{key}=%s" for key in values)
        params = list(values.values())
        if APP_MODE == "fixed" and current["status"] == "approved":
            assignments += ", status='pending', approved_by=NULL, approved_payee=NULL, " \
                "approved_amount=NULL, approved_currency=NULL, approved_version=NULL"
        params.extend([payment_id])
        row = conn.execute(
            f"UPDATE lab.payments SET {assignments}, version=version+1 "
            "WHERE id=%s RETURNING *",
            params,
        ).fetchone()
    return dict(row)


@app.post("/payments/{payment_id}/execute")
def execute_payment(payment_id: str) -> dict[str, Any]:
    with db() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT * FROM lab.payments WHERE id=%s FOR UPDATE", (payment_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Payment not found")
            if row["status"] != "approved":
                raise HTTPException(status_code=409, detail="Payment requires current approval")
            if APP_MODE == "fixed" and (
                row["approved_version"] != row["version"]
                or row["approved_payee"] != row["payee"]
                or row["approved_amount"] != row["amount"]
                or row["approved_currency"].strip() != row["currency"].strip()
            ):
                raise HTTPException(status_code=409, detail="Approval no longer matches payment terms")
            ledger = conn.execute(
                """INSERT INTO lab.ledger (payment_id, payee, amount, currency)
                   VALUES (%s, %s, %s, %s) RETURNING *""",
                (payment_id, row["payee"], row["amount"], row["currency"]),
            ).fetchone()
            conn.execute(
                "UPDATE lab.payments SET status='executed', executed_at=NOW() WHERE id=%s",
                (payment_id,),
            )
    return {"payment": dict(row), "ledger_entry": dict(ledger)}


@app.get("/demo/state")
def demo_state() -> dict[str, list[dict[str, Any]]]:
    with db() as conn:
        payments = conn.execute(
            "SELECT * FROM lab.payments ORDER BY id"
        ).fetchall()
        ledger = conn.execute(
            "SELECT * FROM lab.ledger ORDER BY id"
        ).fetchall()
    return {"payments": [dict(row) for row in payments], "ledger": [dict(row) for row in ledger]}
