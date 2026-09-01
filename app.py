import base64
import hashlib
import hmac
import json
import os
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, request, session
from flask_cors import CORS
from supabase import create_client

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5500").rstrip("/")
CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID", "")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY", "")
CASHFREE_ENVIRONMENT = (os.getenv("CASHFREE_ENVIRONMENT", "sandbox") or "sandbox").lower()
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "supabase-postgres")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "SlokamTech@Admin2026")
ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "slokamtech-admin-secret")
CURRENCY = "INR"

COURSES = {
    "java-fullstack-claude": {
        "name": "Claude Code for Java Full-Stack Developers",
        "amount": 4999,
    }
}
DEFAULT_COURSE_ID = "java-fullstack-claude"

if FRONTEND_URL and not FRONTEND_URL.startswith("http"):
    FRONTEND_URL = f"https://{FRONTEND_URL}"


def normalize_supabase_url(value):
    url = (value or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return url.rstrip("/")


SUPABASE_URL = normalize_supabase_url(SUPABASE_URL)


def utc_now():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def init_db():
    return True


def get_supabase_client():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase configuration is missing. Set SUPABASE_URL and SUPABASE_KEY.")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def generate_order_id():
    return f"WS-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{os.urandom(4).hex()}"


def validate_order_id(value):
    return bool(re.match(r"^[A-Za-z0-9_-]+$", value or ""))


def normalize_mode(value):
    mode = (value or "Online").strip()
    lowered = mode.lower()
    if lowered in {"online", "virtual", "remote"}:
        return "Online"
    if lowered in {"offline", "onsite"}:
        return "Offline"
    if lowered in {"hybrid", "online & offline", "online and offline"}:
        return "Online & Offline"
    return "Online"


def normalize_phone(value):
    digits = re.sub(r"\D", "", (value or "").strip())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def get_course(course_id):
    selected_id = (course_id or DEFAULT_COURSE_ID).strip()
    course = COURSES.get(selected_id)
    if not course:
        raise ValueError("Invalid course selected.")
    return selected_id, course


def masked_email(value):
    email = (value or "").strip()
    if "@" not in email:
        return "<missing>"
    local, domain = email.split("@", 1)
    return f"{local[:2]}***@{domain}"


def error_response(message, status=400, **details):
    body = {"success": False, "error": message, "message": message}
    body.update({key: value for key, value in details.items() if value is not None})
    return jsonify(body), status


def sanitize_error_message(message):
    safe = str(message or "Unknown server error.")
    secret_values = [SUPABASE_KEY, CASHFREE_SECRET_KEY, os.getenv("DATABASE_URL", "")]
    for secret in secret_values:
        if secret:
            safe = safe.replace(secret, "[REDACTED]")
    parsed_db_url = urlparse(os.getenv("DATABASE_URL", ""))
    if parsed_db_url.password:
        safe = safe.replace(parsed_db_url.password, "[REDACTED]")
    return safe


def safe_exception_message(exc):
    for attr in ("message", "details", "hint", "code"):
        value = getattr(exc, attr, None)
        if value:
            return sanitize_error_message(value)
    return sanitize_error_message(str(exc))


def supabase_error_response(action, exc):
    message = safe_exception_message(exc)
    app.logger.error("SUPABASE ERROR:\n%s", message)
    return error_response(message, 502)


def get_json_payload():
    if not request.is_json:
        return None, error_response("Request body must be JSON.", 400)
    payload = request.get_json(silent=True)
    if payload is None:
        return None, error_response("Invalid JSON request body.", 400)
    if not isinstance(payload, dict):
        return None, error_response("JSON request body must be an object.", 400)
    return payload, None


def validate_name(value):
    return bool((value or "").strip())


def validate_email(value):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", (value or "").strip()))


def validate_phone(value):
    return len(normalize_phone(value)) == 10


def validate_experience(value):
    return bool((value or "").strip())


def generate_cashfree_customer_id(registration_id):
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", str(registration_id or "")).strip("_-")
    if not safe_id:
        safe_id = hashlib.sha256(os.urandom(16)).hexdigest()[:12]
    return f"cust_{safe_id}"


def normalize_registration_payload(payload):
    full_name = (payload.get("full_name") or payload.get("name") or "").strip()
    email = (payload.get("email") or "").strip()
    phone = normalize_phone(payload.get("phone"))
    experience_level = (payload.get("experience_level") or payload.get("experience") or "Other").strip()
    mode = normalize_mode(payload.get("mode"))
    course_id, course = get_course(payload.get("course_id") or payload.get("course"))

    if not validate_name(full_name):
        raise ValueError("Missing required field: full_name")
    if not validate_email(email):
        raise ValueError("Missing required field: email")
    if not validate_phone(phone):
        raise ValueError("Missing required field: phone")
    if not validate_experience(experience_level):
        raise ValueError("Missing required field: experience_level")

    return {
        "course_id": course_id,
        "course_name": course["name"],
        "full_name": full_name,
        "email": email,
        "phone": phone,
        "experience_level": experience_level,
        "mode": mode,
        "amount": course["amount"],
    }


def insert_registration(registration, order_id):
    client = get_supabase_client()
    now = utc_now()
    payload = {
        "course_id": registration["course_id"],
        "course_name": registration["course_name"],
        "full_name": registration["full_name"],
        "email": registration["email"],
        "phone": registration["phone"],
        "experience_level": registration["experience_level"],
        "mode": registration["mode"],
        "amount": registration["amount"],
        "cashfree_order_id": order_id,
        "cashfree_payment_id": None,
        "payment_session_id": None,
        "payment_status": "PENDING",
        "registration_status": "PAYMENT_PENDING",
        "paid_at": None,
        "created_at": now,
        "updated_at": now,
    }
    response = client.table("registrations").insert(payload).execute()
    rows = response.data or []
    if not rows:
        raise RuntimeError("Unable to create registration record in Supabase.")
    return rows[0]


def get_registration_by_email(email):
    client = get_supabase_client()
    response = (
        client.table("registrations")
        .select("*")
        .eq("email", email)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    return rows[0] if rows else None


def get_all_registrations():
    client = get_supabase_client()
    response = client.table("registrations").select("*").order("created_at", desc=True).execute()
    return response.data or []


def get_registration_by_order_id(order_id):
    client = get_supabase_client()
    response = (
        client.table("registrations")
        .select("*")
        .eq("cashfree_order_id", order_id)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    return rows[0] if rows else None


def update_registration(registration_id, values):
    client = get_supabase_client()
    payload = dict(values)
    payload["updated_at"] = utc_now()
    response = client.table("registrations").update(payload).eq("id", registration_id).execute()
    return response.data or []


def update_registration_order(order_id, registration_id, payment_session_id=None):
    payload = {
        "payment_session_id": payment_session_id,
        "payment_status": "PENDING",
        "registration_status": "PAYMENT_PENDING",
    }
    return update_registration(registration_id, payload)


def update_registration_payment(order_id, payment_status, registration_status, payment_session_id=None, payment_id=None):
    row = get_registration_by_order_id(order_id)
    if not row:
        return []
    payload = {
        "payment_status": payment_status,
        "registration_status": registration_status,
    }
    if payment_session_id:
        payload["payment_session_id"] = payment_session_id
    if payment_id:
        payload["cashfree_payment_id"] = payment_id
    if payment_status == "PAID":
        payload["paid_at"] = utc_now()
    return update_registration(row["id"], payload)


def get_cashfree_base_url():
    if CASHFREE_ENVIRONMENT == "production":
        return "https://api.cashfree.com/pg"
    return "https://sandbox.cashfree.com/pg"


def make_cashfree_headers():
    return {
        "x-client-id": CASHFREE_APP_ID,
        "x-client-secret": CASHFREE_SECRET_KEY,
        "x-api-version": "2022-09-01",
        "Content-Type": "application/json",
    }


def create_cashfree_order(order_id, registration):
    if not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY:
        raise ValueError("Cashfree credentials are not configured.")

    customer_id = generate_cashfree_customer_id(registration.get("id"))
    payload = {
        "order_id": order_id,
        "order_amount": float(registration["amount"]),
        "order_currency": CURRENCY,
        "customer_details": {
            "customer_id": customer_id,
            "customer_name": registration["full_name"],
            "customer_email": registration["email"],
            "customer_phone": registration["phone"],
        },
        "order_meta": {
            "return_url": f"{FRONTEND_URL}/payment-success.html?order_id={order_id}",
        },
        "order_note": f"{registration['course_name']} registration",
    }

    notify_url = os.getenv("CASHFREE_NOTIFY_URL", "").strip()
    if notify_url:
        payload["order_meta"]["notify_url"] = notify_url

    endpoint = f"{get_cashfree_base_url()}/orders"
    app.logger.info("Creating Cashfree order order_id=%s amount=%s", order_id, payload["order_amount"])
    response = requests.post(endpoint, json=payload, headers=make_cashfree_headers(), timeout=30)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        try:
            error_data = response.json()
        except ValueError:
            error_data = {"message": response.text[:300]}
        app.logger.error("Cashfree order creation failed status=%s response=%s", response.status_code, error_data)
        exc.cashfree_status_code = response.status_code
        exc.cashfree_error = error_data
        raise

    data = response.json()
    payment_session_id = data.get("payment_session_id") or data.get("paymentSessionId") or data.get("payment_session")
    if not payment_session_id:
        raise ValueError("Cashfree order creation response did not include payment_session_id.")
    return payment_session_id, data


def get_order_status(order_id):
    if not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY:
        raise ValueError("Cashfree credentials are not configured.")
    response = requests.get(f"{get_cashfree_base_url()}/orders/{order_id}", headers=make_cashfree_headers(), timeout=30)
    response.raise_for_status()
    return response.json()


def get_order_payments(order_id):
    if not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY:
        raise ValueError("Cashfree credentials are not configured.")
    response = requests.get(f"{get_cashfree_base_url()}/orders/{order_id}/payments", headers=make_cashfree_headers(), timeout=30)
    response.raise_for_status()
    return response.json()


def first_payment(payments):
    if isinstance(payments, list) and payments:
        successful = [item for item in payments if str(item.get("payment_status", "")).upper() in {"SUCCESS", "PAID"}]
        return successful[0] if successful else payments[0]
    return {}


def map_cashfree_status(order_data, payment_data):
    order_status = str(order_data.get("order_status") or order_data.get("status") or "").upper()
    payment_status = str(payment_data.get("payment_status") or "").upper()
    status = payment_status or order_status
    if status in {"PAID", "SUCCESS"}:
        return "PAID", "CONFIRMED"
    if status in {"FAILED", "CANCELLED"}:
        return "FAILED", "PAYMENT_FAILED"
    if status == "USER_DROPPED":
        return "USER_DROPPED", "PAYMENT_FAILED"
    if status in {"PENDING", "ACTIVE"}:
        return "PENDING", "PAYMENT_PENDING"
    return "UNKNOWN", "PAYMENT_PENDING"


def get_payment_id(payment_data):
    return (
        payment_data.get("cf_payment_id")
        or payment_data.get("bank_reference")
        or payment_data.get("auth_id")
    )


def verify_webhook_signature(raw_body, signature, timestamp):
    if not CASHFREE_SECRET_KEY or not signature or not timestamp:
        return False
    signed_payload = f"{timestamp}{raw_body}"
    expected = base64.b64encode(
        hmac.new(CASHFREE_SECRET_KEY.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def build_admin_summary(rows):
    total_members = len(rows)
    paid = sum(1 for item in rows if str(item.get("payment_status") or "").upper() == "PAID")
    pending = sum(1 for item in rows if str(item.get("payment_status") or "").upper() != "PAID")
    total_revenue = sum(float(item.get("amount") or 0) for item in rows if str(item.get("payment_status") or "").upper() == "PAID")
    by_course = {}
    by_mode = {}
    by_experience = {}

    for item in rows:
        course_name = item.get("course_name") or item.get("course_id") or "Unknown"
        mode = item.get("mode") or "Unknown"
        experience = item.get("experience_level") or "Unknown"
        by_course[course_name] = by_course.get(course_name, 0) + 1
        by_mode[mode] = by_mode.get(mode, 0) + 1
        by_experience[experience] = by_experience.get(experience, 0) + 1

    return {
        "total_members": total_members,
        "paid_members": paid,
        "pending_members": pending,
        "total_revenue": round(total_revenue, 2),
        "by_course": dict(sorted(by_course.items())),
        "by_mode": dict(sorted(by_mode.items())),
        "by_experience": dict(sorted(by_experience.items())),
    }


def build_receipt_pdf(registration):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=48, leftMargin=48, topMargin=48, bottomMargin=48)
    styles = getSampleStyleSheet()
    story = [
        Paragraph("SLOKAMTECH", styles["Title"]),
        Paragraph("Payment Receipt", styles["Heading2"]),
        Spacer(1, 18),
    ]
    course_name = registration.get("course_name") or registration.get("course") or COURSES[DEFAULT_COURSE_ID]["name"]
    rows = [
        ["Registration Details", ""],
        ["Name", registration.get("full_name") or registration.get("name", "")],
        ["Email", registration.get("email", "")],
        ["Phone", registration.get("phone", "")],
        ["Course", course_name],
        ["Mode", registration.get("mode", "")],
        ["Experience Level", registration.get("experience_level", "")],
        ["", ""],
        ["Payment Details", ""],
        ["Amount", f"{CURRENCY} {registration.get('amount', '')}"],
        ["Currency", CURRENCY],
        ["Cashfree Order ID", registration.get("cashfree_order_id") or registration.get("order_id", "")],
        ["Payment ID", registration.get("cashfree_payment_id", "")],
        ["Payment Status", registration.get("payment_status", "")],
        ["Payment Date", registration.get("paid_at") or registration.get("updated_at") or ""],
        ["Status", f"{registration.get('payment_status', '')} / {registration.get('registration_status', '')}"],
    ]
    table = Table(rows, colWidths=[150, 330])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0a1220")),
        ("BACKGROUND", (0, 8), (-1, 8), colors.HexColor("#0a1220")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("TEXTCOLOR", (0, 8), (-1, 8), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 8), (-1, 8), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d8dee9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    story.extend([table, Spacer(1, 20), Paragraph("Thank you for registering.", styles["Normal"])])
    doc.build(story)
    buffer.seek(0)
    return buffer.read()


app = Flask(__name__)
app.secret_key = ADMIN_SESSION_SECRET
allowed_origins = {
    FRONTEND_URL,
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:5500",
    "http://localhost:5500",
    "http://127.0.0.1:5000",
    "http://localhost:5000",
}
CORS(app, resources={r"/api/*": {"origins": sorted(allowed_origins)}}, supports_credentials=True)


@app.route("/admin-login", methods=["GET"])
def admin_login_page():
    html = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="UTF-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1.0" />
      <title>Admin Login</title>
      <style>
        body { font-family: Arial, sans-serif; background: #08101c; color: #edf3ff; margin: 0; display: grid; place-items: center; min-height: 100vh; }
        .card { width: min(92vw, 420px); background: rgba(15, 23, 42, 0.9); border: 1px solid rgba(96, 165, 250, 0.18); border-radius: 16px; box-shadow: 0 20px 50px rgba(0,0,0,0.25); padding: 2rem; }
        h1 { margin-bottom: 1.25rem; font-size: 1.8rem; }
        label { display: block; margin-bottom: 0.5rem; color: #cfe3ff; }
        input { width: 100%; padding: 0.8rem 0.9rem; border-radius: 10px; border: 1px solid rgba(125,211,252,0.25); background: rgba(8, 12, 20, 0.95); color: #edf3ff; margin-bottom: 1rem; }
        button { width: 100%; padding: 0.9rem; border: none; background: linear-gradient(135deg,#f5c542,#d4a017); color: #0a1220; font-weight: 800; border-radius: 12px; cursor: pointer; }
        .message { margin-top: 1rem; min-height: 1.2rem; color: #ffd9d9; }
      </style>
    </head>
    <body>
      <div class="card">
        <h1>Admin Login</h1>
        <form id="adminLoginForm">
          <label for="username">Username</label>
          <input id="username" name="username" type="text" required />
          <label for="password">Password</label>
          <input id="password" name="password" type="password" required />
          <button type="submit">Login</button>
        </form>
        <div id="message" class="message"></div>
      </div>
      <script>
        const form = document.getElementById('adminLoginForm');
        const message = document.getElementById('message');
        form.addEventListener('submit', async (event) => {
          event.preventDefault();
          message.textContent = 'Signing in...';
          const payload = {
            username: document.getElementById('username').value,
            password: document.getElementById('password').value,
          };
          const response = await fetch('/api/admin/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            credentials: 'same-origin'
          });
          const data = await response.json();
          if (!response.ok) {
            message.textContent = data.message || 'Invalid admin credentials.';
            return;
          }
          window.location.href = '/admin';
        });
      </script>
    </body>
    </html>
    """
    return Response(html, mimetype="text/html")


@app.route("/admin", methods=["GET"])
def admin_dashboard_page():
    if not session.get("admin_authenticated"):
        return redirect("/admin-login")
    html = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="UTF-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1.0" />
      <title>Admin Dashboard</title>
      <style>
        body { font-family: Arial, sans-serif; background: #08101c; color: #edf3ff; margin: 0; padding: 2rem; }
        .header { display: flex; justify-content: space-between; align-items: center; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.5rem; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit,minmax(160px,1fr)); gap: 1rem; margin-bottom: 1.5rem; }
        .stat { background: rgba(15, 23, 42, 0.9); border: 1px solid rgba(96,165,250,0.18); border-radius: 12px; padding: 1rem; }
        .stat-label { color: #bfd0eb; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em; }
        .stat-value { font-size: 1.8rem; font-weight: 800; margin-top: 0.4rem; }
        table { width: 100%; border-collapse: collapse; background: rgba(15, 23, 42, 0.9); border: 1px solid rgba(96,165,250,0.18); border-radius: 12px; overflow: hidden; }
        th, td { padding: 0.85rem 0.8rem; border-bottom: 1px solid rgba(148,163,184,0.18); text-align: left; vertical-align: top; }
        th { background: rgba(30,41,59,0.9); }
        .badge { display: inline-block; padding: 0.3rem 0.6rem; border-radius: 999px; font-size: 0.72rem; font-weight: 700; }
        .paid { background: rgba(52,211,153,0.15); color: #96f7c4; }
        .pending { background: rgba(245,197,66,0.15); color: #f9e39e; }
        button { padding: 0.7rem 1rem; border: none; border-radius: 10px; background: rgba(255,255,255,0.08); color: #edf3ff; cursor: pointer; }
        @media (max-width: 600px) { body { padding: 1rem; } table { display: block; overflow-x: auto; } }
      </style>
    </head>
    <body>
      <div class="header">
        <div>
          <h1>Workshop Admin Dashboard</h1>
        </div>
        <button id="logoutBtn">Logout</button>
      </div>
      <div id="stats" class="stats"></div>
      <div style="overflow:auto; border-radius:12px;">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Email</th>
              <th>Phone</th>
              <th>Course</th>
              <th>Mode</th>
              <th>Experience</th>
              <th>Payment Status</th>
              <th>Registration Status</th>
              <th>Amount</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody id="rows"></tbody>
        </table>
      </div>
      <script>
        async function loadDashboard() {
          const response = await fetch('/api/admin/dashboard', { credentials: 'same-origin' });
          const data = await response.json();
          if (!response.ok) {
            if (response.status === 401) {
              window.location.href = '/admin-login';
              return;
            }
            document.getElementById('stats').innerHTML = '<div class="stat"><div class="stat-label">Error</div><div class="stat-value">' + (data.message || 'Unable to load dashboard') + '</div></div>';
            return;
          }
          const stats = data.stats || {};
          const rows = data.data || [];
          document.getElementById('stats').innerHTML = `
            <div class="stat"><div class="stat-label">Total Members</div><div class="stat-value">${stats.total_members || 0}</div></div>
            <div class="stat"><div class="stat-label">Paid Members</div><div class="stat-value">${stats.paid_members || 0}</div></div>
            <div class="stat"><div class="stat-label">Pending Members</div><div class="stat-value">${stats.pending_members || 0}</div></div>
            <div class="stat"><div class="stat-label">Revenue</div><div class="stat-value">₹${Number(stats.total_revenue || 0).toLocaleString('en-IN')}</div></div>
          `;
          const tbody = document.getElementById('rows');
          if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="10">No registrations found.</td></tr>';
            return;
          }
          tbody.innerHTML = rows.map((row) => {
            const payment = (row.payment_status || 'PENDING').toUpperCase();
            const registrationStatus = (row.registration_status || 'PAYMENT_PENDING').toUpperCase();
            return `
              <tr>
                <td>${row.full_name || ''}</td>
                <td>${row.email || ''}</td>
                <td>${row.phone || ''}</td>
                <td>${row.course_name || row.course_id || ''}</td>
                <td>${row.mode || ''}</td>
                <td>${row.experience_level || ''}</td>
                <td><span class="badge ${payment === 'PAID' ? 'paid' : 'pending'}">${payment}</span></td>
                <td><span class="badge ${registrationStatus === 'CONFIRMED' ? 'paid' : 'pending'}">${registrationStatus}</span></td>
                <td>₹${Number(row.amount || 0).toLocaleString('en-IN')}</td>
                <td>${(row.created_at || '').replace('T', ' ').replace('Z', '')}</td>
              </tr>
            `;
          }).join('');
        }
        document.getElementById('logoutBtn').addEventListener('click', async () => {
          await fetch('/api/admin/logout', { method: 'POST', credentials: 'same-origin' });
          window.location.href = '/admin-login';
        });
        loadDashboard();
      </script>
    </body>
    </html>
    """
    return Response(html, mimetype="text/html")


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
    elif FRONTEND_URL:
        response.headers["Access-Control-Allow-Origin"] = FRONTEND_URL
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
    response.headers["Vary"] = "Origin"
    return response


@app.route("/", methods=["GET"])
def root():
    return jsonify({"message": "SlokamTech workshop API is running."})


@app.route("/api/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "ok",
        "timestamp": utc_now(),
        "environment": CASHFREE_ENVIRONMENT,
        "database": DATABASE_URL,
        "courses": list(COURSES.keys()),
    })


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    payload, json_error = get_json_payload()
    if json_error:
        return json_error

    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        return error_response("Invalid admin credentials.", 401)

    session["admin_authenticated"] = True
    session["admin_username"] = username
    return jsonify({"success": True, "message": "Login successful.", "username": username})


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin_authenticated", None)
    session.pop("admin_username", None)
    return jsonify({"success": True, "message": "Logged out."})


@app.route("/api/admin/dashboard", methods=["GET"])
def admin_dashboard():
    if not session.get("admin_authenticated"):
        return error_response("Unauthorized.", 401)

    try:
        rows = get_all_registrations()
        summary = build_admin_summary(rows)
    except Exception as exc:
        app.logger.exception("Admin dashboard failed")
        return supabase_error_response("load admin dashboard", exc)

    return jsonify({"success": True, "stats": summary, "data": rows})


@app.route("/api/create-order", methods=["OPTIONS"])
def create_order_options():
    return jsonify({"success": True}), 200


@app.route("/api/create-order", methods=["POST"])
def create_order():
    payload, json_error = get_json_payload()
    if json_error:
        return json_error

    try:
        registration_input = normalize_registration_payload(payload)
    except ValueError as exc:
        app.logger.warning("create_order validation failed reason=%s", str(exc))
        return error_response(str(exc), 400)

    if not SUPABASE_URL or not SUPABASE_KEY:
        return error_response("Supabase is not configured on the server.", 503)
    if not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY:
        return error_response("Cashfree is not configured on the server.", 503)

    try:
        existing = get_registration_by_email(registration_input["email"])
        if existing and existing.get("payment_status") == "PAID":
            order_id = existing.get("cashfree_order_id")
            return error_response("Registration is already confirmed for this email.", 409, order_id=order_id)
        order_id = generate_order_id()
        registration_row = insert_registration(registration_input, order_id)
    except Exception as exc:
        app.logger.exception("Supabase registration insert failed email=%s", masked_email(registration_input["email"]))
        return supabase_error_response("save registration", exc)

    registration_for_cashfree = dict(registration_input)
    registration_for_cashfree["id"] = registration_row.get("id")
    try:
        payment_session_id, cashfree_data = create_cashfree_order(order_id, registration_for_cashfree)
    except requests.HTTPError as exc:
        details = getattr(exc, "cashfree_error", {}) or {}
        cashfree_message = details.get("message") or details.get("error_description") or details.get("error") or "Cashfree rejected the order request."
        return error_response(f"Cashfree order creation failed: {cashfree_message}", 502, cashfree_status_code=getattr(exc, "cashfree_status_code", None))
    except Exception as exc:
        app.logger.exception("Cashfree order creation failed order_id=%s", order_id)
        return error_response("Unable to create payment order right now.", 502)

    try:
        update_registration_order(order_id, registration_row["id"], payment_session_id)
    except Exception as exc:
        app.logger.exception("Supabase registration update failed order_id=%s", order_id)
        return supabase_error_response("update registration", exc)

    return jsonify({
        "success": True,
        "message": "Payment session created successfully.",
        "order_id": order_id,
        "cashfree_order_id": order_id,
        "status": "PENDING",
        "payment_session_id": payment_session_id,
        "order_token": payment_session_id,
        "amount": registration_input["amount"],
        "currency": CURRENCY,
        "course_id": registration_input["course_id"],
        "course_name": registration_input["course_name"],
        "cashfree": {
            "order_status": cashfree_data.get("order_status"),
            "payment_session_id": payment_session_id,
        },
    }), 201


@app.route("/api/verify-payment", methods=["GET"])
def verify_payment():
    order_id = (request.args.get("order_id") or "").strip()
    if not order_id:
        return error_response("Missing required query parameter: order_id", 400)
    if not validate_order_id(order_id):
        return error_response("Invalid order_id.", 400)
    return verify_payment_order(order_id)


def verify_payment_order(order_id):
    try:
        registration = get_registration_by_order_id(order_id)
        if not registration:
            return error_response("Registration not found for this order.", 404)
        order_data = get_order_status(order_id)
        payments = get_order_payments(order_id)
        payment_data = first_payment(payments)
        payment_status, registration_status = map_cashfree_status(order_data, payment_data)
        payment_id = get_payment_id(payment_data)
        updated_rows = update_registration_payment(
            order_id,
            payment_status,
            registration_status,
            payment_session_id=registration.get("payment_session_id"),
            payment_id=payment_id,
        )
        registration = updated_rows[0] if updated_rows else get_registration_by_order_id(order_id)
    except requests.HTTPError as exc:
        app.logger.exception("Cashfree payment verification failed order_id=%s", order_id)
        return error_response("Unable to verify payment with Cashfree right now.", 502)
    except Exception as exc:
        app.logger.exception("Payment verification failed order_id=%s", order_id)
        return supabase_error_response("verify payment", exc)

    return jsonify({
        "success": payment_status == "PAID",
        "order_id": order_id,
        "cashfree_order_id": order_id,
        "payment_id": payment_id,
        "payment_status": payment_status,
        "registration_status": registration_status,
        "registration": registration,
    })


@app.route("/api/payment/status/<order_id>", methods=["GET"])
def payment_status(order_id):
    if not validate_order_id(order_id):
        return error_response("Invalid order_id.", 400)
    return verify_payment_order(order_id)


@app.route("/api/payment/return", methods=["GET"])
def payment_return():
    return verify_payment()


@app.route("/api/receipt/<order_id>", methods=["GET"])
def download_receipt(order_id):
    if not validate_order_id(order_id):
        return error_response("Invalid order_id.", 400)
    try:
        registration = get_registration_by_order_id(order_id)
        if not registration:
            return error_response("Registration not found for this order.", 404)
        if str(registration.get("payment_status", "")).upper() != "PAID":
            return error_response("Receipt is available only after payment is confirmed.", 403)
        pdf_bytes = build_receipt_pdf(registration)
    except Exception as exc:
        app.logger.exception("Receipt generation failed order_id=%s", order_id)
        return error_response("Unable to generate receipt right now.", 502)

    filename = f"slokamtech-receipt-{order_id}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/api/cashfree/webhook", methods=["POST"])
@app.route("/api/payment/webhook", methods=["POST"])
def payment_webhook():
    raw_body = request.get_data(as_text=True)
    signature = request.headers.get("x-webhook-signature") or request.headers.get("X-Webhook-Signature")
    timestamp = request.headers.get("x-webhook-timestamp") or request.headers.get("X-Webhook-Timestamp")

    if not verify_webhook_signature(raw_body, signature, timestamp):
        return error_response("Invalid webhook signature.", 401)

    try:
        payload = json.loads(raw_body)
    except ValueError:
        return error_response("Invalid webhook JSON payload.", 400)

    data = payload.get("data") or {}
    order_data = data.get("order") or data
    payment_data = data.get("payment") or data
    order_id = order_data.get("order_id") or data.get("order_id") or payload.get("order_id")
    if not order_id:
        return error_response("Missing order_id in webhook payload.", 400)

    payment_status, registration_status = map_cashfree_status(order_data, payment_data)
    payment_id = get_payment_id(payment_data)
    try:
        update_registration_payment(order_id, payment_status, registration_status, payment_id=payment_id)
    except Exception as exc:
        app.logger.exception("Webhook registration update failed order_id=%s", order_id)
        return supabase_error_response("update registration from webhook", exc)

    return jsonify({"success": True, "order_id": order_id, "payment_status": payment_status, "registration_status": registration_status})


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
