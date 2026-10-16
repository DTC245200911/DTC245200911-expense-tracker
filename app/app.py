import logging
import os
import re
import threading
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import psycopg2
from flask import Flask, Response, jsonify, redirect, request, session, url_for
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from werkzeug.security import check_password_hash, generate_password_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ["SECRET_KEY"],
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCK_SECONDS = 30
LOGIN_ATTEMPTS = {}
LOGIN_ATTEMPTS_LOCK = threading.Lock()
INVALID_PASSWORD_HASH = generate_password_hash("invalid-password-timing-check")
REQUESTS = Counter(
    "app_requests_total",
    "Số yêu cầu HTTP xử lý bởi ứng dụng",
    ("method", "endpoint", "status"),
)
REQUEST_DURATION = Histogram(
    "app_request_seconds",
    "Thời gian xử lý yêu cầu HTTP",
    ("endpoint",),
)
TRANSACTIONS_CREATED = Counter(
    "expense_transactions_created_total",
    "Số giao dịch chi tiêu được tạo thành công",
)


def get_db_connection():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "db"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=5,
    )


@app.before_request
def start_request_timer():
    request.request_started_at = time.perf_counter()
    public_endpoints = {
        "health",
        "favicon",
        "index",
        "calendar_page",
        "login_page",
        "metrics",
        "auth_register",
        "auth_login",
        "auth_logout",
        "auth_me",
    }
    if request.endpoint not in public_endpoints and "user_id" not in session:
        return jsonify(error="authentication required"), 401


@app.after_request
def record_request(response):
    endpoint = request.endpoint or "unknown"
    duration = time.perf_counter() - getattr(request, "request_started_at", time.perf_counter())
    REQUESTS.labels(request.method, endpoint, str(response.status_code)).inc()
    REQUEST_DURATION.labels(endpoint).observe(duration)
    logger.info("%s %s -> %s", request.method, request.path, response.status_code)
    return response


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.post("/api/auth/register")
def auth_register():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return auth_error("invalid payload", 400)
    username = payload.get("username")
    password = payload.get("password")
    if (
        not isinstance(username, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{3,50}", username.strip())
        or not isinstance(password, str)
        or not 10 <= len(password) <= 128
    ):
        return auth_error("invalid payload", 400)
    username = username.strip().lower()
    try:
        with get_db_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (username, password_hash)
                VALUES (%s, %s)
                RETURNING id, username
                """,
                (username, generate_password_hash(password)),
            )
            user_id, saved_username = cursor.fetchone()
            cursor.execute(
                "UPDATE transactions SET user_id = %s WHERE user_id IS NULL",
                (user_id,),
            )
    except psycopg2.errors.UniqueViolation:
        logger.warning("Đăng ký thất bại: username đã tồn tại")
        return auth_error("username already exists", 409)
    session.clear()
    session["user_id"] = user_id
    session["username"] = saved_username
    logger.info("Đã đăng ký tài khoản username=%s", saved_username)
    return jsonify(username=saved_username), 201


@app.post("/api/auth/login")
def auth_login():
    # Nginx tự ghi đè X-Real-IP để bộ đếm khóa theo đúng máy khách.
    client_ip = request.headers.get("X-Real-IP") or request.remote_addr or "unknown"
    now = time.monotonic()
    with LOGIN_ATTEMPTS_LOCK:
        failures, locked_until, _ = LOGIN_ATTEMPTS.get(client_ip, (0, 0.0, 0.0))
        if locked_until > now:
            return failed_login(locked_until - now, client_ip)
        for address, (_, blocked_until, last_attempt) in list(LOGIN_ATTEMPTS.items()):
            if blocked_until <= now and now - last_attempt > 600:
                LOGIN_ATTEMPTS.pop(address, None)

    payload = request.get_json(silent=True)
    username = payload.get("username") if isinstance(payload, dict) else None
    password = payload.get("password") if isinstance(payload, dict) else None
    valid_fields = (
        isinstance(username, str)
        and bool(username)
        and username == username.strip()
        and isinstance(password, str)
        and bool(password)
        and len(password) <= 128
    )
    user = None
    if valid_fields:
        with get_db_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, password_hash FROM users WHERE username = %s",
                (username.lower(),),
            )
            user = cursor.fetchone()
    password_matches = check_password_hash(
        user[2] if user is not None else INVALID_PASSWORD_HASH,
        password if isinstance(password, str) else "",
    )
    if not valid_fields or user is None or not password_matches:
        with LOGIN_ATTEMPTS_LOCK:
            failures, _, _ = LOGIN_ATTEMPTS.get(client_ip, (0, 0.0, 0.0))
            failures += 1
            if failures >= LOGIN_MAX_ATTEMPTS:
                locked_until = time.monotonic() + LOGIN_LOCK_SECONDS
                LOGIN_ATTEMPTS[client_ip] = (failures, locked_until, now)
                return failed_login(LOGIN_LOCK_SECONDS, client_ip)
            LOGIN_ATTEMPTS[client_ip] = (failures, 0.0, now)
        return failed_login(0, client_ip)

    with LOGIN_ATTEMPTS_LOCK:
        LOGIN_ATTEMPTS.pop(client_ip, None)
    session.clear()
    session["user_id"] = user[0]
    session["username"] = user[1]
    return jsonify(username=user[1])


def failed_login(retry_after, client_ip):
    logger.warning("Đăng nhập thất bại từ client=%s", client_ip)
    response = jsonify(error="Sai mật khẩu")
    response.status_code = 429 if retry_after else 401
    if retry_after:
        response.headers["Retry-After"] = str(max(1, int(retry_after)))
    return response


@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    return "", 204


@app.get("/api/auth/me")
def auth_me():
    if "user_id" not in session:
        return jsonify(error="authentication required"), 401
    return jsonify(username=session["username"])


def auth_error(message, status):
    return jsonify(error=message), status


@app.get("/api/categories")
def categories():
    with get_db_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id, name, type FROM categories ORDER BY id")
        rows = cursor.fetchall()
    return jsonify([{"id": row[0], "name": row[1], "type": row[2]} for row in rows])


@app.get("/api/transactions")
def transactions():
    query = """
        SELECT t.id, t.category_id, c.name, c.type, t.amount, t.note, t.tx_date
        FROM transactions AS t
        JOIN categories AS c ON c.id = t.category_id
        WHERE t.user_id = %s
        ORDER BY t.tx_date DESC, t.id DESC
        LIMIT 100
    """
    with get_db_connection() as connection, connection.cursor() as cursor:
        cursor.execute(query, (session["user_id"],))
        rows = cursor.fetchall()
    return jsonify([
        {
            "id": row[0],
            "category_id": row[1],
            "category_name": row[2],
            "type": row[3],
            "amount": float(row[4]),
            "note": row[5],
            "tx_date": row[6].isoformat(),
        }
        for row in rows
    ])


@app.post("/api/transactions")
def create_transaction():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return invalid_payload()
    amount_value = payload.get("amount")
    category_value = payload.get("category_id")
    note = payload.get("note", "")
    tx_date_value = payload.get("tx_date")
    try:
        amount = Decimal(str(amount_value))
        category_id = int(category_value)
        category_is_integer = not isinstance(category_value, bool) and str(category_id) == str(category_value).strip()
        tx_date = date.fromisoformat(tx_date_value) if tx_date_value is not None else date.today()
    except (InvalidOperation, TypeError, ValueError):
        return invalid_payload()
    if (
        not amount.is_finite()
        or amount < Decimal("1000")
        or amount > Decimal("999999999999.99")
        or not category_is_integer
        or not isinstance(note, str)
        or len(note) > 200
        or tx_date.year < 1900
    ):
        return invalid_payload()
    with get_db_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT name, type FROM categories WHERE id = %s", (category_id,))
        category = cursor.fetchone()
        if category is None:
            return invalid_payload()
        if category == ("Khác", "expense") and not note.strip():
            logger.warning("Từ chối giao dịch Khác vì chưa mô tả khoản chi")
            return jsonify(error="other category requires a description"), 400
        cursor.execute(
            """
            INSERT INTO transactions (user_id, category_id, amount, note, tx_date)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, category_id, amount, note, tx_date
            """,
            (session["user_id"], category_id, amount, note, tx_date),
        )
        row = cursor.fetchone()
    TRANSACTIONS_CREATED.inc()
    logger.info("Đã tạo giao dịch id=%s category_id=%s", row[0], row[1])
    return jsonify({
        "id": row[0],
        "category_id": row[1],
        "amount": float(row[2]),
        "note": row[3],
        "tx_date": row[4].isoformat(),
    }), 201


def invalid_payload():
    logger.warning("Payload giao dịch không hợp lệ")
    return jsonify(error="invalid payload"), 400


@app.get("/api/calendar")
def calendar_summary():
    try:
        year = int(request.args.get("year", ""))
        month = int(request.args.get("month", ""))
        month_start = date(year, month, 1)
        next_month_start = date(year + (month == 12), month % 12 + 1, 1)
    except (TypeError, ValueError):
        return jsonify(error="invalid month"), 400
    if year < 1900:
        return jsonify(error="invalid month"), 400

    previous_month_start = month_start - timedelta(days=1)
    previous_month_start = previous_month_start.replace(day=1)
    user_id = session["user_id"]
    with get_db_connection() as connection, connection.cursor() as cursor:
        # Cộng theo tháng và loại giao dịch, chỉ trong phạm vi tài khoản hiện tại.
        cursor.execute(
            """
            SELECT date_trunc('month', t.tx_date)::date, c.type, SUM(t.amount)
            FROM transactions AS t
            JOIN categories AS c ON c.id = t.category_id
            WHERE t.user_id = %s
              AND t.tx_date >= %s
              AND t.tx_date < %s
            GROUP BY 1, c.type
            """,
            (user_id, previous_month_start, next_month_start),
        )
        month_totals = cursor.fetchall()
        cursor.execute(
            """
            SELECT t.tx_date, c.type, SUM(t.amount)
            FROM transactions AS t
            JOIN categories AS c ON c.id = t.category_id
            WHERE t.user_id = %s
              AND t.tx_date >= %s
              AND t.tx_date < %s
            GROUP BY t.tx_date, c.type
            ORDER BY t.tx_date
            """,
            (user_id, month_start, next_month_start),
        )
        daily_totals = cursor.fetchall()

    summary = {
        "income": Decimal(0),
        "expense": Decimal(0),
        "previous_income": Decimal(0),
        "previous_expense": Decimal(0),
    }
    for transaction_month, transaction_type, total in month_totals:
        key = "previous_" + transaction_type if transaction_month < month_start else transaction_type
        summary[key] = total
    days = {}
    for transaction_date, transaction_type, total in daily_totals:
        key = transaction_date.isoformat()
        days.setdefault(key, {"date": key, "income": 0.0, "expense": 0.0})
        days[key][transaction_type] = float(total)
    return jsonify(
        month=f"{year:04d}-{month:02d}",
        summary={key: float(value) for key, value in summary.items()},
        days=list(days.values()),
    )


@app.get("/api/calendar/day")
def calendar_day():
    try:
        transaction_date = date.fromisoformat(request.args.get("date", ""))
    except (TypeError, ValueError):
        return jsonify(error="invalid date"), 400
    if transaction_date.year < 1900:
        return jsonify(error="invalid date"), 400
    with get_db_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT t.id, c.name, c.type, t.amount, t.note
            FROM transactions AS t
            JOIN categories AS c ON c.id = t.category_id
            WHERE t.user_id = %s AND t.tx_date = %s
            ORDER BY t.id DESC
            """,
            (session["user_id"], transaction_date),
        )
        rows = cursor.fetchall()
    return jsonify([
        {
            "id": row[0],
            "category_name": row[1],
            "type": row[2],
            "amount": float(row[3]),
            "note": row[4],
        }
        for row in rows
    ])


@app.delete("/api/transactions/<int:transaction_id>")
def delete_transaction(transaction_id):
    with get_db_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM transactions WHERE id = %s AND user_id = %s RETURNING id",
            (transaction_id, session["user_id"]),
        )
        deleted = cursor.fetchone()
    if deleted is None:
        return jsonify(error="not found"), 404
    return "", 204


@app.get("/api/report")
def report():
    query = """
        SELECT c.id, c.name, c.type, COALESCE(SUM(t.amount), 0)::numeric(14, 2)
        FROM categories AS c
        LEFT JOIN transactions AS t ON t.category_id = c.id AND t.user_id = %s
        GROUP BY c.id, c.name, c.type
        ORDER BY c.type, c.name
    """
    with get_db_connection() as connection, connection.cursor() as cursor:
        cursor.execute(query, (session["user_id"],))
        rows = cursor.fetchall()
    return jsonify([
        {"category_id": row[0], "category_name": row[1], "type": row[2], "total": float(row[3])}
        for row in rows
    ])


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)


@app.get("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login_page"))
    return Response(PAGE, mimetype="text/html")


@app.get("/login")
def login_page():
    if "user_id" in session:
        return redirect(url_for("index"))
    return Response(PAGE, mimetype="text/html")


@app.get("/calendar")
def calendar_page():
    if "user_id" not in session:
        return redirect(url_for("login_page"))
    return Response(PAGE, mimetype="text/html")


PAGE = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quản lý chi tiêu</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      /* Bảng màu xanh dịu giúp giao diện nhất quán, dễ đọc. */
      --green-900:#14532d;--green-600:#16a34a;--green-100:#dcfce7;
      --page:#f8fafc;--surface:#fff;--text:#172033;--muted:#64748b;
      --border:#e2e8f0;--red-700:#b91c1c;--red-100:#fef2f2;
      --shadow:0 12px 32px rgba(15,23,42,.07);--radius:16px;
    }
    *{box-sizing:border-box}
    body{margin:0;min-height:100vh;background:var(--page);color:var(--text);font-family:"Be Vietnam Pro",system-ui,sans-serif}
    [hidden]{display:none!important}
    .container{width:min(1100px,calc(100% - 40px));margin:42px auto 64px}
    h1,h2,h3,p{margin-top:0}
    h1{color:var(--green-900);font-size:clamp(1.65rem,3vw,2.15rem);letter-spacing:-.04em;margin-bottom:0}
    h2{color:var(--green-900);font-size:1.25rem;letter-spacing:-.02em;margin:0 0 20px}
    .brand-mark{display:inline-grid;place-items:center;width:42px;height:42px;border-radius:14px;background:var(--green-100);color:var(--green-900);font-size:1.35rem}
    .auth-card{width:min(460px,calc(100% - 32px));margin:clamp(32px,10vh,100px) auto;padding:36px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow)}
    .auth-heading{display:flex;align-items:center;gap:14px;margin-bottom:30px}
    .auth-heading p{margin:6px 0 0;color:var(--muted);font-size:.92rem}
    .auth-card form{display:grid;gap:10px;margin-bottom:10px}
    label{font-weight:600;font-size:.9rem}
    input,select,button{font:inherit}
    input,select{width:100%;height:48px;padding:0 14px;border:1px solid #cbd5e1;border-radius:10px;background:#fff;color:var(--text);outline:none;transition:border-color .18s,box-shadow .18s}
    #amount{-moz-appearance:textfield}
    #amount::-webkit-inner-spin-button,#amount::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
    input::placeholder{color:#94a3b8}
    input:focus,select:focus{border-color:var(--green-600);box-shadow:0 0 0 4px rgba(22,163,74,.16)}
    button{min-height:48px;padding:0 18px;border:0;border-radius:10px;background:var(--green-900);color:#fff;font-weight:600;cursor:pointer;transition:background .18s,transform .18s,box-shadow .18s}
    button:hover{background:var(--green-600);box-shadow:0 6px 14px rgba(22,163,74,.2)}
    button:active{transform:translateY(1px)}
    button:focus-visible{outline:3px solid rgba(22,163,74,.35);outline-offset:2px}
    button:disabled{background:#94a3b8;box-shadow:none;cursor:not-allowed}
    .auth-card form>button{margin-top:8px}
    .link-button{min-height:auto;padding:8px 0;background:transparent;color:var(--green-900);box-shadow:none}
    .link-button:hover{background:transparent;color:var(--green-600);box-shadow:none;text-decoration:underline}
    .password-wrap{position:relative}
    .password-wrap input{padding-right:84px}
    .password-toggle{position:absolute;top:5px;right:6px;min-height:38px;padding:0 10px;background:transparent;color:var(--green-900);font-size:.82rem}
    .password-toggle:hover{background:var(--green-100);box-shadow:none}
    .card{margin-top:22px;padding:26px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow)}
    .topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:28px}
    .brand{display:flex;align-items:center;gap:14px}
    .user-tools{display:flex;align-items:center;gap:14px;color:var(--muted);font-size:.92rem}
    .page-nav{display:flex;gap:8px;margin:-12px 0 22px}
    .page-nav a{padding:9px 14px;border-radius:9px;color:var(--green-900);font-weight:600;text-decoration:none}
    .page-nav a[aria-current="page"]{background:var(--green-100)}
    .transaction-form{display:grid;grid-template-columns:minmax(145px,1fr) minmax(145px,.9fr) minmax(190px,1.4fr) minmax(145px,.9fr) auto;gap:12px;align-items:start}
    .transaction-form button{height:48px;white-space:nowrap}
    .field{min-width:0}
    .field small{display:block;min-height:20px;margin-top:4px;text-align:right;color:var(--muted);font-size:.76rem}
    .note-field #note-hint{text-align:left;color:var(--red-700)}
    .month-controls{display:flex;align-items:center;gap:9px}
    .month-controls button{min-height:40px;padding:0 13px}
    .month-controls select,.month-controls input{height:40px;width:auto}
    .month-controls input{max-width:105px}
    .summary-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
    .summary-card{padding:20px;border:1px solid var(--border);border-radius:12px;background:#fff}
    .summary-card h3{margin:0 0 8px;color:var(--muted);font-size:.92rem}
    .summary-value{margin:0 0 8px;font-size:clamp(1.35rem,3vw,1.8rem);font-weight:700;font-variant-numeric:tabular-nums}
    .summary-value.expense{color:var(--red-700)}.summary-value.income{color:var(--green-900)}
    .comparison{margin:0;color:var(--muted);font-size:.84rem}
    .comparison.up{color:var(--red-700)}.comparison.down{color:var(--green-900)}
    .chart-period{margin:-5px 0 16px;color:var(--muted)}
    .chart-period strong{color:#2584df;font-weight:600}
    .chart-layout{display:grid;grid-template-columns:84px minmax(0,1fr);gap:12px}
    .chart-axis{height:280px;display:flex;flex-direction:column;justify-content:space-between;padding:0 5px 0 0;color:var(--muted);font-size:.78rem;text-align:right}
    .chart-plot{position:relative;height:280px;border-left:1px solid #cbd5e1;border-bottom:1px solid #94a3b8;background:repeating-linear-gradient(to bottom,transparent 0,transparent calc(50% - .5px),#e2e8f0 calc(50% - .5px),#e2e8f0 calc(50% + .5px),transparent calc(50% + .5px),transparent 100%)}
    .chart-bars{position:absolute;inset:0 7% 0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18%;align-items:stretch}
    .chart-bar-group{position:relative;min-width:0;height:100%}
    .chart-bar-value{position:absolute;left:50%;z-index:1;transform:translateX(-50%);font-size:.82rem;font-weight:600;white-space:nowrap}
    .chart-bar{position:absolute;bottom:0;left:50%;width:min(100%,150px);transform:translateX(-50%);border-radius:12px 12px 0 0;transition:height .25s ease}
    .chart-bar.income{background:#2d8cf0}
    .chart-bar.expense{background:#e60023}
    .chart-legend{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18%;width:calc(100% - 96px);margin:0 0 0 96px;padding:12px 7% 0}
    .chart-legend-item{min-width:0}
    .chart-legend-title{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:.84rem}
    .chart-dot{width:11px;height:11px;flex:none;border-radius:50%}
    .chart-dot.income{background:#2d8cf0}.chart-dot.expense{background:#e60023}
    .chart-legend-value{margin:4px 0 0 19px;font-size:clamp(1.1rem,2.5vw,1.55rem);font-weight:600;font-variant-numeric:tabular-nums}
    .calendar-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:7px}
    .calendar-weekday{padding:7px 2px;text-align:center;color:var(--muted);font-size:.8rem;font-weight:600}
    .calendar-day{display:flex;flex-direction:column;align-items:flex-start;gap:5px;min-width:0;min-height:96px;padding:9px;background:#fff;color:var(--text);border:1px solid var(--border);text-align:left}
    .calendar-day:hover{background:#f0fdf4;color:var(--text)}
    .calendar-day[aria-pressed="true"]{border:2px solid var(--green-600);background:var(--green-100);padding:8px}
    .calendar-day.outside-month{visibility:hidden}
    .calendar-day .day-number{font-weight:700}
    .calendar-day .day-expense,.calendar-day .day-income{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.72rem}
    .calendar-day .day-expense{color:var(--red-700)}.calendar-day .day-income{color:var(--green-900)}
    .day-transactions{margin-top:22px}
    .day-transactions h3{margin:0;color:var(--green-900)}
    .table-wrap{width:100%;overflow-x:auto;border:1px solid var(--border);border-radius:12px}
    table{width:100%;min-width:620px;border-collapse:collapse}
    th,td{padding:13px 15px;border-bottom:1px solid var(--border);text-align:left;vertical-align:middle}
    th{background:var(--green-100);color:var(--green-900);font-size:.84rem;font-weight:700;white-space:nowrap}
    tbody tr:nth-child(even){background:#f8fafc}
    tbody tr:hover{background:#f0fdf4}
    tbody tr:last-child td{border-bottom:0}
    .amount{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;font-weight:600}
    .amount.income{color:var(--green-900)}.amount.expense{color:var(--red-700)}
    .badge{display:inline-flex;align-items:center;padding:4px 10px;border-radius:999px;font-size:.78rem;font-weight:600}
    .badge.income{background:var(--green-100);color:var(--green-900)}
    .badge.expense{background:var(--red-100);color:var(--red-700)}
    .delete-button{min-height:34px;padding:0 12px;background:transparent;color:var(--red-700);font-size:.85rem}
    .delete-button:hover{background:var(--red-100);box-shadow:none}
    .empty-state{padding:26px!important;text-align:center!important;color:var(--muted);font-weight:500}
    .alert{display:flex;align-items:center;gap:9px;margin:12px 0 0;padding:12px 14px;border-radius:10px;font-size:.9rem}
    .alert.success{background:var(--green-100);color:var(--green-900)}
    .alert.error{background:var(--red-100);color:var(--red-700)}
    .alert.success::before{content:"✓";font-weight:700}
    .alert.error::before{content:"!";display:inline-grid;place-items:center;width:19px;height:19px;border:1.5px solid currentColor;border-radius:50%;font-weight:700}
    .alert[hidden]{display:none!important}
    .alert.shake{animation:shake .34s ease-in-out}
    @keyframes shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-6px)}50%{transform:translateX(6px)}75%{transform:translateX(-4px)}}
    .section-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}
    .section-heading h2{margin:0}
    .subtle{color:var(--muted);font-size:.84rem}
    @media(max-width:760px){
      .container{width:min(100% - 28px,620px);margin:24px auto 40px}
      .card{padding:20px;margin-top:16px}
      .transaction-form{grid-template-columns:1fr 1fr}
      .transaction-form .note-field,.transaction-form .date-field{grid-column:1/-1}
      .transaction-form button{grid-column:1/-1}
      .topbar{align-items:flex-start}.user-tools{flex-direction:column;align-items:flex-end;gap:6px}
      .calendar-day{min-height:78px;padding:6px}
      .calendar-day[aria-pressed="true"]{padding:5px}
      .calendar-day .day-expense,.calendar-day .day-income{font-size:.63rem}
    }
    @media(max-width:480px){
      .auth-card{padding:25px 21px}
      .container{width:calc(100% - 22px);margin-top:16px}
      .card{padding:17px}
      .transaction-form{grid-template-columns:1fr}
      .transaction-form .note-field,.transaction-form .date-field,.transaction-form button{grid-column:auto}
      .brand{gap:10px}.brand-mark{width:38px;height:38px}
      .user-tools{font-size:.78rem}.user-tools button{min-height:38px;padding:0 11px}
      th,td{padding:11px 12px}
      .section-heading{align-items:flex-start;flex-direction:column}
      .month-controls{width:100%;flex-wrap:wrap}
      .month-controls select{flex:1;min-width:130px}
      .summary-grid{grid-template-columns:1fr}
      .chart-layout{grid-template-columns:60px minmax(0,1fr);gap:8px}
      .chart-axis,.chart-plot{height:220px}
      .chart-axis{font-size:.68rem}
      .chart-bars{inset-inline:5%;gap:12%}
      .chart-legend{gap:12%;width:calc(100% - 68px);padding:12px 5% 0;margin-left:68px}
      .chart-legend-title{font-size:.72rem}
      .chart-bar-value{font-size:.68rem}
      .chart-legend-value{font-size:1rem}
      .calendar-grid{gap:3px}
      .calendar-weekday{font-size:.68rem}
      .calendar-day{min-height:66px;padding:4px}
      .calendar-day[aria-pressed="true"]{padding:3px}
      .calendar-day .day-expense,.calendar-day .day-income{font-size:.55rem}
    }
  </style>
</head>
<body>
  <section id="auth-section" class="auth-card">
    <div class="auth-heading">
      <span class="brand-mark" aria-hidden="true">₫</span>
      <div><h1>Quản lý chi tiêu</h1><p>Ghi chép hôm nay, an tâm ngày mai.</p></div>
    </div>
    <h2 id="auth-title">Đăng nhập</h2>
    <form id="auth-form">
      <label for="username">Tên đăng nhập</label>
      <input id="username" name="username" maxlength="128" autocomplete="username">
      <label id="password-label" for="password">Mật khẩu (ít nhất 10 ký tự)</label>
      <div class="password-wrap">
        <input id="password" name="password" type="password" maxlength="256" autocomplete="current-password">
        <button id="password-toggle" class="password-toggle" type="button" aria-pressed="false">Hiện</button>
      </div>
      <button id="auth-submit" type="submit">Đăng nhập</button>
    </form>
    <div id="auth-message" class="alert" role="alert" aria-live="assertive" hidden></div>
    <button id="auth-toggle" class="link-button" type="button">Chưa có tài khoản? Đăng ký</button>
  </section>
  <main id="expense-app" class="container" hidden>
    <header class="topbar">
      <div class="brand"><span class="brand-mark" aria-hidden="true">₫</span><h1>Quản lý chi tiêu</h1></div>
      <div class="user-tools"><span>Xin chào, <strong id="current-user"></strong></span><button id="logout" type="button">Đăng xuất</button></div>
    </header>
    <nav class="page-nav" aria-label="Trang quản lý">
      <a href="/" id="dashboard-link">Tổng quan</a>
      <a href="/calendar" id="calendar-link">Chi tiêu theo ngày</a>
    </nav>
    <section class="card">
      <div class="section-heading"><h2>Thêm giao dịch</h2><span class="subtle">Theo dõi khoản thu và chi</span></div>
      <form id="transaction-form" class="transaction-form">
        <div class="field"><select id="category" required aria-label="Danh mục"></select></div>
        <div class="field">
          <input id="amount" type="number" min="1000" max="999999999999.99" step="any" placeholder="Số tiền" required aria-label="Số tiền">
        </div>
        <div class="field note-field">
          <input id="note" maxlength="200" placeholder="Ghi chú (không bắt buộc)" aria-label="Ghi chú">
          <small id="note-hint" class="note-hint" aria-live="polite"></small>
          <small id="note-counter">0/200</small>
        </div>
        <div class="field date-field"><input id="tx-date" type="date" min="1900-01-01" required aria-label="Ngày giao dịch"></div>
        <button type="submit">Thêm giao dịch</button>
      </form>
      <div id="message" class="alert" role="status" aria-live="polite" hidden></div>
    </section>
    <section id="calendar-view" hidden>
      <section class="card">
        <div class="section-heading">
          <div><h2 id="calendar-title">Thống kê tháng</h2><span class="subtle">Theo dõi tổng thu, chi và biến động so với tháng trước</span></div>
          <div class="month-controls">
            <button id="previous-month" type="button" aria-label="Tháng trước">‹</button>
            <select id="month-select" aria-label="Chọn tháng"></select>
            <input id="year-select" type="number" min="1900" max="9998" step="1" aria-label="Chọn năm">
            <button id="apply-month" type="button">Xem tháng</button>
            <button id="next-month" type="button" aria-label="Tháng sau">›</button>
          </div>
        </div>
        <div class="summary-grid">
          <article class="summary-card">
            <h3>Tổng chi trong tháng</h3>
            <p id="month-expense" class="summary-value expense">0 ₫</p>
            <p id="expense-comparison" class="comparison"></p>
          </article>
          <article class="summary-card">
            <h3>Tổng thu nhập trong tháng</h3>
            <p id="month-income" class="summary-value income">0 ₫</p>
            <p id="income-comparison" class="comparison"></p>
          </article>
        </div>
      </section>
      <section class="card">
        <div class="section-heading"><h2>Biểu đồ thu chi</h2><span class="subtle">So sánh tổng tiền thu và chi</span></div>
        <p id="chart-period" class="chart-period"></p>
        <div class="chart-layout" role="img" aria-label="Biểu đồ cột so sánh thu nhập và chi tiêu">
          <div id="chart-axis" class="chart-axis" aria-hidden="true"></div>
          <div class="chart-plot">
            <div class="chart-bars">
              <div class="chart-bar-group">
                <span id="chart-income-value" class="chart-bar-value"></span>
                <div id="chart-income-bar" class="chart-bar income"></div>
              </div>
              <div class="chart-bar-group">
                <span id="chart-expense-value" class="chart-bar-value"></span>
                <div id="chart-expense-bar" class="chart-bar expense"></div>
              </div>
            </div>
          </div>
        </div>
        <div class="chart-legend">
          <div class="chart-legend-item">
            <div class="chart-legend-title"><span class="chart-dot income"></span>Tổng thu (VNĐ)</div>
            <p id="chart-income-total" class="chart-legend-value">0 ₫</p>
          </div>
          <div class="chart-legend-item">
            <div class="chart-legend-title"><span class="chart-dot expense"></span>Tổng chi (VNĐ)</div>
            <p id="chart-expense-total" class="chart-legend-value">0 ₫</p>
          </div>
        </div>
      </section>
      <section class="card">
        <div class="section-heading"><h2 id="calendar-month-heading">Chi tiêu từng ngày</h2><span class="subtle">Chọn một ngày để xem các giao dịch</span></div>
        <div class="calendar-grid" id="calendar-grid" role="grid" aria-label="Lịch chi tiêu tháng"></div>
      </section>
      <section class="card day-transactions">
        <div class="section-heading"><h3 id="selected-day-heading">Giao dịch trong ngày</h3><span id="selected-day-totals" class="subtle"></span></div>
        <div class="table-wrap"><table><thead><tr><th>Danh mục</th><th>Loại</th><th>Ghi chú</th><th class="amount">Số tiền</th><th></th></tr></thead>
          <tbody id="day-transactions"></tbody></table></div>
      </section>
    </section>
    <section id="dashboard-view">
    <section class="card">
      <div class="section-heading"><h2>Giao dịch gần đây</h2><span class="subtle">Tối đa 100 giao dịch mới nhất</span></div>
      <div class="table-wrap"><table><thead><tr><th>Ngày</th><th>Danh mục</th><th>Ghi chú</th><th class="amount">Số tiền</th><th></th></tr></thead>
        <tbody id="transactions"></tbody></table></div>
    </section>
    <section class="card">
      <div class="section-heading"><h2>Báo cáo theo danh mục</h2><span class="subtle">Tổng hợp giao dịch của bạn</span></div>
      <div class="table-wrap"><table><thead><tr><th>Danh mục</th><th>Loại</th><th class="amount">Tổng</th></tr></thead>
        <tbody id="report"></tbody></table></div>
    </section>
    </section>
  </main>
  <script>
    const money = value => new Intl.NumberFormat('vi-VN', {
      style: 'currency', currency: 'VND', maximumFractionDigits: 0
    }).format(value);
    const localDateString = date => [
      date.getFullYear(),
      String(date.getMonth() + 1).padStart(2, '0'),
      String(date.getDate()).padStart(2, '0')
    ].join('-');
    const currentDate = new Date();
    let calendarMonth = new Date(currentDate.getFullYear(), currentDate.getMonth(), 1);
    let selectedDate = localDateString(currentDate);
    let calendarDays = new Map();
    const alertTimers = new WeakMap();
    function showAlert(element, text, kind) {
      clearTimeout(alertTimers.get(element));
      element.textContent = text;
      element.className = `alert ${kind}`;
      element.hidden = false;
      if (element.id === 'auth-message' && kind === 'error') {
        element.classList.remove('shake');
        void element.offsetWidth;
        element.classList.add('shake');
      }
      alertTimers.set(element, setTimeout(() => { element.hidden = true; }, 4500));
    }
    let registering = false;
    let cooldownTimer;
    function setAuthMode() {
      document.querySelector('#auth-title').textContent = registering ? 'Tạo tài khoản' : 'Đăng nhập';
      document.querySelector('#auth-submit').textContent = registering ? 'Đăng ký' : 'Đăng nhập';
      document.querySelector('#password').autocomplete = registering ? 'new-password' : 'current-password';
      document.querySelector('#auth-toggle').textContent = registering
        ? 'Đã có tài khoản? Đăng nhập'
        : 'Chưa có tài khoản? Đăng ký';
      document.querySelector('#password-label').textContent = registering
        ? 'Mật khẩu (10–128 ký tự)'
        : 'Mật khẩu';
    }
    async function enterApp(username) {
      document.querySelector('#current-user').textContent = username;
      document.querySelector('#auth-section').hidden = true;
      document.querySelector('#expense-app').hidden = false;
      const isCalendarPage = window.location.pathname === '/calendar';
      document.querySelector('#calendar-view').hidden = !isCalendarPage;
      document.querySelector('#dashboard-view').hidden = isCalendarPage;
      document.querySelector('#calendar-link').setAttribute('aria-current', isCalendarPage ? 'page' : 'false');
      document.querySelector('#dashboard-link').setAttribute('aria-current', isCalendarPage ? 'false' : 'page');
      const requestedMonth = new URLSearchParams(window.location.search).get('month');
      if (isCalendarPage && requestedMonth && /^\\d{4}-\\d{2}$/.test(requestedMonth)) {
        const [year, month] = requestedMonth.split('-').map(Number);
        if (year >= 1900 && year <= 9998 && month >= 1 && month <= 12) {
          calendarMonth = new Date(year, month - 1, 1);
          selectedDate = `${requestedMonth}-${String(Math.min(
            currentDate.getDate(),
            new Date(year, month, 0).getDate()
          )).padStart(2, '0')}`;
        }
      }
      document.querySelector('#tx-date').value = selectedDate;
      await loadCategories();
      await refresh();
    }
    document.querySelector('#auth-toggle').addEventListener('click', () => {
      registering = !registering;
      document.querySelector('#auth-message').hidden = true;
      setAuthMode();
    });
    document.querySelector('#password-toggle').addEventListener('click', event => {
      const password = document.querySelector('#password');
      const visible = password.type === 'password';
      password.type = visible ? 'text' : 'password';
      event.currentTarget.textContent = visible ? 'Ẩn' : 'Hiện';
      event.currentTarget.setAttribute('aria-pressed', String(visible));
    });
    document.querySelector('#note').addEventListener('input', event => {
      document.querySelector('#note-counter').textContent = `${event.currentTarget.value.length}/200`;
    });
    function startLoginCooldown(seconds) {
      clearInterval(cooldownTimer);
      const button = document.querySelector('#auth-submit');
      const originalText = registering ? 'Đăng ký' : 'Đăng nhập';
      let remaining = seconds;
      button.disabled = true;
      button.textContent = `Thử lại sau ${remaining} giây`;
      cooldownTimer = setInterval(() => {
        remaining -= 1;
        if (remaining <= 0) {
          clearInterval(cooldownTimer);
          button.disabled = false;
          button.textContent = originalText;
          return;
        }
        button.textContent = `Thử lại sau ${remaining} giây`;
      }, 1000);
    }
    document.querySelector('#auth-form').addEventListener('submit', async event => {
      event.preventDefault();
      const message = document.querySelector('#auth-message');
      const mode = registering ? 'register' : 'login';
      const passwordField = document.querySelector('#password');
      try {
        const response = await fetch(`/api/auth/${mode}`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            username: document.querySelector('#username').value,
            password: document.querySelector('#password').value
          })
        });
        const result = await response.json();
        if (!response.ok) {
          if (!registering) {
            showAlert(message, 'Sai mật khẩu', 'error');
            passwordField.value = '';
            passwordField.focus();
            if (response.status === 429) {
              startLoginCooldown(Number(response.headers.get('Retry-After')) || 30);
            }
            return;
          }
          showAlert(
            message,
            result.error === 'username already exists'
              ? 'Tên đăng nhập đã được sử dụng.'
              : 'Thông tin chưa hợp lệ. Hãy kiểm tra tên đăng nhập và mật khẩu.',
            'error'
          );
          return;
        }
        window.location.assign('/');
      } catch (error) {
        if (!registering) {
          showAlert(message, 'Sai mật khẩu', 'error');
          passwordField.value = '';
          passwordField.focus();
        } else {
          showAlert(message, 'Không thể kết nối. Vui lòng thử lại.', 'error');
        }
      }
    });
    document.querySelector('#logout').addEventListener('click', async () => {
      try {
        const response = await fetch('/api/auth/logout', {method: 'POST'});
        if (!response.ok) throw new Error('Logout failed');
        document.querySelector('#expense-app').hidden = true;
        document.querySelector('#auth-section').hidden = false;
        document.querySelector('#password').value = '';
        history.replaceState(null, '', '/login');
        showAlert(document.querySelector('#auth-message'), 'Đã đăng xuất.', 'success');
      } catch (error) {
        document.querySelector('#message').textContent = 'Không thể đăng xuất. Vui lòng thử lại.';
      }
    });
    async function loadCategories() {
      const response = await fetch('/api/categories');
      if (!response.ok) throw new Error('Không tải được danh mục');
      const categories = await response.json();
      const select = document.querySelector('#category');
      select.replaceChildren();
      for (const category of categories) {
        const option = document.createElement('option');
        option.value = category.id;
        option.textContent = `${category.name} (${category.type === 'income' ? 'Thu' : 'Chi'})`;
        option.dataset.name = category.name;
        option.dataset.type = category.type;
        select.append(option);
      }
      updateNoteRequirement();
    }
    function selectedCategoryRequiresNote() {
      const select = document.querySelector('#category');
      const option = select.selectedOptions[0];
      return option?.dataset.name === 'Khác' && option.dataset.type === 'expense';
    }
    function updateNoteRequirement() {
      const required = selectedCategoryRequiresNote();
      const note = document.querySelector('#note');
      const hint = document.querySelector('#note-hint');
      note.setAttribute('aria-required', String(required));
      note.placeholder = required ? 'Ghi rõ khoản chi ở mục Khác' : 'Ghi chú (không bắt buộc)';
      note.setAttribute('aria-label', required ? 'Mô tả khoản chi (bắt buộc)' : 'Ghi chú');
      hint.textContent = required ? 'Bắt buộc: ghi rõ khoản chi này là gì.' : '';
    }
    document.querySelector('#category').addEventListener('change', updateNoteRequirement);
    async function loadTransactions() {
      const response = await fetch('/api/transactions');
      if (!response.ok) throw new Error('Không tải được giao dịch');
      const items = await response.json();
      const body = document.querySelector('#transactions');
      body.replaceChildren();
      if (items.length === 0) {
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = 5;
        cell.className = 'empty-state';
        cell.textContent = 'Chưa có giao dịch nào';
        row.append(cell);
        body.append(row);
        return;
      }
      for (const item of items) {
        const row = document.createElement('tr');
        for (const value of [item.tx_date, item.category_name, item.note || '']) {
          const cell = document.createElement('td');
          cell.textContent = value;
          row.append(cell);
        }
        const amount = document.createElement('td');
        amount.className = `amount ${item.type}`;
        amount.textContent = money(item.amount);
        row.append(amount);
        const action = document.createElement('td');
        const button = document.createElement('button');
        button.textContent = 'Xóa';
        button.className = 'delete-button';
        button.addEventListener('click', async () => {
          try {
            const result = await fetch(`/api/transactions/${item.id}`, {method: 'DELETE'});
            if (!result.ok) throw new Error('Không xóa được giao dịch');
            await refresh();
            showAlert(document.querySelector('#message'), 'Đã xóa giao dịch.', 'success');
          } catch (error) {
            showAlert(document.querySelector('#message'), error.message, 'error');
          }
        });
        action.append(button);
        row.append(action);
        body.append(row);
      }
    }
    async function loadReport() {
      const response = await fetch('/api/report');
      if (!response.ok) throw new Error('Không tải được báo cáo');
      const items = await response.json();
      const body = document.querySelector('#report');
      body.replaceChildren();
      for (const item of items) {
        const row = document.createElement('tr');
        const name = document.createElement('td');
        name.textContent = item.category_name;
        row.append(name);
        const typeCell = document.createElement('td');
        const badge = document.createElement('span');
        badge.className = `badge ${item.type}`;
        badge.textContent = item.type === 'income' ? 'Thu' : 'Chi';
        typeCell.append(badge);
        row.append(typeCell);
        const total = document.createElement('td');
        total.className = `amount ${item.type}`;
        total.textContent = money(item.total);
        row.append(total);
        body.append(row);
      }
    }
    const monthNames = [
      'Tháng 1', 'Tháng 2', 'Tháng 3', 'Tháng 4', 'Tháng 5', 'Tháng 6',
      'Tháng 7', 'Tháng 8', 'Tháng 9', 'Tháng 10', 'Tháng 11', 'Tháng 12'
    ];
    const monthSelect = document.querySelector('#month-select');
    for (const [index, name] of monthNames.entries()) {
      const option = document.createElement('option');
      option.value = index + 1;
      option.textContent = name;
      monthSelect.append(option);
    }
    function updateComparison(element, current, previous, type) {
      const difference = current - previous;
      const direction = difference > 0 ? 'Tăng' : difference < 0 ? 'Giảm' : 'Không đổi';
      const sign = difference > 0 ? '+' : difference < 0 ? '−' : '';
      const change = Math.abs(difference);
      const percentage = previous > 0
        ? ` (${Math.round(change / previous * 100)}%)`
        : current > 0 ? ' (tháng trước chưa có giao dịch)' : '';
      element.textContent = `So với tháng trước (${money(previous)}): ${direction} ${sign}${money(change)}${percentage}`;
      const favorable = type === 'expense' ? difference < 0 : difference > 0;
      element.className = `comparison ${difference === 0 ? '' : favorable ? 'down' : 'up'}`;
    }
    function renderIncomeExpenseChart(days) {
      const year = calendarMonth.getFullYear();
      const month = calendarMonth.getMonth();
      const firstDay = new Date(year, month, 1);
      const lastDay = new Date(year, month + 1, 0);
      const today = new Date();
      const isCurrentMonth = year === today.getFullYear() && month === today.getMonth();
      const periodEnd = isCurrentMonth ? today : lastDay;
      const periodEndString = localDateString(periodEnd);
      const totals = days
        .filter(day => day.date <= periodEndString)
        .reduce((result, day) => ({
          income: result.income + day.income,
          expense: result.expense + day.expense
        }), {income: 0, expense: 0});
      const maximum = Math.max(totals.income, totals.expense);
      const axisMaximum = maximum > 0 ? maximum * 1.2 : 100;
      const incomeHeight = totals.income / axisMaximum * 100;
      const expenseHeight = totals.expense / axisMaximum * 100;
      const dateFormat = new Intl.DateTimeFormat('vi-VN', {
        day: '2-digit', month: '2-digit', year: 'numeric'
      });
      const period = document.querySelector('#chart-period');
      period.replaceChildren(document.createTextNode('Tính từ '));
      const dates = document.createElement('strong');
      dates.textContent = `${dateFormat.format(firstDay)} - ${dateFormat.format(periodEnd)}`;
      period.append(dates);
      const axis = document.querySelector('#chart-axis');
      axis.replaceChildren();
      for (const value of [axisMaximum, axisMaximum / 2, 0]) {
        const tick = document.createElement('span');
        tick.textContent = money(value);
        axis.append(tick);
      }
      for (const [kind, amount, height] of [
        ['income', totals.income, incomeHeight],
        ['expense', totals.expense, expenseHeight]
      ]) {
        document.querySelector(`#chart-${kind}-value`).textContent = money(amount);
        document.querySelector(`#chart-${kind}-value`).style.bottom = `calc(${height}% + 8px)`;
        document.querySelector(`#chart-${kind}-bar`).style.height = `${height}%`;
        document.querySelector(`#chart-${kind}-total`).textContent = money(amount);
      }
      document.querySelector('.chart-layout').setAttribute(
        'aria-label',
        `Từ ${dateFormat.format(firstDay)} đến ${dateFormat.format(periodEnd)}. Tổng thu ${money(totals.income)}; tổng chi ${money(totals.expense)}.`
      );
    }
    function renderCalendar() {
      const year = calendarMonth.getFullYear();
      const month = calendarMonth.getMonth();
      const firstWeekday = (new Date(year, month, 1).getDay() + 6) % 7;
      const daysInMonth = new Date(year, month + 1, 0).getDate();
      const grid = document.querySelector('#calendar-grid');
      grid.replaceChildren();
      for (const weekday of ['T2', 'T3', 'T4', 'T5', 'T6', 'T7', 'CN']) {
        const heading = document.createElement('span');
        heading.className = 'calendar-weekday';
        heading.setAttribute('role', 'columnheader');
        heading.textContent = weekday;
        grid.append(heading);
      }
      for (let index = 0; index < firstWeekday; index += 1) {
        const blank = document.createElement('span');
        blank.className = 'calendar-day outside-month';
        blank.setAttribute('aria-hidden', 'true');
        grid.append(blank);
      }
      for (let dayNumber = 1; dayNumber <= daysInMonth; dayNumber += 1) {
        const day = document.createElement('button');
        const dayDate = `${year}-${String(month + 1).padStart(2, '0')}-${String(dayNumber).padStart(2, '0')}`;
        const totals = calendarDays.get(dayDate) || {income: 0, expense: 0};
        day.type = 'button';
        day.className = 'calendar-day';
        day.setAttribute('role', 'gridcell');
        day.setAttribute('aria-pressed', String(dayDate === selectedDate));
        day.setAttribute('aria-label', `${dayNumber}, chi ${money(totals.expense)}, thu ${money(totals.income)}`);
        const number = document.createElement('span');
        number.className = 'day-number';
        number.textContent = dayNumber;
        day.append(number);
        if (totals.expense) {
          const expense = document.createElement('span');
          expense.className = 'day-expense';
          expense.textContent = `−${money(totals.expense)}`;
          day.append(expense);
        }
        if (totals.income) {
          const income = document.createElement('span');
          income.className = 'day-income';
          income.textContent = `+${money(totals.income)}`;
          day.append(income);
        }
        day.addEventListener('click', async () => {
          selectedDate = dayDate;
          document.querySelector('#tx-date').value = dayDate;
          renderCalendar();
          await loadDayTransactions();
        });
        grid.append(day);
      }
      const totalCells = firstWeekday + daysInMonth;
      const trailingCells = (7 - totalCells % 7) % 7;
      for (let index = 0; index < trailingCells; index += 1) {
        const blank = document.createElement('span');
        blank.className = 'calendar-day outside-month';
        blank.setAttribute('aria-hidden', 'true');
        grid.append(blank);
      }
    }
    async function loadCalendar() {
      const year = calendarMonth.getFullYear();
      const month = calendarMonth.getMonth() + 1;
      const response = await fetch(`/api/calendar?year=${year}&month=${month}`);
      if (!response.ok) throw new Error('Không tải được thống kê tháng');
      const data = await response.json();
      const monthLabel = new Intl.DateTimeFormat('vi-VN', {month: 'long', year: 'numeric'})
        .format(calendarMonth);
      document.querySelector('#calendar-title').textContent = `Thống kê ${monthLabel}`;
      document.querySelector('#calendar-month-heading').textContent = `Chi tiêu từng ngày · ${monthLabel}`;
      document.querySelector('#month-select').value = String(month);
      document.querySelector('#year-select').value = String(year);
      document.querySelector('#month-expense').textContent = money(data.summary.expense);
      document.querySelector('#month-income').textContent = money(data.summary.income);
      updateComparison(
        document.querySelector('#expense-comparison'),
        data.summary.expense, data.summary.previous_expense, 'expense'
      );
      updateComparison(
        document.querySelector('#income-comparison'),
        data.summary.income, data.summary.previous_income, 'income'
      );
      calendarDays = new Map(data.days.map(day => [day.date, day]));
      renderIncomeExpenseChart(data.days);
      renderCalendar();
      await loadDayTransactions();
    }
    async function loadDayTransactions() {
      const response = await fetch(`/api/calendar/day?date=${selectedDate}`);
      if (!response.ok) throw new Error('Không tải được giao dịch trong ngày');
      const items = await response.json();
      const body = document.querySelector('#day-transactions');
      const heading = document.querySelector('#selected-day-heading');
      const totalsLabel = document.querySelector('#selected-day-totals');
      const dayTotals = items.reduce((totals, item) => {
        totals[item.type] += item.amount;
        return totals;
      }, {income: 0, expense: 0});
      heading.textContent = `Giao dịch ngày ${new Intl.DateTimeFormat('vi-VN', {
        day: 'numeric', month: 'long', year: 'numeric'
      }).format(new Date(`${selectedDate}T00:00:00`))}`;
      totalsLabel.textContent = `Thu ${money(dayTotals.income)} · Chi ${money(dayTotals.expense)}`;
      body.replaceChildren();
      if (items.length === 0) {
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = 5;
        cell.className = 'empty-state';
        cell.textContent = 'Ngày này chưa có giao dịch nào';
        row.append(cell);
        body.append(row);
        return;
      }
      for (const item of items) {
        const row = document.createElement('tr');
        const category = document.createElement('td');
        category.textContent = item.category_name;
        row.append(category);
        const typeCell = document.createElement('td');
        const badge = document.createElement('span');
        badge.className = `badge ${item.type}`;
        badge.textContent = item.type === 'income' ? 'Thu' : 'Chi';
        typeCell.append(badge);
        row.append(typeCell);
        const note = document.createElement('td');
        note.textContent = item.note || '';
        row.append(note);
        const amount = document.createElement('td');
        amount.className = `amount ${item.type}`;
        amount.textContent = money(item.amount);
        row.append(amount);
        const action = document.createElement('td');
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = 'Xóa';
        button.className = 'delete-button';
        button.addEventListener('click', async () => {
          try {
            const result = await fetch(`/api/transactions/${item.id}`, {method: 'DELETE'});
            if (!result.ok) throw new Error('Không xóa được giao dịch');
            await refresh();
            showAlert(document.querySelector('#message'), 'Đã xóa giao dịch.', 'success');
          } catch (error) {
            showAlert(document.querySelector('#message'), error.message, 'error');
          }
        });
        action.append(button);
        row.append(action);
        body.append(row);
      }
    }
    async function changeCalendarMonth(monthOffset) {
      const nextMonth = new Date(calendarMonth.getFullYear(), calendarMonth.getMonth() + monthOffset, 1);
      if (nextMonth.getFullYear() < 1900 || nextMonth.getFullYear() > 9998) return;
      calendarMonth = nextMonth;
      selectedDate = `${calendarMonth.getFullYear()}-${String(calendarMonth.getMonth() + 1).padStart(2, '0')}-01`;
      document.querySelector('#tx-date').value = selectedDate;
      await loadCalendar();
    }
    document.querySelector('#previous-month').addEventListener('click', () => changeCalendarMonth(-1));
    document.querySelector('#next-month').addEventListener('click', () => changeCalendarMonth(1));
    document.querySelector('#apply-month').addEventListener('click', () => {
      const year = Number(document.querySelector('#year-select').value);
      const month = Number(document.querySelector('#month-select').value);
      if (!Number.isInteger(year) || year < 1900 || year > 9998 || month < 1 || month > 12) {
        document.querySelector('#year-select').value = String(calendarMonth.getFullYear());
        return;
      }
      calendarMonth = new Date(year, month - 1, 1);
      selectedDate = `${year}-${String(month).padStart(2, '0')}-01`;
      document.querySelector('#tx-date').value = selectedDate;
      loadCalendar();
    });
    document.querySelector('#tx-date').addEventListener('change', event => {
      if (!event.target.value || window.location.pathname !== '/calendar') return;
      selectedDate = event.target.value;
      const [year, month] = selectedDate.split('-').map(Number);
      calendarMonth = new Date(year, month - 1, 1);
      loadCalendar();
    });
    async function refresh() {
      const tasks = [loadTransactions(), loadReport()];
      if (window.location.pathname === '/calendar') tasks.push(loadCalendar());
      await Promise.all(tasks);
    }
    document.querySelector('#transaction-form').addEventListener('submit', async event => {
      event.preventDefault();
      const message = document.querySelector('#message');
      const note = document.querySelector('#note');
      if (selectedCategoryRequiresNote() && !note.value.trim()) {
        showAlert(message, 'Vui lòng ghi rõ khoản chi khi chọn mục Khác.', 'error');
        note.focus();
        return;
      }
      try {
        const response = await fetch('/api/transactions', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            category_id: Number(document.querySelector('#category').value),
            amount: document.querySelector('#amount').value,
            note: document.querySelector('#note').value,
            tx_date: document.querySelector('#tx-date').value
          })
        });
        if (!response.ok) {
          const result = await response.json();
          if (result.error === 'other category requires a description') {
            note.focus();
            throw new Error('Vui lòng ghi rõ khoản chi khi chọn mục Khác.');
          }
          throw new Error('Dữ liệu không hợp lệ hoặc không thể lưu');
        }
        event.target.reset();
        document.querySelector('#note-counter').textContent = '0/200';
        document.querySelector('#tx-date').value = window.location.pathname === '/calendar'
          ? selectedDate : localDateString(new Date());
        await refresh();
        showAlert(message, 'Đã lưu giao dịch.', 'success');
      } catch (error) {
        showAlert(message, error.message, 'error');
      }
    });
    fetch('/api/auth/me').then(async response => {
      if (response.ok) {
        const user = await response.json();
        if (window.location.pathname === '/login') {
          window.location.replace('/');
          return;
        }
        await enterApp(user.username);
      }
    }).catch(() => {
      showAlert(document.querySelector('#auth-message'), 'Không thể kết nối. Vui lòng thử lại.', 'error');
    });
  </script>
</body>
</html>"""
