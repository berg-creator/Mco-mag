"""Иллюстрация к посту с Wikimedia Commons: фото самой вещи.

Почему только Commons. Пресс-фото брендов и кадры показов каналу, который
продаёт рекламу, брать нельзя — это коммерческое использование чужих снимков,
и требование придёт каналу. Commons — единственный источник, где лицензию
можно проверить машиной, а не на глаз: у каждого файла есть машинный код
лицензии, флаг обязательной атрибуции и список ограничений.

Что здесь важнее кода — лицензия и атрибуция:

* NC (только некоммерческое) и ND (без переработки) не годятся вовсе: канал
  продаёт рекламу, то есть использование коммерческое. GFDL тоже мимо —
  она требует таскать за картинкой полный текст лицензии.
* CC BY и CC BY-SA требуют указать автора. Строка атрибуции собирается здесь
  и уходит в подпись; не влезла в 1024 знака — фото не отправляется вовсе,
  потому что фото без подписи автора это нарушение, а не мелочь.
* Непустое поле Restrictions (товарный знак, права изображённого человека)
  отбрасывает файл: для коммерческого канала это ровно тот риск, из-за
  которого мы не берём пресс-фото.

Поиск строится из `brand` и `story_id` — story_id уже латиницей и по делу
(`si-tela-stella`, `run-dmc-adidas-1986`), поэтому ни схему постов, ни промпты
трогать не пришлось. Отвергнутая альтернатива — просить у модели отдельное
поле с английским запросом: та же точность за лишнее поле в схеме, лишнюю
строку в промпте и правку всех уже сгенерированных постов.

Находится далеко не всё: вещь должен был кто-то сфотографировать и выложить
под свободной лицензией. Отменённого сэмпла Nike SB там нет и не будет.
Не нашлось — пост уходит с карточкой `src.cover`, и это видно в сухом прогоне.

    python -m src.commons --dry-run   что нашлось бы для очереди
    python -m src.commons --fill      записать найденное в поле cover
"""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter, Retry

from . import config, state, telegram

log = logging.getLogger("commons")

API = "https://commons.wikimedia.org/w/api.php"
# Commons просит опознаваемый User-Agent и без него отвечает отказом.
# Только латиница: в HTTP-заголовок кириллица не помещается вовсе.
UA = "mcomag/0.1 (Telegram @mcomagazine)"
TIMEOUT = 20

# Commons ограничивает частоту и отвечает 429. Заметно это только на пачке:
# батч на 15 постов — до 45 запросов подряд, и после четвёртого поста Commons
# начинает отказывать всем остальным. Выглядит это как «фото не нашлось»,
# хотя фото есть, — и вся пачка уходит с карточками-заглушками.
#
# Отступ считает urllib3 внутри requests: Retry-After от Commons он читает сам,
# и своего кода с таймерами писать не нужно.
_SESSION = requests.Session()
_SESSION.mount(
    "https://",
    HTTPAdapter(max_retries=Retry(total=4, backoff_factor=2, status_forcelist=(429, 503))),
)

# Ширина, до которой просим уменьшить картинку. Оригиналы на Commons бывают
# по двадцать мегабайт — Telegram такое по ссылке не скачает.
THUMB_WIDTH = 1280

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def license_ok(code: str) -> bool:
    """Годится ли лицензия каналу, который продаёт рекламу."""
    code = code.lower()
    if "-nc" in code or "-nd" in code:
        return False
    return code.startswith(("pd", "cc0", "cc-by"))


def _plain(html: str) -> str:
    """Автор приходит куском HTML со ссылкой на профиль — оставляем имя."""
    return _SPACE.sub(" ", _TAG.sub("", html)).strip()


# Какая доля ключевых слов сюжета должна найтись в названии файла. Порог
# нужен из-за буквальных совпадений: по запросу «Stone Island ghost campaign»
# Commons честно предлагает каменный дом на Большом Заяцком острове, а по
# «Nike SB Dunk Freddy Krueger» — обычный Dunk 2023 года. Показать читателю
# не ту вещь хуже, чем не показать ничего: канал держится на том, что факт
# в нём проверяемый.
RELEVANCE = 0.6

_WORD = re.compile(r"[a-z0-9]+")

# Слова, по которым в категориях Commons узнаётся вещь, а не место и не человек.
# Нужны из-за омонимов: «Stone Island.jpg» — это остров, и категория у него
# одна, «Stone Island». У куртки из той же ткани категории две: «Jackets»
# и «Stone Island» — вот по второму слову вещь и отличается от географии.
THING_WORDS = (
    "shoe", "sneaker", "footwear", "boot", "trainer",
    "jacket", "coat", "parka", "hoodie", "knitwear", "shirt", "t-shirt",
    "trouser", "jeans", "denim", "dress", "suit", "uniform", "garment",
    "clothing", "cloth", "fashion", "apparel", "sportswear",
    "hat", "cap", "bag", "handbag", "sunglass", "watch",
)


def keywords(post: dict) -> list[str]:
    """Ключевые слова сюжета — по ним проверяется попадание.

    Считаются по story_id, а не по бренду: у файла «Tela stella» слов бренда
    в названии нет вовсе, и требование их найти выбросило бы единственное
    точное фото к посту про Stone Island.

    Годы выбрасываем: в названиях файлов на Commons их почти не пишут,
    а «run dmc adidas 1986» из-за одного года потеряло бы верное фото.
    """
    words = post.get("story_id", "").split("-")
    return [w for w in dict.fromkeys(words) if len(w) > 2 and not w.isdigit()]


def query(post: dict) -> str:
    """Запрос к Commons. Бренд и сюжет вместе: story_id уже латиницей.

    Запроса по одному бренду тут намеренно нет: он находит витрины, логотипы
    и одноимённые географические места, то есть ровно то, что потом отсеет
    проверка релевантности.
    """
    story = " ".join(part for part in post.get("story_id", "").split("-") if len(part) > 2)
    return f"{post.get('brand', '').strip()} {story}".strip()


def relevant(pic: dict, words: list[str]) -> bool:
    """Та самая вещь, о которой пост."""
    if not words:
        return False
    haystack = set(_WORD.findall(f"{pic['what']} {pic['file']}".lower()))
    hits = sum(1 for word in words if word in haystack)
    return hits / len(words) >= RELEVANCE


def is_thing(pic: dict) -> bool:
    """Вещь ли на фото — судим по категориям Commons.

    Слово сверяется целиком, а не подстрокой и не по началу: короткое «cap»
    сидит и в «landscape», и в «capitol», из-за чего посту про владельцев
    Salomon достался речной пейзаж Саломона ван Рёйсдала, а посту про деки
    Supreme — здание Верховного суда США. Допускается только множественное
    число: в категориях Commons пишут «jackets» и «shoes», а не «jacket».
    """
    words = _WORD.findall(" ".join(pic["cats"]))
    forms = {form for thing in THING_WORDS for form in (thing, thing + "s", thing + "es")}
    return any(word in forms for word in words)


def is_store(pic: dict) -> bool:
    """Витрина магазина. Формально одежда, а по сути — вывеска.

    Отсекается отдельно, потому что по категориям («Clothing stores»)
    магазин от вещи не отличить, а к посту о вещи витрина не ассоциируется
    ничем, кроме логотипа.
    """
    cats = " ".join(pic["cats"])
    return any(word in cats for word in ("shop", "store", "boutique"))


def of_brand(pic: dict, brand: str) -> bool:
    """Вещь того самого бренда — по категориям, а не по названию файла.

    У куртки из Tela Stella названия бренда в имени файла нет вовсе, зато
    есть в категориях. Требуем обе метки — бренд и вещь: без второй посту
    про Stone Island достанется одноимённый остров, без первой — чужая куртка.
    """
    cats = " ".join(pic["cats"])
    words = [w for w in brand.lower().split() if len(w) > 2]
    return bool(words) and all(word in cats for word in words) and is_thing(pic)


def theme(post: dict) -> list[str]:
    """Слова сюжета без слов бренда: собственно тема поста.

    Для «nike-sb-dunk-freddy-krueger-2007» это dunk, freddy, krueger — то,
    по чему видно, что фото хотя бы про ту модель, а не «что-нибудь Nike».
    """
    brand_words = set(post.get("brand", "").lower().split())
    return [w for w in keywords(post) if w not in brand_words]


def rank(pic: dict, post: dict) -> int:
    """Насколько фото годится посту: 3 — та вещь, 2 — по теме, 1 — бренд, 0 — нет.

    Ассоциация нужна именно с темой поста, а не с брендом вообще: посту
    про отменённый Dunk кроссовок Dunk другого года — родня, а витрина
    магазина или футбольная форма того же бренда — нет.
    """
    if is_store(pic):
        return 0
    if relevant(pic, keywords(post)):
        return 3
    words = set(_WORD.findall(f"{pic['what']} {pic['file']} {' '.join(pic['cats'])}".lower()))
    if is_thing(pic) and any(word in words for word in theme(post)):
        return 2
    if of_brand(pic, post.get("brand", "")):
        return 1
    return 0


def search(query: str, limit: int = 8) -> list[dict]:
    """Ищет картинки и оставляет те, что каналу можно взять."""
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        # filetype:bitmap отсекает схемы и карты в SVG: нам нужна фотография.
        "gsrsearch": f"filetype:bitmap {query}",
        "gsrnamespace": 6,
        "gsrlimit": limit,
        # Категории — тем же запросом: по ним отличается вещь от одноимённого
        # острова, и по ним же находится вещь нужного бренда.
        "prop": "imageinfo|categories",
        "iiprop": "url|extmetadata",
        "iiurlwidth": THUMB_WIDTH,
        "cllimit": 100,
        "clshow": "!hidden",
    }
    try:
        response = _SESSION.get(API, params=params, headers={"User-Agent": UA}, timeout=TIMEOUT)
        response.raise_for_status()
        pages = (response.json().get("query") or {}).get("pages") or {}
    except (requests.RequestException, ValueError) as exc:
        # Молчание Commons — не повод ронять генерацию: пост выйдет с карточкой.
        log.warning("Commons не ответил (%s)", exc)
        return []

    found = []
    # Порядок выдачи задаётся полем index, а не порядком ключей в словаре.
    for page in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})

        def field(name: str) -> str:
            return str(meta.get(name, {}).get("value", "") or "")

        if not license_ok(field("License")):
            continue
        if field("Restrictions"):
            # Товарный знак или права изображённого человека — то самое,
            # из-за чего мы не берём пресс-фото.
            continue

        url = info.get("thumburl") or info.get("url")
        if not url:
            continue

        found.append(
            {
                "url": url,
                "what": field("ObjectName") or Path(page["title"][5:]).stem,
                "file": Path(page["title"][5:]).stem,
                "cats": [c["title"][9:].lower() for c in page.get("categories", [])],
                "author": _plain(field("Artist")),
                "license": field("LicenseShortName"),
                "page": info.get("descriptionurl", ""),
            }
        )
    return found


def credit(pic: dict) -> str:
    """Подпись под фото: что на нём, кто автор, какая лицензия.

    Автора пишем всегда, даже когда лицензия не обязывает. И всегда прямо
    говорим, если на фото другая вещь бренда: выдать похожую вещь за ту самую —
    это то же вранье, что выдуманный год, а канал держится на обратном.
    """
    parts = [pic["what"]]
    if pic["author"]:
        parts.append(pic["author"])
    parts.append(f"{pic['license']}, Wikimedia Commons")
    head = "На фото" if pic.get("exact", True) else "На фото не предмет поста"
    return f"{head}: " + " · ".join(parts)


def find(post: dict) -> dict | None:
    """Картинка к посту: сначала та самая вещь, потом другая вещь бренда.

    Вторая очередь нужна потому, что фото именно той вещи есть далеко
    не всегда: отменённый сэмпл Nike SB или кампания этого сезона никем
    под свободной лицензией не выложены. Кроссовок того же бренда рядом
    с текстом честнее и полезнее, чем плашка с заголовком, — при условии,
    что подпись прямо говорит, что вещь другая.
    """
    brand = post.get("brand", "")
    best_score, best = 0, None

    # incategory ищет внутри категории бренда — там лежат его вещи, а не
    # одноимённые острова. Свободный поиск по бренду идёт последним: он самый
    # мусорный.
    #
    # Кандидаты собираются со всех запросов и сравниваются между собой:
    # взять первого подошедшего было ошибкой — посту про кампанию Stone Island
    # доставалась витрина магазина из первого запроса, хотя фото куртки лежало
    # в категории бренда.
    for request in (query(post), f'incategory:"{brand}"', brand):
        for pic in search(request):
            score = rank(pic, post)
            if score == 3:
                return pic | {"exact": True}
            if score > best_score:
                best_score, best = score, pic
    return best | {"exact": False} if best else None


def fits(post: dict, pic: dict) -> bool:
    """Влезает ли пост вместе со строкой атрибуции в подпись к фото.

    Не влезает — фото не берём: подпись автора не та часть, которую можно
    обрезать, а Telegram режет подпись по 1024 знакам молча.
    """
    from .publish import caption  # локально: publish тоже импортирует нас

    return len(telegram.sanitize(caption(post, credit(pic)))) <= telegram.MAX_CAPTION


def apply(post: dict, pic: dict) -> None:
    """Записывает найденное в пост."""
    post["cover"] = pic["url"]
    post["cover_credit"] = credit(pic)


def fill(post: dict) -> dict | None:
    """Ищет посту картинку и дописывает её. Возвращает картинку или None."""
    if post.get("cover"):
        return None
    pic = find(post)
    if pic is None or not fits(post, pic):
        return None
    apply(post, pic)
    return pic


def main() -> int:
    parser = argparse.ArgumentParser(description="Иллюстрации с Wikimedia Commons")
    parser.add_argument("--dry-run", action="store_true", help="показать, что нашлось, не записывая")
    parser.add_argument("--fill", action="store_true", help="записать найденное в посты очереди")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    assert license_ok("cc0") and license_ok("cc-by-sa-4.0") and license_ok("pd")
    assert not license_ok("cc-by-nc-2.0") and not license_ok("cc-by-nd-4.0")
    assert not license_ok("gfdl")
    # Тот самый каменный дом: по словам совпадает, по смыслу — нет.
    ghost = {"brand": "Stone Island", "story_id": "stone-island-ghost-fw2026-campaign"}
    house = {"what": "RUS-2016-Bolshoi Zayatsky Island-Stone House", "file": "stone-house"}
    assert not relevant(house, keywords(ghost))
    tela = {"what": "Tela stella", "file": "Tela stella", "cats": ["jackets", "stone island"]}
    assert relevant(tela, keywords({"brand": "Stone Island", "story_id": "si-tela-stella"}))
    # Остров и куртка лежат в одной категории «Stone Island», а вещь только одна.
    assert of_brand(tela, "Stone Island")
    island = {"what": "Stone Island", "file": "Stone Island", "cats": ["stone island"]}
    assert not of_brand(island, "Stone Island")
    # Пейзаж голландца по имени Саломон — не кроссовки Salomon: «landscape»
    # не кепка, даже если «cap» в нём и правда есть.
    ruysdael = {
        "what": "Salomon van Ruysdael - A river landscape",
        "file": "salomon-van-ruysdael",
        "cats": ["17th-century landscape paintings of the netherlands"],
    }
    assert not is_thing(ruysdael) and not of_brand(ruysdael, "Salomon")
    # Здание Верховного суда — не кепка: «capitol» тоже начинается на «cap».
    court = {
        "what": "Supreme Court of the United States",
        "file": "supreme-court",
        "cats": ["united states supreme court building from the united states capitol"],
    }
    assert not is_thing(court)
    # Множественное число в категориях — обычное дело, его терять нельзя.
    assert is_thing({"cats": ["handbags", "dresses"]}) and is_thing({"cats": ["nike shoes"]})
    # Витрина магазина — не вещь, даже когда категория называется «Clothing stores».
    soho = {"what": "Stone Island - SoHo", "file": "soho", "cats": ["clothing stores"]}
    assert rank(soho, {"brand": "Stone Island", "story_id": "stone-island-ghost"}) == 0
    # Dunk другого года посту про отменённый Dunk — родня по теме, но не он.
    dunk = {"what": "2023 Nike SB Dunk Low Pro", "file": "dunk", "cats": ["nike shoes"]}
    assert rank(dunk, {"brand": "Nike SB", "story_id": "nike-sb-dunk-freddy-krueger-2007"}) == 2

    posts = sorted(config.QUEUE.glob("*.json"))
    if not posts:
        print("Очередь пуста.")
        return 0

    for path in posts:
        post = state.read_json(path, {})
        print(f"\n{path.name} · {post.get('brand', '')} · {post.get('story_id', '')}")
        if post.get("cover"):
            print(f"  обложка уже есть: {post['cover']}")
            continue

        pic = find(post)
        if pic is None:
            print("  не нашлось — пост уйдёт с карточкой")
            continue
        if not fits(post, pic):
            print(f"  нашлось «{pic['what']}», но с подписью автора не влезает в 1024 знака")
            continue

        print(f"  {pic['what']} · {pic['license']}")
        print(f"  {pic['url']}")
        print(f"  {credit(pic)}")
        if args.fill:
            # Записываем именно то, что показали: искать второй раз через
            # fill() значило бы получить, возможно, другую картинку — и один
            # пост так и остался без обложки, хотя вывод обещал обратное.
            apply(post, pic)
            state.write_json(path, post)
            print("  записано в пост")

    if not args.fill:
        print("\nСухой прогон: посты не тронуты. Записать — python -m src.commons --fill")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
