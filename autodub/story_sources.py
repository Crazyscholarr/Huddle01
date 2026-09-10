"""Tìm và tải nguồn video cho chế độ Kể chuyện AI.

Nguồn tìm kiếm dùng extractor của yt-dlp thay vì tự gọi API Bilibili, nhờ vậy
vẫn dùng được cookies đăng nhập và không làm hỏng downloader hiện có.
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

import requests

from . import downloader
from .utils import log, run


# Các nguồn chỉ dùng để lấy tình huống, tiêu đề và từ khóa tham khảo. Luồng
# viết truyện luôn yêu cầu đổi nhân vật/bối cảnh/lời văn, không đọc lại nguyên
# văn bài nguồn.
REFERENCE_SOURCES = [
    {"key": "vnexpress_tamsu", "name": "VnExpress Tâm sự",
     "address": "https://vnexpress.net/tam-su", "type": "Việt - chuyện thật",
     "why": "Bạn đọc tự kể chuyện hôn nhân, gia đình, con cái.", "priority": "Cao",
     "notes": "Chỉ giữ tình huống; viết lại hoàn toàn.", "lang": "vi", "domain": "vnexpress.net"},
    {"key": "dantri_tinhyeu", "name": "Dân trí - Tình yêu Giới tính",
     "address": "https://dantri.com.vn/tinh-yeu-gioi-tinh", "type": "Việt - chuyện thật",
     "why": "Nhiều chất liệu tâm sự gia đình và người lớn tuổi.", "priority": "Cao",
     "notes": "Đổi tên, địa danh và toàn bộ cách kể.", "lang": "vi", "domain": "dantri.com.vn"},
    {"key": "webtretho_honnhan", "name": "Webtretho - Hôn nhân gia đình",
     "address": "https://www.webtretho.vn/f/chuyen-hon-nhan-gia-dinh", "type": "Việt - diễn đàn",
     "why": "Nhiều tình huống mẹ chồng nàng dâu, ngoại tình, ly hôn.", "priority": "Rất cao",
     "notes": "Nội dung diễn đàn cần biên tập nặng.", "lang": "vi", "domain": "webtretho.vn"},
    {"key": "phunuonline", "name": "Phụ nữ Online - Tuổi xế chiều",
     "address": "https://www.phunuonline.com.vn", "type": "Việt - chuyện thật",
     "why": "Tái hôn, cô đơn và tình cảm tuổi 60-70.", "priority": "Trung bình",
     "notes": "Chỉ dùng làm chất liệu tình huống.", "lang": "vi", "domain": "phunuonline.com.vn"},
    {"key": "youtube_comments", "name": "Bình luận khán giả kênh chuyện đời",
     "address": "Kênh Hương Quê Kể Chuyện; Tuổi Già An Nhiên", "type": "Việt - bình luận khán giả",
     "why": "Khán giả tự kể tình huống, đúng tệp tuổi và nhu cầu nghe.", "priority": "Rất cao",
     "notes": "Chỉ đọc để lấy mô-típ; không chép bình luận hay nhận diện người dùng.",
     "lang": "vi", "domain": "youtube.com"},
    {"key": "zhihu_yanxuan", "name": "知乎盐选故事 (Zhihu)",
     "address": "https://www.zhihu.com", "type": "Trung - truyện ngắn",
     "why": "Kho lớn về 婆媳, 中老年 và tranh chấp gia đình.", "priority": "Rất cao",
     "notes": "Bắt buộc Việt hóa mạnh; không sao chép bản dịch.", "lang": "zh", "domain": "zhihu.com",
     "search_suffix": "真实经历 盐选故事", "narrative_style": "Hook nhanh · tâm sự và truyện ngắn",
     "content_form": "short_story"},
    {"key": "douban_groups", "name": "豆瓣小组 · Chuyện đời thật",
     "address": "https://www.douban.com/group/", "type": "Trung - diễn đàn",
     "why": "Nhiều bài ngôi thứ nhất về gia đình, hôn nhân và cha mẹ.", "priority": "Rất cao",
     "notes": "Chỉ lấy tình huống; không sao chép lời kể hay thông tin nhận dạng.",
     "lang": "zh", "domain": "www.douban.com", "search_suffix": "小组 真实经历",
     "narrative_style": "Ngôi thứ nhất · trải nghiệm thật", "content_form": "experience",
     "direct_search": "douban_groups"},
    {"key": "douban_read", "name": "豆瓣阅读 · Truyện gia đình",
     "address": "https://read.douban.com", "type": "Trung - văn học chọn lọc",
     "why": "Truyện gia đình có lớp lang, nhân vật và góc nhìn văn học đa dạng.", "priority": "Cao",
     "notes": "Dùng cấu trúc xung đột; viết lại hoàn toàn thành đời sống Việt.",
     "lang": "zh", "domain": "read.douban.com", "search_suffix": "家庭故事 小说",
     "narrative_style": "Văn học · nhiều lớp nhân vật", "content_form": "literary",
     "direct_search": "douban_read"},
    {"key": "660i_story", "name": "660i 故事大全",
     "address": "https://660i.com/story", "type": "Trung - truyện ngắn",
     "why": "Có kho 民间故事 và 鬼故事 phù hợp nhánh tâm linh ông bà kể.",
     "priority": "Trung bình", "notes": "Chỉ lấy mô-típ dân gian; kể lại như truyền miệng và không cổ súy mê tín.", "lang": "zh", "domain": "660i.com",
     "search_suffix": "民间故事 鬼故事 老人讲", "narrative_style": "Dân gian · kỳ bí · ông bà kể", "content_form": "folklore"},
    {"key": "fanqie", "name": "番茄小说 (Fanqie)",
     "address": "https://fanqienovel.com", "type": "Trung - truyện dài",
     "why": "Truyện đô thị gia đình dài tập, phù hợp làm series.", "priority": "Thấp",
     "notes": "Chỉ dùng ý tưởng/mô-típ; cần cắt gọn và viết mới.", "lang": "zh", "domain": "fanqienovel.com",
     "search_suffix": "现实 家庭 小说", "narrative_style": "Dài tập · hook dày", "content_form": "serial"},
    {"key": "qidian", "name": "起点中文网 (Qidian)",
     "address": "https://www.qidian.com", "type": "Trung - truyện dài",
     "why": "Truyện hiện thực dài tập, có nhịp chương và tuyến nhân vật rõ.", "priority": "Trung bình",
     "notes": "Chỉ lấy cơ chế xung đột; bỏ mô-típ xa lạ với đời sống Việt.",
     "lang": "zh", "domain": "qidian.com", "search_suffix": "现实 家庭 小说",
     "narrative_style": "Dài tập · nhịp chương rõ", "content_form": "serial",
     "direct_search": "qidian_mobile"},
    {"key": "hongxiu", "name": "红袖读书 (Hongxiu)",
     "address": "https://www.hongxiu.com", "type": "Trung - truyện nữ/gia đình",
     "why": "Mạnh về hôn nhân, mẹ chồng nàng dâu, nuôi con và cảm xúc phụ nữ.", "priority": "Cao",
     "notes": "Giữ mâu thuẫn đời thường; bỏ ngôn tình quá đà và Việt hóa toàn bộ.",
     "lang": "zh", "domain": "hongxiu.com", "search_suffix": "现实 家庭 婚姻 小说",
     "narrative_style": "Nữ tính · tình cảm gia đình", "content_form": "serial",
     "direct_search": "hongxiu"},
    {"key": "qimao", "name": "七猫中文网 (Qimao)",
     "address": "https://www.qimao.com", "type": "Trung - truyện dài miễn phí",
     "why": "Truyện đại chúng có hook sớm, cao trào liên tục và số liệu độ phổ biến.", "priority": "Cao",
     "notes": "Ưu tiên bài có điểm/lượt đọc cao; không bê nguyên tình tiết hay thuật ngữ mạng.",
     "lang": "zh", "domain": "qimao.com", "search_suffix": "家庭 婚姻 小说",
     "narrative_style": "Đại chúng · hook nhanh · nhiều cao trào", "content_form": "serial",
     "direct_search": "qimao"},
    {"key": "zongheng", "name": "纵横中文网 (Zongheng)",
     "address": "https://www.zongheng.com", "type": "Trung - truyện dài",
     "why": "Bổ sung truyện hiện thực, phụng dưỡng và xung đột nhiều thế hệ.", "priority": "Trung bình",
     "notes": "Lọc kỹ thể loại; chỉ dùng chất liệu gia đình phù hợp khán giả 45+.",
     "lang": "zh", "domain": "zongheng.com", "search_suffix": "现实 家庭 养老 小说",
     "narrative_style": "Dài tập · xung đột nhiều thế hệ", "content_form": "serial",
     "direct_search": "zongheng"},
    {"key": "reddit_aita", "name": "Reddit r/AmItheAsshole",
     "address": "https://www.reddit.com/r/AmItheAsshole", "type": "Anh - chuyện thật",
     "why": "Xung đột gia đình có cấu trúc rõ và nhiều cú lật.", "priority": "Trung bình",
     "notes": "Phải Việt hóa mạnh bối cảnh và chuẩn mực ứng xử.", "lang": "en", "domain": "reddit.com"},
]

CHINESE_KEYWORDS = [
    {"keyword": "农村老人 灵异故事", "meaning": "Chuyện tâm linh do người già ở quê kể", "topic": "Tâm linh ông bà kể", "note": "Không khí làng quê, lời kể của ông bà", "source_group": "spiritual"},
    {"keyword": "老人讲 亲身经历 怪事", "meaning": "Người già kể chuyện lạ từng trải qua", "topic": "Tâm linh ông bà kể", "note": "Ưu tiên ngôi kể hồi tưởng, trải nghiệm đời xưa", "source_group": "spiritual"},
    {"keyword": "乡村怪谈 老人讲述", "meaning": "Chuyện kỳ bí làng quê do người già kể", "topic": "Tâm linh làng quê", "note": "Hợp giọng kể đêm khuya bên bếp lửa", "source_group": "spiritual"},
    {"keyword": "托梦 真实经历 老人", "meaning": "Người già kể chuyện báo mộng", "topic": "Báo mộng và tình thân", "note": "Giữ sắc thái truyền miệng, không khẳng định có thật", "source_group": "spiritual"},
    {"keyword": "民间因果报应 老人故事", "meaning": "Chuyện nhân quả dân gian của người già", "topic": "Nhân quả tâm linh", "note": "Ưu tiên bài học sống, không cổ súy mê tín", "source_group": "spiritual"},
    {"keyword": "奶奶讲的 鬼故事", "meaning": "Chuyện ma bà kể", "topic": "Chuyện bà kể", "note": "Mô-típ ký ức tuổi thơ và gia đình", "source_group": "spiritual"},
    {"keyword": "婆媳矛盾 故事", "meaning": "Mâu thuẫn mẹ chồng nàng dâu", "topic": "Mẹ chồng nàng dâu", "note": "Từ khóa gốc, dùng nhiều nhất"},
    {"keyword": "婆媳 真实经历", "meaning": "Trải nghiệm thật mẹ chồng nàng dâu", "topic": "Mẹ chồng nàng dâu", "note": "Tình huống người thật kể"},
    {"keyword": "赡养纠纷 故事", "meaning": "Tranh chấp phụng dưỡng cha mẹ", "topic": "Con cái bất hiếu", "note": "Tranh luận trách nhiệm chăm cha mẹ"},
    {"keyword": "儿女不孝 老人", "meaning": "Con cái bất hiếu, người già", "topic": "Con cái bất hiếu", "note": "Chủ đề xung đột mạnh"},
    {"keyword": "中老年情感故事", "meaning": "Chuyện tình cảm trung niên và cao tuổi", "topic": "Tình yêu xế chiều", "note": "Đúng tệp U60-U70"},
    {"keyword": "老年夫妻 感情", "meaning": "Tình cảm vợ chồng già", "topic": "Vợ chồng tuổi già", "note": "Có thể ghép nhân quả"},
    {"keyword": "老伴去世 再婚", "meaning": "Bạn đời mất, tái hôn", "topic": "Cha mẹ tái hôn", "note": "Con cái phản đối tái hôn"},
    {"keyword": "空巢老人 故事", "meaning": "Người già sống một mình", "topic": "Cô đơn tuổi già", "note": "Con đi xa, viện dưỡng lão"},
    {"keyword": "遗产 分家 纠纷", "meaning": "Tranh chấp thừa kế chia gia sản", "topic": "Tranh chấp thừa kế", "note": "Tình huống gia đình nhiều nút thắt"},
    {"keyword": "出轨 报应 中年", "meaning": "Ngoại tình và quả báo tuổi trung niên", "topic": "Nhân quả ngoại tình", "note": "Kết hợp với tuyến gia đình"},
    {"keyword": "农村 婆媳 故事", "meaning": "Mẹ chồng nàng dâu nông thôn", "topic": "Mẹ chồng nàng dâu", "note": "Dễ chuyển sang làng quê Việt"},
    {"keyword": "保姆 雇主 老人 故事", "meaning": "Người giúp việc và ông chủ già", "topic": "Giúp việc và người già", "note": "Tuyến đời thường giàu tình tiết"},
    {"keyword": "养老院 真实故事", "meaning": "Chuyện thật ở viện dưỡng lão", "topic": "Cô đơn tuổi già", "note": "Chất liệu cảm động"},
    {"keyword": "重男轻女 遗产", "meaning": "Trọng nam khinh nữ trong chia thừa kế", "topic": "Tranh chấp thừa kế", "note": "Hợp khán giả phụ nữ 45+"},
    {"keyword": "女婿 岳母 矛盾", "meaning": "Mâu thuẫn con rể và mẹ vợ", "topic": "Gia đình thông gia", "note": "Nhánh ít người làm, nên thử"},
    {"keyword": "黄昏恋 子女反对 故事", "meaning": "Tình yêu xế chiều bị con cái phản đối", "topic": "Tái hôn tuổi già", "note": "Xung đột thế hệ và cảm xúc rõ"},
    {"keyword": "退休夫妻 矛盾 真实经历", "meaning": "Mâu thuẫn vợ chồng sau nghỉ hưu", "topic": "Vợ chồng tuổi già", "note": "Đời thường, gần tệp U60-U70"},
    {"keyword": "兄弟姐妹 养老 分摊", "meaning": "Anh chị em chia trách nhiệm phụng dưỡng", "topic": "Con cái báo hiếu", "note": "Nhiều tuyến nhân vật đối lập"},
    {"keyword": "老人卖房 养老 子女", "meaning": "Người già bán nhà dưỡng già và con cái", "topic": "Nhà cửa tuổi già", "note": "Tài sản và quyền tự quyết"},
    {"keyword": "二婚家庭 财产纠纷", "meaning": "Tranh chấp tài sản gia đình tái hôn", "topic": "Tái hôn và tài sản", "note": "Hợp kịch bản nhiều cú lật"},
    {"keyword": "亲家 矛盾 彩礼", "meaning": "Mâu thuẫn hai bên thông gia và sính lễ", "topic": "Thông gia", "note": "Mở rộng ngoài mẹ chồng nàng dâu"},
    {"keyword": "拆迁款 兄弟姐妹 纠纷", "meaning": "Anh chị em tranh tiền đền bù", "topic": "Tranh chấp gia sản", "note": "Xung đột cụ thể, hook mạnh"},
    {"keyword": "农村留守老人 真实故事", "meaning": "Chuyện thật người già ở lại nông thôn", "topic": "Cô đơn tuổi già", "note": "Dễ chuyển sang làng quê Việt"},
    {"keyword": "保姆 遗嘱 老人", "meaning": "Người giúp việc, di chúc và người già", "topic": "Giúp việc và ông bà chủ", "note": "Bí mật và tranh chấp thừa kế"},
    {"keyword": "老年人被骗 养老钱", "meaning": "Người già bị lừa tiền dưỡng già", "topic": "Lừa đảo tuổi già", "note": "Chủ đề mới, có giá trị cảnh tỉnh"},
]

# Kết quả đọc trước khi xếp hạng được giữ ngắn hạn trong RAM. Khi người dùng
# chọn một bài, bước lưu SQLite dùng lại record này thay vì tải website lần hai.
_REFERENCE_CACHE: Dict[str, tuple] = {}
_REFERENCE_CACHE_LOCK = threading.Lock()
_REFERENCE_CACHE_TTL = 30 * 60
_REFERENCE_CACHE_MAX = 80


def _metric_number(value) -> int:
    text = str(value or "").strip().replace(",", "").replace(" ", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([万亿kKmM]?)", text)
    if not match:
        return 0
    number = float(match.group(1))
    multiplier = {"万": 10_000, "亿": 100_000_000,
                  "k": 1_000, "K": 1_000, "m": 1_000_000,
                  "M": 1_000_000}.get(match.group(2), 1)
    return max(0, int(number * multiplier))


def _text_metrics(text: str) -> Dict[str, int]:
    raw = str(text or "")
    read_patterns = (
        r"被浏览\s*(?:\*\*)?([\d,.]+\s*[万亿]?)",
        r"(?:阅读量|浏览量|热度|人气|点击量|总点击|views?|viewCount)\D{0,16}([\d,.]+\s*[万亿kKmM]?)",
    )
    vote_patterns = (
        r"([\d,.]+\s*[万亿]?)\s*人赞同",
        r"(?:点赞|总推荐|推荐票|收藏|voteup_count)\D{0,12}([\d,.]+\s*[万亿kKmM]?)",
    )
    comment_patterns = (
        r"([\d,.]+\s*[万亿]?)\s*条评论",
        r"comment_count\D{0,12}([\d,.]+\s*[万亿kKmM]?)",
    )
    reads = max((_metric_number(m.group(1)) for pattern in read_patterns
                 for m in re.finditer(pattern, raw, flags=re.I)), default=0)
    votes = max((_metric_number(m.group(1)) for pattern in vote_patterns
                 for m in re.finditer(pattern, raw, flags=re.I)), default=0)
    comments = max((_metric_number(m.group(1)) for pattern in comment_patterns
                    for m in re.finditer(pattern, raw, flags=re.I)), default=0)
    engagement = votes + comments
    return {"read_count": reads, "engagement_count": engagement}


def reference_catalog() -> List[Dict]:
    """Trả về bản sao catalog để API/UI không sửa dữ liệu gốc."""
    return [dict(row) for row in REFERENCE_SOURCES]


def chinese_keyword_catalog() -> List[Dict]:
    return [dict(row) for row in CHINESE_KEYWORDS]


def _clean_html_fragment(value: str) -> str:
    value = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", value or "",
                   flags=re.I)
    value = re.sub(r"<br\s*/?>|</(?:p|div|li|h[1-6])>", "\n", value,
                   flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t]+", " ", value)
    return re.sub(r"\n\s*\n+", "\n\n", value).strip()


def _decode_bing_link(value: str) -> str:
    """Giải mã link ``bing.com/ck/a?...&u=a1BASE64`` thành URL nguồn."""
    link = html.unescape(str(value or "")).strip()
    try:
        parsed = urlparse(link)
        if not parsed.netloc.lower().endswith("bing.com"):
            return link
        encoded = (parse_qs(parsed.query).get("u") or [""])[0]
        encoded = unquote(encoded)
        if encoded.startswith("a1"):
            encoded = encoded[2:]
        if not encoded:
            return link
        encoded += "=" * ((4 - len(encoded) % 4) % 4)
        decoded = base64.urlsafe_b64decode(encoded).decode("utf-8", "replace")
        return decoded if decoded.startswith(("http://", "https://")) else link
    except Exception:
        return link


def _domain_matches(url: str, domain: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().strip(".")
        wanted = str(domain or "").lower().strip(".")
        return bool(host and wanted and (host == wanted or host.endswith("." + wanted)))
    except Exception:
        return False


def _bing_reference_search(keyword: str, domain: str, limit: int) -> List[Dict]:
    """Tìm bài bằng trang HTML và loại cứng mọi kết quả ngoài domain.

    Bing RSS đôi lúc bỏ qua toán tử ``site:`` và từng trả về các trang hoàn
    toàn không liên quan. Trang HTML giữ kết quả chính xác hơn; link theo dõi
    được giải mã trước khi kiểm tra hostname.
    """
    query = "site:%s %s" % (domain, keyword)
    url = ("https://www.bing.com/search?setlang=zh-hans&count=%d&q=%s" %
           (max(10, min(50, int(limit or 10) * 2)), quote_plus(query)))
    request = Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/127 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,vi;q=0.8,en;q=0.7",
    })
    with urlopen(request, timeout=20) as response:
        page = response.read().decode("utf-8", "replace")
    blocks = re.findall(
        r'<li[^>]+class=["\'][^"\']*\bb_algo\b[^"\']*["\'][^>]*>'
        r'([\s\S]*?)</li>', page, flags=re.I)
    if not blocks:
        blocks = [page]
    rows, seen = [], set()
    for block in blocks:
        match = re.search(
            r'<h2[^>]*>[\s\S]*?<a[^>]+href=["\']([^"\']+)["\'][^>]*>'
            r'([\s\S]*?)</a>', block, flags=re.I)
        if not match:
            continue
        link = _decode_bing_link(match.group(1))
        if not _domain_matches(link, domain) or link in seen:
            continue
        title = _clean_html_fragment(match.group(2))
        desc_match = re.search(r'<p[^>]*>([\s\S]*?)</p>', block, flags=re.I)
        excerpt = _clean_html_fragment(desc_match.group(1) if desc_match else "")
        if title:
            seen.add(link)
            rows.append({"kind": "reference", "provider": "bing", "title": title,
                         "url": link, "excerpt": excerpt[:700], "channel": domain,
                         "duration": 0})
        if len(rows) >= limit:
            break
    return rows


def _compact_chinese_query(keyword: str) -> str:
    """Rút cụm chủ đề để ô tìm kiếm nội bộ không bị quá khớp chính xác."""
    chunks = re.findall(r"[\u3400-\u9fff]+", str(keyword or ""))
    compact = "".join(chunks)
    for generic in ("真实经历", "故事大全", "故事", "小说", "经历"):
        compact = compact.replace(generic, "")
    return (compact or str(keyword or "").strip())[:16]


def _focused_chinese_query(keyword: str) -> str:
    compact = _compact_chinese_query(keyword)
    focus_terms = (
        "灵异故事", "鬼故事", "乡村怪谈", "托梦", "因果报应", "灵异", "怪事",
        "婆媳", "儿女不孝", "赡养", "中老年", "老年夫妻", "黄昏恋", "再婚",
        "空巢老人", "遗产", "出轨", "保姆", "养老院", "重男轻女", "女婿",
        "退休夫妻", "兄弟姐妹", "老人卖房", "二婚家庭", "亲家", "拆迁款",
        "留守老人", "老年人被骗",
    )
    return next((term for term in focus_terms if term in compact), compact)


_SPIRITUAL_TERMS = (
    "灵异故事", "鬼故事", "乡村怪谈", "怪谈", "托梦", "因果报应",
    "报应", "闹鬼", "鬼魂", "亡灵", "阴间", "冥界", "灵异", "怪事",
    "tâmlinh", "chuyệnma", "báomộng", "điềmbáo", "ngườiâm",
)
_ORAL_RURAL_TERMS = (
    "农村", "乡村", "民间", "老人", "奶奶", "爷爷", "祖辈", "讲述", "老人讲",
    "ôngbà", "ngườigià", "làngquê", "dângian",
)
_TOPIC_TERMS = (
    *_SPIRITUAL_TERMS, *_ORAL_RURAL_TERMS,
    "婆媳矛盾", "婆媳", "矛盾", "赡养纠纷", "赡养", "儿女不孝",
    "中老年", "老年夫妻", "黄昏恋", "再婚", "空巢老人", "遗产",
    "出轨", "保姆", "养老院", "重男轻女", "女婿", "岳母", "退休夫妻",
    "兄弟姐妹", "老人卖房", "二婚家庭", "亲家", "彩礼", "拆迁款",
    "留守老人", "老年人被骗", "养老钱",
)


def _search_text(value: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]+", "", str(value or "").casefold())


def _reference_relevance(row: Dict, query: str,
                         article: Optional[Dict] = None) -> Dict:
    """Chấm đúng chủ đề trước độ nổi tiếng, có cổng cứng cho truyện tâm linh.

    Trước đây một tiểu thuyết hàng trăm triệu lượt đọc có thể đứng đầu chỉ vì
    công cụ tìm kiếm trả nó về. Điểm này dùng tiêu đề/đoạn trích/toàn văn và,
    với preset tâm linh ông bà kể, buộc bài phải có cả dấu hiệu kỳ bí lẫn chất
    làng quê/lời kể người già khi chính câu tìm kiếm yêu cầu hai nhóm đó.
    """
    article = article or {}
    title = _search_text(" ".join((str(row.get("title") or ""),
                                   str(article.get("title") or ""))))
    excerpt = _search_text(str(row.get("excerpt") or ""))
    content = _search_text(str(article.get("rawContent") or ""))
    compact = _search_text(query)
    terms = list(dict.fromkeys(term for term in _TOPIC_TERMS if term in compact))
    if not terms and compact:
        terms = [compact]

    score = 0
    matched = []
    if compact:
        if compact in title:
            score += 55
        elif compact in excerpt:
            score += 30
        elif compact in content:
            score += 16
    for term in terms:
        hit = False
        if term in title:
            score += 24
            hit = True
        if term in excerpt:
            score += 10
            hit = True
        if term in content:
            score += 5
            hit = True
        if hit:
            matched.append(term)

    spiritual_query = any(term in compact for term in _SPIRITUAL_TERMS)
    context_query = any(term in compact for term in _ORAL_RURAL_TERMS)
    haystack = title + excerpt + content
    spiritual_hit = any(term in haystack for term in _SPIRITUAL_TERMS)
    context_hit = any(term in haystack for term in _ORAL_RURAL_TERMS)
    accepted = (not spiritual_query or
                (spiritual_hit and (not context_query or context_hit)))
    if spiritual_query:
        score += 22 if spiritual_hit else 0
        score += 14 if context_hit else 0
    return {
        "relevance_score": max(0, min(100, int(score))),
        "relevance_terms": matched[:8],
        "relevance_accepted": bool(accepted),
        "strict_relevance": bool(spiritual_query),
    }


def _rank_direct_rows(rows: Iterable[Dict], query: str, limit: int) -> List[Dict]:
    compact = _compact_chinese_query(query)
    terms = [compact[index:index + 2] for index in range(0, len(compact), 2)
             if len(compact[index:index + 2]) == 2]

    def score(row):
        title = str(row.get("title") or "")
        excerpt = str(row.get("excerpt") or "")
        return (sum(title.count(term) for term in terms) * 10 +
                sum(excerpt.count(term) for term in terms))

    return sorted((dict(row) for row in rows), key=score, reverse=True)[:limit]


def _html_search_links(page: str, base_url: str, domain: str,
                       path_pattern: str, limit: int) -> List[Dict]:
    rows, seen = [], set()
    pattern = re.compile(
        r'<a\b([^>]*?)href=["\']([^"\']+)["\']([^>]*)>([\s\S]*?)</a>', re.I)
    for match in pattern.finditer(page or ""):
        attrs = (match.group(1) or "") + " " + (match.group(3) or "")
        link = html.unescape(match.group(2) or "").strip()
        link = urljoin(base_url, link if not link.startswith("//") else "https:" + link)
        if (not _domain_matches(link, domain) or link in seen or
                not re.search(path_pattern, urlparse(link).path, flags=re.I)):
            continue
        title_attr = re.search(r'\btitle=["\']([^"\']+)["\']', attrs, flags=re.I)
        title = _clean_html_fragment(title_attr.group(1) if title_attr else match.group(4))
        if len(title) < 2:
            continue
        nearby = (page[max(0, match.start() - 300):
                       min(len(page), match.end() + 700)])
        excerpt = _clean_html_fragment(nearby)
        metrics = _text_metrics(nearby)
        seen.add(link)
        rows.append({
            "kind": "reference", "provider": "site_search", "title": title,
            "url": link, "excerpt": excerpt[:700], "channel": domain,
            "duration": 0, **metrics,
        })
        if len(rows) >= limit:
            break
    return rows


def _direct_reference_search(keyword: str, source: Dict, limit: int) -> List[Dict]:
    """Dùng ô tìm kiếm/API chính chủ trước, Bing chỉ là phương án dự phòng."""
    strategy = str(source.get("direct_search") or "").strip()
    if not strategy:
        return []
    compact = _compact_chinese_query(keyword)
    spiritual = any(term in compact for term in _SPIRITUAL_TERMS)
    query = (_compact_chinese_query(keyword) if strategy in {
        "douban_groups", "douban_read", "hongxiu"
    } and not spiritual else _focused_chinese_query(keyword))
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/127 Safari/537.36"),
        "Accept-Language": "zh-CN,zh;q=0.9,vi;q=0.8",
    }
    if strategy == "qimao":
        response = requests.get("https://www.qimao.com/api/search/result",
                                params={"keyword": query, "page": 1},
                                headers=headers, timeout=20)
        response.raise_for_status()
        items = ((response.json().get("data") or {}).get("search_list") or [])
        rows = [{
            "kind": "reference", "provider": "qimao_search",
            "title": _clean_html_fragment(str(item.get("title") or "")),
            "url": str(item.get("read_url") or
                       "https://www.qimao.com/shuku/%s/" % item.get("book_id")),
            "excerpt": _clean_html_fragment(str(item.get("intro") or ""))[:700],
            "channel": source["domain"], "duration": 0,
            "author": str(item.get("author") or ""),
            "word_count_label": str(item.get("words_num") or ""),
        } for item in items if item.get("title") and item.get("book_id")]
        return _rank_direct_rows(rows, query, limit)
    if strategy == "zongheng":
        response = requests.get("https://search.zongheng.com/search/book",
                                params={"keyword": query, "pageNo": 1,
                                        "pageNum": max(10, limit * 3),
                                        "sort": "totalClick"},
                                headers={**headers, "Referer": "https://search.zongheng.com/"},
                                timeout=20)
        response.raise_for_status()
        payload = response.json()
        items = ((((payload.get("data") or {}).get("datas") or {}).get("list")) or [])
        rows = [{
            "kind": "reference", "provider": "zongheng_search",
            "title": _clean_html_fragment(str(item.get("name") or "")),
            "url": "https://book.zongheng.com/book/%s.html" % item.get("bookId"),
            "excerpt": _clean_html_fragment(str(item.get("description") or ""))[:700],
            "channel": source["domain"], "duration": 0,
            "author": str(item.get("authorName") or ""),
            "word_count": int(item.get("totalWord") or 0),
            "read_count": int(item.get("totalClick") or 0),
            "engagement_count": int(item.get("totalRecommend") or 0),
        } for item in items if item.get("name") and item.get("bookId")]
        return _rank_direct_rows(rows, query, limit)

    if strategy == "douban_groups":
        url = ("https://www.douban.com/group/search?cat=1013&sort=relevance&q=" +
               quote_plus(query))
        path_pattern = r"/group/topic/\d+"
    elif strategy == "douban_read":
        url = "https://read.douban.com/search?q=" + quote_plus(query)
        path_pattern = r"/(?:ebook|column)/\d+"
    elif strategy == "hongxiu":
        url = "https://www.hongxiu.com/search?kw=" + quote_plus(query)
        path_pattern = r"/book/\d+"
    elif strategy == "qidian_mobile":
        url = "https://m.qidian.com/search?kw=" + quote_plus(query)
        path_pattern = r"/chapter/\d+/0"
    else:
        return []
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    rows = _html_search_links(response.text, url, source["domain"],
                              path_pattern, max(10, limit * 3))
    return _rank_direct_rows(rows, query, limit)


def search_web_references(keyword: str, source_keys=None, limit: int = 20) -> List[Dict]:
    """Tìm metadata/snippet bài tham khảo, chưa tải toàn văn bài gốc."""
    keyword = str(keyword or "").strip()
    if not keyword:
        raise ValueError("Hãy nhập từ khóa tham khảo.")
    selected = set(str(x) for x in (source_keys or []) if str(x).strip())
    sources = [x for x in REFERENCE_SOURCES if not selected or x["key"] in selected]
    rows, seen = [], set()
    wanted = max(1, min(50, int(limit or 20)))
    each = max(1, min(10, (wanted + max(1, len(sources)) - 1) // max(1, len(sources))))

    def search_one(source):
        suffix = str(source.get("search_suffix") or "").strip()
        query = " ".join(value for value in (keyword, suffix) if value)
        direct_error = ""
        try:
            direct = _direct_reference_search(keyword, source, each)
        except Exception as exc:
            direct, direct_error = [], str(exc)
        direct_fallback = list(direct)
        # Ô tìm nội bộ của vài website khớp kiểu OR. Với truy vấn tâm linh,
        # bỏ các dòng chỉ khớp "nông thôn/người già" trước khi quyết định rằng
        # tìm trực tiếp đã thành công, để còn được thử Bing chính xác hơn.
        if direct and any(term in _search_text(keyword)
                          for term in _SPIRITUAL_TERMS):
            direct = [row for row in direct
                      if _reference_relevance(row, keyword)["relevance_accepted"]]
        if direct:
            return source, direct, ""
        try:
            bing = _bing_reference_search(query, source["domain"], each)
            return source, (bing or direct_fallback), ""
        except Exception as exc:
            if direct_fallback:
                return source, direct_fallback, ""
            error = str(exc)
            if direct_error:
                error = "tìm trực tiếp: %s; Bing: %s" % (direct_error, error)
            return source, [], error

    # Số nền tảng Trung Quốc đã tăng đáng kể. Tìm song song giúp thời gian chờ
    # gần với một lượt Bing thay vì cộng dồn timeout của từng website.
    results = []
    workers = max(1, min(4, len(sources)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(search_one, source): index
                   for index, source in enumerate(sources)}
        for future in as_completed(futures):
            source, found, error = future.result()
            results.append((futures[future], source, found, error))

    # Trả về theo thứ tự catalog ổn định; bước enrich sau đó mới xếp theo lượt đọc.
    for _index, source, found, error in sorted(results, key=lambda value: value[0]):
        if error:
            log("Nguồn tham khảo %s tạm không truy cập được: %s" %
                (source["name"], error), "warn")
            continue
        for row in found:
            if row["url"] in seen:
                continue
            seen.add(row["url"])
            row["source_key"] = source["key"]
            row["source_name"] = source["name"]
            row["language"] = source["lang"]
            row["source_priority"] = source.get("priority", "")
            row["narrative_style"] = source.get("narrative_style", "")
            row["content_form"] = source.get("content_form", "")
            row["source_notes"] = source.get("notes", "")
            rows.append(row)
    return rows[:wanted]


def _json_ld_article(page: str) -> Dict:
    def candidates(value):
        if isinstance(value, list):
            for item in value:
                yield from candidates(item)
        elif isinstance(value, dict):
            yield value
            yield from candidates(value.get("@graph"))

    for raw in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>'
            r'([\s\S]*?)</script>', page or "", flags=re.I):
        try:
            payload = json.loads(html.unescape(raw).strip())
        except Exception:
            continue
        for item in candidates(payload):
            body = item.get("articleBody") or item.get("text") or ""
            if len(_clean_html_fragment(str(body))) < 180:
                continue
            author = item.get("author") or ""
            if isinstance(author, list):
                author = ", ".join(str(x.get("name") or "") for x in author
                                   if isinstance(x, dict))
            elif isinstance(author, dict):
                author = author.get("name") or ""
            metrics = _text_metrics(json.dumps(item, ensure_ascii=False))
            interactions = item.get("interactionStatistic") or []
            if isinstance(interactions, dict):
                interactions = [interactions]
            for stat in interactions if isinstance(interactions, list) else []:
                if not isinstance(stat, dict):
                    continue
                count = _metric_number(stat.get("userInteractionCount"))
                kind = str((stat.get("interactionType") or {}).get("@type")
                           if isinstance(stat.get("interactionType"), dict)
                           else stat.get("interactionType") or "").lower()
                if "view" in kind:
                    metrics["read_count"] = max(metrics["read_count"], count)
                else:
                    metrics["engagement_count"] += count
            return {
                "title": _clean_html_fragment(str(
                    item.get("headline") or item.get("name") or "")),
                "content": _clean_html_fragment(str(body)),
                "author": _clean_html_fragment(str(author)),
                "published_at": str(item.get("datePublished") or ""),
                **metrics,
            }
    return {}


def _html_article(page: str) -> Dict:
    structured = _json_ld_article(page)
    if structured:
        return structured
    title = ""
    for pattern in (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title',
        r'<title[^>]*>([\s\S]*?)</title>',
    ):
        match = re.search(pattern, page or "", flags=re.I)
        if match:
            title = _clean_html_fragment(match.group(1))
            break
    candidates = []
    for pattern in (
        r'<article\b[^>]*>([\s\S]*?)</article>',
        r'<main\b[^>]*>([\s\S]*?)</main>',
        r'<(?:div|section)[^>]+(?:class|id)=["\'][^"\']*'
        r'(?:RichText|article-body|article-content|post-content|story-content|entry-content)'
        r'[^"\']*["\'][^>]*>([\s\S]*?)</(?:div|section)>',
    ):
        for value in re.findall(pattern, page or "", flags=re.I):
            text = _clean_html_fragment(value)
            if len(text) >= 180:
                candidates.append(text)
    content = max(candidates, key=len) if candidates else ""
    return {"title": title, "content": content, "author": "",
            "published_at": "", **_text_metrics(page)}


def _jina_article(url: str, timeout: int) -> Dict:
    parsed = urlparse(url)
    target = "http://" + parsed.netloc + (parsed.path or "/")
    if parsed.query:
        target += "?" + parsed.query
    response = requests.get("https://r.jina.ai/" + target, headers={
        "User-Agent": "AutoDubVN/2.5 content-research",
        "Accept": "text/plain",
    }, timeout=timeout)
    response.raise_for_status()
    raw = response.text
    metrics = _text_metrics(raw)
    title_match = re.search(r"^Title:\s*(.+)$", raw, flags=re.M)
    content = raw.split("Markdown Content:", 1)[-1] if "Markdown Content:" in raw else raw
    content = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", content)
    content = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", content)
    content = re.sub(r"^#{1,6}\s*", "", content, flags=re.M)
    content = re.sub(r"^[-*]{3,}\s*$", "", content, flags=re.M)
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    return {
        "title": (title_match.group(1).strip() if title_match else ""),
        "content": content,
        "author": "", "published_at": "", "fetch_mode": "reader_fallback",
        **metrics,
    }


def _reddit_article(url: str, timeout: int) -> Dict:
    clean = re.sub(r"[?#].*$", "", url.rstrip("/"))
    response = requests.get(clean + ".json", headers={
        "User-Agent": "windows:AutoDubVN:2.5 (content research)",
        "Accept": "application/json",
    }, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    post = (((payload[0] or {}).get("data") or {}).get("children") or [{}])[0]
    data = post.get("data") or {}
    return {
        "title": str(data.get("title") or "").strip(),
        "content": str(data.get("selftext") or "").strip(),
        "author": str(data.get("author") or "").strip(),
        "published_at": str(data.get("created_utc") or ""),
        "fetch_mode": "reddit_json",
        "read_count": 0,
        "engagement_count": int(data.get("score") or 0) +
                            int(data.get("num_comments") or 0),
    }


def fetch_reference_article(item, cookie: str = "", timeout: int = 30,
                            force_refresh: bool = False) -> Dict:
    """Tải một bài công khai thành record tương thích ``ContentStore``.

    Cookie Zhihu (nếu người dùng chủ động cung cấp) chỉ nằm trong header của
    lượt gọi này, không được ghi vào record, log hay cấu hình. Khi website
    chặn trình đọc trực tiếp, hệ thống thử reader công khai và báo rõ cách lấy.
    """
    row = dict(item) if isinstance(item, dict) else {"url": str(item or "")}
    url = str(row.get("url") or row.get("source_url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("Link bài viết không hợp lệ: " + url[:120])
    host = (urlparse(url).hostname or "").lower()
    timeout = max(8, min(90, int(timeout or 30)))
    if not str(cookie or "").strip() and not force_refresh:
        with _REFERENCE_CACHE_LOCK:
            cached = _REFERENCE_CACHE.get(url)
            if cached and time.time() - float(cached[0]) <= _REFERENCE_CACHE_TTL:
                return dict(cached[1])
    if host.endswith("reddit.com"):
        article = _reddit_article(url, timeout)
    else:
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 Chrome/127 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,vi;q=0.8,en;q=0.7",
        }
        if str(cookie or "").strip():
            headers["Cookie"] = str(cookie).strip()
        article = {}
        direct_error = ""
        try:
            response = requests.get(url, headers=headers, timeout=timeout,
                                    allow_redirects=True)
            response.raise_for_status()
            article = _html_article(response.text)
            article["fetch_mode"] = "direct"
        except Exception as exc:
            direct_error = str(exc)
        if len(str(article.get("content") or "").strip()) < 280:
            try:
                article = _jina_article(url, timeout)
            except Exception as exc:
                message = str(exc) or direct_error or "website từ chối truy cập"
                raise RuntimeError("Không lấy được nội dung công khai: " + message) from exc
    content = str(article.get("content") or "").strip()
    # Trang chặn thường chỉ trả một câu mời đăng nhập; không lưu loại kết quả này.
    if len(content) < 280:
        raise RuntimeError(
            "Trang chỉ trả tiêu đề/đoạn giới thiệu, chưa đủ nội dung để lưu. "
            "Với Zhihu, hãy dán Cookie phiên đăng nhập hoặc thử link câu hỏi khác.")
    source_name = str(row.get("source_name") or row.get("source") or host).strip()
    catalog_source = next((source for source in REFERENCE_SOURCES
                           if _domain_matches(url, source.get("domain", ""))), {})
    language = str(row.get("language") or catalog_source.get("lang") or "").strip()
    keyword = str(row.get("keyword") or "").strip()
    topic = str(row.get("topic") or "").strip()
    narrative_style = str(row.get("narrative_style") or
                          catalog_source.get("narrative_style") or "").strip()
    mode = str(article.get("fetch_mode") or "direct")
    note = ("Chỉ dùng làm chất liệu; phải viết lại hoàn toàn. "
            "Cách lấy nội dung: %s." % mode)
    source_note = str(row.get("source_notes") or catalog_source.get("notes") or "").strip()
    if source_note:
        note += " Quy tắc nguồn: " + source_note
    record = {
        "title": str(article.get("title") or row.get("title") or url).strip(),
        "rawContent": content,
        "source": source_name,
        "sourceUrl": url,
        "author": str(article.get("author") or "").strip(),
        "publishedAt": str(article.get("published_at") or "").strip(),
        "language": language,
        "tags": [value for value in (keyword, topic, narrative_style) if value],
        "notes": note,
        "narrative_style": narrative_style,
        "content_form": str(row.get("content_form") or
                            catalog_source.get("content_form") or "").strip(),
        "fetch_mode": mode,
        "read_count": int(article.get("read_count") or 0),
        "engagement_count": int(article.get("engagement_count") or 0),
    }
    if not str(cookie or "").strip():
        with _REFERENCE_CACHE_LOCK:
            _REFERENCE_CACHE[url] = (time.time(), dict(record))
            if len(_REFERENCE_CACHE) > _REFERENCE_CACHE_MAX:
                oldest = sorted(_REFERENCE_CACHE.items(), key=lambda pair: pair[1][0])
                for key, _value in oldest[:len(_REFERENCE_CACHE) - _REFERENCE_CACHE_MAX]:
                    _REFERENCE_CACHE.pop(key, None)
    return record


def enrich_reference_rows(rows: Iterable[Dict], max_workers: int = 4,
                          progress: Optional[Callable[[int, int, Dict], None]] = None,
                          query: str = ""
                          ) -> List[Dict]:
    """Đọc trước nội dung rồi xếp đúng chủ đề trước, lượt đọc sau."""
    items = [dict(row) for row in rows or []]
    if not items:
        return []
    total, done = len(items), 0

    def one(pair):
        index, row = pair
        out = dict(row)
        out["search_rank"] = index + 1
        try:
            article = fetch_reference_article(row, timeout=35)
            out.update({
                "read_count": int(article.get("read_count") or 0),
                "engagement_count": int(article.get("engagement_count") or 0),
                "content_ready": True,
                "content_chars": len(str(article.get("rawContent") or "")),
                "fetch_error": "",
            })
            out.update(_reference_relevance(out, query, article))
        except Exception as exc:
            out.update({"read_count": 0, "engagement_count": 0,
                        "content_ready": False, "content_chars": 0,
                        "fetch_error": str(exc)[:240]})
            out.update(_reference_relevance(out, query))
        return out

    results = []
    with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers or 4), len(items)))) as pool:
        futures = {pool.submit(one, pair): pair[1]
                   for pair in enumerate(items)}
        for future in as_completed(futures):
            item = future.result()
            results.append(item)
            done += 1
            if progress:
                progress(done, total, item)
    if query and any(item.get("strict_relevance") for item in results):
        results = [item for item in results if item.get("relevance_accepted")]
    results.sort(key=lambda x: (
        -int(x.get("relevance_score") or 0),
        0 if int(x.get("read_count") or 0) > 0 else 1,
        -int(x.get("read_count") or 0),
        -int(x.get("engagement_count") or 0),
        int(x.get("search_rank") or 9999),
    ))
    for index, item in enumerate(results, 1):
        item["popularity_rank"] = index
    return results


def _video_files(paths: Iterable[str]) -> List[str]:
    out, seen = [], set()
    for raw in paths or []:
        path = os.path.abspath(str(raw or "").strip().strip('"'))
        candidates = []
        if os.path.isdir(path):
            candidates = [os.path.join(path, name)
                          for name in sorted(os.listdir(path))]
        elif os.path.isfile(path):
            candidates = [path]
        for item in candidates:
            if not os.path.isfile(item) or os.path.splitext(item)[1].lower() not in {
                    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}:
                continue
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def cut_video_segments(paths: Iterable[str], out_dir: str,
                       min_seconds: float = 300, max_seconds: float = 600,
                       progress: Optional[Callable[[int, int, str], None]] = None,
                       filename_prefix: str = "") -> List[str]:
    """Cắt video nguồn thành các đoạn 5–10 phút bằng stream-copy nhanh.

    Chọn điểm giữa khoảng min/max để mỗi nguồn có các đoạn ổn định ~7,5 phút.
    FFmpeg cắt tại keyframe gần nhất, vì vậy thời lượng thực tế có thể lệch
    một ít; các clip vẫn được planner random-pick dùng trực tiếp.
    """
    sources = _video_files(paths)
    if not sources:
        raise ValueError("Chưa có file video nguồn để cắt.")
    try:
        lo = max(2.0, float(min_seconds))
        hi = max(lo, float(max_seconds))
    except (TypeError, ValueError):
        lo, hi = 300.0, 600.0
    segment_time = (lo + hi) / 2.0
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    results: List[str] = []
    total = len(sources)
    safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(filename_prefix or ""))
    for index, source in enumerate(sources, 1):
        stem = re.sub(r"[^\w.-]+", "_", Path(source).stem, flags=re.UNICODE).strip("._") or "video"
        pattern = os.path.join(out_dir, f"{safe_prefix}{index:03d}_{stem}_%03d.mp4")
        run([
            "ffmpeg", "-y", "-hide_banner", "-nostdin", "-i", source,
            "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
            "-f", "segment", "-segment_time", f"{segment_time:.3f}",
            "-reset_timestamps", "1", "-segment_format", "mp4", pattern,
        ], check=True, quiet=True)
        prefix = os.path.basename(pattern).split("%03d", 1)[0]
        made = [os.path.join(out_dir, name) for name in sorted(os.listdir(out_dir))
                if name.startswith(prefix) and name.lower().endswith(".mp4")]
        results.extend(path for path in made if path not in results)
        if progress:
            progress(index, total, os.path.basename(source))
    if not results:
        raise RuntimeError("FFmpeg không tạo được clip nào.")
    return results


def _run_metadata(cmd: List[str]):
    """Chạy truy vấn yt-dlp; cookie browser bị khóa thì lui về nguồn công khai."""
    try:
        return downloader.run(cmd, quiet=True)
    except RuntimeError as exc:
        if (downloader._browser_cookie_failed(exc) and
                "--cookies-from-browser" in cmd):
            log("Không đọc được cookie trình duyệt; tìm lại trong nguồn video công khai.",
                "warn")
            return downloader.run(
                downloader._without_option_value(cmd, "--cookies-from-browser"),
                quiet=True)
        raise


def _entry_url(entry: Dict) -> str:
    url = str(entry.get("webpage_url") or entry.get("original_url") or
              entry.get("url") or "").strip()
    if url.startswith("http"):
        return url
    ident = str(entry.get("id") or "").strip()
    if ident:
        extractor = str(entry.get("extractor_key") or entry.get("extractor") or "").lower()
        if "youtube" in extractor:
            return "https://www.youtube.com/watch?v=" + ident
        return "https://www.bilibili.com/video/" + ident
    return ""


def _normalise_entry(entry: Dict, index: int = 0) -> Dict:
    url = _entry_url(entry)
    extractor = str(entry.get("extractor_key") or entry.get("extractor") or "").lower()
    is_youtube = ("youtube" in extractor or "youtube.com/" in url.lower() or
                  "youtu.be/" in url.lower())
    return {
        "index": int(index or entry.get("playlist_index") or 0),
        "id": str(entry.get("id") or ""),
        "title": str(entry.get("title") or entry.get("fulltitle") or "").strip(),
        "url": url,
        "duration": float(entry.get("duration") or 0),
        "thumbnail": str(entry.get("thumbnail") or ""),
        "channel": str(entry.get("channel") or entry.get("uploader") or ""),
        "provider": ("youtube" if is_youtube else "bilibili"),
    }


def _duration_seconds(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    parts = str(value or "").strip().split(":")
    try:
        total = 0.0
        for part in parts:
            total = total * 60 + float(part)
        return total
    except (TypeError, ValueError):
        return 0.0


def _search_bilibili_api(keyword: str, limit: int) -> List[Dict]:
    """Fallback chính chủ khi yt-dlp không nhận trang search.bilibili.com."""
    url = ("https://api.bilibili.com/x/web-interface/search/type?"
           "search_type=video&page=1&page_size=%d&keyword=%s" %
           (limit, quote_plus(keyword)))
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 AutoDubVN/2.5",
        "Referer": "https://search.bilibili.com/",
        "Accept": "application/json",
    })
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    items = ((payload.get("data") or {}).get("result") or []) if isinstance(payload, dict) else []
    rows = []
    for index, item in enumerate(items[:limit], 1):
        bvid = str(item.get("bvid") or item.get("id") or "").strip()
        if not bvid:
            continue
        title = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).strip()
        thumb = str(item.get("pic") or "")
        if thumb.startswith("//"):
            thumb = "https:" + thumb
        rows.append({
            "index": index, "id": bvid, "title": title,
            "url": "https://www.bilibili.com/video/" + bvid,
            "duration": _duration_seconds(item.get("duration")),
            "thumbnail": thumb,
            "channel": str(item.get("author") or "Bilibili"),
            "provider": "bilibili",
        })
    return rows


def search_bilibili(keyword: str, limit: int = 10,
                    cookies_from_browser: Optional[str] = None,
                    cookies_file: Optional[str] = None) -> List[Dict]:
    """Tìm tối đa ``limit`` video trên trang tìm kiếm Bilibili."""
    keyword = str(keyword or "").strip()
    if not keyword:
        raise ValueError("Hãy nhập từ khóa tìm video Bilibili.")
    cookies_from_browser = downloader._normalise_cookie_browser(cookies_from_browser)
    limit = max(1, min(50, int(limit or 10)))
    cmd = [
        *downloader._ytdlp_cmd(), "--flat-playlist", "--dump-single-json",
        "--skip-download", "--playlist-end", str(limit), "--no-warnings",
        "--ignore-errors", "https://search.bilibili.com/all?keyword=" +
        quote_plus(keyword),
    ]
    if cookies_from_browser:
        cmd[cmd.index("--no-warnings"):cmd.index("--no-warnings")] = [
            "--cookies-from-browser", str(cookies_from_browser)]
    if cookies_file:
        cmd[cmd.index("--no-warnings"):cmd.index("--no-warnings")] = [
            "--cookies", str(cookies_file)]
    try:
        result = _run_metadata(cmd)
    except RuntimeError as exc:
        if "unsupported url" in str(exc).lower():
            return _search_bilibili_api(keyword, limit)
        raise
    raw = str(result.stdout or "").strip()
    if not raw:
        return []
    payloads: List[Dict] = []
    try:
        payload = json.loads(raw)
        payloads = list(payload.get("entries") or []) if isinstance(payload, dict) else []
    except json.JSONDecodeError:
        for line in raw.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                payloads.append(item)
    out = []
    seen = set()
    for i, item in enumerate(payloads[:limit], 1):
        row = _normalise_entry(item, i)
        if not row["url"] or row["url"] in seen:
            continue
        seen.add(row["url"])
        out.append(row)
    return out


def search_youtube(keyword: str, limit: int = 10,
                   cookies_from_browser: Optional[str] = None,
                   cookies_file: Optional[str] = None) -> List[Dict]:
    """Tìm video YouTube bằng ytsearch của yt-dlp, không cần API key."""
    keyword = str(keyword or "").strip()
    if not keyword:
        raise ValueError("Hãy nhập từ khóa tìm video YouTube.")
    cookies_from_browser = downloader._normalise_cookie_browser(cookies_from_browser)
    limit = max(1, min(50, int(limit or 10)))
    cmd = [*downloader._ytdlp_cmd(), "--flat-playlist", "--dump-single-json",
           "--skip-download", "--no-warnings", "--ignore-errors",
           "ytsearch%d:%s" % (limit, keyword)]
    if cookies_from_browser:
        cmd[cmd.index("--no-warnings"):cmd.index("--no-warnings")] = [
            "--cookies-from-browser", str(cookies_from_browser)]
    if cookies_file:
        cmd[cmd.index("--no-warnings"):cmd.index("--no-warnings")] = [
            "--cookies", str(cookies_file)]
    result = _run_metadata(cmd)
    try:
        payload = json.loads(str(result.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return []
    entries = list(payload.get("entries") or []) if isinstance(payload, dict) else []
    return [_normalise_entry(item, i) for i, item in enumerate(entries[:limit], 1)
            if isinstance(item, dict) and _entry_url(item)]


def search(keyword: str, limit: int = 10, provider: str = "bilibili",
           cookies_from_browser: Optional[str] = None,
           cookies_file: Optional[str] = None) -> List[Dict]:
    """Tìm một hoặc hai nguồn và gộp kết quả theo đúng giới hạn."""
    provider = str(provider or "bilibili").strip().lower()
    if provider == "youtube":
        return search_youtube(keyword, limit, cookies_from_browser, cookies_file)
    if provider in {"all", "both", "tat_ca"}:
        each = max(1, (int(limit) + 1) // 2)
        rows = []
        try:
            rows += search_bilibili(keyword, each, cookies_from_browser, cookies_file)
        except Exception as exc:
            log("Bilibili tạm chặn tìm kiếm; vẫn tiếp tục với YouTube: %s" % exc,
                "warn")
        try:
            # Nguồn còn hoạt động được phép bù toàn bộ giới hạn khi nguồn kia lỗi.
            youtube_limit = max(1, int(limit) - len(rows))
            rows += search_youtube(
                keyword, youtube_limit, cookies_from_browser, cookies_file)
        except Exception as exc:
            if not rows:
                raise
            log("YouTube tạm lỗi; dùng kết quả Bilibili đã tìm được: %s" % exc,
                "warn")
        return rows[:max(1, int(limit))]
    return search_bilibili(keyword, limit, cookies_from_browser, cookies_file)


def download_many(urls: Iterable[str], out_dir: str, quality: str = "best",
                  cookies_from_browser: Optional[str] = None,
                  cookies_file: Optional[str] = None,
                  concurrent_fragments: int = 8,
                  external_downloader: Optional[str] = "auto",
                  progress: Optional[Callable[[int, int, str], None]] = None,
                  live_progress: Optional[Callable[[float, str], None]] = None,
                  max_workers: int = 3) -> List[Dict]:
    """Tải song song một nhóm link, trả về các file hợp lệ theo thứ tự hoàn tất."""
    links = []
    seen = set()
    for raw in urls or []:
        url = downloader.extract_url(str(raw))
        if url and url not in seen:
            links.append(url)
            seen.add(url)
    if not links:
        raise ValueError("Chưa có link video nền hợp lệ để tải.")
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    total, done, out = len(links), 0, []
    live_lock = threading.Lock()
    item_percent = {url: 0.0 for url in links}

    def one(url: str) -> Dict:
        def _item_progress(info: Dict) -> None:
            if not live_progress:
                return
            pct = info.get("percent")
            if pct is None:
                return
            with live_lock:
                # Một video DASH có thể báo 100% cho hình rồi bắt đầu luồng
                # tiếng từ 0%. Giữ mức cao nhất để thanh tổng không chạy lùi;
                # dòng chi tiết từ downloader vẫn cho biết đúng luồng hiện tại.
                item_percent[url] = max(item_percent[url],
                                        max(0.0, min(100.0, float(pct))))
                overall = sum(item_percent.values()) / max(1, total)
            detail = "%5.1f%% tổng · %s" % (
                overall, str(info.get("text") or "Đang tải video…"))
            live_progress(overall, detail)

        path = downloader.download_video(
            url, out_dir, quality=quality,
            cookies_from_browser=cookies_from_browser,
            cookies_file=cookies_file,
            concurrent_fragments=concurrent_fragments,
            external_downloader=external_downloader,
            progress_callback=_item_progress)
        with live_lock:
            item_percent[url] = 100.0
        return {"url": url, "path": os.path.abspath(path),
                "title": os.path.splitext(os.path.basename(path))[0]}

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(links)))) as pool:
        futures = {pool.submit(one, url): url for url in links}
        for future in as_completed(futures):
            url = futures[future]
            done += 1
            try:
                item = future.result()
                out.append(item)
                log("Đã tải nguồn video %d/%d: %s" % (done, total, os.path.basename(item["path"])), "ok")
                message = os.path.basename(item["path"])
            except Exception as exc:
                log("Tải nguồn video lỗi (%s): %s" % (url, exc), "err")
                message = "Lỗi: %s" % str(exc)[:140]
            if progress:
                progress(done, total, message)
    if not out:
        raise RuntimeError("Không tải được video nào trong danh sách.")
    return out
