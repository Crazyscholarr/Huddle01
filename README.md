# AutoDubVN — Tự động lồng tiếng Việt cho video

Đưa **1 video bất kỳ (kể cả tiếng Trung)** vào → nhận về **video nói tiếng Việt**, chạy **offline** trên máy bạn (i5‑13400F + RTX 3060). Pipeline:

> Tách audio → **nhận phụ đề (ASR)** → *(tùy chọn tách nhân vật)* → **dịch sang tiếng Việt (Gemini)** → **tổng hợp giọng nói + chống đè thoại** → **che sub gốc (blur) + render (NVENC)**.

---

## 1. Cài đặt (làm 1 lần)

1. Cài **Python 3.10 hoặc 3.11** (tích "Add to PATH").
2. Cài **ffmpeg**: tải ở https://www.gyan.dev/ffmpeg/builds/ → giải nén → thêm thư mục `bin` vào biến môi trường **PATH**.
3. Nhấp đúp **`install.bat`** (tự tạo venv, cài PyTorch CUDA + thư viện).
4. **Dịch:** mặc định dùng **tài khoản Gemini Pro qua trình duyệt Edge** — *không cần API key*. Lần đầu chạy, cửa sổ Edge mở ra để bạn **đăng nhập Gemini 1 lần** (nhớ luôn cho các lần sau). *(Không thích lái web thì có thể chuyển sang API key ở mục 5.)*

> Nếu card không phải NVIDIA hoặc chưa có CUDA: đặt `asr.device: cpu` và `asr.compute_type: int8` trong config (chậm hơn nhưng vẫn chạy).

## 2. Chạy — APP DESKTOP (khuyên dùng)

Nhấp đúp **`run_gui.bat`** → mở **cửa sổ ứng dụng riêng** trên máy (không phải tab trình duyệt). Lần đầu chạy nó tự cài thư viện giao diện.

App dùng **pywebview** + **WebView2** đã có sẵn trên Windows 10/11, nên **không cài thêm `.exe` nào** — hợp với máy bị Device Guard/WDAC chặn file lạ (Qt hay Electron thì dễ bị chặn).

Cửa sổ **không viền**, thanh tiêu đề tự vẽ với nút thu nhỏ / phóng to / đóng ở góc phải, kéo được ở vùng tiêu đề. Nút **Chọn video** mở **hộp thoại file thật của Windows** (chọn được nhiều video một lúc vào hàng đợi); nút **Chọn ảnh logo** cũng vậy. Việc xong thì bấm **📂 mở thư mục** ở ô hàng đợi để mở thẳng Explorer tới file kết quả.

**Ba lớp trên khung hình, kéo–thả và đổi kích cỡ bằng chuột:**

| Lớp | Là gì | Màu khung | Chỉnh được |
|---|---|---|---|
| 1 | Video gốc, sub tiếng Trung **cháy cứng** trong hình | — | không xoá được, phải phủ lên |
| 2 | **Vùng làm mờ / xoá logo** phủ lên sub gốc | đỏ / cam | kéo, đổi cỡ, chỉnh độ mờ 4–60 |
| 3 | **Phụ đề tiếng Việt** vẽ trên cùng | xanh lam | kéo, đổi cỡ, font, màu, viền, canh lề |

Bấm **Tự dò sub cứng** để máy tự khoanh đúng vùng chữ Trung (phân tích độ tương phản ngang + độ nhấp nháy theo thời gian qua ~24 khung hình mẫu), khỏi kéo tay.

Bật **Xem trước hiệu ứng** để thấy ngay vùng mờ và phụ đề Việt đúng font/màu/cỡ ngay trên video, trước khi xuất.

Bên phải có 7 thẻ: **Làm mờ · Phụ đề · Logo · Nhận dạng · Dịch · Giọng đọc · Xuất file**. Chạy từng bước riêng hoặc bấm **CHẠY TẤT CẢ**. Bảng **Sửa từng dòng** cho sửa bản dịch tại chỗ, bấm ▶ nghe để nhảy tới đúng thời điểm.

Phím tắt: `Space` phát/dừng · `←` `→` lùi/tới 5s · `Delete` xoá vùng đang chọn.

> Nếu app báo không mở được cửa sổ: máy thiếu **Microsoft Edge WebView2 Runtime** (Win11 có sẵn). Tải tại https://developer.microsoft.com/microsoft-edge/webview2/

## 3. Chạy — dòng lệnh (dự phòng)

Nhấp đúp **`run.bat`** → chương trình hỏi **đường dẫn video HOẶC link**.
- Dán **link Bilibili/YouTube** (`https://...`) → tự **tải bản gốc sạch (không watermark)** rồi lồng tiếng luôn.
- Hoặc kéo‑thả **file video** local vào cửa sổ.
- Hoặc: `run.bat "E:\Video\phim.mp4"` / `run.bat "https://www.bilibili.com/video/BVxxxx"`

Kết quả nằm trong `output\<tên video>\`:
- `*.vietsub_dub.mp4` — **video tiếng Việt hoàn chỉnh**
- `*.src.srt` — phụ đề gốc (đã nhận diện)
- `*.vi.srt` — phụ đề tiếng Việt (đã dịch)

> Bạn có thể **sửa tay** file `*.vi.srt` cho hay hơn rồi chạy lại — chương trình sẽ **dùng lại bản dịch đó** (bật sẵn `translation.reuse_existing: true`) và chỉ lồng tiếng lại.

---

## 4. Các lựa chọn quan trọng trong `config.yaml`

### Nhận phụ đề (ASR) — thiết kế CHỐNG MẤT ĐOẠN

**Vì sao không dùng WhisperX:** nó có 2 lỗi gốc khiến video 3 tiếng chỉ ra 15 phút sub. (1) VAD pyannote bị ép `pad_onset=pad_offset=0` nên cắt cụt và bỏ qua cả đoạn có tiếng. (2) Nghiêm trọng hơn: đường giải mã theo lô **không có temperature fallback**, và các tham số an toàn (`no_speech_threshold`, `compression_ratio_threshold`, `log_prob_threshold`) **không hề được đọc** — nên khi Whisper chỉ đọc câu đầu rồi ngắt, không có gì bắt lỗi; timestamp vẫn đủ dài mà chữ thì mất sạch phần giữa.

`asr.backend`:
- **`paraformer`** *(mặc định)* — FunASR Paraformer‑large + fsmn‑vad + ct‑punc. **Tốt nhất cho tiếng Trung** (CER ~1.7% so với Whisper ~10%), có timestamp câu gốc, chỉ ~2GB VRAM, xử lý file 3 tiếng an toàn trong một lần gọi.
- **`faster-whisper`** — đa ngôn ngữ. Đã bật **temperature fallback** (thứ WhisperX thiếu) + VAD Silero với `speech_pad_ms=400` (đệm thật quanh câu nói) + tắt `condition_on_previous_text`.
- **`sensevoice`** — rất nhanh nhưng timestamp thô (chỉ theo VAD), không hợp lồng tiếng.
- **`whisperx`** — chỉ để tương thích. Đã ép tham số an toàn nhưng **vẫn không khuyên dùng**.

**Ba lớp bảo vệ tự động:**
1. **Chuẩn hoá âm lượng** (`asr.loudnorm`) trước khi nhận, và tự phát hiện trường hợp trộn mono bị triệt tiêu (2 kênh ngược pha) để lấy riêng một kênh.
2. **Kiểm tra độ phủ** — so tổng thời lượng có lời với độ dài video. Thiếu là **báo động ngay**, không im lặng cho qua. Bạn sẽ thấy dòng `Độ phủ: ... (xx%)` mỗi lần chạy.
3. **Tự vá lỗ hổng** (`asr.rescue_gaps`) — dò các khoảng trống dài hơn `min_gap_seconds`, cắt riêng đoạn đó ra, nhận diện lại rồi ghép vào **đúng mốc thời gian tuyệt đối**. Đoạn nào thực sự im lặng (dưới `silence_db`) thì bỏ qua cho nhanh. Đây chính là thứ chữa dứt bệnh "mất đoạn giữa".

> Nguồn **tiếng Trung** → để nguyên `paraformer`. Tiếng khác → đổi sang `faster-whisper`.

**Nếu FunASR báo `paraformer-zh is not registered`** kèm dòng `'HubConfig' object has no attribute '_logged_out'`: đây là lỗi tương thích của `modelscope` ≥ 1.38 (nó tách phần hub sang gói `modelscope-hub` riêng và bị lệch phiên bản), **không phải lỗi cấu hình của bạn**. Dòng "not registered" chỉ là hệ quả — tải model thất bại nên FunASR không đăng ký được model. Cách sửa:

```
python -m pip uninstall -y modelscope modelscope-hub
python -m pip install "funasr==1.3.29" "modelscope==1.37.1"
```

(Bản trước của tài liệu nhắc tới tuỳ chọn `asr.hub: hf` — tuỳ chọn này **chưa được cài đặt trong code** nên đã bỏ khỏi hướng dẫn; cách sửa duy nhất hiện tại là ghim phiên bản như trên.)

Các dòng `ModuleNotFoundError` về `pytorch_wpe`, `einops`, `more_itertools`, `cn_tn` khi nạp FunASR là **vô hại** — chúng thuộc các mô-đun khác (tách giọng, SenseVoice) mà Paraformer không dùng. Muốn log sạch thì `python -m pip install einops more_itertools`.

### Khi nghe thấy "tiếng chạy trước hình"

Triệu chứng: mấy phút đầu khớp rất mượt, sau 1-2 phút giọng đọc bắt đầu đi trước, càng về sau càng lệch, và video rải rác nhiều khoảng lặng.

Có **hai nguyên nhân khác hẳn nhau** cùng cho ra triệu chứng này. Chạy `python tools/doi_chieu_dich.py` để biết mình đang gặp cái nào.

**Nguyên nhân 1 — câu dịch dài hơn ô thời gian.** Mốc bắt đầu từng câu vẫn đúng tuyệt đối (track hình và track tiếng chỉ lệch 14-32 mili giây), nhưng câu tiếng Việt không đọc kịp: TTS phải tăng tốc tới 1.6× rồi cắt cụt đuôi, đọc xong khi nhân vật vẫn đang nói. Đo trên một video 96 phút: bản dịch cần **98 phút** để đọc tự nhiên trong khi chỉ có **68 phút** ô trống — thừa 43%.

Đây là chuyện khó tránh chứ không phải lỗi: một chữ Hán nở ra trung vị **4,4 ký tự tiếng Việt** (đo trên 4 video thật), vì tiếng Việt ghi rời từng âm tiết. Cùng một nội dung, bản Việt luôn cần thêm khoảng 45% thời gian đọc.

Cách xử lý:

1. `translation.chars_per_sec` (mặc định 15) là số ký tự giọng đọc kịp phát trong 1 giây. Nếu vẫn lệch, hạ xuống 12-13.
2. `translation.shorten_long_lines: true` bắt những dòng vượt mốc và hỏi lại model để rút gọn vừa đủ. Bước này có bộ chặn: bản rút gọn phải giữ nguyên tên riêng, con số, và không được ngắn hơn một tỉ lệ sàn — nên không tái diễn kiểu hỏng nghĩa "mà bọn nó nói" thành "mà bọn nói".
3. Mỗi lần chạy, trước bước lồng tiếng chương trình in dòng `Ap luc doc: ...`. Nếu con số vượt 100% nhiều thì biết ngay là sẽ lệch, khỏi phải render xong mới phát hiện.

**Nguyên nhân 2 — nội dung trôi khỏi mốc thời gian của nó.** Nặng hơn nhiều và không sửa được bằng cách rút gọn câu. Ở đây lời thoại nói ra là *nội dung của một câu khác*, cách chỗ đúng của nó hàng chục giây. Đo bằng con số làm mốc neo, một video có tới **49% số mốc lệch quá 6 giây, chỗ nặng nhất lệch 86 giây**; trong khi video khác cùng bộ chỉ 3%.

Gốc rễ nằm ở ASR: `ct-punc` chấm câu sai chỗ, cắt ngang giữa từ ghép và giữa mệnh đề, cho ra những dòng vụn vô nghĩa như `操。季秋这里只？` hay `个妇女多嘴`. Model dịch nhận nguyên khối 15 dòng vụn đó, tự ghép lại theo nghĩa rồi rải nội dung ra 15 dòng theo ranh giới ngữ nghĩa của nó — vốn không trùng ranh giới thời gian của ASR. Sai lệch cộng dồn trong khối rồi trả về 0 ở đầu khối sau, đúng như cảm giác "vài phút đầu mượt, sau đó lệch dần".

Chương trình xử lý việc này ở hai chỗ, không cần bật gì thêm:

1. Trước khi dịch, `repair_asr_punctuation` gỡ dấu câu mà ASR chèn vào giữa từ ghép (`他，们` → `他们`, `他。们` → `他们`), rồi khâu gộp nhận diện dòng cụt qua hai dấu hiệu mà dấu chấm của ASR không nói lên được: dòng kết thúc bằng hư từ (`季秋这里只？`) và dòng mở đầu bằng hậu tố không bao giờ đứng đầu mệnh đề (`个妇女多嘴`, `们怎么`). Trên 4 video thật, tỉ lệ dòng vụn giao cho model dịch giảm từ 36% xuống 17%. Bước này chỉ nối chuỗi và mở rộng khung thời gian, không đánh rơi chữ nào.
2. Prompt dịch nói rõ mỗi dòng gắn với một mốc thời gian cố định, nên dòng `[k]` phải nói đúng nội dung dòng nguồn `[k]`, không kéo ý dòng trước xuống cũng không đẩy ý sang dòng sau — kể cả khi làm vậy nghe xuôi tai hơn.

**Nguyên nhân 3 — chia lại phụ đề bằng tỉ lệ số ký tự.** Đây là nguyên nhân âm thầm nhất, và là thứ khiến "sửa mãi không hết": nó không nằm ở bản dịch mà ở khâu **chia dòng**.

Pipeline gom nhiều mảnh ASR thành một ô để dịch cho có ngữ cảnh (ví dụ ô 3,6 → 17,8 giây, dài 14 giây), rồi chia bản dịch của ô đó trở lại thành 2-3 dòng cho vừa màn hình. Phép chia cũ chia **thời gian theo tỉ lệ số ký tự**, tức coi cả ô là một băng nói liên tục. Thực tế bên trong ô có quãng nghỉ, có tiếng động, có nhạc — nên mốc chia rơi vào chỗ không ai nói, và giọng Việt của phần sau phát sớm/muộn vài giây so với miệng nhân vật. Chuỗi này chạy tới ba lần liên tiếp (gom câu nguồn → chia lại bản dịch → tách theo dấu câu), sai số cộng dồn.

Cách chữa: **BẢN ĐỒ THOẠI** (`autodub/speechmap.py`). Paraformer trả timestamp theo **từng ký tự**, faster-whisper theo **từng từ** — trước đây dữ liệu này bị bỏ ngay sau khi lấy start/end của câu. Giờ toàn bộ mốc đó được gom thành một bản đồ dùng chung cho cả lần chạy, và mọi khâu chia lại phụ đề đặt ranh giới đúng vào khe giữa hai ký tự có thật, rồi hút thêm về quãng nghỉ gần nhất (người ngắt câu ở chỗ nghỉ, nên máy cũng ngắt ở đó). Đây cũng là cách VideoLingo dùng word-level timestamp của WhisperX để khỏi trôi phụ đề; khác là Paraformer đã cho sẵn mốc ký tự nên không phải nạp thêm model căn chỉnh nào.

Đo trên chính hai video thật, lấy mốc bắt đầu từng câu của ASR làm đối chứng (video 48 phút: 959 ranh giới; video 2 phút: 43 ranh giới):

| Sai số mốc bắt đầu dòng | Chia theo tỉ lệ (cũ) | Bản đồ thoại (nay) |
|---|---|---|
| Trung bình — video 48 phút | 0,36 s | **0,13 s** |
| Trung vị — video 48 phút | 0,26 s | **0,00 s** |
| Lệch quá 1 giây | 51 dòng | **19 dòng** |
| Lệch quá 2 giây | 8 dòng | **0 dòng** |
| Trung bình — video 2 phút | 0,73 s | **0,11 s** |
| Lệch quá 1 giây | 15 dòng | **0 dòng** |

Những điều nên biết khi dùng:

- Bản đồ được lưu thành `<tên video>.ban_do_thoai.json` trong thư mục output, nên các lần chạy sau (kể cả chỉ bấm riêng bước Dịch hay Dựng giọng đọc trong app) đều dùng lại được.
- Chạy lại trên phụ đề cũ mà **không còn** file bản đồ: chương trình tự dò vùng có tiếng từ audio bằng ffmpeg rồi dựng bản đồ thô. Loại này không biết từng ký tự nằm đâu nhưng biết chính xác chỗ nào im lặng, đủ để không chia phụ đề vắt qua quãng lặng.
- Tái dùng file `*.vi.srt` đã chia sẵn từ bản cũ: mốc thời gian trong đó được **neo lại** theo bản đồ, chữ giữ nguyên (kể cả phần bạn sửa tay). Log in `Da neo lai moc thoi gian cho N dong`.
- Backend không trả mốc nào (SenseVoice, WhisperX) thì log cảnh báo rõ và mọi thứ lùi về cách chia cũ — không vỡ luồng.
- Muốn so sánh trước/sau bằng chính video của bạn: đặt biến môi trường `AUTODUB_TAT_BAN_DO_THOAI=1` để tắt bản đồ rồi chạy lại.

Kiểm tra lại sau khi chạy:

- `python tools/kiem_tra_dong_bo.py` in thêm hai dòng mới: `dong Viet dat dung luc co tieng: N/M` và `lech so voi moc co tieng gan nhat`. Đây là phép đo trực tiếp bệnh trượt tiếng: dòng nào bắt đầu giữa quãng lặng là dòng đó đã bị đặt lệch. Hai video kiểm thử đạt 100% dòng nằm đúng chỗ có tiếng.
- `python tools/doi_chieu_dich.py` in mục `Do troi dat noi dung`. Dưới 10% số mốc lệch quá 6 giây là chấp nhận được; 40-50% thì bản dịch đã lệch pha, nên dịch lại.
- Mục `cau_goc_bi_cat` trong cùng báo cáo cho biết ASR còn cắt vụn nhiều hay không.
- Nếu nguồn có phụ đề sẵn thì nên dùng, vì tránh được hẳn nhóm lỗi này.

> Prompt đổi kéo theo `TRANSLATION_CACHE_VERSION` lên `v6`, nên lần chạy tới sẽ dịch lại từ đầu. Nhớ xoá `*.vi.srt` cũ, nếu không `reuse_existing: true` vẫn lồng tiếng bằng bản cũ.

### Hai công cụ kiểm tra sản phẩm

Cả hai đọc thẳng những gì đã có trong `output/`, không cần `ffprobe`, không gọi mạng. Bỏ trống tên thư mục thì chạy cho mọi video.

`python tools/kiem_tra_dong_bo.py` — soi phần **kỹ thuật**: độ dài track hình so với track tiếng, độ phủ thoại, áp lực đọc, mật độ chữ theo từng khung thời gian. Dùng khi nghi file xuất ra bị lệch hoặc thừa khoảng lặng.

`python tools/doi_chieu_dich.py` — soi phần **nội dung**, đặt bản gốc cạnh bản Việt theo trục thời gian. Ngoài phép đo trôi dạt nói trên, nó liệt kê: `mat_noi_dung` (dịch cụt mất vế), `bia_them` (model thêm thắt), `khong_dich`, `sot_chu_han`, `lech_con_so`, `mat_tu_latin`, `viet_hoa_la` (trả về kiểu Viết Hoa Từng Chữ), `cau_goc_bi_cat` và `lap_lai`. Thêm `--loai <ten_loai>` để chỉ xem một nhóm, `--so N` để đổi số ví dụ in ra.

> Quan trọng: `reuse_existing: true` khiến chương trình dùng lại file `*.vi.srt` cũ. Muốn hưởng bản dịch gọn hơn thì phải **xoá file `*.vi.srt`** rồi chạy lại, nếu không nó vẫn lồng tiếng bằng bản dài cũ.

### Chống đè thoại (phần cốt lõi)
Câu tiếng Việt thường dài hơn bản gốc và timestamp video dài dễ sát nhau. Chương trình tự **tăng tốc nói vừa đủ** để mỗi câu lọt khe trước câu kế tiếp, **không vượt** `tts.max_speed` (mặc định 1.6 = tối đa +60%). Nếu vẫn không đủ chỗ, nó **đẩy nhẹ các câu sau (cascade)** để **không bao giờ chồng giọng**.
- `tts.max_speed`: hạ xuống 1.4 nếu muốn nghe thong thả; nâng 1.8 nếu muốn bám sát mốc gốc hơn.
- `tts.min_gap`: khoảng nghỉ tối thiểu giữa 2 câu.

### Giọng nói & nhân vật
- `tts.voice_mode`:
  - `narrator` — 1 giọng kể (nhanh, gọn).
  - `alternate` — nam/nữ luân phiên.
  - `per-speaker` — mỗi nhân vật 1 giọng (cần bật diarization bên dưới).
- Giọng gốc: `vi-VN-NamMinhNeural` (nam), `vi-VN-HoaiMyNeural` (nữ). Chương trình tạo thêm biến thể (già/trẻ) bằng dịch cao độ để có **nhiều chất giọng**.

### Tách nhân vật (diarization) — tùy chọn
`diarization.enabled: true` + `hf_token` (free tại https://huggingface.co/settings/tokens, và bấm đồng ý điều khoản model `pyannote/speaker-diarization-3.1`). Cài thêm: bỏ `#` ở `pyannote.audio` trong `requirements.txt`.

### Tải video từ link (Bilibili/YouTube...)
Chỉ cần dán link khi chạy. Link Bilibili được tải bằng API trực tiếp, tự dò nhiều CDN và tải 4 khối song song; từng khối lỗi sẽ đổi CDN và file `.part` được nối tiếp. Nếu API trực tiếp không dùng được, chương trình tự lui về **yt-dlp**. YouTube và các nguồn còn lại vẫn dùng yt-dlp.
- `download.quality`: `best` | `1080` | `720` | `480` | `360`.
- Video Bilibili công khai không cần mở Edge hay đọc cookie trình duyệt. Bản nét bị khóa đăng nhập vẫn có thể dùng `download.cookies_file` (cookies.txt), hoặc đường lui yt-dlp với `download.cookies_from_browser: edge:Default`.
- Nếu video có **logo cháy cứng ở góc** (do người đăng chèn), yt-dlp không xoá được — dùng `video.delogo` bên dưới.

### Xoá logo cháy cứng ở góc
`video.delogo`: `null` = tắt; hoặc `"x:y:w:h"` (pixel) khoanh vùng logo, ví dụ `"1600:40:280:90"` cho logo góc phải‑trên video 1080p. ffmpeg sẽ xoá mờ vùng đó khi render.

### Che sub ngôn ngữ gốc bằng ảnh mờ
`video.blur_bottom_ratio`:
- `0.0` — **không che**, và khi đó video **không bị encode lại** (chỉ ghép tiếng, **render cực nhanh**).
- ví dụ `0.18` — làm mờ **18% dải đáy** màn hình (che phụ đề cháy cứng ở dưới). `blur_strength` chỉnh độ mờ.

### Âm gốc & phụ đề Việt
- `video.keep_original_db`: `null` = thay hẳn tiếng gốc; `-18` = giữ nền gốc rất nhỏ (nhạc/hiệu ứng).
- `video.hardsub_vietnamese: true` = ghi cứng phụ đề Việt lên hình.
- `video.use_gpu: true` = render bằng **h264_nvenc** (RTX 3060) cho nhanh.

---

## 5. Làm video kể chuyện từ file truyện TXT

### Luồng một nút: từ tiêu đề ra luôn video

Trong chế độ **📖 Video kể chuyện**, ô **0 — Tự viết truyện từ tiêu đề** nối trực
tiếp tới công cụ ở thư mục anh em `../Tạo kịch bản`. Chỉ cần nhập tiêu đề rồi bấm
**TẠO TRUYỆN → TẠO ẢNH → RA VIDEO**. Quy trình tự làm tuần tự:

1. Lập thiết kế và viết từng chương bằng công cụ Tạo kịch bản.
2. Xuất `KICH_BAN_DOC.txt` (chỉ có phần văn dành cho giọng đọc).
3. Rút 14 prompt ảnh bám theo 6 chương và lưu thành gói ảnh có manifest.
4. Mặc định mở Gemini Pro bằng profile đã đăng nhập, tự chọn **Tạo hình ảnh**,
   gửi từng cảnh, chờ và tải đủ ảnh; không cần API key. Chạy
   `login_gemini.bat` một lần trước khi dùng và đóng cửa sổ đăng nhập sau khi
   hoàn tất. Chế độ API chỉ chạy khi đặt `tao_anh.provider: api`.
5. Khi đủ ảnh mới tạo TTS → nhạc nền → phụ đề khớp giọng → video từ ảnh.

Không dùng `TOAN_BO.txt` làm đầu vào TTS vì file đó còn chứa bản thiết kế và báo
cáo kiểm tra. Có thể đổi vị trí công cụ hoặc Python trong mục `tao_kich_ban` của
`config.yaml`. Mặc định `require_quality_pass: true`: chưa đạt độ dài, tỉ lệ đoạn
đối thoại hoặc còn từ cấm thì hệ thống giữ file text để sửa nhưng chưa tốn TTS/render.
Công cụ viết truyện vẫn chạy độc lập như trước nếu cần.

Giao diện có **bộ chuyển 2 chế độ** ngay thanh trên: **🎬 Lồng tiếng video** (pipeline Trung→Việt) và **📖 Video kể chuyện**. Chế độ kể chuyện là một mặt bằng riêng 3 cột: **kho ảnh** bên trái (chọn nhiều ảnh một lúc, đánh số cảnh, đổi thứ tự), **khung xem trước** ở giữa (thấy ảnh + phụ đề mẫu đúng cỡ/màu/vị trí; dựng xong phát luôn video tại đây), và **5 bước** bên phải. Điểm mới so với bản cũ: **phụ đề cứng tự khớp mốc thời gian giọng đọc**, chọn khổ **ngang 16:9 hoặc dọc 9:16** (TikTok/Shorts), và nút **CHẠY TẤT CẢ** đi thẳng từ text ra video hoàn chỉnh (giọng đọc → nhạc nền → phụ đề → dựng ảnh).

Luồng này không dùng ASR hay dịch: bạn có sẵn nội dung tiếng Việt, chỉ cần đọc thành giọng rồi dán lên hình. Làm theo thứ tự từ trên xuống.

**Bước 1 — giọng đọc.** Dán nội dung hoặc bấm *Tải file văn bản* (nhận `.txt`, `.md`; tự nhận UTF-8, UTF-16 và CP1258 nên file lưu từ Notepad cũ vẫn đọc được). Tối đa 200.000 ký tự mỗi lần. Chọn bộ giọng, tốc độ rồi bấm *Tạo file MP3*. Khác với lồng tiếng video, ở đây giọng đọc giữ đúng tốc độ tự nhiên vì không phải ép cho khớp phụ đề.

Bấm **🔊 Nghe giọng đang chọn** để đọc thử **một câu** bằng đúng giọng, cao độ và tốc độ đang chọn. Truyện 12.000 từ đọc mất vài phút mới ra file, nên trước đây muốn biết giọng nào hợp thì phải tạo cả file rồi nghe, không hợp lại tạo lại — giờ đổi giọng trong danh sách rồi bấm nghe thử là so được ngay trong vài giây. Nếu ô nội dung đã có truyện thì nó đọc chính **đoạn đầu truyện của bạn** (cắt ở dấu kết câu, tối đa 320 ký tự), không thì đọc một câu mẫu có đủ dấu thanh và khẩu ngữ Nam Bộ. Bản nghe thử được nhớ theo (bộ giọng, giọng, cao độ, tốc độ, câu) trong `output/_nghe_thu/`, nên bấm lại đúng cấu hình cũ là phát tức thì, không gọi TTS lần nữa. Nút này cũng có ở thẻ **Giọng đọc** của chế độ lồng tiếng video.

Riêng CapCut, giao diện đọc trực tiếp toàn bộ catalog tiếng Việt trong `Voice.json`
(hiện có 24 giọng). Bấm **Mở thư viện giọng** để mỗi giọng có một nút nghe riêng.
Dấu `✓` là giọng đã phát thành công, dấu `✕` là giọng CapCut vừa trả lỗi; trạng thái
được lưu trong `output/_nghe_thu/capcut_voice_status.json` để lần sau không tự đề
xuất một giọng đang hỏng.

Nút **Phân tích & đề xuất giọng** đo thể loại, độ dài, mật độ đối thoại, sắc thái
gia đình/bí ẩn/kinh dị/chiêm nghiệm và trọng tâm nhân vật ngay tại máy, sau đó xếp
hạng 5 giọng thật đang có trong engine. Khi bật **Tự phân tích truyện và chọn giọng
kể phù hợp**, cả luồng từ tiêu đề lẫn truyện dán tay đều tự chọn giọng kể đứng đầu;
bạn vẫn có thể tắt để giữ nguyên giọng đã chọn thủ công.

Bật **Đa giọng: mỗi nhân vật một giọng** để có dàn giọng thật: lời dẫn luôn dùng
giọng kể, chỉ phần nằm trong dấu ngoặc kép/dấu gạch thoại mới chuyển sang giọng nhân
vật. Với truyện do công cụ **Tạo kịch bản** sinh ra, AutoDub đọc luôn mục `NHÂN VẬT`
trong `00_ban_thiet_ke.txt` để biết tên, tuổi, giới tính, quan hệ và sắc thái; với
truyện dán tay, chương trình tự dò tên gọi trong nội dung. Giọng được chấm theo giới
tính, nhóm tuổi (trẻ em/người trẻ/trưởng thành/lớn tuổi), chất trầm/sáng và độ hợp
đối thoại, đồng thời tránh giọng robot/demon/hiệu ứng. Một nhân vật giữ đúng một
voice id từ đầu đến cuối; tối đa 8 nhân vật nổi bật có giọng riêng.

Giao diện hiện trước **dàn nhân vật → giọng được chọn → số lượt thoại** và có nút
nghe thử từng vai. Những câu không đủ bằng chứng để biết chắc người nói sẽ dùng
giọng kể thay vì đoán bừa. Sau khi dựng, sơ đồ dàn giọng và tỉ lệ nhận diện được lưu
cạnh kết quả dưới dạng `*.giong.json`, thuận tiện kiểm tra hoặc tái dựng cùng dàn
giọng. Phụ đề vẫn lấy từ timeline clip thật nên tiếp tục khớp cả khi hai nhân vật có
tốc độ nói khác nhau.

> Đang chạy một việc dài (dựng giọng, xuất video) thì nút nghe thử tạm báo "đang bận" — hai luồng TTS cùng lúc dễ bị CapCut chặn rate-limit, còn VieNeu thì nạp model hai lần.

**Bước 2 — nhạc nền.** Chọn bài rồi bấm *Trộn nhạc nền vào giọng đọc*. Chưa có nhạc thì chương trình tự tải về vài bài.

Nhạc lấy từ Wikimedia Commons và **chỉ nhận giấy phép CC0 hoặc Public Domain**, tức dùng thương mại thoải mái, không phải xin phép cũng không bắt buộc ghi công. Nguồn, tác giả và đường dẫn gốc của từng bài được ghi lại trong `assets/nhac_nen/nguon.json` để bạn ghi công nếu muốn. Chương trình không ghim sẵn đường dẫn bài hát nào mà hỏi thẳng danh mục lúc chạy, nên không lo link chết dần theo thời gian. Muốn dùng nhạc riêng thì chỉ cần chép file vào `assets/nhac_nen/`.

Nếu Wikimedia trả `HTTP 429`, chương trình dừng tìm ngay và nghỉ 15 phút thay vì
quét tiếp cây danh mục. Video vẫn dùng nhạc đã có trong máy; tên bài dài luôn giữ
đuôi `.mp3`/`.flac` để kho nhận ra file vừa tải.

Về mức âm: `nhac_nen.muc_db` mặc định `-38`, nằm trong khoảng bạn muốn (-40 đến -35). Con số này là mức nhạc **sau khi đã chuẩn hoá**, không phải lượng giảm đi. Chương trình đo mức trung bình của từng bài rồi bù đúng lượng cần thiết, nên bài thu to hay thu nhỏ cuối cùng đều nghe bằng nhau — đây là điểm khác biệt so với việc giảm cứng một số dB.

`nhac_nen.duck: true` cho nhạc tự lùi lại mỗi khi có lời và trở lại lúc im. Đo trên bản dựng thử: đoạn có lời nhạc hạ khoảng 4 dB với `duck_ratio` thấp, khoảng 10-12 dB với mức mặc định `8`; đoạn không lời nhạc giữ nguyên.

**Bước 3 — hình.** Có hai đường:

- *Dựng video từ ảnh*: điền đường dẫn ảnh hoặc thư mục ảnh, mỗi dòng một mục. Ảnh được chia đều theo độ dài giọng đọc nên video luôn vừa khít, không thừa khung đen ở cuối. Ảnh lệch tỉ lệ khung hình được đặt giữa, hai bên lấp bằng chính ảnh đó làm nền mờ thay vì hai dải đen. Kiểu *ảnh trôi và phóng chậm* (Ken Burns) đỡ tĩnh nhưng phải encode nặng hơn; kiểu *ảnh đứng yên* dựng nhanh hơn nhiều, hợp video dài.
- *Ghép audio vào video đang chọn*: dùng khi đã có sẵn video, vẫn áp dụng cắt/làm mờ/logo/phụ đề như thường.

Video ra luôn là khổ **ngang 1920x1080, 30 hình/giây** — đúng chuẩn YouTube, kể cả khi ảnh đầu vào là ảnh dọc. Đổi trong `config.yaml` mục `slideshow` nếu cần khổ khác.

Không có bước AI nào trong đường này: ảnh của bạn được dùng nguyên vẹn xuyên suốt video, chương trình chỉ cắt khung, phóng to thu nhỏ và ghép nối bằng FFmpeg. Cụ thể số lượng ảnh được xử lý thế nào:

| Bạn đưa vào | Kết quả |
|---|---|
| 1 ảnh | Đúng một cảnh chạy suốt video, không cắt vụn |
| Ít ảnh, truyện dài | Quay vòng lại từ đầu, không tấm nào đứng im quá 25 giây |
| Nhiều ảnh, truyện ngắn | Dùng hết ảnh, trừ khi mỗi tấm sẽ ngắn hơn 2 giây thì lấy rải đều |
Chỉnh mặc định trong `config.yaml` ở hai mục `nhac_nen` và `slideshow`.

### Từ TIÊU ĐỀ ra kịch bản 12.000 từ (bộ prompt 4A - 4B - 4C)

Chỗ mất thời gian nhất của video kể chuyện không phải dựng hình mà là **có nội dung dài đủ**. Mọi prompt kiểu "viết cho tôi một truyện" đều trả về 3.000-4.000 từ, tức 25-30 phút đọc — vùng chết của ngách này, vì video trên 30.000 view đều dài từ 51 phút. Không có cách nào bắt model viết 12.000 từ trong một lượt, nên phải chia ba bước: **thiết kế trước, viết từng chương sau, kiểm tra cuối**. Mỗi chương 2.000 từ là mức model viết tốt trong một lượt; sáu chương ra 12.000 từ, đọc ở 135 từ/phút là 88-90 phút.

```bat
cd /d "E:\Video\Tạo kịch bản"
python run.py -t "Con dâu ở quê chăm mẹ chồng 8 năm, ngày chia đất bà Bảy đưa nó tờ giấy cũ"
```

Thư mục **Tạo kịch bản** hiện điều khiển Perplexity trong Chrome bằng phiên đăng
nhập riêng, không cần API key. Model, chế độ suy luận, hồ sơ trình duyệt, số từ mỗi
chương và việc chạy kiểm tra cuối được đặt trong `E:\Video\Tạo kịch bản\config.json`.
Lần đầu cần mở công cụ đó để đăng nhập; các lần sau luồng một nút dùng lại hồ sơ đã lưu.

Kết quả kịch bản nằm trong `E:\Video\Tạo kịch bản\output\<tên>\` (hoặc
`output_dir` đã đặt trong cấu hình của công cụ viết):

| File | Là gì |
|---|---|
| `00_ban_thiet_ke.txt` | Bản thiết kế 9 mục: câu hỏi cốt lõi, bối cảnh, nhân vật, nhịp leo thang và cú lật |
| `chuong_01..06.txt` | Từng chương, viết tiếp liền mạch bằng 200 từ cuối của chương trước |
| `KICH_BAN_DOC.txt` | **File đúng để nạp vào TTS**; luồng một nút tự lấy file này |
| `98_kiem_tra_tu_dong.txt` | Số từ, tỉ lệ đoạn đối thoại, từ cấm, đoạn dài/lặp và mốc chương do code đếm |
| `99_kiem_tra.txt` | Đánh giá biên tập bằng AI: logic, cú lật, giọng văn, kết truyện |
| `TOAN_BO.txt` | Hồ sơ đầy đủ gồm thiết kế, các chương và báo cáo; không đưa vào TTS |
| `thong_tin.json` | Metadata có cấu trúc để AutoDubVN và công cụ khác đọc |

Hai chỗ chương trình làm thay bạn, vì đây là hai lỗi khiến cả 12.000 từ thành vô dụng:

- **Tự bắt chương viết thiếu.** Sau mỗi chương nó đếm từ thật (bỏ dòng `[Số từ: xxx]` mà model tự khai, vì con số đó thường sai). Chương nào dưới 85% mục tiêu thì nhắc lại đúng câu "chương này chỉ có xxx từ, thiếu yyy từ, hãy thêm đối thoại và tình tiết, không thêm đoạn tả cảnh".
- **Đọc lại bản thiết kế để lấy đúng tên chương và số từ mục tiêu.** Model trả về đủ kiểu định dạng (in đậm, gạch ngang, nội dung chương nằm ngay sau tên, có bản trả cả bản thiết kế trong một chuỗi JSON với `\n` viết thành hai ký tự). Chỗ nào không đọc được thì lấy phân bổ mặc định 2.000/2.000/2.000/2.000/2.200/1.800 chứ không dừng cả quy trình.

Cuối cùng nó in bảng tóm tắt: tổng số từ, số phút đọc, mốc `mm:ss` từng chương, và cảnh báo nếu chưa đủ 12.000 từ hoặc còn từ trong danh sách cấm (`tổng tài`, `trọng sinh`, `thiên kim`...).

**Về ảnh.** Luồng từ tiêu đề tự rút prompt theo bản thiết kế, có thể gọi Gemini
Image API để tạo 14 ảnh. Cấu hình nằm trong `tao_anh`; API không khả dụng thì app
không chạy video thiếu hình mà hiện prompt trong cửa sổ riêng, đồng thời lưu tại
`output/story_image_packs/.../PROMPTS_AI_STUDIO.txt`. Ảnh thêm thủ công vẫn được
giữ đúng thứ tự trong `manifest.json` và rải theo từng chương.

Mỗi sản phẩm video kể chuyện được gom trong `output/<tên video>/` để dễ tìm; tên
được làm sạch các ký tự Windows không cho phép. Luồng hoàn chỉnh xuất video,
phụ đề và sơ đồ dàn giọng vào đúng thư mục mang tiêu đề đó.

---

## 6. Tối ưu tốc độ
- Không che sub (`blur_bottom_ratio: 0`) → video **copy nguyên luồng hình**, chỉ thay tiếng → nhanh nhất.
- Cần che/hardsub → bắt buộc encode lại, nhưng dùng **NVENC p4/hq** (nén tử tế, tua được) nên vẫn nhanh.
- `asr.batch_size` tăng lên 24–32 nếu còn VRAM (whisperx).
- Dịch và tổng hợp giọng chạy **song song nhiều luồng** (`tts.concurrency`).

### Khâu render nhanh tới đâu, và vì sao lại chọn cách đó

Đo trên RTX 3060, clip 1080p dài 3 phút, mỗi cấu hình chạy 4 lượt xen kẽ nhau để máy nóng lên đều rồi lấy trung vị:

| Việc phải làm | Trước | Sau |
|---|---|---|
| Ba vùng che + ghi phụ đề lên hình | 47,7 s | **28,2 s** |
| Một dải mờ ở đáy + ghi phụ đề lên hình | 38,3 s | **26,7 s** |
| Đổi HEVC sang H.264 (không đụng filter hình) | 15,0 s | **10,0 s** |

Hàng đầu là cấu hình hay dùng nhất. Chiếu sang một video thật dài 1 giờ 36 phút từng render mất 34 phút thì nay còn khoảng 20 phút.

Hai thay đổi đứng sau con số đó:

**Đổi bộ làm mờ.** `gblur` được thay bằng `avgblur`, ở cả dải mờ đáy lẫn các vùng che bạn khoanh trong giao diện. Nhìn bằng mắt gần như không phân biệt được (chỉ số SSIM giữa hai bản là 0,99) nhưng `avgblur` cộng dồn theo hàng và cột nên chi phí không tăng theo bán kính, rẻ hơn khoảng 3,8 lần. Mỗi vùng che là một lần lọc, nên càng khoanh nhiều vùng càng lợi.

**Giải mã trên GPU, nhưng chỉ đúng một trường hợp.** Khi không có filter hình nào, khung hình đi thẳng từ bộ giải mã sang bộ mã hoá mà không rời GPU lần nào (`-hwaccel cuda -hwaccel_output_format cuda`). Đây chính là trường hợp video tải về là HEVC và chỉ cần đổi sang H.264 cho Windows Photos mở được.

Đường này **không** được bật khi có mờ đáy hoặc hardsub, dù nghe qua thì tưởng lúc nào cũng lợi. Lý do là các filter đó chạy bằng CPU nên khung hình phải tải từ GPU về RAM rồi đẩy ngược lên, và tiền copy đắt hơn tiền giải mã tiết kiệm được: cùng clip trên, bật `-hwaccel cuda` làm thời gian tăng từ 26,7 s lên 40,8 s. Máy nào không nuốt được đường GPU thì chương trình tự lùi về cách thường, lỗi lộ ra ngay lúc dựng chuỗi filter nên chỉ mất vài giây.

### File xuất nhỏ hơn và tua được

Trước đây NVENC chạy `-preset p1 -tune ll` (chế độ livestream): không B-frame, GOP vô hạn. File hoạt hình 1080p ra khoảng 10–12 Mbps, và cả video chỉ có **một** khung hình khoá ở giây 0 — tua trên trình phát giật, cắt bằng `copy` thì kéo theo gần như cả file.

Đo trên RTX 3060, cắt 2 phút giữa video thật (AV1 1080p30, ba vùng che + phụ đề Việt cháy cứng, `cq` 20 như trên giao diện). Encode ra `null` 4 lượt xen kẽ, lấy trung vị cho thời gian; dung lượng đo trên file MP4:

| | Trước (`p1` + `ll`) | Sau (`p4` + `hq`, GOP 2 s, trần 8 Mbps) |
|---|---|---|
| Thời gian encode | 23,5 s | **24,1 s** (~5× realtime) |
| Dung lượng / bitrate | 178,8 MB · 11,9 Mbps | **113,8 MB · 7,6 Mbps** |
| Số khung hình khoá | 1 (chỉ giây 0) | **67** (mỗi 2 giây) |
| Cắt `copy` 2 giây tại phút 1 | 90 MB (phải lấy từ đầu file) | **4,0 MB** |

Thời gian gần như không đổi vì nút cổ chai vẫn là lọc CPU (`avgblur` + phụ đề), không phải NVENC. Chiếu sang video 1 giờ 36 phút từng nặng 7,5 GB: còn khoảng **4,8 GB**, tua/cắt bình thường. Trần 8 Mbps là mức YouTube khuyến nghị cho 1080p, tỉ lệ theo số pixel (720p thấp hơn, 4K cao hơn).

## 7. Dịch bằng tài khoản Gemini Pro (không cần API key)

**Vì sao không để Playwright tự mở trình duyệt:** khi Playwright khởi động trình duyệt, nó gắn cờ *automation*; Google phát hiện cờ này và **cấm đăng nhập** ("This browser or app may not be secure"). Không sửa selector nào lách được.

`translation.browser_mode` — 3 chế độ:

- **`attach`** *(mặc định, khuyên dùng)* — **gắn vào Edge THẬT của bạn** qua CDP. Trình duyệt do chính bạn mở nên không mang cờ automation → Google không chặn, và bạn **dùng luôn phiên Gemini Pro đã đăng nhập, không phải đăng nhập lại lần nào**. Chương trình sẽ nhắc bạn **đóng hẳn Edge** (kể cả icon dưới khay hệ thống) rồi tự mở lại ở chế độ gỡ lỗi. Nếu 2 lô liên tiếp thất bại, nó **tự chuyển sang chế độ thủ công** cho phần còn lại.
- **`manual`** — chắc chắn 100%, không dính chặn bot. Chương trình tự copy từng lô vào clipboard; bạn dán vào Gemini (Ctrl+V), đợi trả lời, bôi đen + Ctrl+C, quay lại bấm ENTER. Gõ `r` để copy lại câu hỏi, `s` để bỏ qua lô. Câu hỏi mỗi lô cũng được lưu ra file `lo_XXX_hoi.txt` trong thư mục output phòng khi clipboard hỏng.
- **`playwright`** — cách cũ (profile riêng). Chỉ dùng nếu bạn đăng nhập được.

Hoặc **`provider: gemini`** — dùng **API key** free tại https://aistudio.google.com/apikey, dán vào `gemini_api_key`. Ổn định nhất nhưng cần key.

Hoặc **`provider: nvidia`** — dùng **NVIDIA NIM** (build.nvidia.com): tạo key `nvapi-...` miễn phí (không cần thẻ), **một key dùng cho mọi model** trong catalog. Mặc định chạy `minimaxai/minimax-m3`; muốn thử model khác chỉ cần đổi `nvidia_model`. Tier free có thể chặn burst request nên chương trình đã đặt sẵn thời gian chờ 429 dài hơn cho provider này.

> Bản dịch được lưu ra `*.vi.srt`. Bạn **sửa tay** file đó rồi chạy lại thì chương trình dùng luôn bản đã sửa (`reuse_existing: true`), chỉ lồng tiếng lại.

## 8. Xử lý sự cố
- `NVIDIA loi: HTTP 404` khi chạy **dòng lệnh** với `provider: nvidia` → đã sửa. Bảng "provider nào lấy key/model nào" trước đây bị viết hai lần (một bản trong app, một bản trong `main.py`) và bản của `main.py` thiếu nhánh `nvidia`, nên nó gửi key + tên model của Gemini tới endpoint NVIDIA. Giờ cả hai đường dùng chung `translate.api_params_for_provider`.
- `Bản dịch còn tiếng Trung ở dòng X` → một lô dịch trả thiếu dòng đó. Chương trình **tự dịch lại đúng những dòng này** (tối đa 2 lần, ngay sau bước Dịch và trước TTS/xuất video); chỉ khi vẫn hỏng mới dừng và báo lỗi. Tắt bằng `translation.auto_retranslate: false`, hoặc sửa tay dòng đó trong bảng **Sửa từng dòng**.
- `Thiếu công cụ 'ffmpeg'` → chưa thêm ffmpeg vào PATH.
- `Chưa có GEMINI_API_KEY` → dán key vào `config.yaml`, hoặc chuyển `provider: browser`.
- Hết VRAM → `asr.model_size: medium` hoặc `compute_type: int8`.
- Muốn xem file tạm (audio/clip) → `output.keep_temp: true`.

---
*Cấu trúc:* `main.py` điều phối; `autodub/asr.py` (nhận phụ đề), `speechmap.py` (bản đồ thoại — mốc thời gian từng ký tự, chống lệch tiếng/hình), `translate.py` (dịch), `tts.py` (giọng + chống đè), `timeline.py` (thuật toán chống đè), `video.py` (ffmpeg/blur/render), `diarize.py` (tách nhân vật), `srt_utils.py` (SRT), `kich_ban.py` (bộ prompt viết kịch bản truyện audio), `nhac_nen.py` + `slideshow.py` (video kể chuyện).

*Kiểm thử:* `python -m unittest discover -s tests` (127 test, không gọi mạng, không cần ffmpeg).
