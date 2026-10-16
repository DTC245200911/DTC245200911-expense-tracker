# Báo cáo kiểm thử tổng thể

Thực hiện trên Windows 11 + Docker Desktop Linux/WSL2 từ volume sạch: `docker compose down -v`, tạo lại chứng chỉ tự ký nếu cần, sau đó `docker compose up -d --build`. Không có `.env` trong Git; kiểm tra dùng file `.env` cục bộ.

| Mã | Mô tả | Lệnh/kiểm tra | Kỳ vọng | Kết quả |
|---|---|---|---|---|
| ENV-01 | Kiểm tra cấu hình Compose | `docker compose config --quiet` | Cấu hình hợp lệ | PASS |
| ENV-02 | Chạy toàn bộ stack từ volume sạch | `docker compose down -v`; `docker compose up -d --build` | Tất cả service khởi động | PASS |
| APP-01 | Health và danh mục | `curl -k https://localhost:8443/health`; `GET /api/categories` | `status=ok`, 6 danh mục seed | PASS |
| APP-02 | Giao dịch và báo cáo | POST amount `123.45`, GET list/report, DELETE | POST 201; báo cáo cộng đúng; DELETE 204 | PASS |
| APP-03 | Danh mục rỗng và dữ liệu ghi chú | GET `/api/report`; gửi ghi chú `<script>` | Đủ 6 danh mục tổng 0 sau dọn; dữ liệu trả về nguyên văn, giao diện dùng `textContent` | PASS |
| APP-04 | Payload âm | POST amount `-1` | HTTP 400 `{"error":"invalid payload"}` và log WARNING | PASS |
| AUTH-01 | Đăng ký, đăng nhập và phiên | Tạo tài khoản hợp lệ; đăng xuất; đăng nhập đúng/sai | Đăng ký 201, đăng xuất 204, đăng nhập đúng 200/sai 401; cookie Secure và HttpOnly | PASS |
| AUTH-02 | Bảo vệ và cô lập dữ liệu | Gọi API khi chưa đăng nhập; tạo hai tài khoản và thử đọc/xóa chéo | Chưa đăng nhập 401; tài khoản khác không thấy giao dịch/tổng báo cáo và không xóa được | PASS |
| AUTH-03 | Xác thực đăng ký | Mật khẩu quá ngắn và tên đăng nhập trùng không phân biệt hoa thường | HTTP 400 với mật khẩu yếu; HTTP 409 khi trùng tên | PASS |
| AUTH-04 | Cập nhật schema database đang có dữ liệu | Chạy `db/02-auth-migration.sql` với role app trong `.env` | Tạo bảng users, thêm owner_id trên giao dịch và cấp quyền cần thiết; chạy lặp lại an toàn | PASS |
| AUTH-05 | Thông báo đăng nhập đồng nhất | Để trống hai ô/từng ô; sai tên/mật khẩu, đảo trường, mật khẩu thiếu/thừa ký tự và khoảng trắng thừa ở từng ô | Mọi trường hợp HTTP 401 với đúng `{"error":"Sai mật khẩu"}` | PASS |
| AUTH-06 | Khóa đăng nhập sau nhiều lần sai | Gửi 6 lần sai từ cùng client IP | Lần 1–4 HTTP 401; lần 5–6 HTTP 429; body luôn `{"error":"Sai mật khẩu"}`, `Retry-After` giảm từ 30 giây | PASS |
| AUTH-07 | Chuyển hướng trang | Mở `/` không phiên, mở `/login`, rồi mở cả hai khi đã đăng nhập | Người chưa đăng nhập được chuyển tới `/login`; phiên hợp lệ chỉ hiển thị trang chính | PASS |
| CAL-01 | Thêm giao dịch theo ngày chọn | POST giao dịch với `tx_date` trong tháng hiện tại và tháng trước | HTTP 201; ngày trả về khớp ngày đã chọn | PASS |
| CAL-02 | Tổng hợp tháng và từng ngày | Tạo giao dịch thu/chi cô lập theo tài khoản ở hai tháng; GET `/api/calendar?year=...&month=...` | Tổng tháng và từng ngày cộng đúng, chỉ gồm giao dịch tài khoản hiện tại | PASS |
| CAL-03 | Giao dịch theo ngày | GET `/api/calendar/day?date=...` cho ngày có dữ liệu và ngày trống | Trả đủ giao dịch/ngày trống; chỉ dữ liệu của tài khoản đăng nhập | PASS |
| CAL-04 | Chọn tháng, năm và ngày | Mở `/calendar`; chọn tháng/năm, điều hướng tháng và chọn một ngày | Lịch đúng số ngày (bao gồm tháng nhuận), ngày chọn cập nhật form và chi tiết | PASS |
| CAL-05 | Payload ngày/tháng và xác thực | Gửi tháng 13, ngày sai định dạng; mở trang/API khi chưa đăng nhập | Payload sai HTTP 400; trang chuyển tới đăng nhập, API HTTP 401 | PASS |
| CAL-06 | Bố cục điện thoại | Mở lịch ở viewport 390px | Bảy cột lịch vẫn hiển thị, trang không tràn ngang | PASS |
| CHART-01 | Tổng dữ liệu của biểu đồ | Tạo giao dịch thu/chi ở nhiều ngày; đọc API tổng hợp và trang lịch | Tổng thu/chi khớp số đã tạo, chỉ lấy ngày trong kỳ và phần tử biểu đồ có trên trang | PASS |
| CHART-02 | Quy tắc responsive cho biểu đồ | Kiểm tra media query `max-width:480px` của trục, cột và phần chú giải | Chiều cao/nhãn thu nhỏ và phần chú giải căn theo vùng biểu đồ | PASS (kiểm tra CSS) |
| CAT-01 | Danh mục chi khác | Khởi tạo DB mới và chạy lại migration trên DB hiện có; gọi `GET /api/categories` | Có đúng một danh mục `Khác` loại `expense`, hiển thị trong lựa chọn giao dịch | PASS |
| CAT-02 | Mô tả bắt buộc cho khoản chi Khác | Gửi giao dịch Khác với ghi chú trống/chỉ có dấu cách và với mô tả hợp lệ; đổi danh mục trên form | Ghi chú trống bị chặn ở UI và API; mô tả hợp lệ được lưu; các danh mục khác vẫn để ghi chú tùy chọn | PASS |
| AMT-01 | Giới hạn số tiền | Kiểm tra ô nhập và gửi API với 999.99 và 1000 | Placeholder chỉ là “Số tiền”, ô không có nút điều chỉnh riêng; UI/API từ chối dưới 1.000 ₫, 1.000 ₫ được chấp nhận | PASS |
| DB-01 | Quyền DDL của role app | Thử `DROP TABLE transactions` bằng `APP_DB_USER` | PostgreSQL từ chối do không phải owner | PASS |
| WEB-01 | HTTPS, redirect và header | `curl -kI https://localhost:8443/`; `curl -I http://localhost:8080/` | HTTPS 200, đủ 5 header; HTTP 301 sang HTTPS 8443 | PASS |
| WEB-02 | Ẩn endpoint metrics | `curl -k https://localhost:8443/metrics` | HTTP 403 | PASS |
| WEB-03 | App không publish port | `curl http://localhost:5000/health` | Kết nối host thất bại | PASS |
| WEB-04 | CSP cho font giao diện | Mở trang qua HTTPS, kiểm tra stylesheet/font Google Fonts và CSP response header | Cho phép `fonts.googleapis.com` và `fonts.gstatic.com`; các nguồn khác tiếp tục bị giới hạn | PASS |
| ADM-01 | pgAdmin truy cập được | `curl http://localhost:5050/login` | HTTP 200 | PASS |
| MON-01 | Prometheus scrape | `GET /api/v1/targets` | Đủ 5 job, tất cả `up` | PASS |
| MON-02 | App/container/DB metrics | Gửi 200 GET `/api/report`, query `rate(app_requests_total[1m])`, `container_cpu_usage_seconds_total{id!=""}`, `pg_up` | Có rate >0; cAdvisor có series `id`; `pg_up=1` | PASS |
| MON-03 | Grafana provision | `/api/health`, `/api/datasources`, dashboard `expense-overview` | Health OK; 2 datasource; dashboard 7 panel | PASS |
| LOG-01 | Loki và Promtail | Loki `/ready`; `docker compose logs promtail` | Ready; không lỗi kết nối/đẩy log | PASS |
| LOG-02 | Năm truy vấn LogQL | Chạy 5 truy vấn trong `docs/logql-queries.md` qua Loki API | Cả 5 có kết quả; regex bắt đúng request 404 `/khong-ton-tai` | PASS |
| SEC-01 | Non-root và rootfs chỉ đọc | `docker compose exec app id`; kiểm tra `ReadonlyRootfs`, `CapDrop`; `touch /x` dưới user root trong container | App UID 10001, Nginx UID 101; ghi rootfs báo `Read-only file system` | PASS |
| SEC-02 | Database isolation | Kiểm tra `net-db`, `NetworkSettings.Ports`, TCP tới `1.1.1.1:443` từ network đó | Internal true; `5432/tcp=null`; egress `Network is unreachable` | PASS |
| SEC-03 | Secret và cổng quản trị | `git ls-files`, `git check-ignore .env`, `docker compose port` | `.env` không track; admin bind loopback | PASS |
| SEC-04 | Nginx không lộ phiên bản | Kiểm tra `Server` header HTTPS | Không có số phiên bản Nginx | PASS |

### Ghi chú kết quả thực tế

- Máy đã có container `sochi-nginx-1` dùng cổng 80/443; vì vậy dự án dùng `http://localhost:8080` và `https://localhost:8443`, không dừng dịch vụ có sẵn.
- Docker Compose v5 trả `invalid IP:0` cho `docker compose port db 5432` vì không khai báo host port; `docker inspect` xác nhận `{"5432/tcp":null}`.
- cAdvisor trên Docker Desktop/WSL2 trả metric có nhãn `id` thay vì `name`; dashboard CPU đã chuyển sang `id`. Có thể xuất hiện cảnh báo cAdvisor về layer của container cũ không còn tồn tại.
- Loki trả 503 trong thời gian ring khởi tạo ngay sau lần start sạch; `/ready` trả `ready` sau khi ingester lên, sau đó các truy vấn đều đạt.
- Chứng chỉ TLS là tự ký; các lệnh curl kiểm thử dùng `-k`.
- Đăng nhập mới được kiểm thử với hai tài khoản tạm; các giao dịch thử đã xóa sau kiểm thử. Tài khoản của người dùng thật cần đăng ký trên trang ứng dụng.

## Đối chiếu tiêu chí chấm

| Tiêu chí | Kết quả | Bằng chứng |
|---|---|---|
| GitHub có ít nhất 3 commit rõ nghĩa và README chạy được | FAIL — README và 5 commit local đã kiểm tra; chưa tạo remote/GitHub hoặc push theo yêu cầu không push | `git log --oneline`; clean-volume deployment ở ENV-02 |
| App + PostgreSQL ổn định, pgAdmin hoạt động | PASS | APP-01–04, DB-01, ADM-01 |
| Nginx reverse proxy, HTTPS và security headers | PASS | WEB-01–03 |
| Prometheus + Grafana dashboard web/container/DB | PASS | MON-01–03 |
| Loki + Promtail và ít nhất 2–3 truy vấn LogQL | PASS | LOG-01–02, 5 truy vấn đều có kết quả |
| Có ít nhất 3–4 biện pháp hardening | PASS | SEC-01–04 |
| Toàn hệ thống chạy bằng Docker Compose | PASS | ENV-01–02; 11 service chạy đồng thời |

FAIL duy nhất là phần tạo/push repo GitHub; cần chủ repo tạo repo remote và push thủ công như hướng dẫn ở README/yêu cầu bàn giao.
