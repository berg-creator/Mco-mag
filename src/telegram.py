"""Клиент Telegram Bot API — ровно те методы, что нужны каналу.

Готовой библиотеки здесь нет намеренно: из всего Bot API каналу нужно восемь
методов, а любая обёртка тянет за собой асинхронный рантайм и свои версии
зависимостей. Пятнадцать строк на requests понятнее и не ломаются в CI.

Про просмотры постов: Bot API их не отдаёт вовсе — ни чужих, ни своих.
Цифры для медиакита придётся брать из статистики канала в клиенте либо
через MTProto с обычного аккаунта, и это отдельное решение (см. README).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import requests

from . import config

API = "https://api.telegram.org/bot{token}/{method}"

# Ограничения Telegram: подпись к фото вчетверо короче обычного сообщения.
# Для канала про одежду это важнее, чем для текстового: пост почти всегда
# уходит картинкой, значит потолок поста — 1024 знака, а не 4096.
MAX_TEXT = 4096
MAX_CAPTION = 1024

# Telegram понимает лишь небольшой набор тегов. Всё остальное — ошибка разбора,
# из-за которой сообщение не уходит вовсе, поэтому текст чистится перед отправкой.
ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "code", "pre", "blockquote", "tg-spoiler", "span",
}

_BR = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_P_CLOSE = re.compile(r"<\s*/\s*p\s*>", re.IGNORECASE)
_TAG = re.compile(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")


class TelegramError(RuntimeError):
    pass


def sanitize(text: str) -> str:
    """Убирает разметку, которую Telegram не поддерживает.

    Модель периодически добавляет <br>, <p> или списки — с ними API отвечает
    ошибкой разбора, и пост не публикуется вовсе. Полезные теги сохраняются.
    """
    text = _BR.sub("\n", text)
    text = _P_CLOSE.sub("\n\n", text)

    def keep_or_drop(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1).lower() in ALLOWED_TAGS else ""

    text = _TAG.sub(keep_or_drop, text)

    # Модель иногда оформляет абзацы markdown-цитатой «> ». В HTML-режиме
    # Telegram выводит эти символы как есть, и пост выглядит сломанным.
    text = re.sub(r"(?m)^\s*&gt;\s?", "", text)
    text = re.sub(r"(?m)^\s*>\s?", "", text)

    # Голый амперсанд Telegram по документации требует экранировать: в тексте
    # разрешены только настоящие сущности. Бренд «Warm & Wonderful» иначе
    # рискует уронить отправку целиком — а падает она молча, ошибкой разбора.
    # Уже экранированное не трогаем: за «&» с именем сущности и точкой с запятой
    # regexp не идёт.
    text = re.sub(r"&(?!(?:[a-zA-Z]+|#\d+);)", "&amp;", text)

    # Ссылка, приклеенная к последнему слову, читается как опечатка.
    text = re.sub(r"(?<=[^\s>\n])(<a\s+href=)", r"\n\n\1", text)

    # Схлопываем лишние пустые строки, появившиеся после вырезанных тегов.
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _call(method: str, payload: dict[str, Any], files: dict | None = None) -> dict:
    token = config.secret("TELEGRAM_BOT_TOKEN")
    response = requests.post(
        API.format(token=token, method=method), data=payload, files=files, timeout=90
    )

    try:
        data = response.json()
    except ValueError:
        raise TelegramError(f"{method}: ответ не JSON (код {response.status_code})")

    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description', 'неизвестная ошибка')}")
    return data["result"]


def send_message(
    chat_id: str,
    text: str,
    *,
    preview: bool = False,
    buttons: list[list[dict]] | None = None,
    force_reply: bool = False,
) -> dict:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": sanitize(text)[:MAX_TEXT],
        "parse_mode": "HTML",
        "link_preview_options": json.dumps({"is_disabled": not preview}),
    }
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    elif force_reply:
        # Ответ на это сообщение вернётся с reply_to_message — по нему поллер
        # и узнаёт, к какому посту правка. Хранить «кто что сейчас правит»
        # в файле состояния не нужно: Telegram хранит связь сам.
        payload["reply_markup"] = json.dumps({"force_reply": True})
    return _call("sendMessage", payload)


def send_photo(chat_id: str, photo_url: str, caption: str) -> dict:
    """Отправляет картинку по ссылке. Telegram скачивает её сам."""
    return _call(
        "sendPhoto",
        {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": sanitize(caption)[:MAX_CAPTION],
            "parse_mode": "HTML",
        },
    )


def send_photo_file(chat_id: str, path: Path, caption: str) -> dict:
    """Отправляет картинку с диска — так уходят карточки, которые рисуем сами:
    публичной ссылки на них нет, файл идёт прямо в загрузку."""
    with path.open("rb") as handle:
        return _call(
            "sendPhoto",
            {
                "chat_id": chat_id,
                "caption": sanitize(caption)[:MAX_CAPTION],
                "parse_mode": "HTML",
            },
            files={"photo": (path.name, handle, "image/jpeg")},
        )


def approval_buttons(post_id: str) -> list[list[dict]]:
    """Кнопки под превью поста. В callback_data кладём имя файла в очереди."""
    return [
        [
            {"text": "✅ В канал", "callback_data": f"pub:{post_id}"},
            {"text": "🗑 Удалить", "callback_data": f"del:{post_id}"},
        ],
        [
            {"text": "✏️ Поправить", "callback_data": f"fix:{post_id}"},
            {"text": "⏭ Позже", "callback_data": f"skip:{post_id}"},
        ],
    ]


def get_updates(offset: int = 0, timeout: int = 0) -> list[dict]:
    """Забирает новые события бота — в том числе нажатия кнопок."""
    return _call(
        "getUpdates",
        {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": json.dumps(["callback_query", "message"]),
        },
    )


def answer_callback(callback_id: str, text: str = "") -> None:
    """Гасит «часики» на кнопке и показывает всплывающее уведомление."""
    try:
        _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})
    except TelegramError:
        # Уведомление живёт недолго: если запоздали — не повод падать.
        pass


def edit_markup(chat_id: str, message_id: int, buttons: list[list[dict]] | None) -> None:
    """Меняет кнопки под уже отправленным сообщением (или убирает их)."""
    try:
        _call(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": json.dumps({"inline_keyboard": buttons or []}),
            },
        )
    except TelegramError:
        pass


def get_chat(chat_id: str) -> dict:
    """Карточка чата: описание, привязанный чат обсуждений, реакции."""
    return _call("getChat", {"chat_id": chat_id})


def member_count(chat_id: str) -> int:
    return int(_call("getChatMemberCount", {"chat_id": chat_id}))


def administrators(chat_id: str) -> list[dict]:
    return _call("getChatAdministrators", {"chat_id": chat_id})


def check() -> str:
    """Проверяет токен и возвращает имя бота — быстрый тест настройки."""
    me = _call("getMe", {})
    return f"@{me.get('username', '?')}"
