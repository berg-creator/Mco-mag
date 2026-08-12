"""Единая точка правды: пути, рубрики, лимиты, секреты.

Меняешь поведение канала — почти всегда сюда. Пути к данным берутся отсюда,
вручную их собирать не нужно: иначе при переезде файла придётся искать
все места, где он упомянут.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA = ROOT / "data"
PROMPTS = ROOT / "prompts"
QUEUE = ROOT / "content" / "queue"
ARCHIVE = ROOT / "content" / "archive"

# Курируемые базы. Пополняются руками и это сделано намеренно: бесплатных
# источников фактов о моде не существует, а придуманный год убивает канал
# быстрее всего. Модель пишет только из того, что здесь лежит.
BRANDS_FILE = DATA / "brands.json"
COLLECTIONS_FILE = DATA / "collections.json"
CELEBRITIES_FILE = DATA / "celebrities.json"
UNRELEASED_FILE = DATA / "unreleased.json"
FEEDS_FILE = DATA / "feeds.json"

# Служебное состояние конвейера.
INBOX_FILE = DATA / "inbox.jsonl"
SEEN_FILE = DATA / "seen.json"
POSTED_FILE = DATA / "posted.json"
USED_FILE = DATA / "used.json"
BATCH_FILE = DATA / "pending_batch.json"

# Журнал «чем пополнить базу»: сюда падает всё, о чём хотели написать,
# но фактов не хватило. Это список работы по реальному спросу, а не догадки.
GAPS_FILE = DATA / "gaps.jsonl"

# Сколько постов держим в очереди. Меньше QUEUE_MIN — сторож поднимает тревогу.
QUEUE_TARGET = 20
QUEUE_MIN = 5

# Минимальный интервал между публикациями. Публикатор смотрит не на часы,
# а на «сколько прошло с прошлого поста»: пропуск крона на бесплатном тарифе
# GitHub — обычное дело, и лента от него ломаться не должна.
PUBLISH_INTERVAL_HOURS = 6


@dataclass(frozen=True)
class Rubric:
    """Рубрика канала: чем кормится и с каким весом попадает в очередь."""

    key: str
    title: str
    # Доля в очереди. Сумма нормируется, точных чисел не требуется.
    weight: int
    # Из какой базы берётся материал: brands | feeds | celebrities | unreleased
    feeds_on: str
    description: str


RUBRICS: tuple[Rubric, ...] = (
    Rubric(
        key="history",
        title="ИСТОРИЯ",
        weight=45,
        feeds_on="brands",
        description=(
            "Как появился бренд, как рождалась старая коллекция, кто её конструировал. "
            "Главная рубрика: единственная, которую нельзя нагуглить за минуту."
        ),
    ),
    Rubric(
        key="fresh",
        title="НОВОЕ",
        weight=25,
        feeds_on="feeds",
        description=(
            "Свежие коллекции, дропы, коллаборации. Единственная рубрика "
            "на автосборе — и единственная, где повод приходит извне."
        ),
    ),
    Rubric(
        key="worn",
        title="НА КОМ",
        weight=20,
        feeds_on="celebrities",
        description=(
            "Любимые бренды знаменитостей и что они сделали для индустрии. "
            "Кто, что, когда и где это зафиксировано."
        ),
    ),
    Rubric(
        key="unreleased",
        title="НЕ ВЫШЛО",
        weight=10,
        feeds_on="unreleased",
        description=(
            "Отменённые коллекции, прототипы, сэмплы. Территория слухов, "
            "поэтому запись без ссылки на подтверждение постом не становится."
        ),
    ),
)

RUBRIC_BY_KEY = {r.key: r for r in RUBRICS}

# Какие рубрики уже работают. Остальные заведены в RUBRICS заранее, чтобы
# веса и промпты не пришлось переставлять задним числом, но в план генерации
# не попадают. Фаза 1 — только ИСТОРИЯ на курируемой базе брендов.
ACTIVE_RUBRICS: tuple[str, ...] = ("history",)

# ─────────────────────────── реклама ───────────────────────────
#
# Заложено с первого дня намеренно: дописать учёт рекламы в готовый формат
# постов дороже, чем предусмотреть поля сейчас. У поста есть тип:
#
#   post    обычный пост канала
#   native  нативная интеграция магазина — своя вещь внутри обычного текста
#   ad      платная реклама: в России требует пометки и токена erid
#
# Токен erid выдаёт не Telegram и не мы, а ОРД (оператор рекламных данных:
# Яндекс ОРД, VK ОРД, Медиаскаут). Это ручной шаг: рекламодатель регистрирует
# креатив, отдаёт токен, мы кладём его в поле. Автоматизировать нечем.
POST_KINDS = ("post", "native", "ad")

# Строка, которой размечается платный пост. По закону «О рекламе» нужны
# пометка, сведения о рекламодателе и erid.
AD_LABEL = "Реклама. {advertiser}. erid: {erid}"


def secret(name: str, *, required: bool = True) -> str:
    """Достаёт секрет из окружения. В GitHub Actions они приходят из Secrets."""
    value = os.environ.get(name, "").strip()
    if not value and required:
        raise RuntimeError(
            f"Не задана переменная окружения {name}. "
            f"Локально — положи её в .env, на GitHub — в Settings → Secrets → Actions."
        )
    return value


def admin_ids() -> list[str]:
    """Кому уходят посты на утверждение и чьи нажатия принимаются.

    Канал ведут несколько человек, поэтому id перечисляются через запятую.
    Превью приходит каждому, а решение принимается от любого: первый нажавший
    и решает судьбу поста — второму кнопка ответит, что поста в очереди уже нет.
    """
    raw = secret("TELEGRAM_ADMIN_ID", required=False)
    return [part.strip() for part in raw.split(",") if part.strip()]


def load_dotenv(path: Path | None = None) -> None:
    """Простейший загрузчик .env для локального запуска (в Actions не нужен).

    Отдельный пакет python-dotenv ради двадцати строк ставить незачем:
    каждая зависимость — это ещё одно место, которое может сломаться в CI.
    """
    env_path = path or (ROOT / ".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
