"""Провайдер Anthropic Claude: обычная генерация, батч и кэш промпта.

Батч выбран основным режимом, потому что он ровно вдвое дешевле обычного
вызова, а канал никуда не спешит: посты пишутся впрок и лежат в очереди.
Плата за это — асинхронность (результат приходит не сразу), и она устраивает:
GitHub Actions всё равно не должен часами ждать ответа в открытом соединении.

Голос канала уходит в system с пометкой кэша: он одинаков во всех запросах
и на повторных вызовах читается примерно за десятую часть цены.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from .. import config

MODEL = "claude-opus-5"

# Пост канала — это тысяча знаков, но модель сначала думает, а потом пишет.
# Потолок с запасом: упереться в него означает получить обрезанный JSON,
# который потом не разберётся.
MAX_TOKENS = 8000


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=config.secret("ANTHROPIC_API_KEY"))


def _params(system: str, user: str, schema: dict) -> dict:
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }


def generate(system: str, user: str, schema: dict) -> dict:
    return parse_message(_client().messages.create(**_params(system, user, schema)))


def submit_batch(jobs: list[tuple[str, str, str, dict]]) -> str:
    """jobs — список (custom_id, system, user, schema). Возвращает id батча."""
    payload = [
        {"custom_id": custom_id, "params": _params(system, user, schema)}
        for custom_id, system, user, schema in jobs
    ]
    return _client().messages.batches.create(requests=payload).id


def batch_status(batch_id: str) -> str:
    return _client().messages.batches.retrieve(batch_id).processing_status


def fetch_batch(batch_id: str) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for entry in _client().messages.batches.results(batch_id):
        if entry.result.type != "succeeded":
            results[entry.custom_id] = {
                "skip": True,
                "text": "",
                "reason": f"ошибка генерации: {entry.result.type}",
            }
            continue
        results[entry.custom_id] = parse_message(entry.result.message)
    return results


def parse_message(message: Any) -> dict:
    """Достаёт из ответа наш JSON. Схема задана в запросе, но подстраховка
    нужна: при обрыве по лимиту токенов приходит оборванный текст."""
    for block in message.content:
        if getattr(block, "type", None) == "text":
            try:
                data = json.loads(block.text)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and "text" in data:
                return {
                    "skip": bool(data.get("skip", False)),
                    "text": (data.get("text") or "").strip(),
                    "reason": (data.get("reason") or "").strip(),
                }
    return {"skip": True, "text": "", "reason": "модель вернула неразборчивый ответ"}
