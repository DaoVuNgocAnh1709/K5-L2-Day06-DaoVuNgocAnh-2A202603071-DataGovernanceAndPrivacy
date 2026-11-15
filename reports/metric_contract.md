# Metric contract (khai báo bắt buộc — chốt ở phút 30, trước khi chạy eval)

> Các metric chuẩn đã được cài trong `src/metrics/tracks.py`. File này là nơi nhóm **cam kết** cách dùng chúng.
> Không đổi testset, threshold hay trọng số sau khi đã thấy kết quả eval.

| Mục | Khai báo của nhóm |
|---|---|
| Track | T04 · Sensor Sync Guard: Đồng bộ camera và IMU |
| Primary metric (tên đúng như evaluator in ra) | Pair Accuracy@50ms |
| Tăng tốt hay giảm tốt? | higher (càng cao càng tốt, tối ưu = 1.0) |
| Đơn vị đánh giá (image / region / pair / release / batch / case …) | frame (cặp frame-IMU) |
| Công thức + numerator / denominator | #frame có \|pred − GT\| ≤ 50 ms / #frame có GT partner (trên split eval) |
| Cách ghép prediction ↔ GT (IoU? id? tolerance?) | Khớp theo frame_id, tính khoảng cách thời gian \|imu_t_ms - gt_imu_t_ms\| ≤ 50 ms |
| Candidate pool cố định | 3,570 frames từ 6 segments (camera.csv) và 11,850 IMU samples (imu.csv) |
| Review budget B / K | B = 20 frames nghi ngờ nhất (đoạn dropout hoặc residual cao) |
| Seed | 42 |
| Threshold policy (chọn trên dev như thế nào) | Ước lượng độ trôi tuyến tính (linear drift) và độ lệch offset ban đầu qua Cross-correlation (grid 1ms) trên từng segment 20s; ngưỡng bắt dropout: khoảng cách mẫu IMU gần nhất > 50ms |
| Eval split (không dùng để tune) | segments 1, 3, 5 (theo segments.csv: split = eval, N = 1,770 frames) |
| Chỉ số lỗi xấu (miss / false alarm / false reject) | Bad-pair recall (frame dropout được đưa vào HOLD), % frames HOLD (valid frames) |
| Slices sẽ báo cáo (≥2) | segment=1,drift=0.5; segment=3,drift=2.0; segment=5,drift=2.0 |
| Cách báo độ bất định khi N nhỏ (Wilson CI / bootstrap) | Bootstrap 95% confidence interval cho Pair Accuracy và Median Residual |
| Ngày giờ chốt + người chốt | 2026-11-14 16:20 (UTC+7) · Dao Vu Ngoc Anh (Lead Data QA) |

Nếu baseline = 0 thì ghi rõ "baseline = 0" thay vì phần trăm cải thiện.
