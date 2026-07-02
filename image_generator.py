"""
Generator grafik pod posty na Instagrama.

Bierze zaakceptowaną treść zgłoszenia i renderuje z niej kwadratowy obrazek
(1080x1080, format wymagany przez Instagram Graph API) na bazie prostego,
"kartkowego" szablonu: gradientowe tło + biała karta + tekst.

Wynik zapisywany jest jako JPEG w static/generated/, żeby Flask mógł go
serwować pod publicznym URL-em (wymaganym przez Instagram do pobrania obrazka).
"""

import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

BASE_DIR = os.path.dirname(__file__)
FONT_DIR = os.path.join(BASE_DIR, "static", "fonts")
OUTPUT_DIR = os.path.join(BASE_DIR, "static", "generated")

os.makedirs(OUTPUT_DIR, exist_ok=True)

SIZE = 1080
CARD_MARGIN = 90
CARD_RADIUS = 46
CARD_PADDING_X = 80
CARD_PADDING_TOP = 90
CARD_PADDING_BOTTOM = 70

# Kolory
COLOR_GRADIENT_TOP = (33, 41, 90)      # ciemny granat
COLOR_GRADIENT_BOTTOM = (86, 60, 122)  # fiolet
COLOR_CARD = (255, 255, 255)
COLOR_TEXT = (30, 32, 46)
COLOR_MUTED = (140, 140, 155)
COLOR_ACCENT = (255, 255, 255)
COLOR_ACCENT_BG = (33, 41, 90)
COLOR_QUOTE_MARK = (230, 230, 238)

SITE_LABEL = os.getenv("IG_TEMPLATE_TITLE", "spotted żyrardów")
FOOTER_LABEL = os.getenv("IG_TEMPLATE_FOOTER", "napisz swoją historię — link w bio")


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(os.path.join(FONT_DIR, f"{name}.ttf"), size)


def _vertical_gradient(size, top_color, bottom_color) -> Image.Image:
    """Tworzy kwadratowy obrazek size x size z pionowym gradientem liniowym."""
    column = Image.new("RGB", (1, size), color=0)
    draw = ImageDraw.Draw(column)
    for y in range(size):
        t = y / (size - 1)
        r = round(top_color[0] + (bottom_color[0] - top_color[0]) * t)
        g = round(top_color[1] + (bottom_color[1] - top_color[1]) * t)
        b = round(top_color[2] + (bottom_color[2] - top_color[2]) * t)
        draw.point((0, y), fill=(r, g, b))
    return column.resize((size, size))


def _rounded_rect_shadow(img: Image.Image, box, radius, blur=28, offset=(0, 18), opacity=90):
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    x0, y0, x1, y1 = box
    sd.rounded_rectangle(
        (x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]),
        radius=radius,
        fill=(0, 0, 0, opacity),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    img.alpha_composite(shadow)


def _break_long_word(draw, word, font, max_width):
    """Twardo łamie pojedyncze 'słowo' bez spacji (np. link, spam), gdy samo nie mieści się w linii."""
    if draw.textlength(word, font=font) <= max_width:
        return [word]
    chunks = []
    current = ""
    for ch in word:
        candidate = current + ch
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            chunks.append(current)
            current = ch
    if current:
        chunks.append(current)
    return chunks


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int):
    lines = []
    for paragraph in text.split("\n"):
        words = paragraph.split(" ")
        if not words:
            lines.append("")
            continue
        current = ""
        for word in words:
            for piece in _break_long_word(draw, word, font, max_width):
                candidate = f"{current} {piece}".strip() if current else piece
                if draw.textlength(candidate, font=font) <= max_width:
                    current = candidate
                else:
                    if current:
                        lines.append(current)
                    current = piece
        lines.append(current)
    return lines


def _fit_text(draw, text, max_width, max_height, font_name="Poppins-Medium",
              start_size=58, min_size=28, line_spacing=1.4):
    for size in range(start_size, min_size - 1, -2):
        font = _font(font_name, size)
        lines = _wrap_text(draw, text, font, max_width)
        line_height = font.getbbox("Ślężąy")[3] * line_spacing
        block_height = line_height * len(lines)
        if block_height <= max_height:
            return font, lines, line_height
    # ostateczność - najmniejszy rozmiar, nawet jeśli się nie mieści (przytniemy wizualnie)
    font = _font(font_name, min_size)
    lines = _wrap_text(draw, text, font, max_width)
    line_height = font.getbbox("Ślężąy")[3] * line_spacing
    return font, lines, line_height


def generate_post_image(submission_id: int, content: str) -> str:
    """
    Renderuje grafikę pod zgłoszenie i zapisuje jako JPEG.
    Zwraca nazwę pliku (nie pełną ścieżkę) w static/generated/.
    """
    bg = _vertical_gradient(SIZE, COLOR_GRADIENT_TOP, COLOR_GRADIENT_BOTTOM).convert("RGBA")
    canvas = Image.new("RGBA", (SIZE, SIZE))
    canvas.alpha_composite(bg)

    card_box = (CARD_MARGIN, CARD_MARGIN, SIZE - CARD_MARGIN, SIZE - CARD_MARGIN)
    _rounded_rect_shadow(canvas, card_box, CARD_RADIUS)

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(card_box, radius=CARD_RADIUS, fill=COLOR_CARD)

    content_left = CARD_MARGIN + CARD_PADDING_X
    content_right = SIZE - CARD_MARGIN - CARD_PADDING_X
    content_width = content_right - content_left

    # --- nagłówek: kropka + nazwa strony, po prawej numer zgłoszenia ---
    header_y = CARD_MARGIN + CARD_PADDING_TOP
    dot_r = 10
    draw.ellipse(
        (content_left, header_y, content_left + dot_r * 2, header_y + dot_r * 2),
        fill=COLOR_ACCENT_BG,
    )
    label_font = _font("Poppins-SemiBold", 30)
    draw.text(
        (content_left + dot_r * 2 + 16, header_y - 8),
        SITE_LABEL,
        font=label_font,
        fill=COLOR_TEXT,
    )

    tag_font = _font("Poppins-SemiBold", 24)
    tag_text = f"#{submission_id}"
    tag_w = draw.textlength(tag_text, font=tag_font)
    tag_pad_x, tag_pad_y = 22, 10
    tag_box = (
        content_right - tag_w - tag_pad_x * 2,
        header_y - tag_pad_y,
        content_right,
        header_y + 34 + tag_pad_y,
    )
    draw.rounded_rectangle(tag_box, radius=22, fill=(240, 240, 245))
    draw.text(
        (tag_box[0] + tag_pad_x, tag_box[1] + tag_pad_y - 2),
        tag_text,
        font=tag_font,
        fill=COLOR_MUTED,
    )

    # --- duży cudzysłów dekoracyjny ---
    quote_font = _font("Poppins-ExtraBold", 130)
    quote_y = header_y + 70
    draw.text((content_left - 8, quote_y), "\u201d", font=quote_font, fill=COLOR_QUOTE_MARK)

    # --- treść zgłoszenia, wyśrodkowana w pionie w pozostałej przestrzeni karty ---
    text_top = quote_y + 130
    text_bottom = SIZE - CARD_MARGIN - CARD_PADDING_BOTTOM - 60
    available_height = text_bottom - text_top

    font, lines, line_height = _fit_text(draw, content.strip(), content_width, available_height)

    block_height = line_height * len(lines)
    start_y = text_top + max(0, (available_height - block_height) / 2)

    y = start_y
    for line in lines:
        draw.text((content_left, y), line, font=font, fill=COLOR_TEXT)
        y += line_height

    # --- stopka ---
    footer_y = SIZE - CARD_MARGIN - CARD_PADDING_BOTTOM + 10
    draw.line((content_left, footer_y, content_right, footer_y), fill=(230, 230, 236), width=2)
    footer_font = _font("Poppins-Medium", 24)
    draw.text((content_left, footer_y + 20), FOOTER_LABEL, font=footer_font, fill=COLOR_MUTED)

    filename = f"post_{submission_id}.jpg"
    out_path = os.path.join(OUTPUT_DIR, filename)
    canvas.convert("RGB").save(out_path, "JPEG", quality=92)
    return filename
