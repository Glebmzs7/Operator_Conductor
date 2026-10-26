"""
Локально известная (последняя подтверждённая) версия каждого репозитория.

Храним рядом с самим AutoSync (в папке Save/), НЕ внутри репозиториев — по двум причинам:
  1. Чтобы не путать со содержимым проектов и не требовать добавлять файл в .gitignore каждого
     репозитория.
  2. Чтобы файл состояния сам не попадал в watch_paths и не триггерил собственную же проверку.

Это именно "версия, которую мы сами в прошлый раз подтвердили" — то, с чем сравнивается версия,
пришедшая с git, в SVERKA_VERSIY (см. Class_Watcher.py). Это НЕ то же самое, что self.current_tag —
current_tag просто показывает, что сейчас реально стоит на git (для строки статуса), а
known_version — это то, что мы "приняли" как своё последнее согласованное состояние.
"""

import json
from pathlib import Path
from typing import Optional

# Этот файл лежит в Class/ — данные (Save/) находятся уровнем выше, рядом с Class/, а не внутри
# него (см. переезд файлов программы в Class/, а данных — в Save/, 22.09).
_STATE_FILE = Path(__file__).resolve().parent.parent / "Save" / "state.json"


def _load_all() -> dict:
    if not _STATE_FILE.exists():
        return {}
    return json.loads(_STATE_FILE.read_text(encoding="utf-8"))


def load_known_version(repo_name: str) -> Optional[str]:
    return _load_all().get(repo_name)


def save_known_version(repo_name: str, version_tag: str) -> None:
    data = _load_all()
    data[repo_name] = version_tag
    # Защита на случай, если Save/ вдруг ещё не создана (обычно создаётся сама при git checkout,
    # т.к. в ней лежит хотя бы один отслеживаемый файл — config.example.json).
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
