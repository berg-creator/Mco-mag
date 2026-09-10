"""Поллер бота: разбирает нажатия кнопок под постами в личке владельца.

    python -m src.moderate             обработать накопившееся и выйти
    python -m src.moderate --serve 55  дежурить 55 минут, отвечая сразу
    python -m src.moderate --dry-run   показать, что пришло, ничего не делая

Постоянно работающего сервера у проекта нет, поэтому события забирает
по расписанию этот скрипт. Отсюда и задержка: нажатие лежит в очереди
Telegram, пока не поднимется следующий запуск.

**Основной режим — разовый разбор, и это решение про деньги, а не про код.**
Дежурство (`--serve`) держит соединение открытым и отвечает за секунды, но
стоит 22 часа рантайма в сутки. Репозиторий приватный, а приватному на
бесплатном тарифе положено 2000 минут Actions в месяц — смена по 55 минут
раз в час это около 10 000. Разовый запуск стоит минуту (GitHub округляет
задание вверх), то есть примерно 700 минут в месяц. Владелец выбрал
10 сентября 2026 задержку вместо счёта.

`--serve` остался и нужен: когда решение по посту нужно провести сейчас,
смена запускается руками через workflow_dispatch.

**Опросчик должен быть ровно один.** У бота общий offset в getUpdates: кто
первый забрал событие, для того оно и исчезло. Второй параллельный поллер
воровал бы нажатия у первого, поэтому всё, что появится позже (сервис в личке,
ответы читателям), разбирается здесь же, а не отдельным скриптом.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import time

from . import config, publish, state, telegram

log = logging.getLogger("moderate")

OFFSET_FILE = config.DATA / "tg_offset.json"

# Имя файла поста в тексте сообщения-приглашения. По нему правка находит
# свой пост: связь «кто что сейчас правит» держит сам Telegram через
# reply_to_message, и своего состояния для этого заводить не нужно —
# при двух ведущих оно бы ещё и путалось.
_POST_ID = re.compile(r"[\w.\-]+\.json")

# Сколько секунд Telegram придерживает запрос, если событий нет. Больше —
# меньше пустых обращений; больше 50 сервер обрывает сам.
POLL_TIMEOUT = 25

# Как часто дежурство отправляет состояние в репозиторий.
PUSH_EVERY = 600


def handle(action: str, post_id: str, chat_id: str) -> str:
    """Выполняет решение владельца. Возвращает текст для всплывашки."""
    path = config.QUEUE / post_id

    if action == "skip":
        return "Оставил в очереди"

    if action == "fix":
        # Пост остаётся в очереди: правка — это не решение о публикации,
        # а материал для правки формата. Ответ на это сообщение поймает note_fix.
        telegram.send_message(
            chat_id,
            f"✏️ Правка к посту <code>{post_id}</code>\n\n"
            f"Ответь на это сообщение: что не так — или пришли свой вариант "
            f"текста целиком. Запишу в рефы, дальше буду писать по ним.",
            force_reply=True,
        )
        return "Жду правку ответом"

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


def note_fix(message: dict, admins: list[str], dry_run: bool) -> bool:
    """Записывает правку ведущего в журнал рефов. False — это не правка.

    Правки копятся в data/feedback.jsonl вместе с текстом поста, к которому
    относятся: без оригинала «сделай короче» через месяц ничего не значит.
    Текст поста в очереди при этом не подменяется — «сделай короче» стало бы
    самим постом, а отличить замечание от переписанного текста надёжно нельзя.
    """
    reply = message.get("reply_to_message") or {}
    found = _POST_ID.search(reply.get("text", ""))
    if not found:
        return False

    sender = str(message.get("from", {}).get("id", ""))
    text = (message.get("text") or "").strip()
    if sender not in admins or not text:
        return False

    post_id = found.group(0)
    chat_id = str(message.get("chat", {}).get("id", "")) or sender

    if dry_run:
        print(f"  правка к {post_id} (от {sender}): {text[:60]}")
        return True

    # Пост мог уже уйти в канал — тогда он лежит в архиве, а не в очереди.
    post = state.read_json(config.QUEUE / post_id, None)
    if post is None:
        post = state.read_json(config.ARCHIVE / post_id, {})

    state.append_jsonl(
        config.FEEDBACK_FILE,
        [
            {
                "post_id": post_id,
                "rubric": post.get("rubric", ""),
                "brand": post.get("brand", ""),
                "author": message.get("from", {}).get("username", "") or sender,
                "fix": text,
                "original": post.get("text", ""),
                "at": state.iso(),
            }
        ],
    )
    telegram.send_message(chat_id, "Записал. Следующие посты пишу с учётом этого.")
    print(f"  правка к {post_id} записана (от {sender})")
    return True


def process(updates: list[dict], admins: list[str], dry_run: bool, offset: int) -> tuple[int, int]:
    """Разбирает пачку событий. Возвращает (обработано нажатий, новый offset)."""
    handled = 0
    last_id = offset

    for update in updates:
        last_id = max(last_id, update.get("update_id", 0) + 1)

        query = update.get("callback_query")
        if not query:
            # Обычное сообщение боту разбирается здесь же: второй поллер завёл бы
            # войну за offset getUpdates — кто первый забрал, для того событие
            # и исчезло.
            if update.get("message") and note_fix(update["message"], admins, dry_run):
                handled += 1
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

        message = query.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", "")) or sender

        result = handle(action, post_id, chat_id)
        telegram.answer_callback(query["id"], result)

        # Кнопки убираем в том чате, где нажали: ведущих несколько, и превью
        # у каждого своё. У остальных клавиатура останется — повторное нажатие
        # получит честное «поста уже нет в очереди». После «Поправить»
        # и «Позже» пост живёт дальше, поэтому кнопки остаются на месте.
        if action in ("pub", "del") and message.get("message_id"):
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
    print(f"Дежурство окончено. Обработано событий: {total}.")
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
    """Разовый разбор накопившегося — основной режим для крона.

    Состояние отсюда в репозиторий НЕ отправляется: за этим следит шаг
    воркфлоу. Своего push_state() тут нет намеренно — коммит в середине
    короткого запуска мешал бы больше, чем помогал. Но если запускать
    этот режим в Actions без шага сохранения, машина исчезнет вместе
    с решением владельца: опубликованный пост останется в очереди в git
    и уйдёт на утверждение заново, а нажатие Telegram переотдаст, потому
    что offset не подтверждён.
    """
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
        print(f"Событий в очереди: {len(updates)}, из них разобрано: {handled}.")
        return 0

    state.write_json(OFFSET_FILE, {"offset": last_id})
    print(f"Обработано событий: {handled}.")
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
