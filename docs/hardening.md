# Kiểm chứng hardening

Các lệnh dưới đây chạy từ thư mục gốc dự án. Kết quả ghi lại theo lần chạy trên Windows 11 + Docker Desktop (Linux/WSL2); nếu khác môi trường, thực hiện lại lệnh để cập nhật kết quả.

| Biện pháp | File cấu hình | Lệnh kiểm chứng | Kết quả thực tế |
|---|---|---|---|
| App chạy non-root UID 10001 | `app/Dockerfile` | `docker compose exec app id` | PASS — UID/GID 10001 (`appuser`). |
| Nginx dùng image unprivileged | `docker-compose.yml` | `docker compose exec nginx id` | PASS — user `nginx`, UID 101. |
| PostgreSQL chỉ ở mạng nội bộ, không publish cổng | `docker-compose.yml` | `docker network inspect expense-tracker_net-db`; `docker inspect $(docker compose ps -q db) --format '{{json .NetworkSettings.Ports}}'` | PASS — `net-db` có `Internal=true`; `5432/tcp` là `null` (không có host binding). Compose v5 `docker compose port db 5432` báo `invalid IP:0` do service không publish cổng. |
| Database không ra Internet | `docker-compose.yml` | `docker run --rm --network expense-tracker_net-db python:3.12-slim python -c "import socket; s=socket.socket(); s.settimeout(3); s.connect(('1.1.1.1',443))"` | PASS — kết nối báo `Network is unreachable`. |
| Secret không được commit | `.gitignore`, `.env.example` | `git ls-files`; `git check-ignore .env`; `rg -n -i '(password|passwd|secret)\\s*[:=]\\s*"?[A-Za-z0-9_]{16,}' -g '!*.example' .` | PASS — chỉ `.env.example` được track; `.env` bị ignore; không có literal password assignment trong source/config. |
| Tài khoản app không được DDL | `db/01-init.sh` | Kết nối bằng `APP_DB_USER`, thử `DROP TABLE transactions` | PASS — PostgreSQL từ chối vì role app không sở hữu bảng; role chỉ được CRUD giao dịch và SELECT/INSERT user account. |
| Xác thực người dùng và tách dữ liệu | `app/app.py`, `db/01-init.sh` | Thử API không đăng nhập; tạo hai tài khoản rồi kiểm tra giao dịch/báo cáo chéo | PASS — API dữ liệu yêu cầu phiên; tài khoản thứ hai không đọc/xóa giao dịch hay tổng báo cáo của tài khoản thứ nhất. Mật khẩu băm; cookie Secure/HttpOnly/SameSite. |
| HTTPS và security headers | `nginx/default.conf` | `curl -kI https://localhost:8443/` | PASS — có X-Frame-Options, X-Content-Type-Options, Referrer-Policy, HSTS và CSP. |
| Giảm bề mặt công khai | `docker-compose.yml`, `nginx/default.conf` | `docker compose port pgadmin 80`; `docker compose port grafana 3000`; `docker compose port prometheus 9090`; `curl -k https://localhost:8443/metrics`; kiểm tra `Server` header | PASS — các trang quản trị bind `127.0.0.1`, `/metrics` trả 403, Nginx ẩn phiên bản. |
| App/Nginx filesystem chỉ đọc, capability tối thiểu | `docker-compose.yml` | `docker compose exec --user 0 app touch /x`; `docker compose exec --user 0 nginx touch /x` | PASS — ghi vào root filesystem báo `Read-only file system`; `/tmp` là tmpfs để Gunicorn/Nginx dùng file tạm. |
| PostgreSQL không nâng quyền | `docker-compose.yml` | `docker inspect expense-tracker-db-1` | PASS — bật `no-new-privileges`; không dùng `cap_drop: ALL` vì PostgreSQL cần capability khi khởi tạo. |

## Giới hạn đã biết

- cAdvisor và Promtail cần mount các đường dẫn hệ thống/Docker socket để thu thập số liệu và log, vì vậy không thể hardening hoàn toàn như app. Mount được đặt `:ro` và các exporter không publish port ra host. Mount socket `:ro` chỉ giới hạn ghi filesystem, **không** biến Docker API thành API chỉ đọc.
- Trên Docker Desktop/WSL2 cAdvisor có thể không xuất nhãn `name`, hoặc cảnh báo container cũ không tìm thấy mount layer. Dashboard nhóm CPU theo nhãn `id`; nếu cAdvisor không hoạt động, dùng Linux host/metrics runtime tương thích hơn.
- Nginx dùng cổng host 8080/8443 do 80/443 đã bị một dịch vụ khác chiếm. Chứng chỉ là tự ký, chỉ dành cho demo local.
