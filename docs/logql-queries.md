# Truy vấn LogQL

Mở Grafana Explore, chọn datasource Loki rồi chạy các truy vấn sau:

1. `{service="app"}` — xem toàn bộ log của ứng dụng Flask.
2. `{service="nginx"} |= "POST /api/transactions"` — lọc request tạo giao dịch đi qua Nginx.
3. `{service="nginx"} |~ " (4|5)[0-9]{2} "` — tìm request Nginx có mã phản hồi 4xx hoặc 5xx.
4. `sum by (service) (count_over_time({service=~".+"}[5m]))` — đếm số dòng log trong 5 phút theo service.
5. `sum(rate({service="nginx"}[1m]))` — tốc độ log Nginx mỗi giây trong một phút.
