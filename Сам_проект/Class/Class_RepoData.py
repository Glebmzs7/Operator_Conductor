"""
Копия части настроек репозитория (ветка, интервал проверки, последняя подтверждённая версия),
которая лежит ВНУТРИ самого проекта — в папке `.autosync_data/` в корне git-репозитория — а не
только в общем config.json/state.json рядом с самим AutoSync.

Смысл: эти настройки должны "путешествовать" вместе с проектом при переносе/клонировании на
другую машину — поэтому файл коммитится в git вместе с остальным проектом (не в .gitignore).

Это ДУБЛИКАТ данных, а не единственный источник: обычная работа программы по-прежнему опирается
на config.json/state.json рядом с AutoSync. Если то, что лежит в .autosync_data, разойдётся с
центральным config.json/state.json (например, файл в проекте поменяли на другой машине и он
подтянулся через git pull) — AutoSync не выбирает сам, а спрашивает пользователя, какому источнику
верить (см. watcher.py — RepoWatcher._reconcile_repo_data_on_start/resolve_repo_data_conflict,
gui.py — AutoSyncGUI.ask_repo_data_conflict).
"""

import json
from pathlib import Path
from typing import Optional

DATA_DIRNAME = ".autosync_data"
DATA_FILENAME = "repo_config.json"

# Смысловые поля, которые участвуют в сравнении "совпадают ли данные" — saved_at не в счёт,
# это просто время последней записи, а не настройка.
_COMPARE_KEYS = ("branch", "check_interval_minutes", "known_version")


def data_dir(repo_path: Path) -> Path:
    return Path(repo_path) / DATA_DIRNAME


def data_file(repo_path: Path) -> Path:
    return data_dir(repo_path) / DATA_FILENAME


def load(repo_path: Path) -> Optional[dict]:
    """Возвращает содержимое .autosync_data/repo_config.json или None, если файла нет/он битый."""
    path = data_file(repo_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save(repo_path: Path, *, name: str, branch: str, check_interval_minutes: int,
          known_version: Optional[str], saved_at: str) -> None:
    """Создаёт папку .autosync_data при необходимости и (пере)записывает repo_config.json."""
    data_dir(repo_path).mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name,
        "branch": branch,
        "check_interval_minutes": check_interval_minutes,
        "known_version": known_version,
        "saved_at": saved_at,
    }
    data_file(repo_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def matches(folder_data: dict, central_data: dict) -> bool:
    """Сравнение только по смысловым полям (см. _COMPARE_KEYS)."""
    return all(folder_data.get(k) == central_data.get(k) for k in _COMPARE_KEYS)


_GITIGNORE_ENTRY = "/.autosync_data/"


def ensure_gitignore_entry(repo_path: Path) -> None:
    """Автоматически добавляет исключение папки .autosync_data/ в .gitignore репозитория, если
    его там ещё нет — чтобы НЕ редактировать .gitignore каждого проекта руками, в том числе для
    репозиториев, добавленных позже (Power_struggle и любые следующие). Ничего не делает, если
    строка уже есть; если .gitignore ещё не существует — создаёт его с этой одной строкой.
    known_version/branch этот файл не коммитится в git (у каждого пользователя своя ветка) —
    поэтому и нужна эта автоматическая правка .gitignore при каждой сверке репозитория."""
    entry_path = Path(repo_path) / ".gitignore"
    try:
        existing = entry_path.read_text(encoding="utf-8") if entry_path.exists() else ""
    except OSError:
        return
    if any(line.strip() == _GITIGNORE_ENTRY for line in existing.splitlines()):
        return
    sep = "" if (existing == "" or existing.endswith("\n")) else "\n"
    try:
        entry_path.write_text(existing + sep + _GITIGNORE_ENTRY + "\n", encoding="utf-8")
    except OSError:
        pass
