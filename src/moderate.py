"""Поллер бота: разбирает нажатия кнопок под постами в личке владельца.

    python -m src.moderate             обработать накопившееся и выйти
    python -m src.moderate --serve 55  дежурить 55 минут, отвечая сразу
    python -m src.moderate --dry-run   показать, что пришло, ничего не делая

Постоянно работающего сервера у проекта нет, поэтому события забирает
по расписанию этот скрипт. Между нажатием кнопки и публикацией проходит
до нескольких минут — единственное отличие от «настоящего» бота.

Дежурство (`--serve`) держит соединение открытым (long polling) и отвечает
за секунды. Оно же и основной режим: цикл спит внутри getUpdates, а не крутится
вхолостую. Разовый запуск остался для отладки.

**Опросчик должен быть ровно один.** У бота общий offset в getUpdates: кто
первый забрал событие, для того оно и исчезло. Второй параллельный поллер
воровал бы нажатия у первого, поэтому всё, что появится позже (сервис в личке,
ответы читателям), разбирается здесь же, а не отдельным скриптом.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import time

from . import config, publish, state, telegram

log = logging.getLogger("moderate")

OFFSET_FILE = config.DATA / "tg_offset.json"

# Сколько секунд Telegram придерживает запрос, если событий нет. Больше —
# меньше пустых обращений; больше 50 сервер обрывает сам.
POLL_TIMEOUT = 25

# Как часто дежурство отправляет состояние в репозиторий.
PUSH_EVERY = 600


def handle(action: str, post_id: str) -> str:
    """Выполняет решение владельца. Возвращает текст для всплывашки."""
    path = config.QUEUE / post_id

    if action == "skip":
        return "Оставил в очереди"

    if not path.exists():
        return "Поста уже нет в очереди"

    post = state.read_json(path, {})

    if action == "del":
        path.unlink()
        return "Удалил"

    if action == "pub":
        try:
            publish.send(post, config.secret("TELEGRAM_CHANNEL_ID"))
        except telegram.TelegramError as exc:
            log.error("Не удалось опубликовать %s: %s", post_id, exc)
            return f"Ошибка: {exc}"

        publish.record(post, path, "channel")
        publish.archive(path)
        return "Опубликовано в канал"

    return "Непонятная команда"


def process(updates: list[dict], admins: list[str], dry_run: bool, offset: int) -> tuple[int, int]:
    """Разбирает пачку событий. Возвращает (обработано нажатий, новый offset)."""
    handled = 0
    last_id = offset

    for update in updates:
        last_id = max(last_id, update.get("update_id", 0) + 1)

        query = update.get("callback_query")
        if not query:
            continue

        data = query.get("data", "")
        if ":" not in data:
            continue

        # Кнопки модерации принимаются только от ведущих канала: callback_data
        # видно всем, кому переслали сообщение, и чужое нажатие опубликовало бы
        # пост в канал.
        sender = str(query.get("from", {}).get("id", ""))
        if sender not in admins:
            telegram.answer_callback(query["id"], "Это не твой канал")
            continue

        action, post_id = data.split(":", 1)

        if dry_run:
            print(f"  {action} → {post_id} (от {sender})")
            handled += 1
            continue

        result = handle(action, post_id)
        telegram.answer_callback(query["id"], result)

        # Кнопки убираем в том чате, где нажали: ведущих несколько, и превью
        # у каждого своё. У остальных клавиатура останется — повторное нажатие
        # получит честное «поста уже нет в очереди».
        message = query.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", "")) or sender
        if message.get("message_id"):
            telegram.edit_markup(chat_id, message["message_id"], None)

        print(f"  {post_id}: {result} (решил {sender})")
        handled += 1

    return handled, last_id


def serve(minutes: int) -> int:
    """Дежурство: держим соединение открытым и отвечаем сразу.

    Состояние пишется на диск после каждой пачки: запуск в Actions обрывают
    по лимиту времени, и уже обработанные события не должны разбираться заново.
    """
    admins = config.admin_ids()
    deadline = time.monotonic() + minutes * 60
    offset = state.read_json(OFFSET_FILE, {"offset": 0}).get("offset", 0)
    total = 0

    print(f"Дежурство {minutes} мин. Бот отвечает сразу.")
    next_push = time.monotonic() + PUSH_EVERY

    while time.monotonic() < deadline:
        try:
            updates = telegram.get_updates(offset=offset, timeout=POLL_TIMEOUT)
        except telegram.TelegramError as exc:
            # Обрыв связи не повод заканчивать дежурство: подождём и вернёмся.
            log.warning("Опрос сорвался: %s", exc)
            time.sleep(5)
            continue

        if updates:
            handled, offset = process(updates, admins, False, offset)
            total += handled
            state.write_json(OFFSET_FILE, {"offset": offset})

        if time.monotonic() >= next_push:
            push_state()
            next_push = time.monotonic() + PUSH_EVERY

    push_state()
    print(f"Дежурство окончено. Обработано нажатий: {total}.")
    return 0


def push_state() -> None:
    """Отправляет состояние в репозиторий прямо посреди дежурства.

    На GitHub машина после запуска исчезает, а состояние живёт только в git.
    Оборванное дежурство с несохранённым offset заставит бота разобрать
    те же нажатия второй раз. Локально ничего не делает: там файлы никуда
    не денутся.
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=config.ROOT,
        capture_output=True,
        text=True,
    ).stdout.strip() or "main"

    commands = (
        ["git", "add", "data/", "content/"],
        ["git", "commit", "-m", "дежурство: решения по постам"],
        # Пока идёт смена, в ветку пишут и другие задачи — публикация, генерация.
        # Поэтому перед отправкой всегда подтягиваем чужое.
        ["git", "pull", "--rebase", "--autostash", "origin", branch],
        ["git", "push", "origin", f"HEAD:{branch}"],
    )
    for command in commands:
        result = subprocess.run(command, cwd=config.ROOT, capture_output=True, text=True)
        if result.returncode != 0:
            # Коммитить нечего — обычное дело, тишина в логе тут уместнее ошибки.
            if command[1] != "commit":
                log.warning("git %s: %s", command[1], result.stderr.strip()[:200])
            return


def once(dry_run: bool) -> int:
    """Разовый разбор накопившегося — режим для крона."""
    offset = state.read_json(OFFSET_FILE, {"offset": 0}).get("offset", 0)
    updates = telegram.get_updates(offset=offset)

    if not updates:
        print("Новых событий нет.")
        return 0

    admins = config.admin_ids()
    handled, last_id = process(updates, admins, dry_run, offset)

    if dry_run:
        # В очереди Telegram лежат и обычные сообщения боту — в разбор они
        # не идут, но молчать про них нельзя: иначе «сухой» прогон выглядит
        # так, будто событий не было вовсе.
        print(f"Событий в очереди: {len(updates)}, из них нажатий кнопок: {handled}.")
        return 0

    state.write_json(OFFSET_FILE, {"offset": last_id})
    print(f"Обработано нажатий: {handled}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Обработка нажатий кнопок модерации")
    parser.add_argument("--dry-run", action="store_true", help="только показать события")
    parser.add_argument("--serve", type=int, metavar="МИНУТ", help="дежурить, отвечая сразу")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config.load_dotenv()

    if args.serve:
        return serve(args.serve)
    return once(args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
