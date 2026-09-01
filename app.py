import hashlib
import hmac
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8000")
CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID", "")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY", "")
CASHFREE_ENVIRONMENT = (os.getenv("CASHFREE_ENVIRONMENT", "sandbox") or "sandbox").lower()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///workshop.db")
WORKSHOP_AMOUNT = 4999
DB_PATH = BASE_DIR / "workshop.db"

if not FRONTEND_URL.startswith("http"):
    FRONTEND_URL = f"https://{FRONTEND_URL}"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS registrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT NOT NULL,
                experience TEXT DEFAULT 'Other',
                mode TEXT DEFAULT 'Online',
                order_id TEXT UNIQUE,
                payment_session_id TEXT,
                amount INTEGER DEFAULT 4999,
                currency TEXT DEFAULT 'INR',
                payment_status TEXT DEFAULT 'PENDING',
                registration_status TEXT DEFAULT 'INITIATED',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def insert_registration(name, email, phone, experience, mode="Online"):
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO registrations (name, email, phone, experience, mode, payment_status, registration_status)
            VALUES (?, ?, ?, ?, ?, 'PENDING', 'INITIATED')
            """,
            (name, email, phone, experience, mode),
        )
        conn.commit()
        return cursor.lastrowid


def get_registration_by_email(email):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM registrations WHERE email = ? ORDER BY id DESC LIMIT 1",
            (email,),
        ).fetchone()
        return dict(row) if row else None


def get_registration_by_order_id(order_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM registrations WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        return dict(row) if row else None


def update_registration_order(order_id, registration_id, payment_session_id=None):
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE registrations
            SET order_id = ?, payment_session_id = ?, payment_status = 'PENDING', registration_status = 'INITIATED', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (order_id, payment_session_id, registration_id),
        )
        conn.commit()


def update_registration_payment(order_id, payment_status, registration_status="CONFIRMED", payment_session_id=None):
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE registrations
            SET payment_status = ?, registration_status = ?, updated_at = CURRENT_TIMESTAMP, payment_session_id = COALESCE(?, payment_session_id)
            WHERE order_id = ?
            """,
            (payment_status, registration_status, payment_session_id, order_id),
        )
        conn.commit()


def list_registrations():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM registrations ORDER BY id DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def generate_order_id():
    return f"WS-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{os.urandom(4).hex()}"


def normalize_mode(value):
    mode = (value or "Online").strip().lower()
    if mode in {"online", "virtual", "remote"}:
        return "Online"
    if mode in {"offline", "onsite", "hybrid"}:
        return "Offline"
    return "Online"


def validate_name(value):
    return bool((value or "").strip())


def validate_email(value):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", (value or "").strip()))


def validate_phone(value):
    digits = re.sub(r"\D", "", (value or "").strip())
    return len(digits) == 10


def validate_experience(value):
    return bool((value or "").strip())


def get_cashfree_base_url():
    if CASHFREE_ENVIRONMENT == "production":
        return "https://api.cashfree.com"
    return "https://sandbox.cashfree.com"


def make_cashfree_headers():
    return {
        "x-client-id": CASHFREE_APP_ID,
        "x-client-secret": CASHFREE_SECRET_KEY,
        "x-api-version": "2022-09-01",
        "Content-Type": "application/json",
    }


def create_cashfree_order(order_id, customer_name, customer_email, customer_phone, amount=WORKSHOP_AMOUNT):
    if not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY:
        raise ValueError("Cashfree credentials are not configured.")

    payload = {
        "order_id": order_id,
        "order_amount": float(amount),
        "order_currency": "INR",
        "customer_details": {
            "customer_id": customer_email,
            "customer_name": customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
        },
        "order_meta": {
            "return_url": f"{FRONTEND_URL}/payment-success?order_id={order_id}",
            "notify_url": f"{FRONTEND_URL}/api/payment/webhook",
        },
        "order_note": "Java Full-Stack with Claude workshop registration",
    }

    endpoint = f"{get_cashfree_base_url()}/pg/services/order"
    response = requests.post(endpoint, json=payload, headers=make_cashfree_headers(), timeout=30)
    response.raise_for_status()
    data = response.json()
    payment_session_id = data.get("payment_session_id") or data.get("paymentSessionId") or data.get("payment_session")
    if not payment_session_id:
        raise ValueError("Cashfree order creation response did not include payment_session_id.")
    return payment_session_id, data


def get_order_status(order_id):
    if not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY:
        raise ValueError("Cashfree credentials are not configured.")

    endpoint = f"{get_cashfree_base_url()}/pg/orders/{order_id}"
    response = requests.get(endpoint, headers=make_cashfree_headers(), timeout=30)
    response.raise_for_status()
    data = response.json()
    return data.get("order") or data


def verify_webhook_signature(raw_body, signature):
    if not CASHFREE_SECRET_KEY:
        return False
    expected = hmac.new(CASHFREE_SECRET_KEY.encode("utf-8"), raw_body.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


app = Flask(__name__)
allowed_origins = {
    FRONTEND_URL,
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:5500",
    "http://localhost:5500",
    "http://127.0.0.1:5000",
    "http://localhost:5000",
}
CORS(
    app,
    resources={r"/api/*": {"origins": sorted(allowed_origins)}},
    supports_credentials=True,
)


@app.route("/api/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "environment": CASHFREE_ENVIRONMENT,
        "database": DATABASE_URL,
    })


@app.route("/", methods=["GET"])
def root():
    return jsonify({"message": "SlokamTech workshop API is running."})


@app.route("/api/register", methods=["POST"])
def register_user():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    email = (payload.get("email") or "").strip()
    phone = (payload.get("phone") or "").strip()
    experience = (payload.get("experience") or "Other").strip()
    mode = normalize_mode(payload.get("mode"))

    if not validate_name(name):
        return jsonify({"success": False, "message": "Name is required."}), 400
    if not validate_email(email):
        return jsonify({"success": False, "message": "A valid email address is required."}), 400
    if not validate_phone(phone):
        return jsonify({"success": False, "message": "A valid 10-digit Indian phone number is required."}), 400
    if not validate_experience(experience):
        return jsonify({"success": False, "message": "Experience is required."}), 400

    existing = get_registration_by_email(email)
    if existing and existing.get("payment_status") in {"PENDING", "PAID"}:
        return jsonify({
            "success": False,
            "message": "A recent registration already exists for this email.",
            "order_id": existing.get("order_id"),
            "payment_status": existing.get("payment_status")
        }), 409

    registration_id = insert_registration(name, email, phone, experience, mode)
    return jsonify({
        "success": True,
        "message": "Registration created successfully.",
        "registration_id": registration_id,
        "amount": WORKSHOP_AMOUNT,
        "currency": "INR"
    }), 201


@app.route("/api/register", methods=["GET"])
def get_registration_status():
    email = request.args.get("email", "").strip()
    if not email:
        return jsonify({"success": False, "message": "Email is required."}), 400
    row = get_registration_by_email(email)
    if not row:
        return jsonify({"success": False, "message": "No registration found."}), 404
    return jsonify({"success": True, "registration": row})


@app.route("/api/create-order", methods=["POST"])
def create_order():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    email = (payload.get("email") or "").strip()
    phone = (payload.get("phone") or "").strip()
    experience = (payload.get("experience") or "Other").strip()
    mode = normalize_mode(payload.get("mode"))

    if not validate_name(name):
        return jsonify({"success": False, "message": "Name is required."}), 400
    if not validate_email(email):
        return jsonify({"success": False, "message": "Valid email is required."}), 400
    if not validate_phone(phone):
        return jsonify({"success": False, "message": "Valid 10-digit Indian phone number is required."}), 400
    if not validate_experience(experience):
        return jsonify({"success": False, "message": "Experience is required."}), 400

    existing = None
    for item in list_registrations():
        if item.get("email") == email and item.get("payment_status") in {"PENDING", "PAID"}:
            existing = item
            break

    if existing and existing.get("order_id"):
        return jsonify({
            "success": False,
            "message": "A recent registration already exists for this email.",
            "order_id": existing.get("order_id"),
            "payment_status": existing.get("payment_status")
        }), 409

    order_id = generate_order_id()
    try:
        payment_session_id, _ = create_cashfree_order(
            order_id=order_id,
            customer_name=name,
            customer_email=email,
            customer_phone=phone,
            amount=WORKSHOP_AMOUNT,
        )
    except Exception:
        return jsonify({"success": False, "message": "Unable to create payment order right now."}), 500

    existing_reg = get_registration_by_email(email)
    if existing_reg:
        update_registration_order(order_id, existing_reg["id"], payment_session_id)
    else:
        reg_id = insert_registration(name, email, phone, experience, mode)
        update_registration_order(order_id, reg_id, payment_session_id)

    return jsonify({
        "success": True,
        "message": "Payment session created successfully.",
        "order_id": order_id,
        "payment_session_id": payment_session_id,
        "amount": WORKSHOP_AMOUNT,
        "currency": "INR",
        "environment": CASHFREE_ENVIRONMENT,
        "customer": {"name": name, "email": email, "phone": phone, "experience": experience, "mode": mode},
    }), 201


@app.route("/api/payment/return", methods=["GET"])
def payment_return():
    order_id = request.args.get("order_id")
    if not order_id:
        return jsonify({"success": False, "message": "Missing order_id."}), 400

    result = get_registration_by_order_id(order_id)
    if not result:
        return jsonify({"success": False, "message": "Order not found."}), 404

    try:
        status_data = get_order_status(order_id)
        status = str(status_data.get("order_status") or status_data.get("status") or "PENDING").upper()
    except Exception:
        status = result.get("payment_status", "PENDING")

    if status in {"PAID", "SUCCESS", "COMPLETED"}:
        update_registration_payment(order_id, "PAID", "CONFIRMED")
        return jsonify({"success": True, "status": "PAID", "order_id": order_id, "registration_status": "CONFIRMED"})

    if status in {"FAILED", "CANCELLED"}:
        update_registration_payment(order_id, status, "INITIATED")
        return jsonify({"success": False, "status": status, "order_id": order_id}), 402

    return jsonify({"success": True, "status": "PENDING", "order_id": order_id})


@app.route("/api/payment/status/<order_id>", methods=["GET"])
def payment_status(order_id):
    row = get_registration_by_order_id(order_id)
    if not row:
        return jsonify({"success": False, "message": "Order not found."}), 404

    try:
        data = get_order_status(order_id)
        status = str(data.get("order_status") or data.get("status") or row.get("payment_status", "PENDING")).upper()
    except Exception:
        status = str(row.get("payment_status", "PENDING")).upper()

    if status in {"PAID", "SUCCESS", "COMPLETED"}:
        update_registration_payment(order_id, "PAID", "CONFIRMED")
    elif status in {"FAILED", "CANCELLED"}:
        update_registration_payment(order_id, status, "INITIATED")

    updated_row = get_registration_by_order_id(order_id)
    return jsonify({
        "order_id": order_id,
        "status": str(updated_row.get("payment_status", status)).upper(),
        "registration_status": updated_row.get("registration_status", "INITIATED")
    })


@app.route("/api/payment/webhook", methods=["POST"])
def payment_webhook():
    raw_body = request.get_data(as_text=True)
    signature = request.headers.get("x-webhook-signature") or request.headers.get("X-Webhook-Signature")

    if not signature or not verify_webhook_signature(raw_body, signature):
        return jsonify({"success": False, "message": "Invalid webhook signature."}), 401

    try:
        payload = request.get_json(silent=True) or json.loads(raw_body)
    except Exception:
        payload = {}

    order_id = payload.get("order") or payload.get("data", {}).get("order_id") or payload.get("order_id")
    if not order_id:
        return jsonify({"success": False, "message": "Missing order_id in webhook payload."}), 400

    order_status = str((payload.get("data") or {}).get("payment_status") or payload.get("payment_status") or "PENDING").upper()
    if order_status in {"PAID", "SUCCESS", "COMPLETED"}:
        update_registration_payment(order_id, "PAID", "CONFIRMED")
        return jsonify({"success": True, "message": "Payment confirmed.", "order_id": order_id})

    if order_status in {"FAILED", "CANCELLED"}:
        update_registration_payment(order_id, order_status, "INITIATED")
        return jsonify({"success": True, "message": "Payment failed/cancelled.", "order_id": order_id})

    return jsonify({"success": True, "message": "Webhook received but status is pending.", "order_id": order_id})


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
