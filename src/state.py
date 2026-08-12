"""Чтение и запись состояния.

Состояние живёт в обычных JSON-файлах репозитория, а git работает бесплатной
базой с историей и откатом: видно, когда пост появился в очереди и кто его
изменил. Альтернатива — внешняя база — потребовала бы сервера, а сервера
у проекта нет и не планируется.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    return (moment or now()).isoformat(timespec="seconds")


def parse(stamp: str) -> datetime | None:
    """Разбирает метку времени. Без таймзоны считаем, что это UTC."""
    try:
        parsed = datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Битый файл не должен ронять весь запуск: откатываемся к пустому
        # состоянию, а испорченный вариант оставляем рядом для разбора.
        path.rename(path.with_suffix(path.suffix + ".broken"))
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")


def append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def fingerprint(*parts: str) -> str:
    """Стабильный отпечаток материала — основа защиты от повторов."""
    joined = "|".join(p.strip().lower() for p in parts if p)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def log_gap(subject: str, reason: str, rubric: str = "") -> None:
    """Записывает, чего не хватило для поста.

    Это не лог ошибок, а список работы: чем чаще имя всплывает в gaps.jsonl,
    тем нужнее завести его в базу. Спрос виден по факту, а не по догадке.
    """
    append_jsonl(
        config.GAPS_FILE,
        [{"subject": subject, "reason": reason, "rubric": rubric, "at": iso()}],
    )


def git_commit(message: str, paths: list[Path]) -> bool:
    """Коммитит изменения состояния. False — менять было нечего.

    Внутри GitHub Actions коммит делает встроенный GITHUB_TOKEN, отдельный
    ключ для этого не нужен.
    """
    existing = [str(p.relative_to(config.ROOT)) for p in paths if p.exists()]
    if not existing:
        return False

    subprocess.run(["git", "add", "--", *existing], cwd=config.ROOT, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=config.ROOT).returncode
    if staged == 0:
        return False

    subprocess.run(["git", "commit", "-m", message], cwd=config.ROOT, check=True)
    return True
