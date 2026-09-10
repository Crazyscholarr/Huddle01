# Pipeline kho truyện → kế hoạch sản xuất YouTube

Hệ thống này nằm trực tiếp trong AutoDubVN. Có thể tìm và tải bài ngay trong
GUI, nhận file JSON do `pixel-acre-brook-comet` xuất ra, hoặc nhập một cơ sở dữ
liệu SQLite. Dữ liệu được lưu cục bộ trong `data/content_ideas.sqlite`; mỗi kết
quả được ghi ngay sau khi hoàn thành nên có thể tiếp tục an toàn nếu mạng ngắt
hoặc ứng dụng đóng.

## 1. Luồng dữ liệu

```text
Zhihu / Reddit / Fanqie / nguồn Việt
                 │
                 ▼
 Tìm & tải trực tiếp / JSON / SQLite
                 │
                 ▼
 Chuẩn hóa schema + giữ bản nguồn để kiểm toán
                 │
        ┌────────┴─────────┐
        ▼                  ▼
 Heuristic offline    NVIDIA / ZenMux / Gemini
        └────────┬─────────┘
                 ▼
 Cảm xúc · Hook · Twist · Chủ đề · Nhân vật · Thời lượng
                 ▼
 8 tiêu đề · 3 mô tả · 3 thumbnail · outline · series
                 ▼
        Xem trước và sửa thủ công
          ┌──────┴────────┐
          ▼               ▼
 Excel + JSON       AutoDubVN AI Story
```

## 2. Các tầng xử lý

### Tầng 1 — nhập và chuẩn hóa

- Tab **Tìm & tải nội dung** có đủ 15 preset từ khóa Trung, nhóm nguồn Zhihu,
  Trung Quốc, Việt Nam, Reddit và chế độ dán nhiều link cụ thể.
- Kết quả tìm chỉ chứa metadata/snippet; chỉ những bài tải được nội dung đủ dài
  mới được ghi vào SQLite. Không dùng truyện mẫu để thay kết quả mạng thất bại.
- Hệ thống đọc trước lượt xem/tương tác khi nguồn công khai số liệu, xếp lượt
  đọc cao xuống thấp và ghi rõ `Lượt đọc không công khai` nếu nguồn giấu số.
- Nội dung đã đọc trước được cache 30 phút trong RAM; bấm **Tải chuyện này**
  chuyển thẳng URL sang backend và lưu kho, không phải mở/copy/dán link.
- Link chuyển hướng của công cụ tìm kiếm được giải mã và kiểm tra cứng theo
  domain để không lẫn kết quả rác ngoài nguồn đã chọn.
- Cookie Zhihu tùy chọn chỉ tồn tại trong lượt tải, không ghi vào config/log.
- Tự nhận diện JSON dạng mảng hoặc các khóa `stories`, `items`, `data`,
  `results`, `records`.
- Tự dò bảng SQLite có cột nội dung nếu không chỉ định tên bảng.
- Tương thích trực tiếp các trường `titleOriginal`, `titleLocalized`,
  `rawContent`, `cleanedContent`, `localizedContent`, `sourceUrl`, `language`,
  `tags`, `wordCount` của `pixel-acre-brook-comet`.
- Giữ nội dung gốc riêng để kiểm toán nguồn; không dùng nguyên văn làm kịch bản.

### Tầng 2 — phân tích và lập kế hoạch

- Lớp offline luôn tạo được kết quả: cảm xúc, chủ đề, archetype, Hook/Plot
  Twist, thời lượng, mức dễ sản xuất, độ ưu tiên.
- Lớp AI bổ sung 8 tiêu đề đúng prompt Gốc Mít, 3 mô tả, 3 ý tưởng thumbnail, outline ba phần,
  nhân vật, từ khóa, tag, series và giờ đăng.
- Chạy song song 1–8 truyện (mặc định 3) và ghi từng kết quả vào SQLite.
- Nếu API chưa có key hoặc lỗi, truyện đó tự rơi về offline và ghi lý do ở
  trường `Lỗi AI`; cả lô không bị dừng.

### Tầng 3 — duyệt và triển khai

- Tab **Kho đã tải & phân tích** cho phép lọc, tick nhiều mục, sửa tiêu đề,
  mô tả, tag, series, outline và ghi chú.
- Khi bấm **Dùng trong AI Story**, URL chuẩn hóa và thời điểm dùng được lưu vào
  SQLite. Bộ lọc mặc định chỉ hiện chuyện chưa dùng; **Lịch sử đã dùng** cho
  phép xem lại, còn backend khóa tuyệt đối lần sản xuất thứ hai.
- Nút **Dùng trong AI Story** chuyển tiêu đề đã chọn, mô tả, tag và outline
  sang chế độ Video kể chuyện.
- Khi render xong, AutoDubVN đặt tên MP4 theo tiêu đề đã duyệt và tạo file
  `.youtube.json` cạnh MP4, chứa sẵn tiêu đề, mô tả, tag, outline và ID ý tưởng.
- Excel và JSON được xuất đồng thời để dùng trong AutoDubVN hoặc công cụ khác.

## 3. Provider AI và bảo mật khóa

Trong **Cài đặt → API Dịch & AI**, chọn một provider và dán khóa vào ô mật
khẩu. Không đặt khóa trong mã nguồn, file hướng dẫn, lệnh terminal hoặc commit.

- NVIDIA: base URL `https://integrate.api.nvidia.com/v1`, model mặc định
  `minimaxai/minimax-m3`.
- ZenMux: base URL `https://zenmux.ai/api/v1`, model mặc định
  `z-ai/glm-5.3-free`.
- Offline: không gọi mạng và không cần key.

Nút kiểm thử trong Cài đặt dùng cùng đường gọi với pipeline. Nếu model miễn phí
đổi tên hoặc nhà cung cấp hết hạn mức, sửa model ở giao diện rồi kiểm thử lại.

## 4. Prompt mẫu

Prompt phân tích yêu cầu JSON thuần với schema cố định: thể loại, cảm xúc,
Hook/Plot Twist 1–10, chủ đề, archetype, keyword, tag, series, 8 tiêu đề,
3 mô tả, 3 thumbnail, outline, nhân vật, mức dễ sản xuất, khuyến nghị và giờ
đăng. Prompt nhấn mạnh chỉ dùng mô-típ để sáng tạo truyện Việt mới, không dịch
hoặc sao chép nguyên văn.

Prompt tiêu đề áp dụng công thức:

```text
[MÓC CẢM XÚC] : [TÌNH HUỐNG CỤ THỂ VIẾT HOA] | [ĐUÔI TỪ KHÓA]
```

Mỗi tiêu đề tối đa 100 ký tự, không tên kênh, không số tập, không tiết lộ kết
thúc. Toàn bộ prompt thực tế nằm trong `autodub/content_pipeline.py` và được
chép vào sheet **Prompt mẫu** của workbook.

## 5. Cấu trúc Excel

- **Dashboard**: tổng số ý tưởng, số mục nên làm, Hook/Twist trung bình và biểu
  đồ theo series.
- **Kế hoạch sản xuất**: toàn bộ metadata, điểm, 8 tiêu đề, 3 mô tả, 3
  thumbnail, outline, nhân vật, trạng thái và lỗi AI. Ô vàng là ô chỉnh tay;
  điểm ưu tiên, thời lượng và khuyến nghị là công thức.
- **Cụm Series**: số ý tưởng và điểm trung bình theo series.
- **Dữ liệu gốc**: nội dung dùng phân tích và nội dung thô để đối chiếu.
- **Cấu hình**: trọng số Hook/Twist/dễ sản xuất và ngưỡng khuyến nghị.
- **Prompt mẫu**: prompt đánh giá, tiêu đề và mô tả.

## 6. Cài đặt và chạy

```powershell
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe scripts\content_pipeline.py run --input "D:\stories.json" --all
```

Chỉ phân tích offline:

```powershell
venv\Scripts\python.exe scripts\content_pipeline.py run --input "D:\stories.db" --provider heuristic --all
```

Các lệnh riêng: `import`, `analyze`, `export`, `sample`. Dùng `--limit 50` để
kiểm thử một lô nhỏ và `--concurrency 1` nếu provider giới hạn tốc độ.

Chạy hàng tuần bằng Windows Task Scheduler: trỏ chương trình tới
`scripts\CHAY_KE_HOACH_HANG_TUAN.bat` và truyền đường dẫn JSON/SQLite làm đối
số. Có thể bỏ đối số và điền `content_pipeline.weekly_input` trong
`config.yaml`, sau đó lập lịch lệnh Python `content_pipeline.py run --all`.

## 7. Nguyên tắc nguồn

- Nguồn Trung Quốc là tài liệu tham khảo để lấy mô-típ; phải đổi toàn bộ tên,
  địa danh, quan hệ, chi tiết, diễn biến và lời văn sang bối cảnh Việt.
- Bilibili ở Công cụ Video chỉ là nguồn tải **video nền**, không nằm trong kho
  tham khảo truyện.
- Luôn kiểm tra quyền sử dụng nguồn, không đọc nguyên văn bài báo/thread và
  không tuyên bố nội dung hư cấu là chuyện thật.
