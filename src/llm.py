"""Генерация текстов за фасадом: рубрики и промпты от провайдера не зависят.

Провайдер сейчас один — Claude. Фасад всё равно нужен: он держит в одном месте
схему ответа, сборку промпта из голоса канала и правил рубрики, и точку, куда
добавится запасной генератор, когда владелец решит, нужен ли он.

Ответ жёстко ограничен схемой: модель отдаёт JSON, а не свободный текст,
который пришлось бы разбирать регулярками и угадывать, где кончился пост
и начались извинения.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache

from . import config
from .providers import claude

log = logging.getLogger("llm")

POST_SCHEMA = {
    "type": "object",
    "properties": {
        "skip": {
            "type": "boolean",
            "description": "true, если фактов не хватает на пост — тогда text пустой",
        },
        "text": {
            "type": "string",
            "description": "Готовый текст поста с HTML-разметкой Telegram",
        },
        "reason": {
            "type": "string",
            "description": "Если skip=true — одной строкой чего не хватило",
        },
    },
    "required": ["skip", "text", "reason"],
    "additionalProperties": False,
}


@lru_cache(maxsize=1)
def voice() -> str:
    return (config.PROMPTS / "voice.md").read_text(encoding="utf-8")


@lru_cache(maxsize=16)
def rubric_prompt(key: str) -> str:
    path = config.PROMPTS / "rubrics" / f"{key}.md"
    if not path.exists():
        raise FileNotFoundError(f"Нет промпта рубрики: {path}")
    return path.read_text(encoding="utf-8")


def build_user_prompt(rubric_key: str, payload: dict) -> str:
    """Собирает запрос: правила рубрики + данные из курируемой базы.

    Данные идут блоком JSON, а не пересказом: так в промпте видно границу
    между «это факты» и «это инструкция». Всё, чего в блоке нет, писать нельзя —
    об этом сказано и здесь, и в голосе канала.
    """
    return (
        f"{rubric_prompt(rubric_key)}\n\n"
        f"## Данные для этого поста\n\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n\n"
        f"Напиши пост по правилам рубрики и голосу канала. Опирайся только "
        f"на факты из блока выше. Если их не хватает — верни skip=true "
        f"и одной строкой скажи, какого факта недостаёт."
    )


def generate_now(rubric_key: str, payload: dict) -> dict:
    """Один пост сразу. Дороже батча вдвое — режим для отладки и срочного."""
    return claude.generate(voice(), build_user_prompt(rubric_key, payload), POST_SCHEMA)


def submit_batch(jobs: list[tuple[str, str, dict]]) -> str:
    """jobs — список (custom_id, ключ рубрики, данные)."""
    prepared = [
        (custom_id, voice(), build_user_prompt(key, payload), POST_SCHEMA)
        for custom_id, key, payload in jobs
    ]
    return claude.submit_batch(prepared)


def batch_status(batch_id: str) -> str:
    return claude.batch_status(batch_id)


def fetch_batch(batch_id: str) -> dict[str, dict]:
    return claude.fetch_batch(batch_id)


def describe() -> str:
    """Человекочитаемое название генератора — для логов и отчётов."""
    return f"Anthropic {claude.MODEL}"
