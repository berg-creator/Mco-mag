"""Обложки постов: карточка с заголовком, нарисованная своим шрифтом.

Почему карточки, а не фотографии вещей. Пресс-фото брендов и кадры показов
каналу, который продаёт рекламу, брать нельзя: это коммерческое использование
чужих снимков, и требование придёт каналу, а не «интернету». Живых источников
три — съёмки магазина (файлов пока нет и они закроют только то, что есть
в наличии), Wikimedia Commons (вещей там почти нет, находятся логотипы
и витрины) и карточки, которые рисуем сами. Карточка работает для любого
поста без исключения, стоит ноль и ничьих прав не трогает.

Отвергнутая альтернатива — рисовать обложки заранее и складывать в репозиторий:
воркфлоу коммитят `content/` обратно, и каждая публикация тащила бы в историю
git по картинке. Рисуем на месте, в системный временный каталог: сорок
миллисекунд на карточку дешевле распухшей истории.

Шрифт берётся с диска, а не из Pillow: встроенный в Pillow шрифт кириллицы
не знает и рисует вместо букв квадратики — проверено. Нет шрифта на диске —
обложки нет, и пост уходит текстом; ронять публикацию из-за картинки нельзя.

    python -m src.cover --demo     нарисовать обложки для всей очереди
"""

from __future__ import annotations

import argparse
import logging
import re
import tempfile
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import config, state

log = logging.getLogger("cover")

# Первый существующий и побеждает. DejaVu — то, что есть на ubuntu в Actions,
# Arial и Helvetica — на макбуке владельца. Все три знают кириллицу.
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)

SIZE = 1080
MARGIN = 88

BACKGROUND = (18, 16, 14)
HEADLINE_COLOR = (242, 239, 233)
HANDLE_COLOR = (110, 106, 100)

# Цвет рубрики: читатель узнаёт рубрику до того, как прочтёт подпись.
# Живёт здесь, а не в config.RUBRICS — это про вид карточки, а не про
# поведение канала.
ACCENT = {
    "history": (200, 169, 126),
    "fresh": (143, 184, 168),
    "worn": (208, 140, 96),
    "unreleased": (136, 146, 160),
}
ACCENT_DEFAULT = (200, 169, 126)

# Заголовок подбирается сверху вниз: первый размер, который влезает в блок.
HEADLINE_SIZES = (96, 84, 76, 68, 60, 54, 48, 42)
LINE_SPACING = 1.18

LABEL_SIZE = 34
HANDLE_SIZE = 30
HANDLE = "@mcomagazine"

_TAG = re.compile(r"<[^>]+>")
_UNSAFE = re.compile(r"[^a-z0-9-]+")


@lru_cache(maxsize=1)
def font_path() -> str | None:
    """Путь к шрифту с кириллицей или None, если ни одного нет."""
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


@lru_cache(maxsize=None)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path(), size)


def headline(post: dict) -> str:
    """Заголовок поста — его первая строка, без разметки."""
    first = post.get("text", "").strip().split("\n", 1)[0]
    return _TAG.sub("", first).strip()


def label(post: dict) -> str:
    """Верхняя строка карточки: рубрика и бренд."""
    rubric = config.RUBRIC_BY_KEY.get(post.get("rubric", ""))
    parts = [rubric.title if rubric else post.get("rubric", ""), post.get("brand", "")]
    return " · ".join(part for part in parts if part).upper()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Переносит текст по замеренной ширине: посчитать буквы нельзя, «ЖЖЖ»
    и «III» одной длины строки занимают втрое разное место."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        probe = f"{line} {word}".strip()
        # Слово шире блока целиком всё равно ставим в строку: обрезать
        # слово посреди заголовка хуже, чем вылезти за поле.
        if not line or draw.textlength(probe, font=font) <= width:
            line = probe
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def draw(post: dict) -> Path | None:
    """Рисует обложку и возвращает путь к файлу. None — если шрифта нет."""
    if font_path() is None:
        log.warning(
            "Шрифта с кириллицей на диске нет (искал %s) — пост уйдёт без обложки",
            ", ".join(FONT_CANDIDATES),
        )
        return None

    accent = ACCENT.get(post.get("rubric", ""), ACCENT_DEFAULT)
    image = Image.new("RGB", (SIZE, SIZE), BACKGROUND)
    canvas = ImageDraw.Draw(image)
    box_width = SIZE - 2 * MARGIN

    canvas.text((MARGIN, MARGIN), label(post), font=_font(LABEL_SIZE), fill=accent)
    rule_y = MARGIN + LABEL_SIZE + 28
    canvas.line((MARGIN, rule_y, MARGIN + 160, rule_y), fill=accent, width=3)

    top = rule_y + 70
    bottom = SIZE - MARGIN - HANDLE_SIZE - 40
    text = headline(post)
    for size in HEADLINE_SIZES:
        font = _font(size)
        lines = _wrap(canvas, text, font, box_width)
        height = len(lines) * size * LINE_SPACING
        if height <= bottom - top:
            break

    # Блок заголовка по центру свободного поля: пустота делится пополам,
    # и короткий заголовок выглядит поставленным нарочно, а не забытым внизу.
    y = top + (bottom - top - height) / 2
    for line in lines:
        canvas.text((MARGIN, y), line, font=font, fill=HEADLINE_COLOR)
        y += size * LINE_SPACING

    canvas.text(
        (MARGIN, SIZE - MARGIN - HANDLE_SIZE),
        HANDLE,
        font=_font(HANDLE_SIZE),
        fill=HANDLE_COLOR,
    )

    name = _UNSAFE.sub("-", (post.get("story_id") or "post").lower()).strip("-")
    out = Path(tempfile.gettempdir()) / f"mco-cover-{name or 'post'}.jpg"
    image.save(out, "JPEG", quality=88, optimize=True)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Обложки постов")
    parser.add_argument(
        "--demo", action="store_true", help="нарисовать обложки для всей очереди и проверить их"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    print(f"Шрифт: {font_path() or 'НЕ НАЙДЕН — обложек не будет'}")
    if not args.demo:
        return 0

    posts = sorted(config.QUEUE.glob("*.json"))
    if not posts:
        print("Очередь пуста.")
        return 0

    for item in posts:
        post = state.read_json(item, {})
        path = draw(post)
        assert path is not None and path.stat().st_size > 5000, f"пустая обложка для {item.name}"
        assert headline(post), f"у {item.name} нет заголовка"
        print(f"{item.name} → {path} ({path.stat().st_size // 1024} КБ)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
