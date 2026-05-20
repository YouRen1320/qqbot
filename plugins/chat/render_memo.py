"""
长回复 → 备忘录风格 PNG 图片
- 触发: __init__.py 在 _send_segments 头部检查 len(reply) > LONG_REPLY_AS_IMAGE_THRESHOLD
- 代码 / 多行技术内容免疫（_looks_like_code 在 __init__.py 里判定）
- 字体: 文泉驿微米黑（Dockerfile 通过 fonts-wqy-microhei 装入，~5MB；本机开发回退 PingFang）
- 失败时返回 None，调用方走纯文字 fallback
"""
import io
import os
import logging

from .config import BOT_NAME

logger = logging.getLogger("chat")

try:
    from PIL import Image, ImageDraw, ImageFont

    PIL_OK = True
except ImportError:
    PIL_OK = False
    logger.warning("PIL 未安装；长回复→图片不可用，将退回文字")

# 字体候选路径
# - Linux 容器: 文泉驿微米黑（fonts-wqy-microhei 包，Dockerfile 装入）
# - 兼容保留: noto-cjk 路径（如果以后切回更大字体不用再改）
# - macOS 本地: PingFang / Hiragino / Songti
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Songti.ttc",
)

_FONT_CACHE: dict[int, "ImageFont.FreeTypeFont"] = {}


def _font(size: int):
    """优先用 NotoSansCJK；命中即缓存"""
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)
                _FONT_CACHE[size] = f
                logger.info(f"render_memo 字体: {p} size={size}")
                return f
            except OSError:
                continue
    logger.warning("render_memo 找不到 CJK 字体，将用 PIL default（中文不显示）")
    f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f


def _text_w(font, text: str) -> int:
    """像素宽度；兼容 Pillow 10 的 getbbox"""
    try:
        bbox = font.getbbox(text)
        return bbox[2] - bbox[0]
    except Exception:
        return len(text) * (getattr(font, "size", 24) // 2)


def _wrap(text: str, font, max_w: int) -> list[str]:
    """按像素宽度软包裹中英文混排，保留原始换行 + 空行"""
    lines: list[str] = []
    for raw in text.splitlines():
        if not raw:
            lines.append("")
            continue
        cur = ""
        for ch in raw:
            test = cur + ch
            if _text_w(font, test) > max_w and cur:
                lines.append(cur)
                cur = ch
            else:
                cur = test
        if cur:
            lines.append(cur)
    return lines or [""]


def render_memo(
    text: str,
    *,
    width: int = 600,
    font_size: int = 22,
) -> bytes | None:
    """
    text → iMessage 浅色卡片风 PNG bytes
    成功返回 bytes,失败返回 None(调用方退回文字路径)

    设计:
    - 外圈浅灰 + 内置白卡 + 20px 圆角 + 1px 浅灰边(无阴影,靠对比凸显层次)
    - 顶部小标签 BOT_NAME 灰字, 正文深灰
    - 字号 22 + 行距 14, 行高合计 36, 视觉宽松
    """
    if not PIL_OK or not text or not text.strip():
        return None
    try:
        body_font = _font(font_size)
        label_font = _font(font_size - 6)  # 标签 16

        # 配色: iOS 浅色系统色对齐
        canvas_bg   = (244, 245, 247)   # 浅灰外圈, 近 systemGray6
        card_bg     = (255, 255, 255)   # 纯白卡
        card_border = (229, 229, 234)   # 浅灰描边, 近 systemGray5
        ink         = (28, 28, 30)      # 正文 #1C1C1E
        label_ink   = (138, 138, 142)   # 副标签 #8A8A8E

        # 间距(像素)
        canvas_margin   = 18    # 卡片到画布边
        card_pad_x      = 32    # 卡片内左右
        card_pad_top    = 28    # 卡片内顶
        card_pad_bottom = 32    # 卡片内底
        label_gap       = 16    # 标签到正文垂直距
        line_gap        = 14    # 正文行间距
        line_h          = font_size + line_gap   # 行高 36

        content_w = width - 2 * (canvas_margin + card_pad_x)
        lines = _wrap(text.strip(), body_font, content_w)
        body_h = len(lines) * line_h
        label_h = (font_size - 6) + label_gap

        card_h = card_pad_top + label_h + body_h + card_pad_bottom
        img_h = card_h + 2 * canvas_margin
        img_h = max(img_h, 160)

        img = Image.new("RGB", (width, img_h), canvas_bg)
        draw = ImageDraw.Draw(img)

        # 白卡片(20px 圆角 + 1px 描边, 无阴影靠对比凸显)
        draw.rounded_rectangle(
            (canvas_margin, canvas_margin, width - canvas_margin, img_h - canvas_margin),
            radius=20,
            fill=card_bg,
            outline=card_border,
            width=1,
        )

        # 顶部小标签
        label_x = canvas_margin + card_pad_x
        label_y = canvas_margin + card_pad_top
        draw.text((label_x, label_y), BOT_NAME, font=label_font, fill=label_ink)

        # 正文
        body_y = label_y + label_h
        for line in lines:
            draw.text((label_x, body_y), line, font=body_font, fill=ink)
            body_y += line_h

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"render_memo 渲染失败: {e!r}")
        return None
