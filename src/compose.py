"""Генератор постов: превращает записи курируемой базы в готовую очередь.

    python -m src.compose --dry-run    показать план: о чём будут посты
    python -m src.compose --submit     отправить пачку в Batch API (вдвое дешевле)
    python -m src.compose --fetch      забрать готовое и разложить в очередь
    python -m src.compose --now N      написать N постов сразу, без батча (дороже)

Батч асинхронный намеренно: один запуск отправляет задание, следующий забирает
результат. GitHub Actions не должен часами держать открытое соединение — а ждать
батч приходится до суток.

Единица работы здесь — не бренд, а сюжет (`stories` в data/brands.json). У одного
бренда их несколько: очки на капюшоне, смена имени, кому продали компанию. Так
двадцать брендов дают не двадцать постов, а сотню, и ни один не пересказывает
предыдущий.
"""

from __future__ import annotations

import argparse
import logging
import random
from pathlib import Path

from . import commons, config, llm, quality, state, telegram

log = logging.getLogger("compose")


# ─────────────────────────── очередь ───────────────────────────


def queue_size() -> int:
    return len(list(config.QUEUE.glob("*.json")))


def save_post(rubric_key: str, text: str, source: dict) -> Path:
    """Кладёт готовый пост в очередь. Имя файла задаёт порядок публикации.

    Разметку чистим сразу при сохранении, чтобы в очереди лежал ровно тот текст,
    который уйдёт в канал: иначе просмотр очереди врёт.

    Иллюстрация ищется здесь же, а не при отправке: найденное записывается
    в пост, ведущие видят картинку в превью и утверждают именно её. Ищи
    публикатор сам — в канал ушло бы не то, что утвердили, потому что выдача
    Commons со временем меняется.
    """
    config.QUEUE.mkdir(parents=True, exist_ok=True)
    stamp = state.now().strftime("%Y%m%d-%H%M%S")
    suffix = random.randint(1000, 9999)
    path = config.QUEUE / f"{stamp}-{suffix}-{rubric_key}.json"

    post = {
        "rubric": rubric_key,
        "text": telegram.sanitize(text),
        "created_at": state.iso(),
        # Тип поста и поля маркировки заведены с первого дня: дописать их
        # в готовый формат дороже, чем предусмотреть сейчас (см. config).
        "kind": "post",
        "advertiser": "",
        "erid": "",
        "brand": source.get("brand", ""),
        "story_id": source.get("story_id", ""),
        "sources": source.get("sources", []),
        "cover": "",
        "cover_credit": "",
    }

    pic = commons.fill(post)
    if pic is None:
        log.info("%s: фото не нашлось, пост выйдет с карточкой", path.name)

    state.write_json(path, post)
    return path


# ─────────────────────────── материал ───────────────────────────


# Курируемые базы остальных рубрик. Формат записи у всех трёх одинаковый
# (items с полями angle/facts/sources), поэтому кода на рубрику не нужно:
# отличается только промпт и файл.
ITEM_SOURCES = {
    "fresh": config.FRESH_FILE,
    "worn": config.CELEBRITIES_FILE,
    "unreleased": config.UNRELEASED_FILE,
}


def curated_items(path: Path) -> list[dict]:
    """Неиспользованные записи базы.

    Запись без ссылок не берётся вовсе. Строже всего это в «НЕ ВЫШЛО» —
    территории слухов, — но правило общее: проверить факт постфактум
    по посту в канале уже нельзя.
    """
    data = state.read_json(path, {"items": []})
    return [
        item
        for item in data.get("items", [])
        if not item.get("used") and item.get("facts") and item.get("sources")
    ]


def brand_stories() -> list[tuple[dict, dict]]:
    """Неиспользованные сюжеты рубрики ИСТОРИЯ: пары (бренд, сюжет)."""
    data = state.read_json(config.BRANDS_FILE, {"brands": []})
    pairs: list[tuple[dict, dict]] = []
    for brand in data.get("brands", []):
        for story in brand.get("stories", []):
            if not story.get("used") and story.get("facts"):
                pairs.append((brand, story))
    return pairs


def history_payload(brand: dict, story: dict) -> dict:
    """Данные для модели. Всё, чего здесь нет, писать нельзя — об этом сказано
    и в промпте рубрики, и в голосе канала."""
    return {
        "brand": {
            "name": brand.get("name", ""),
            "country": brand.get("country", ""),
            "city": brand.get("city", ""),
            "founded": brand.get("founded"),
            "founder": brand.get("founder", ""),
        },
        "roots": brand.get("roots", []),
        "angle": story.get("angle", ""),
        "facts": story.get("facts", []),
        "sources": story.get("sources", []) or brand.get("sources", []),
        "notes": brand.get("notes", ""),
    }


def plan(needed: int, only: str | None = None) -> list[tuple[str, str, dict, dict]]:
    """Задания: (custom_id, ключ рубрики, данные для модели, служебный источник).

    Рубрики, до которых ещё не дошли руки, в план не попадают: список рабочих
    задан в config.ACTIVE_RUBRICS. Веса из config.RUBRICS применяются к ним же,
    поэтому в первой фазе вся квота уходит ИСТОРИИ.

    `only` обходит этот список — так пишутся пробные посты рубрики, которую
    ещё не включили в ленту.
    """
    jobs: list[tuple[str, str, dict, dict]] = []
    counter = 0

    def add(rubric_key: str, payload: dict, source: dict) -> None:
        nonlocal counter
        counter += 1
        jobs.append((f"job-{counter:03d}-{rubric_key}", rubric_key, payload, source))

    keys = (only,) if only else config.ACTIVE_RUBRICS
    weights = {r.key: r.weight for r in config.RUBRICS if r.key in keys}
    total = sum(weights.values()) or 1
    quota = {key: max(1, round(needed * w / total)) for key, w in weights.items()}

    if "history" in quota:
        pairs = brand_stories()
        # Перемешиваем, чтобы лента не шла подряд по одному бренду: четыре поста
        # про Stone Island кряду читаются как реклама Stone Island.
        random.shuffle(pairs)
        for brand, story in pairs[: quota["history"]]:
            add(
                "history",
                history_payload(brand, story),
                {
                    "brand": brand.get("name", ""),
                    "brand_slug": brand.get("slug", ""),
                    "story_id": story.get("id", ""),
                    "sources": story.get("sources", []) or brand.get("sources", []),
                },
            )

    for key, path in ITEM_SOURCES.items():
        if key not in quota:
            continue
        items = curated_items(path)
        random.shuffle(items)
        for item in items[: quota[key]]:
            add(
                key,
                # Модель получает запись целиком: всё, что в ней есть, —
                # проверенный факт, а служебные поля ей ни к чему.
                {k: v for k, v in item.items() if k not in ("id", "used")},
                {
                    "brand": item.get("brand", ""),
                    "story_id": item.get("id", ""),
                    "sources": item.get("sources", []),
                },
            )

    return jobs[:needed]


def mark_used(story_ids: list[str]) -> None:
    """Помечает разобранные сюжеты — только после успешной генерации,
    чтобы сорванный батч не съел материал впустую."""
    if not story_ids:
        return
    wanted = set(story_ids)

    brands = state.read_json(config.BRANDS_FILE, {"brands": []})
    for brand in brands.get("brands", []):
        for story in brand.get("stories", []):
            if story.get("id") in wanted:
                story["used"] = True
    state.write_json(config.BRANDS_FILE, brands)

    for path in ITEM_SOURCES.values():
        data = state.read_json(path, None)
        if not data:
            continue
        touched = False
        for item in data.get("items", []):
            if item.get("id") in wanted and not item.get("used"):
                item["used"] = True
                touched = True
        if touched:
            state.write_json(path, data)


# ─────────────────────────── генерация ───────────────────────────


def generate_checked(rubric_key: str, payload: dict, attempts: int = 3) -> dict:
    """Пишет пост и проверяет его, повторяя при явном браке.

    Повторная попытка стоит дешевле, чем пост с выдуманным годом в канале.
    Осознанный отказ модели (skip) браком не считается: если фактов не хватило,
    второй заход их не добавит.
    """
    last: dict = {"skip": True, "text": "", "reason": "не удалось сгенерировать"}

    for attempt in range(attempts):
        result = llm.generate_now(rubric_key, payload)
        if result["skip"] or not result["text"]:
            return result

        issues = quality.problems(result["text"], rubric_key, payload)
        if not issues:
            return result

        last = result
        log.info("Попытка %d (%s) забракована: %s", attempt + 1, rubric_key, "; ".join(issues))

    return {
        "skip": True,
        "text": "",
        "reason": "брак после "
        + str(attempts)
        + " попыток: "
        + "; ".join(quality.problems(last["text"], rubric_key, payload)),
    }


def _accept(job: dict, result: dict, payload: dict | None = None) -> Path | None:
    """Общая приёмка результата: отбраковка, сохранение, журнал пробелов."""
    rubric = job["rubric"]
    source = job.get("source") or {}

    if result["skip"] or not result["text"]:
        state.log_gap(source.get("brand", "") or source.get("story_id", ""), result.get("reason", ""), rubric)
        return None

    issues = quality.problems(result["text"], rubric, payload)
    if issues:
        # Батч уже оплачен, второй заход внутри него невозможен: честнее
        # не публиковать и записать причину, чем чинить текст вручную.
        state.log_gap(source.get("brand", ""), "брак: " + "; ".join(issues), rubric)
        log.info("Забраковано %s: %s", job.get("custom_id", ""), "; ".join(issues))
        return None

    return save_post(rubric, result["text"], source)


# ─────────────────────────── режимы ───────────────────────────


def do_submit(needed: int) -> int:
    if config.BATCH_FILE.exists():
        pending = state.read_json(config.BATCH_FILE, {})
        print(f"Батч {pending.get('batch_id')} ещё не забран. Сначала выполни --fetch.")
        return 1

    jobs = plan(needed)
    if not jobs:
        print("Нечего генерировать: свободных сюжетов в базах нет. Пополни data/.")
        return 0

    try:
        batch_id = llm.submit_batch([(cid, key, payload) for cid, key, payload, _ in jobs])
    except Exception as exc:
        # Батч не принят — пишем поштучно. Дороже вдвое, но канал не встаёт.
        log.warning("Батч не отправился (%s). Генерирую поштучно.", exc)
        return do_now(len(jobs), jobs)

    state.write_json(
        config.BATCH_FILE,
        {
            "batch_id": batch_id,
            "submitted_at": state.iso(),
            "jobs": [
                {"custom_id": cid, "rubric": key, "payload": payload, "source": src}
                for cid, key, payload, src in jobs
            ],
        },
    )
    print(f"Отправлено заданий: {len(jobs)}. Батч: {batch_id}")
    return 0


def do_fetch() -> int:
    if not config.BATCH_FILE.exists():
        print("Нет отправленного батча.")
        return 0

    pending = state.read_json(config.BATCH_FILE, {})
    batch_id = pending["batch_id"]
    status = llm.batch_status(batch_id)

    if status != "ended":
        print(f"Батч {batch_id} ещё в работе (статус: {status}). Загляну позже.")
        return 0

    results = llm.fetch_batch(batch_id)
    created, skipped, done = 0, 0, []

    for job in pending["jobs"]:
        result = results.get(job["custom_id"])
        if not result:
            continue
        path = _accept(job, result, job.get("payload"))
        if path is None:
            skipped += 1
            continue
        story_id = (job.get("source") or {}).get("story_id", "")
        if story_id:
            done.append(story_id)
        created += 1

    mark_used(done)
    config.BATCH_FILE.unlink()
    print(f"Создано постов: {created}. Не прошли: {skipped}. В очереди: {queue_size()}.")
    return 0


def do_now(
    count: int,
    jobs: list[tuple[str, str, dict, dict]] | None = None,
    only: str | None = None,
) -> int:
    # Готовые задания приходят из сорвавшегося батча: план уже составлен,
    # второй раз тасовать сюжеты незачем.
    jobs = plan(count, only) if jobs is None else jobs
    if not jobs:
        print("Нечего генерировать: свободных сюжетов в базе нет.")
        return 0

    created, done = 0, []
    for custom_id, rubric_key, payload, source in jobs:
        result = generate_checked(rubric_key, payload)
        path = _accept(
            {"custom_id": custom_id, "rubric": rubric_key, "source": source}, result, payload
        )
        if path is None:
            print(f"  — {rubric_key}: пропущено ({result.get('reason', '')})")
            continue
        if source.get("story_id"):
            done.append(source["story_id"])
        created += 1
        print(f"  ✓ {rubric_key}: {path.name} — {source.get('brand', '')}")

    mark_used(done)
    print(f"\nСоздано постов: {created}. В очереди: {queue_size()}.")
    return 0


def do_dry_run(needed: int, only: str | None = None) -> int:
    jobs = plan(needed, only)
    print(f"\nГенератор: {llm.describe()}")
    print(f"В очереди сейчас: {queue_size()}. Нужно добрать: {needed}.\n")
    if not jobs:
        print("Свободных сюжетов нет. Пополни базу рубрики в data/.")
        return 0
    for _, rubric_key, payload, source in jobs:
        title = config.RUBRIC_BY_KEY[rubric_key].title
        print(f"  {title:<10} {source.get('brand', ''):<16} {payload.get('angle', '')}")
    print(f"\nВсего заданий: {len(jobs)}. Токенов пока не потрачено.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Генератор постов канала")
    parser.add_argument("--dry-run", action="store_true", help="показать план без затрат")
    parser.add_argument("--submit", action="store_true", help="отправить батч (вдвое дешевле)")
    parser.add_argument("--fetch", action="store_true", help="забрать готовый батч")
    parser.add_argument("--now", type=int, metavar="N", help="написать N постов сразу")
    parser.add_argument(
        "--rubric",
        choices=[r.key for r in config.RUBRICS],
        help="только эта рубрика, даже если она ещё не включена в ленту",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config.load_dotenv()

    needed = max(0, config.QUEUE_TARGET - queue_size())

    if args.dry_run:
        return do_dry_run(needed or config.QUEUE_TARGET, args.rubric)
    if args.now:
        return do_now(args.now, only=args.rubric)
    if args.fetch:
        return do_fetch()
    if args.submit:
        if needed <= 0:
            print(f"Очередь полна ({queue_size()} постов) — генерировать нечего.")
            return 0
        return do_submit(needed)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
