import logging
import os
import time
from decimal import Decimal, InvalidOperation

import psycopg2
from flask import Flask, Response, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
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
        user=os.environ["APP_DB_USER"],
        password=os.environ["APP_DB_PASSWORD"],
        connect_timeout=5,
    )


@app.before_request
def start_request_timer():
    request.request_started_at = time.perf_counter()


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
        ORDER BY t.tx_date DESC, t.id DESC
        LIMIT 100
    """
    with get_db_connection() as connection, connection.cursor() as cursor:
        cursor.execute(query)
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
    try:
        amount = Decimal(str(amount_value))
        category_id = int(category_value)
        category_is_integer = not isinstance(category_value, bool) and str(category_id) == str(category_value).strip()
    except (InvalidOperation, TypeError, ValueError):
        return invalid_payload()
    if (
        not amount.is_finite()
        or amount <= 0
        or amount > Decimal("999999999999.99")
        or not category_is_integer
        or not isinstance(note, str)
        or len(note) > 200
    ):
        return invalid_payload()

    with get_db_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM categories WHERE id = %s", (category_id,))
        if cursor.fetchone() is None:
            return invalid_payload()
        cursor.execute(
            """
            INSERT INTO transactions (category_id, amount, note)
            VALUES (%s, %s, %s)
            RETURNING id, category_id, amount, note, tx_date
            """,
            (category_id, amount, note),
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


@app.delete("/api/transactions/<int:transaction_id>")
def delete_transaction(transaction_id):
    with get_db_connection() as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM transactions WHERE id = %s RETURNING id", (transaction_id,))
        deleted = cursor.fetchone()
    if deleted is None:
        return jsonify(error="not found"), 404
    return "", 204


@app.get("/api/report")
def report():
    query = """
        SELECT c.id, c.name, c.type, COALESCE(SUM(t.amount), 0)::numeric(14, 2)
        FROM categories AS c
        LEFT JOIN transactions AS t ON t.category_id = c.id
        GROUP BY c.id, c.name, c.type
        ORDER BY c.type, c.name
    """
    with get_db_connection() as connection, connection.cursor() as cursor:
        cursor.execute(query)
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
    return Response(PAGE, mimetype="text/html")


PAGE = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quản lý chi tiêu</title>
  <style>
    body{font-family:system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;color:#202938}
    h1,h2{color:#14532d}form{display:flex;gap:.6rem;flex-wrap:wrap;margin-bottom:1.5rem}
    input,select,button{font:inherit;padding:.55rem}table{border-collapse:collapse;width:100%;margin:1rem 0 2rem}
    th,td{border:1px solid #d1d5db;padding:.55rem;text-align:left}th{background:#f3f4f6}
    button{cursor:pointer}.message{min-height:1.5rem}
  </style>
</head>
<body>
  <h1>Quản lý chi tiêu</h1>
  <form id="transaction-form">
    <select id="category" required aria-label="Danh mục"></select>
    <input id="amount" type="number" min="0.01" step="0.01" placeholder="Số tiền" required>
    <input id="note" maxlength="200" placeholder="Ghi chú (tối đa 200 ký tự)">
    <button type="submit">Thêm giao dịch</button>
  </form>
  <p id="message" class="message" role="status"></p>
  <h2>Giao dịch gần đây</h2>
  <table><thead><tr><th>Ngày</th><th>Danh mục</th><th>Ghi chú</th><th>Số tiền</th><th></th></tr></thead>
    <tbody id="transactions"></tbody></table>
  <h2>Báo cáo theo danh mục</h2>
  <table><thead><tr><th>Danh mục</th><th>Loại</th><th>Tổng</th></tr></thead>
    <tbody id="report"></tbody></table>
  <script>
    const money = value => new Intl.NumberFormat('vi-VN').format(value);
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
        select.append(option);
      }
    }
    async function loadTransactions() {
      const response = await fetch('/api/transactions');
      if (!response.ok) throw new Error('Không tải được giao dịch');
      const items = await response.json();
      const body = document.querySelector('#transactions');
      body.replaceChildren();
      for (const item of items) {
        const row = document.createElement('tr');
        for (const value of [item.tx_date, item.category_name, item.note || '', money(item.amount)]) {
          const cell = document.createElement('td');
          cell.textContent = value;
          row.append(cell);
        }
        const action = document.createElement('td');
        const button = document.createElement('button');
        button.textContent = 'Xóa';
        button.addEventListener('click', async () => {
          const result = await fetch(`/api/transactions/${item.id}`, {method: 'DELETE'});
          if (!result.ok) throw new Error('Không xóa được giao dịch');
          await refresh();
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
        for (const value of [item.category_name, item.type === 'income' ? 'Thu' : 'Chi', money(item.total)]) {
          const cell = document.createElement('td');
          cell.textContent = value;
          row.append(cell);
        }
        body.append(row);
      }
    }
    async function refresh() {
      await Promise.all([loadTransactions(), loadReport()]);
    }
    document.querySelector('#transaction-form').addEventListener('submit', async event => {
      event.preventDefault();
      const message = document.querySelector('#message');
      try {
        const response = await fetch('/api/transactions', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            category_id: Number(document.querySelector('#category').value),
            amount: document.querySelector('#amount').value,
            note: document.querySelector('#note').value
          })
        });
        if (!response.ok) throw new Error('Dữ liệu không hợp lệ hoặc không thể lưu');
        event.target.reset();
        await refresh();
        message.textContent = 'Đã lưu giao dịch.';
      } catch (error) {
        message.textContent = error.message;
      }
    });
    loadCategories().then(refresh).catch(error => {
      document.querySelector('#message').textContent = error.message;
    });
  </script>
</body>
</html>"""
