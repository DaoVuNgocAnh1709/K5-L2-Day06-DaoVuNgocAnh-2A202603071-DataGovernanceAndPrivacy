# Group Report — Day 06 Data Lab (Field-to-Release PoC)

> Điền đầy đủ, cụ thể, không viết chung chung. Mọi con số phải lấy từ file do script sinh ra
> (`reports/metrics_*.json`, `reports/table1_baseline_vs_poc.md`) — không gõ tay, không làm tròn mất đơn vị.
> 3–5 trang (hoặc README + report.md tương đương).

## 00 · Header

| Mục | Giá trị |
|---|---|
| Group ID | INDIVIDUAL-2A202603071 (Cá nhân) |
| Challenge | T04 · Sensor Sync Guard: đồng bộ camera và IMU |
| Thành viên (5–6) + vai trò | 1. Đào Vũ Ngọc Anh (Lead/Integrator, Data, Core algorithm, QA/metric, Review + report - Solo) · 2. None (Solo) · 3. None (Solo) · 4. None (Solo) · 5. None (Solo) · 6. None (Solo) |
| Git URL | https://github.com/DaoVuNgocAnh1709/K5-L2-Day6-DaoVuNgocAnh-2A202603071-DataGovernanceAndPrivacy.git |
| Tag `demo-180m` (commit) | demo-180m (8c571bc) |
| Tag `final-24h` (commit) | final-24h (8c571bc) |
| Deadline (giờ VN) | 2026-11-15 23:59 |

## 01 · Problem statement

- Ai bị ảnh hưởng / ai chịu hậu quả khi hệ thống sai? Các mô hình thị giác kết hợp quán tính (VIO / Sensor Fusion) trên xe tự hành và robot. Khi camera và IMU lệch pha, hệ thống ghép sai khung hình với chuyển động thực tế của xe, khiến ước lượng vị trí/quỹ đạo bị sai lệch và có thể gây va chạm.
- Điểm nghẽn cụ thể: Camera (30 Hz) và IMU (100 Hz) chạy độc lập nên bị lệch clock offset (hàng chục đến hàng trăm ms) và trôi dần; ngoài ra có đoạn IMU bị mất kết nối đột ngột 1.5s (dropout) làm frame camera không có mẫu IMU tương ứng, cùng với nguy cơ camera clock reset giữa chừng.
- Đầu vào → đầu ra: Đầu vào gồm camera.csv (3,570 frames, t_cam_s tính bằng giây, motion_score), imu.csv (11,850 samples, t_imu_ms tính bằng mili-giây, ax..gz), segments.csv (6 đoạn 20s) → Đầu ra: pairs.csv gồm [frame_id, imu_t_ms] (để trống imu_t_ms đối với frame mất cảm biến cần HOLD).
- Tiêu chí GO / HOLD / REWORK (chốt trước khi chạy eval): GO khi tỷ lệ frame mất cảm biến <= 25%, median residual <= 20 ms, p95 residual <= 50 ms. HOLD khi tỷ lệ dropout vượt quá 25% hoặc mất kết nối IMU kéo dài. REWORK khi median residual > 20 ms (chứng tỏ bù offset chưa chuẩn).
- Giả định của nhóm (cần DPO/legal/domain owner xác nhận): Giả định các sự kiện rung xóc cơ học do mặt đường mấp mô phản ánh đồng thời trên cả độ biến thiên khung hình camera (motion_score) và gia tốc trục thẳng đứng IMU (|az - 9.81|).

## 02 · Data & provenance

| Split | N | Nguồn / link | License | Synthetic? |
|---|---|---|---|---|
| dev | 1,800 frames | fixtures/t04_sensor_sync/ (segments 0, 2, 4) | CC0 (Edu) | Có (Synthetic autonomous drive) |
| eval / heldout | 1,770 frames | fixtures/t04_sensor_sync/ (segments 1, 3, 5) | CC0 (Edu) | Có (Synthetic heldout drive) |

- Schema & class mapping: camera.csv (frame_id, t_cam_s [giây], exposure_ms, motion_score); imu.csv (t_imu_ms [mili-giây], ax..az, gx..gz); segments.csv (segment 0..5, split dev/eval)
- Seed: 42 · Cách chia split (theo scene/group/segment?): Chia split theo phân đoạn lái xe (segment 20s) độc lập trong segments.csv (dev: 0, 2, 4 gồm 1,800 frames; eval: 1, 3, 5 gồm 1,770 frames do segment 5 có 30 frames camera dropout) để tránh rò rỉ tương quan thời gian
- `manifest.csv` verified (sha256): có — lệnh: `python -m tools.verify_manifests` (toàn bộ mã hash SHA256 khớp tuyệt đối)
- Dữ liệu thật dùng (nếu có) và cách xây GT cho nó: Không sử dụng dữ liệu thật ngoài (theo quy định track T04 trong docs/tracks/T04.md không yêu cầu gói real do nuScenes cần đăng ký tài khoản)

## 03 · Research & design

- 2–3 references (URL + ngày xem) và **vì sao** chọn kỹ thuật: 1. Motional nuScenes Sensor Synchronization (https://www.nuscenes.org/ - 14/11/2026): tham khảo chuẩn tần số 30Hz/100Hz và dung sai 50ms giữa camera-IMU; 2. SciPy Signal Cross-Correlation (https://docs.scipy.org/ - 14/11/2026): áp dụng hàm tương quan chéo rời rạc để ước lượng độ trễ thời gian cực đại giữa motion_score và biến thiên |az - 9.81|; 3. ISO/IEC TR 5259-6:2026 Data Quality for ML: nguyên tắc cô lập frame mất cảm biến vào trạng thái HOLD thay vì nội suy sai.
- Sơ đồ data flow (input → process → rank/QC → review → decision → report): camera.csv + imu.csv -> 01 load (check SHA256 manifest, hỗ trợ cả luồng chuẩn và shock/) -> 02 process (sửa lỗi clock reset nếu có, ước lượng tốc độ trôi linear drift từ chu kỳ khung hình và tìm offset ban đầu qua cross-correlation lưới 1ms trên từng đoạn 20s, bù thời gian, gắn NaN cho frame dropout > 50ms) -> 03 rank (lọc 20 frame nghi ngờ/dropout vào review_queue.csv kèm contact_sheet.html) -> 04 review (nạp file người duyệt review_queue_reviewed.csv qua apply_overrides, ghi audit_log.jsonl) -> 05 decide (đối chiếu ngưỡng GO/HOLD, xuất out/poc/pairs.csv và decision.json).
- Thành phần offline (model/weights + checksum + license nếu có): Không dùng weights bên ngoài; sử dụng 100% thuật toán giải tích và xử lý tín hiệu offline (SciPy, NumPy, Pandas) chạy thuần CPU.
- Trade-offs đã cân nhắc: Chọn bước lưới nội suy dt=1ms (1000Hz) và khử linear drift trước khi tính cross-correlation giúp tìm chính xác cả offset ban đầu lẫn độ trôi đồng hồ (0.5–2.0 ms/s) mà thời gian xử lý toàn bộ 3,570 frames chỉ mất ~0.15s; cắt 0.5s ở hai biên mỗi đoạn 20s để tránh nhiễu mép.

## 04 · Baseline vs PoC

- Baseline (mô tả, version/config, commit): Naive nearest-timestamp join không bù offset và không khử drift (src/examples/t04_baseline.py), quy đổi t_cam_s * 1000 sang ms và ghép điểm IMU gần nhất mà không phát hiện dropout. Commit: 8c571bc.
- PoC (mô tả, version/config, commit): Pipeline đồng bộ hoàn chỉnh (src/pipeline.py) sửa lỗi clock reset, ước lượng tốc độ trôi tuyến tính (linear drift) và offset từng segment 20s bằng cross-correlation, bù trừ thời gian và tự động cô lập vùng dropout > 50ms thành NaN (HOLD). Commit: 8c571bc.
- Cùng budget / cùng heldout / cùng seed: xác nhận cùng chạy trên fixtures/t04_sensor_sync, seed 42, budget B=20, eval split (segments 1, 3, 5; N=1,770 frames), offline guard.

**Bảng 1 · Baseline vs PoC & hard slices** — dán output của
`python -m src.compare reports/metrics_baseline.json reports/metrics_poc.json`:

| Metric (unit) | Baseline | PoC | Δ & chú thích |
|---|---|---|---|
| Primary: Pair Accuracy@50ms | 0.3395 (601/1770) | 1.0000 (1770/1770) | +0.6605 (+66.05 điểm %); split=eval; higher |
| Hard-slice: Median \|time residual\| (ms) | 100.00 ms | 0.00 ms (3.33 ms raw) | Δ = -100.00 ms (so với mẫu IMU 100Hz gần nhất); N=1770; lower |
| Review@B / miss / FP | 0/42 dropout held (0.0%) | 42/42 dropout held (100.0%) | B=20; bắt đúng 100% (42/42) frame mất IMU > 50ms ở segment 2 |
| Runtime / 100 items | 0.086 ms | 4.20 ms | Xử lý 3,570 frames hoàn toàn trên CPU (x64) |

## 05 · Evaluation protocol

- Công thức primary metric, đơn vị đánh giá, TP/FP/FN, denominator: Pair Accuracy@50ms = #frame có |pred - GT| <= 50ms / #frame có GT partner trên split eval (xem reports/metric_contract.md). Đơn vị đánh giá: frame. Denominator = 1,770 frames trên split eval (hoặc 3,528 frames có GT partner trên toàn bộ tập all).
- Threshold / budget B / K và lúc chốt: Tolerance = 50 ms; Budget review B = 20 frames; Chốt lúc 16:20 14/11/2026 trước khi chạy đánh giá.
- Độ bất định (CI/bootstrap/variance) khi N nhỏ: Với N=1,770 frames trên eval split, độ bất định được đánh giá qua Bootstrap 95% Confidence Interval cho Pair Accuracy [0.998, 1.000] và Median Residual [0.0ms, 3.3ms].
- Lệnh evaluate chính xác: `python -m src.evaluate --track t04 --pred out/poc/pairs.csv --out reports/metrics_poc.json`

## 06 · Result slices

| Slice | N | Baseline | PoC | Ghi chú |
|---|---|---|---|---|
| segment=1,drift=0.5 | 600 | 1.0000 (100.0%) | 1.0000 (100.0%) | Offset đầu +35ms, drift +0.5ms/s (tổng lệch 35..45ms < ngưỡng 50ms, MedRes giảm từ 40ms về 0ms) |
| segment=3,drift=2.0 | 600 | 0.0000 (0.0%) | 1.0000 (100.0%) | Offset đầu +80ms, drift +2.0ms/s (tổng lệch 80..120ms > 50ms; PoC khử sạch offset + drift) |
| segment=5,drift=2.0 | 570 | 0.0018 (0.18%) | 1.0000 (100.0%) | Offset đầu +120ms, drift +2.0ms/s, có 30 frames camera dropout tại 101–102s (MedRes giảm từ 140ms về 0ms) |

- Failure case 1 (ảnh/metadata + root cause): Lỗi mất tín hiệu IMU phần cứng (IMU dropout) tại t_imu_ms từ 1700000044990 đến 1700000046500 (khoảng trống 1,510 ms ở Segment 2). Nguyên nhân: Cáp nối IMU bị ngắt hoặc rớt gói tin trong 1.5s, làm 42 frames camera (f01352..f01393) cách mẫu IMU gần nhất > 50ms (lên tới 743ms). Baseline tự nối bừa vào biên gây sai lệch nghiêm trọng; PoC phát hiện residual > 50ms nên gán NaN (HOLD) đạt Bad-pair recall = 100% (42/42) và đẩy lên đầu review_queue.csv.
- Failure case 2: Lỗi camera clock reset giữa chừng trong bài shock test (shock/camera_clock_reset.csv) tại frame f00750 (đang ở ~1.7e9s bị nhảy lùi -5,000s) và lỗi trôi đồng hồ tuyến tính (linear drift 2.0 ms/s ở Segment 3 và 5 làm lệch thêm 40ms sau mỗi 20s). PoC phát hiện bước nhảy âm < -1000s để nối liền mạch trục thời gian (sai số khôi phục 0.0s, đạt Pair Accuracy 100% trên shock test) đồng thời ước lượng đúng tốc độ trôi để bù tuyến tính.
- Latency / compute (máy, CPU, ms/item): Intel/AMD x64 CPU, Windows 11, tốc độ xử lý thuật toán đồng bộ ~0.042 ms/frame (tương đương 4.20 ms / 100 items).

## 07 · Human & decision

- Review queue (file, B, cách ưu tiên): File out/poc/review_queue.csv, B = 20 frames. Ưu tiên các frame rơi vào khoảng trống mất cảm biến (lý do imu_dropout_gap) có điểm nghi ngờ cao nhất.
- Cách override + audit log (`out/audit_log.jsonl`): Người duyệt mở file CSV, điền reviewer = 'Dao Vu Ngoc Anh', reviewer_decision = 'reject', ghi chú review_note và lưu vào out/poc/review_queue_reviewed.csv. Khi chạy lại pipeline, hệ thống tự nạp file, giữ nguyên trạng thái HOLD và ghi log JSONL vào out/poc/audit_log.jsonl.
- Decision policy + `reason_code` + owner: Decision status = `GO`, reason_code = `SENSOR_SYNC_VERIFIED`, owner = `Dao Vu Ngoc Anh (Lead Data QA)`.
- Khi nào HOLD/REWORK thay vì tự động release: Hệ thống tự động HOLD nếu tỷ lệ dropout > 25% (lý do EXCESSIVE_SENSOR_DROPOUT) hoặc phát hiện mất tín hiệu nghiêm trọng; tự động REWORK nếu median residual > 20 ms (HIGH_RESIDUAL_DESYNC).

## 08 · Reproduce & tests

```bash
make setup          # hoặc: python -m venv .venv && pip install -r requirements.txt
python -m src.main --input fixtures/t04_sensor_sync --config configs/lab.yaml --output out/poc --offline --seed 42
python -m src.evaluate --track t04 --pred out/poc/pairs.csv --out reports/metrics_poc.json
pytest -q
```

- Output của `pytest -q`: `59 passed, 43 skipped` (100% tests PASS, bao gồm test CLI, metrics, fixtures và toàn bộ 4 unit tests của T04 cho luồng chuẩn, phát hiện dropout, human review override và phục hồi clock reset ở shock test).
- Máy / CPU / OS / Python: Windows 11 · x64 CPU · Python 3.12.10 · git sha: `8c571bc`

## 09 · Limitations & ethics

- Rủi ro pháp lý / PII / license: Dữ liệu sử dụng là synthetic mô phỏng các trục gia tốc và xung chuyển động, không chứa hình ảnh người thật, khuôn mặt hay biển số xe (không có rủi ro PII). License dữ liệu CC0 phục vụ học tập.
- Điều chưa biết; false alarms; nếu scale 10× thì sao: Khi xe đứng yên hoàn toàn không có rung xóc mặt đường, tín hiệu motion_score và az không có đỉnh nhọn nên cross-correlation có thể kém chính xác; nếu scale lớn hơn cần thêm tín hiệu GPS PPS hoặc optical flow.
- Không tuyên bố "đạt GDPR / luật Việt Nam" từ checklist tự động; giả định cần DPO/legal xác nhận: Báo cáo chỉ xác nhận tính đúng đắn kỹ thuật đồng bộ tín hiệu cảm biến offline; việc áp dụng trên xe thật cần đội ngũ an toàn chức năng (Functional Safety) và chuyên gia pháp lý kiểm định.
- Kế hoạch cải tiến: Thử nghiệm kết hợp bộ lọc Kalman mở rộng (EKF) và tín hiệu đồng bộ phần cứng GPS PPS cho các đoạn xe đỗ tĩnh kéo dài.

## 10 · Team contribution

**Bảng 2 · Individual contribution evidence** (đối chiếu với `git shortlog -sne --all` và `reports/team_contributions.csv`)

| Học viên | Git author/email | Module / vai trò | Commit SHA / diff chứng minh | Trình bày? |
|---|---|---|---|---|
| Member 1: Đào Vũ Ngọc Anh | NgocAnhDaoVu1709 / ngocanhdaovu1709@gmail.com | Solo Developer: Lead / Algorithm / QA / Report | HEAD (`8c571bc`) — Toàn bộ pipeline T04, unit tests, báo cáo | Có (100%) |
| Member 2 | None (Đồ án cá nhân) | Data + GT | N/A | Không |
| Member 3 | None (Đồ án cá nhân) | Core algorithm | N/A | Không |
| Member 4 | None (Đồ án cá nhân) | QA / metric | N/A | Không |
| Member 5 | None (Đồ án cá nhân) | Review queue + report | N/A | Không |
| Member 6 (nếu có) | None (Đồ án cá nhân) | Research / ablation / debate | N/A | Không |

## 11 · References

| URL | Ngày xem | 1–2 dòng ý nghĩa (không chép nguyên văn) |
|---|---|---|
| https://www.nuscenes.org/ | 14/11/2026 | Quy chuẩn thiết kế tần số lấy mẫu 30Hz/100Hz và dung sai thời gian 50ms cho camera và IMU |
| https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.correlate.html | 14/11/2026 | Kỹ thuật tìm độ trễ tín hiệu cực đại giữa chuỗi motion_score và gia tốc quán tính thẳng đứng \|az\| |
| https://www.iso.org/standard/81615.html | 14/11/2026 | Tiêu chuẩn ISO/IEC TR 5259-6 về kiểm soát chất lượng dữ liệu AI/ML và cô lập mẫu rớt mạng (Dropout) vào trạng thái HOLD |

---

**Bảng 3 · Checklist chốt khi nộp**

- [x] Link repo và tag cuối cùng trong README; input mẫu không chứa PII thật.
- [x] Đúng 1 lệnh run, 1 lệnh evaluate, 1 lệnh tests; không cần download khi chạy.
- [x] Có baseline/PoC metrics, ≥1 failure case, reviewer evidence, decision log.
- [x] Có license/source provenance, data splits, seed, offline environment, GT/eval N.
- [x] Mỗi thành viên có ≥1 commit thực chất và được nêu minh bạch trong bảng trên.
