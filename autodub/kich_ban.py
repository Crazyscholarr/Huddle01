"""BỘ PROMPT VIẾT KỊCH BẢN TRUYỆN AUDIO - từ tiêu đề ra kịch bản 12.000 từ.

VÌ SAO PHẢI CHIA BA BƯỚC
========================
Mọi prompt "viết truyện một lượt" đều cho ra 3.000-4.000 từ, tức 25-30 phút đọc.
Đó là vùng chết của ngách kể chuyện: video trên 30.000 view của ngách này không
có cái nào dưới 51 phút. Không có cách nào bắt model viết 12.000 từ trong một
lượt, nên phải chia:

    4A  thiết kế truyện từ tiêu đề   - chạy 1 lần
    4B  viết từng chương             - chạy 6 lần, mỗi chương ~2.000 từ
    4C  kiểm tra và sửa              - chạy 1 lần

Sáu chương 2.000 từ ra 12.000 từ, đọc ở 135 từ/phút là 88-90 phút.

Module này giữ nguyên văn ba prompt đó (chỉ điền chỗ trống), thêm phần đọc lại
bản thiết kế để biết tên/số từ mục tiêu của từng chương, phần ĐẾM TỪ để tự bắt
chương viết thiếu, và bộ prompt sinh mô tả ảnh minh hoạ cho khâu dựng video.

Hàm `viet_truyen()` nhận vào một hàm `ask(prompt) -> str` nên chạy được với cả
Gemini trong trình duyệt (không cần API key), API key, hay bản giả lập khi test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# Tốc độ đọc dùng để quy ra thời lượng video. 135 từ/phút là mốc đo cho giọng
# AI ở 0,9x; đổi tốc độ đọc thì đổi con số này.
TU_MOI_PHUT = 135

# Phân bổ số từ theo bản thiết kế: 6 chương, tổng 12.000 từ, cú lật mặt ở chương 5.
SO_TU_MAC_DINH = [2000, 2000, 2000, 2000, 2200, 1800]

KHAN_GIA = """KHÁN GIẢ
Nam nữ từ 45 tuổi trở lên, phần lớn là phụ nữ, nhiều người ở quê hoặc gốc quê.
Họ nghe bằng loa điện thoại khi làm việc nhà hoặc trước khi đi ngủ. Họ thích giọng
văn mộc mạc, thích chi tiết đời thường cụ thể, và ghét văn hoa hoặc từ ngữ trẻ trung."""

TU_CAM = [
    "tổng tài", "trà xanh", "bạch nguyệt quang", "hào môn", "thiếu gia",
    "tiểu thư", "xuyên không", "trọng sinh", "hệ thống", "ngôn tình",
    "nữ phụ", "thiên kim",
]


# --------------------------------------------------------------------------- #
#  PROMPT 4A - thiết kế truyện từ tiêu đề (chạy 1 lần)
# --------------------------------------------------------------------------- #
def prompt_thiet_ke(tieu_de: str, ten_kenh: str = "Gốc Mít Kể Chuyện") -> str:
    return f'''Bạn là người viết truyện audio cho kênh YouTube "{ten_kenh}".

{KHAN_GIA}

TIÊU ĐỀ VIDEO: {tieu_de.strip()}

Nhiệm vụ của bạn lúc này KHÔNG phải viết truyện. Chỉ lập bản thiết kế.

Hãy trả về đúng 9 mục sau, không viết gì thêm.

1. CÂU HỎI CỐT LÕI
Tiêu đề đang đặt ra câu hỏi hoặc lời hứa gì với người nghe? Viết trong 1 câu.
Toàn bộ truyện phải trả lời đúng câu này, nếu không người nghe sẽ tắt giữa bài.

2. BỐI CẢNH
Một xóm hoặc ấp cụ thể ở Nam Bộ, tự đặt tên nghe thật (ví dụ xóm Cồn Gió, ấp Bàu Sen).
Nêu rõ: tỉnh nào, làm nghề gì, thời gian câu chuyện diễn ra trong bao lâu.

3. NHÂN VẬT
Từ 4 đến 6 người. Với mỗi người ghi: tên gọi kiểu Nam Bộ (ông Tám, bà Bảy, con Ba,
thằng Tư), tuổi, quan hệ, và MỘT vật hoặc thói quen riêng để người nghe nhận ra
ngay khi nhắc tới (ví dụ: cái radio cũ, tật bấm khớp tay, đôi dép nhựa xanh).

4. CÂU MỞ ĐẦU
Viết luôn câu đầu tiên của truyện, đúng một câu.
Câu này phải là biến cố sốc nhất hoặc mất mát rõ nhất của cả truyện.
CẤM mở đầu bằng tả gió, tả nhà, tả cảnh, hoặc giới thiệu nhân vật.

5. BA NHỊP LEO THANG
Ba sự việc, mỗi cái nặng hơn cái trước, cách nhau bằng thời gian.
Nhịp 1: một dấu hiệu nhỏ mà ai cũng cố làm như không thấy.
Nhịp 2: một lời nói dối để giữ hòa khí, có người bị tổn thương thật.
Nhịp 3: áp lực từ bên ngoài gia đình, khiến mâu thuẫn không thể che nữa.
Ghi rõ mỗi nhịp trong 3 dòng.

6. CÚ LẬT MẶT
Sự thật bị che giấu lộ ra. Phải là điều người nghe không đoán trước ở nhịp 1,
nhưng khi biết rồi thì nhìn lại thấy mọi thứ đều đã có manh mối.

7. KẾT VÀ CÂU ĐÚC KẾT
Kết theo lẽ nhân quả. Kèm một câu đúc kết mộc mạc, nói như người trong xóm nói,
không lên giọng dạy đời, không dùng từ Hán Việt.

8. SÁU CHƯƠNG
Với mỗi chương ghi: số chương, tên chương gợi tò mò (không đặt "Phần 1"),
3 dòng nội dung xảy ra trong chương, và số từ mục tiêu.
Phân bổ số từ: Chương 1: 2.000 / Chương 2: 2.000 / Chương 3: 2.000 /
Chương 4: 2.000 / Chương 5: 2.200 / Chương 6: 1.800. Tổng 12.000 từ.
Cú lật mặt phải nằm ở chương 5.

9. MƯỜI CHI TIẾT VẬT THỂ
Liệt kê 10 vật cụ thể của đời sống nông thôn Nam Bộ sẽ xuất hiện trong truyện
(kiểu: hộp thuốc huyết áp, chai dầu gió xanh, nền gạch bông, giường tre, cái mương,
tiếng ghe ngoài sông). Đây là thứ làm người nghe tin truyện là thật.'''


# --------------------------------------------------------------------------- #
#  PROMPT 4B - viết từng chương (chạy 6 lần)
# --------------------------------------------------------------------------- #
def prompt_chuong(thiet_ke: str, so_chuong: int, ten_chuong: str,
                  so_tu: int, duoi_chuong_truoc: str = "",
                  tong_chuong: int = 6) -> str:
    truoc = (duoi_chuong_truoc or "").strip() or "chưa có"
    return f'''Bạn đang viết chương {so_chuong} trên tổng {tong_chuong} chương của một truyện audio.

BẢN THIẾT KẾ TRUYỆN:
{thiet_ke.strip()}

200 TỪ CUỐI CỦA CHƯƠNG TRƯỚC (để viết tiếp cho liền mạch):
{truoc}

VIẾT CHƯƠNG {so_chuong}: {ten_chuong.strip()}
Độ dài bắt buộc: {so_tu} từ, sai lệch tối đa 10%.

QUY TẮC BẮT BUỘC

Ngôi kể và giọng
- Kể ở ngôi thứ ba, gọi nhân vật là ông Tám, bà Bảy, con Ba
- Câu ngắn. Dùng khẩu ngữ Nam Bộ tự nhiên: vô, hổng, nghen, bây, mắc gì
- Không dùng từ Hán Việt nặng, không dùng văn phong dịch máy

Mật độ nội dung
- Cứ 400 từ phải có MỘT tình tiết mới xảy ra. Không được có đoạn nào chỉ tả cảm xúc
  hoặc tả cảnh mà không có việc gì xảy ra
- Ít nhất 45% số đoạn phải là đối thoại. Đối thoại dễ nghe hơn tả rất nhiều
- Cứ 500 từ phải có ít nhất một vật thể cụ thể lấy từ mục 10 của bản thiết kế

Điều tuyệt đối cấm
- Không dùng: {", ".join(TU_CAM)}
- Không tiết lộ cú lật mặt trước chương 5
- Không tóm tắt lại chương trước ở đầu chương. Vào việc luôn
- Không viết lời dẫn kiểu "Chương này kể về..."

Kết chương
- Chương 1 đến 5 phải kết bằng một câu hoặc một hình ảnh khiến người nghe muốn
  nghe tiếp. Không kết bằng câu tổng kết đóng lại
- Chương 6 kết theo mục 7 của bản thiết kế

TRẢ VỀ
Chỉ phần văn của chương, mở đầu bằng dòng tên chương.
Dòng cuối cùng ghi: [Số từ: xxx] — tự đếm thật, không ước lượng.'''


def prompt_viet_bu(so_chuong: int, dang_co: int, muc_tieu: int) -> str:
    """Nhắc lại khi chương trả về ngắn hơn mục tiêu (mẹo trong bộ prompt gốc)."""
    thieu = max(0, muc_tieu - dang_co)
    return (f"Chương {so_chuong} chỉ có {dang_co} từ, thiếu {thieu} từ. "
            "Hãy viết lại đủ số từ bằng cách thêm đối thoại và thêm tình tiết, "
            "không thêm đoạn tả cảnh. Giữ nguyên mọi mốc nội dung đã có và vẫn "
            "kết chương đúng như yêu cầu trước. "
            "Dòng cuối cùng ghi: [Số từ: xxx] — tự đếm thật.")


# --------------------------------------------------------------------------- #
#  PROMPT 4C - kiểm tra và sửa (chạy 1 lần)
# --------------------------------------------------------------------------- #
def prompt_kiem_tra(kich_ban: str, tu_moi_phut: int = TU_MOI_PHUT) -> str:
    return f'''Đây là kịch bản truyện audio hoàn chỉnh:
{kich_ban.strip()}

Hãy kiểm tra theo 10 tiêu chí dưới đây. Với mỗi tiêu chí trả về ĐẠT hoặc KHÔNG ĐẠT
kèm bằng chứng cụ thể, và nếu không đạt thì viết luôn phần sửa.

1. Tổng số từ từ 12.000 đến 16.000. Ghi con số thật.
2. Câu đầu tiên của truyện là biến cố, không phải tả cảnh.
3. Có đủ ba nhịp leo thang, và nhịp sau nặng hơn nhịp trước.
4. Cú lật mặt nằm ở chương 5 và có manh mối gieo từ trước.
5. Tỉ lệ đoạn đối thoại từ 45% trở lên. Ghi tỉ lệ thật.
6. Không có đoạn nào dài hơn 400 từ mà không có tình tiết mới. Chỉ ra đoạn vi phạm.
7. Không chứa bất kỳ từ nào trong danh sách cấm.
8. Giọng văn nhất quán từ đầu tới cuối, không có chương nào đổi giọng.
9. Truyện trả lời đúng câu hỏi mà tiêu đề đặt ra.
10. Câu đúc kết cuối mộc mạc, không lên giọng dạy đời.

SAU ĐÓ trả về thêm:
- 6 mốc thời gian chương, tính theo tốc độ đọc {tu_moi_phut} từ mỗi phút, định dạng mm:ss
- 1 câu hỏi cho khán giả bình luận, viết cho người 45 tuổi trở lên dễ trả lời
- 3 tag riêng cho video này
- Gợi ý 6 chữ in trên thumbnail'''


# --------------------------------------------------------------------------- #
#  PROMPT ẢNH - mô tả ảnh minh hoạ cho khâu dựng video (bản tối ưu)
# --------------------------------------------------------------------------- #
# Vì sao prompt ảnh phải viết bằng tiếng Anh: các model sinh ảnh (Imagen,
# Flux, SDXL...) hiểu tiếng Anh tốt hơn hẳn, và chữ tiếng Việt trong prompt hay
# bị chúng vẽ THÀNH CHỮ trên ảnh. Vì sao phải có "style anchor" lặp lại nguyên
# văn ở mọi cảnh: đó là cách duy nhất giữ cho 12-20 tấm ảnh trông như cùng một
# bộ phim thay vì 12 phong cách khác nhau. Vì sao ép cỡ trung/cỡ xa và tránh
# cận mặt: mặt người do AI vẽ ở cận cảnh rất dễ méo, mà video kể chuyện thì
# người nghe chủ yếu nghe, ảnh chỉ cần đúng không khí.
NEO_PHONG_CACH_ANH = (
    "cinematic still, warm nostalgic color grading, soft natural light, "
    "35mm film look, subtle grain, shallow depth of field, "
    "rural Mekong Delta village in southern Vietnam, 1990s countryside, "
    "muted earth tones with warm highlights"
)

CAM_TRONG_ANH = (
    "no text, no letters, no captions, no watermark, no logo, "
    "no modern clothing, no smartphones, no cars, no skyscrapers, "
    "no close-up faces, no deformed hands, not anime, not cartoon, "
    "no Chinese or Korean architecture"
)


def prompt_anh(thiet_ke: str, so_canh: int = 14, kho: str = "16:9") -> str:
    """Prompt nhờ model biến bản thiết kế thành mô tả ảnh cho từng cảnh."""
    return f'''Bạn là người dựng hình cho video kể chuyện trên YouTube.

BẢN THIẾT KẾ TRUYỆN:
{thiet_ke.strip()}

Hãy viết {so_canh} mô tả ảnh (image prompt) để tôi đưa vào công cụ sinh ảnh, dùng
làm ảnh minh hoạ chạy suốt video. Yêu cầu:

- Mỗi mô tả viết bằng TIẾNG ANH, một đoạn liền, 30-45 từ, không đánh dấu đầu dòng
  con và không giải thích gì thêm.
- Bám theo mạch 6 chương: {so_canh} cảnh chia đều cho 6 chương theo đúng thứ tự
  thời gian của truyện.
- Mỗi mô tả phải là MỘT khung hình tĩnh có thể chụp được: nêu rõ chỗ nào, lúc
  nào (sáng/trưa/chiều/đêm), ai đang làm gì, và ít nhất một vật thể lấy từ mục 9
  của bản thiết kế.
- Mỗi mô tả phải TỰ ĐỦ NGHĨA khi đưa sang một cuộc chat mới: hễ nhân vật xuất
  hiện thì lặp lại tuổi, khuôn mặt, tóc và trang phục nhận dạng cố định của họ;
  không viết kiểu "same person/character as before".
- Cỡ cảnh: chỉ dùng toàn cảnh, trung cảnh hoặc cảnh qua vai. KHÔNG cận mặt.
  Người trong ảnh nhìn từ xa hoặc nhìn nghiêng, không nhìn thẳng ống kính.
- Kết thúc MỖI mô tả bằng đúng chuỗi này, không đổi chữ nào:
  "{NEO_PHONG_CACH_ANH}, aspect ratio {kho}"

Định dạng trả về, không thêm gì khác:
1. <mô tả cảnh 1>
2. <mô tả cảnh 2>
...
{so_canh}. <mô tả cảnh {so_canh}>

Cuối cùng, thêm đúng một dòng:
NEGATIVE: {CAM_TRONG_ANH}'''


# --------------------------------------------------------------------------- #
#  Đọc lại bản thiết kế + đếm từ
# --------------------------------------------------------------------------- #
@dataclass
class Chuong:
    so: int
    ten: str
    so_tu: int


_SO_TU_RE = re.compile(r"(\d[\d.,]*)\s*t[ừu]", re.IGNORECASE)
_DONG_CHUONG_RE = re.compile(
    r"^\s*(?:[-*\u2022]\s*)?(?:\*\*)?\s*ch[uư][oơ]ng\s*(\d{1,2})\s*(?:\*\*)?\s*[:.\-\u2013]\s*(.+)$",
    re.IGNORECASE)


def dem_tu(text: str) -> int:
    """Đếm từ như người Việt đếm: tách theo khoảng trắng, bỏ phần đánh dấu."""
    s = re.sub(r"\[\s*S[ốo]\s*t[ừu]\s*:?[^\]]*\]", " ", text or "", flags=re.I)
    s = re.sub(r"[*_#>`]+", " ", s)
    return len([w for w in re.split(r"\s+", s.strip()) if any(c.isalnum() for c in w)])


def _so_nguyen(raw: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", raw or "")
    return int(digits) if digits else None


def _ten_chuong_gon(phan_con: str) -> str:
    """Lấy TÊN chương ra khỏi phần còn lại của dòng.

    Model hay viết cả nội dung chương ngay sau tên trên cùng một dòng:
    "Chương 1: Những ngày ốm đau. Bà Bảy nằm liệt giường... Số từ mục tiêu:
    2.000 từ." Nếu lấy cả cụm đó làm tên thì prompt 4B nhận một tên chương dài
    ba dòng. Cắt ở dấu kết câu đầu tiên, hoặc ở dấu gạch/ngoặc dẫn vào số từ.
    """
    s = re.sub(r"(?:\*\*|__)", "", phan_con or "").strip()
    s = re.split(r"\s*[\u2013\u2014\-\(\[]\s*\d", s)[0]
    s = re.split(r"(?<=[^\d])[.!?;]\s+", s)[0]
    s = re.split(r"\s*S[ốo]\s*t[ừu]\s*(?:m[ụu]c\s*ti[êe]u)?\s*[:=]", s,
                 flags=re.IGNORECASE)[0]
    return s.strip(" .:-\u2013\u2014*\"'")[:90]


def doc_danh_sach_chuong(thiet_ke: str,
                         so_tu_mac_dinh: Optional[List[int]] = None) -> List[Chuong]:
    """Rút 6 chương (số, tên, số từ mục tiêu) từ mục 8 của bản thiết kế.

    Model trả về đủ kiểu định dạng ("Chương 1: Tên - 2.000 từ", có/không in đậm,
    số từ ở dòng dưới, hoặc cả bản thiết kế nằm trong một chuỗi JSON với ký tự
    xuống dòng viết thành hai ký tự \\n), nên chỗ nào không đọc được thì lấy
    phân bổ mặc định chứ không dừng cả quy trình.
    """
    mac_dinh = list(so_tu_mac_dinh or SO_TU_MAC_DINH)
    out: List[Chuong] = []
    raw = (thiet_ke or "").replace("\\r\\n", "\n").replace("\\n", "\n")
    dong = raw.splitlines()
    for i, raw in enumerate(dong):
        m = _DONG_CHUONG_RE.match(raw)
        if not m:
            continue
        so = int(m.group(1))
        if any(c.so == so for c in out) or not 1 <= so <= 12:
            continue
        phan_con = m.group(2).strip()
        ten = _ten_chuong_gon(phan_con)
        so_tu = None
        for nguon in [phan_con] + dong[i + 1:i + 5]:
            mm = _SO_TU_RE.search(nguon)
            if mm:
                so_tu = _so_nguyen(mm.group(1))
                if so_tu and so_tu >= 200:
                    break
                so_tu = None
        if not so_tu:
            so_tu = mac_dinh[len(out)] if len(out) < len(mac_dinh) else 2000
        out.append(Chuong(so=so, ten=ten or f"Chương {so}", so_tu=int(so_tu)))
    out.sort(key=lambda c: c.so)
    if not out:
        out = [Chuong(so=i + 1, ten=f"Chương {i + 1}", so_tu=w)
               for i, w in enumerate(mac_dinh)]
    return out


def duoi_200_tu(text: str, so_tu: int = 200) -> str:
    """200 từ cuối của chương trước, để prompt 4B viết tiếp cho liền mạch."""
    words = [w for w in re.split(r"\s+", (text or "").strip()) if w]
    return " ".join(words[-max(1, so_tu):])


def moc_thoi_gian(so_tu_tung_chuong: List[int],
                  tu_moi_phut: int = TU_MOI_PHUT) -> List[str]:
    """Mốc bắt đầu từng chương dạng mm:ss theo tốc độ đọc."""
    out: List[str] = []
    tong = 0.0
    for w in so_tu_tung_chuong:
        giay = tong / max(1, tu_moi_phut) * 60.0
        out.append(f"{int(giay // 60):02d}:{int(round(giay % 60)):02d}")
        tong += max(0, int(w or 0))
    return out


def tim_tu_cam(text: str) -> List[str]:
    low = (text or "").lower()
    return [t for t in TU_CAM if t in low]


def loc_van_chuong(text: str) -> str:
    """Bỏ mấy dòng phụ trợ model hay thêm, giữ đúng phần văn để đọc thành tiếng."""
    s = (text or "").strip()
    s = re.sub(r"\[\s*S[ốo]\s*t[ừu]\s*:?[^\]]*\]", "", s, flags=re.I)
    s = re.sub(r"^\s*(?:Chắc chắn rồi|Dưới đây|Đây là)[^\n]*\n", "", s, count=1,
               flags=re.I)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# --------------------------------------------------------------------------- #
#  Chạy cả quy trình: 4A -> 4B x6 -> 4C
# --------------------------------------------------------------------------- #
@dataclass
class KetQua:
    tieu_de: str
    thiet_ke: str = ""
    chuong: List[str] = field(default_factory=list)
    ten_chuong: List[str] = field(default_factory=list)
    so_tu: List[int] = field(default_factory=list)
    kiem_tra: str = ""
    prompt_anh: str = ""
    canh_anh: List[str] = field(default_factory=list)

    @property
    def kich_ban(self) -> str:
        return "\n\n".join(self.chuong).strip()

    @property
    def tong_tu(self) -> int:
        return sum(self.so_tu)

    @property
    def phut_doc(self) -> float:
        return self.tong_tu / TU_MOI_PHUT


def doc_danh_sach_canh(reply: str) -> List[str]:
    """Rút từng mô tả ảnh đã đánh số từ phản hồi."""
    out: List[str] = []
    for m in re.finditer(r"^\s*(\d{1,2})[.)]\s*(.+?)\s*$", reply or "",
                         re.MULTILINE):
        txt = m.group(2).strip()
        if len(txt) > 20 and not txt.upper().startswith("NEGATIVE"):
            out.append(txt)
    return out


def viet_truyen(tieu_de: str,
                ask: Callable[[str], str],
                so_chuong: int = 6,
                so_canh_anh: int = 14,
                kho_anh: str = "16:9",
                lam_kiem_tra: bool = True,
                lam_prompt_anh: bool = True,
                nguong_thieu: float = 0.85,
                so_lan_bu: int = 1,
                log: Optional[Callable[[str], None]] = None) -> KetQua:
    """Chạy trọn quy trình 4A -> 4B x N -> 4C bằng một hàm hỏi-đáp bất kỳ.

    `ask(prompt) -> str` có thể là Gemini trong trình duyệt, API, hay bản giả
    lập khi test. Chương nào trả về ngắn hơn `nguong_thieu` lần mục tiêu thì tự
    nhắc model viết bù (đúng mẹo trong bộ prompt gốc) tối đa `so_lan_bu` lần.
    """
    noi = log or (lambda _m: None)
    kq = KetQua(tieu_de=tieu_de.strip())

    noi("Bước 1/3 - thiết kế truyện từ tiêu đề (prompt 4A)...")
    kq.thiet_ke = (ask(prompt_thiet_ke(kq.tieu_de)) or "").strip()
    if not kq.thiet_ke:
        raise RuntimeError("Không nhận được bản thiết kế (prompt 4A).")

    danh_sach = doc_danh_sach_chuong(kq.thiet_ke)[:so_chuong]
    while len(danh_sach) < so_chuong:
        i = len(danh_sach)
        danh_sach.append(Chuong(so=i + 1, ten=f"Chương {i + 1}",
                                so_tu=SO_TU_MAC_DINH[i % len(SO_TU_MAC_DINH)]))

    duoi = ""
    for ch in danh_sach:
        noi(f"Bước 2/3 - viết chương {ch.so}/{so_chuong}: {ch.ten} "
            f"({ch.so_tu} từ)...")
        van = loc_van_chuong(ask(prompt_chuong(
            kq.thiet_ke, ch.so, ch.ten, ch.so_tu, duoi, so_chuong)))
        for _ in range(max(0, int(so_lan_bu))):
            dem = dem_tu(van)
            if dem >= ch.so_tu * nguong_thieu:
                break
            noi(f"  chương {ch.so} chỉ có {dem} từ (cần {ch.so_tu}) - "
                "nhắc model viết bù...")
            them = loc_van_chuong(ask(prompt_viet_bu(ch.so, dem, ch.so_tu)))
            if dem_tu(them) > dem:
                van = them
        kq.chuong.append(van)
        kq.ten_chuong.append(ch.ten)
        kq.so_tu.append(dem_tu(van))
        duoi = duoi_200_tu(van)

    if lam_kiem_tra:
        noi("Bước 3/3 - kiểm tra và sửa (prompt 4C)...")
        kq.kiem_tra = (ask(prompt_kiem_tra(kq.kich_ban)) or "").strip()

    if lam_prompt_anh:
        noi(f"Thêm: xin {so_canh_anh} mô tả ảnh minh hoạ...")
        kq.prompt_anh = (ask(prompt_anh(kq.thiet_ke, so_canh_anh, kho_anh))
                         or "").strip()
        kq.canh_anh = doc_danh_sach_canh(kq.prompt_anh)

    return kq


def bao_cao(kq: KetQua) -> str:
    """Bảng tóm tắt để in ra terminal sau khi viết xong."""
    dong = [f"Tiêu đề: {kq.tieu_de}",
            f"Tổng số từ: {kq.tong_tu:,} - đọc khoảng {kq.phut_doc:.0f} phút "
            f"(ở {TU_MOI_PHUT} từ/phút)"]
    mocs = moc_thoi_gian(kq.so_tu)
    for i, (ten, tu, moc) in enumerate(zip(kq.ten_chuong, kq.so_tu, mocs), 1):
        dong.append(f"  {moc}  chương {i}: {ten} - {tu:,} từ")
    if kq.tong_tu < 12000:
        dong.append(f"CẢNH BÁO: chưa đủ 12.000 từ (đang {kq.tong_tu:,}) - "
                    "video sẽ ngắn hơn 88 phút.")
    cam = tim_tu_cam(kq.kich_ban)
    if cam:
        dong.append("CẢNH BÁO: còn từ trong danh sách cấm: " + ", ".join(cam))
    if kq.canh_anh:
        dong.append(f"Mô tả ảnh: {len(kq.canh_anh)} cảnh")
    return "\n".join(dong)
