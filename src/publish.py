"""Публикатор: берёт следующий пост из очереди и отправляет его.

    python -m src.publish --check            бот, канал и права на месте
    python -m src.publish --dry-run          показать, что ушло бы в канал
    python -m src.publish --target admin     прислать себе в личку с кнопками
    python -m src.publish --target channel   опубликовать в канал
    python -m src.publish --preview-all      прислать себе всю очередь целиком

Публикатор смотрит не на часы, а на время последней публикации. Пропуск запуска
по расписанию — обычное дело на бесплатном тарифе GitHub, и лента от него
ломаться не должна: следующий запуск просто увидит, что пауза затянулась.

Модерация включена с первого дня: пока PUBLISH_TARGET=admin, посты уходят
владельцу в личку с кнопками, а не читателям.
"""

from __future__ import annotations

import argparse
import logging
from datetime import timedelta
from pathlib import Path

from . import config, state, telegram

log = logging.getLogger("publish")


def next_post() -> Path | None:
    """Самый ранний пост в очереди — порядок задаётся именем файла."""
    posts = sorted(config.QUEUE.glob("*.json"))
    return posts[0] if posts else None


def due() -> bool:
    """Пора ли публиковать — исходя из времени прошлой публикации."""
    posted = state.read_json(config.POSTED_FILE, {"items": []})
    items = posted.get("items", [])
    if not items:
        return True

    last = state.parse(items[-1].get("published_at", ""))
    if last is None:
        return True
    return state.now() - last >= timedelta(hours=config.PUBLISH_INTERVAL_HOURS)


def render(post: dict) -> str:
    """Текст поста в том виде, в каком он уходит читателю.

    Платная реклама в России требует пометки, сведений о рекламодателе
    и токена erid — без них штраф получает канал, а не рекламодатель.
    Поэтому маркировка приклеивается здесь, а не доверяется модели:
    забыть её в промпте легко, забыть в коде — нет.
    """
    text = post.get("text", "").strip()
    if post.get("kind") != "ad":
        return text

    label = config.AD_LABEL.format(
        advertiser=post.get("advertiser", "") or "рекламодатель не указан",
        erid=post.get("erid", "") or "erid отсутствует",
    )
    return f"{text}\n\n<i>{label}</i>"


def send(post: dict, chat_id: str) -> None:
    """Отправляет пост: с обложкой, если она есть и текст влезает в подпись.

    Пост без картинки в канале про одежду не читают, но текст важнее картинки:
    если обложка не ушла, пост всё равно должен выйти.
    """
    text = render(post)
    cover = post.get("cover", "")

    if cover and len(telegram.sanitize(text)) <= telegram.MAX_CAPTION:
        try:
            telegram.send_photo(chat_id, cover, text)
            return
        except telegram.TelegramError as exc:
            log.warning("Фото не ушло (%s), отправляю текстом", exc)

    telegram.send_message(chat_id, text)


def record(post: dict, path: Path, chat: str) -> None:
    """Пишет факт публикации. Это же — заготовка медиакита: без истории
    с первого дня первые полгода цифр просто не будет, а спросят именно их."""
    posted = state.read_json(config.POSTED_FILE, {"items": []})
    posted.setdefault("items", []).append(
        {
            "file": path.name,
            "rubric": post.get("rubric", ""),
            "brand": post.get("brand", ""),
            "kind": post.get("kind", "post"),
            "chat": chat,
            "published_at": state.iso(),
        }
    )
    # Храним последние 500 записей: этого хватает для аналитики и не раздувает файл.
    posted["items"] = posted["items"][-500:]
    state.write_json(config.POSTED_FILE, posted)


def archive(path: Path) -> None:
    config.ARCHIVE.mkdir(parents=True, exist_ok=True)
    path.rename(config.ARCHIVE / path.name)


def send_for_approval(post: dict, path: Path, chat_ids: list[str]) -> None:
    """Показывает пост ведущим канала и подкладывает кнопки решения.

    Кнопки идут отдельным сообщением: пост может оказаться фотографией,
    а к ней клавиатуру приложить получается не всегда.

    Ведущих несколько, поэтому пост уходит каждому. Решение принимается первым
    нажатием: второму кнопка честно ответит, что поста в очереди уже нет.
    """
    # Отметка «уже у ведущих»: пост остаётся в очереди, пока не нажали кнопку,
    # а публикатор запускается по расписанию — без отметки он присылал бы
    # один и тот же пост каждые несколько часов.
    post["sent_at"] = state.iso()
    state.write_json(path, post)

    rubric = config.RUBRIC_BY_KEY.get(post.get("rubric", ""))
    title = rubric.title if rubric else post.get("rubric", "")
    brand = post.get("brand", "")

    sources = post.get("sources", [])
    hint = "\n\n<i>Источники: " + ", ".join(sources) + "</i>" if sources else ""

    for chat_id in chat_ids:
        try:
            telegram.send_message(
                chat_id, f"— — — <b>{title}</b>{f' · {brand}' if brand else ''} — — —"
            )
            send(post, chat_id)
            telegram.send_message(
                chat_id,
                "Что делаем с постом?"
                "\n<i>Кнопка подумает пару минут — решения применяются по расписанию.</i>" + hint,
                buttons=telegram.approval_buttons(path.name),
            )
        except telegram.TelegramError as exc:
            # Один не написал боту или заблокировал его — остальные должны
            # получить пост в любом случае.
            log.warning("Не доставлено %s: %s", chat_id, exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Публикация постов в Telegram")
    parser.add_argument(
        "--target",
        choices=["admin", "channel"],
        default="admin",
        help="куда отправлять: admin — себе в личку (по умолчанию), channel — в канал",
    )
    parser.add_argument("--dry-run", action="store_true", help="показать пост, не отправляя")
    parser.add_argument("--check", action="store_true", help="проверить настройки бота и канала")
    parser.add_argument("--force", action="store_true", help="игнорировать интервал между постами")
    parser.add_argument(
        "--preview-all",
        action="store_true",
        help="прислать себе в личку всю очередь целиком, ничего не публикуя",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config.load_dotenv()

    if args.check:
        return do_check()

    if args.preview_all:
        posts = sorted(config.QUEUE.glob("*.json"))
        if not posts:
            print("Очередь пуста.")
            return 0
        chat_ids = config.admin_ids()
        for chat_id in chat_ids:
            telegram.send_message(
                chat_id,
                f"<b>Очередь на просмотр — {len(posts)} постов.</b>\n"
                f"Это превью: в канал ничего не ушло.",
            )
        for item in posts:
            send_for_approval(state.read_json(item, {}), item, chat_ids)
        print(f"Отправлено на просмотр: {len(posts)}. Очередь не тронута.")
        return 0

    path = next_post()
    if path is None:
        print("Очередь пуста. Запусти генерацию: python -m src.compose --submit")
        return 0

    post = state.read_json(path, {})

    if args.dry_run:
        print(f"\nФайл: {path.name}")
        print(f"Рубрика: {post.get('rubric')} · {post.get('brand', '')}")
        print(f"Тип: {post.get('kind', 'post')} · обложка: {post.get('cover') or 'нет'}\n")
        print(render(post))
        return 0

    if not args.force and not due():
        print(f"Рано: с прошлой публикации не прошло {config.PUBLISH_INTERVAL_HOURS} ч.")
        return 0

    if args.target == "channel":
        chat_id = config.secret("TELEGRAM_CHANNEL_ID")
        send(post, chat_id)
        record(post, path, "channel")
        archive(path)
        print(f"Опубликовано: {path.name}. Осталось в очереди: {queue_left()}")
    else:
        # В личку пост уходит с кнопками и остаётся в очереди, пока кто-то
        # из ведущих не нажмёт «В канал» или «Удалить». Пока решения нет,
        # второй раз не шлём: расписание иначе долбит одним и тем же постом.
        if post.get("sent_at") and not args.force:
            print(f"{path.name} ждёт решения с {post['sent_at']} — повторно не шлю. "
                  f"Нужно ещё раз — запусти с --force.")
            return 0
        send_for_approval(post, path, config.admin_ids())
        print(f"Отправлено на утверждение: {path.name}")

    return 0


def queue_left() -> int:
    return len(list(config.QUEUE.glob("*.json")))


def do_check() -> int:
    """Проверка настройки: бот, канал, права, очередь. Ничего не публикует."""
    print(f"Бот на связи: {telegram.check()}")

    channel = config.secret("TELEGRAM_CHANNEL_ID", required=False)
    if channel:
        try:
            chat = telegram.get_chat(channel)
            print(f"Канал: {chat.get('title', '?')} ({channel}), подписчиков: "
                  f"{telegram.member_count(channel)}")
            admins = {a.get("user", {}).get("username", "") for a in telegram.administrators(channel)}
            bot_name = telegram.check().lstrip("@")
            print("Бот в администраторах канала." if bot_name in admins
                  else "ВНИМАНИЕ: бот не администратор канала — публиковать не сможет.")
        except telegram.TelegramError as exc:
            print(f"Канал недоступен: {exc}")
    else:
        print("TELEGRAM_CHANNEL_ID не задан.")

    admins = config.admin_ids()
    if admins:
        for chat_id in admins:
            try:
                telegram.send_message(
                    chat_id, "<b>Mco mag</b> на связи. Проверка прошла успешно."
                )
                print(f"Тестовое сообщение доставлено: {chat_id}")
            except telegram.TelegramError as exc:
                # Обычная причина — человек не написал боту: Telegram не даёт
                # боту начать переписку первым.
                print(f"Не доставлено {chat_id}: {exc}")
    else:
        print("TELEGRAM_ADMIN_ID не задан — модерация работать не будет.")

    print(f"В очереди постов: {queue_left()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
