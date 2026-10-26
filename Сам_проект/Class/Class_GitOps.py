"""
Обёртка над git-командами для AutoSync.

Важно: здесь НЕТ своей логики трёхстороннего слияния — конфликты решает сам git
(`git merge` / `git mergetool`), это уже готовый, проверенный инструмент (ответ на вопрос
«есть ли готовые шаблоны для решения этих вопросов» — да, не изобретаем велосипед).
Наша обвязка только читает состояние (fetch/ahead-behind/diff) и, если git может разрешить
конфликт сам, — вызывает его: `git mergetool` откроет тот diff/merge-инструмент, который уже
настроен у пользователя (по умолчанию Windows предложит выбрать при первом вызове — VS Code,
Beyond Compare, kdiff3, meld и т.п. — либо оставит текстовые маркеры <<<<<<< для ручного
разрешения прямо в редакторе).
"""

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional


class GitError(RuntimeError):
    pass


# Необязательный колбэк (repo_path, command_str) -> None, устанавливается один раз из watcher.py
# (см. _log_git_command / git_ops.set_command_logger), чтобы КАЖДАЯ выполняемая git-команда была
# видна пользователю в журнале программы, а не только итоговый результат ("синхронизировано" и
# т.п.) — программа запускается без консоли (AutoSync.pyw), поэтому это единственное место, где
# пользователь может увидеть, какими именно git-командами мы пользуемся.
_command_logger: Optional[Callable[[Path, str], None]] = None


def set_command_logger(callback: Callable[[Path, str], None]) -> None:
    global _command_logger
    _command_logger = callback


def _log_command(repo_path: Path, command: str) -> None:
    if _command_logger is None:
        return
    try:
        _command_logger(repo_path, command)
    except Exception:
        pass  # логирование команды не должно ломать саму git-операцию


# На Windows каждый subprocess.run(["git", ...]) без этого флага открывает своё маленькое
# консольное окно (мелькает и сразу закрывается) — при частых проверках нескольких репозиториев
# это и даёт "очень много окон" при запуске. CREATE_NO_WINDOW убирает именно консоль команды,
# не трогая работу самой команды. На других ОС такого флага нет — там просто 0 (по умолчанию).
_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _run(repo_path: Path, *args: str) -> str:
    _log_command(repo_path, " ".join(args))
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW_FLAGS,
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {repo_path}:\n{result.stderr}")
    return result.stdout.strip()


def fetch(repo_path: Path, remote: str = "origin") -> None:
    _run(repo_path, "fetch", "--prune", remote)


def current_branch(repo_path: Path) -> str:
    return _run(repo_path, "rev-parse", "--abbrev-ref", "HEAD")


def checkout(repo_path: Path, branch: str) -> None:
    """Переключить рабочую копию на нужную ветку. Используется, когда на диске физически
    оказалась переключена не та ветка, за которой следит AutoSync (см. watcher.py —
    _action_push). git сам откажет (бросит GitError), если переключению мешают
    незакоммиченные изменения, конфликтующие с целевой веткой — тогда переключаться
    насильно (checkout -f) мы не пытаемся, чтобы не потерять чужие несохранённые правки."""
    _run(repo_path, "checkout", branch)


def rev_parse(repo_path: Path, ref: str) -> str:
    """Полный хэш коммита, на который указывает ref (HEAD, origin/<branch>, тег и т.п.) —
    однозначная проверка "это тот же коммит или нет", без неоднозначностей git describe (см.
    _action_push в watcher.py: раньше проверка после пуша сравнивала теги через describe, что
    ломалось, если на одном и том же коммите оказывалось НЕСКОЛЬКО тегов)."""
    return _run(repo_path, "rev-parse", ref)


def remote_url(repo_path: Path, remote: str = "origin") -> Optional[str]:
    """Для ссылки на git в окне (пункт 2 списка строки) — берём как есть, что настроено у git,
    без своих догадок."""
    try:
        return _run(repo_path, "remote", "get-url", remote)
    except GitError:
        return None


@dataclass
class AheadBehind:
    ahead: int   # локальных коммитов, которых нет в remote
    behind: int  # коммитов в remote, которых нет локально


def ahead_behind(repo_path: Path, branch: Optional[str] = None, remote: str = "origin") -> AheadBehind:
    branch = branch or current_branch(repo_path)
    out = _run(repo_path, "rev-list", "--left-right", "--count", f"{branch}...{remote}/{branch}")
    ahead_s, behind_s = out.split()
    return AheadBehind(ahead=int(ahead_s), behind=int(behind_s))


def diff_name_status(repo_path: Path, ref_a: str, ref_b: str) -> List[str]:
    out = _run(repo_path, "diff", "--name-status", ref_a, ref_b)
    return out.splitlines() if out else []


def has_local_changes(repo_path: Path) -> bool:
    return bool(_run(repo_path, "status", "--porcelain"))


def changed_files(repo_path: Path) -> List[str]:
    """То же самое, что видит has_local_changes(), но построчно — какие именно файлы git считает
    изменёнными/новыми/удалёнными. Используется только для логирования (см. watcher.py —
    _action_push), чтобы пользователь мог увидеть, какой конкретно файл вызвал пуш, а не только
    сам факт "есть материал для пуша"."""
    out = _run(repo_path, "status", "--porcelain")
    return out.splitlines() if out else []


def add_commit(repo_path: Path, paths: List[str], message: str) -> None:
    _run(repo_path, "add", *paths)
    _run(repo_path, "commit", "-m", message)


def push(repo_path: Path, branch: Optional[str] = None, remote: str = "origin") -> None:
    branch = branch or current_branch(repo_path)
    _run(repo_path, "push", remote, branch)


def tag_exists(repo_path: Path, tag: str) -> bool:
    out = _run(repo_path, "tag", "--list", tag)
    return bool(out)


def remote_tag_exists(repo_path: Path, tag: str, remote: str = "origin") -> bool:
    """Есть ли тег с таким именем на сервере (git ls-remote). Бросает GitError, если сервер
    недоступен — вызывающий код сам решает, считать ли это обрывом связи."""
    out = _run(repo_path, "ls-remote", "--tags", remote, f"refs/tags/{tag}")
    return bool(out)


# Фрагменты текста ошибок git, по которым видно, что это обрыв/отсутствие связи, а не отказ
# сервера по существу (только в этом случае повтор той же команды может помочь).
_CONNECTION_ERROR_MARKERS = (
    "could not resolve host",
    "unable to access",
    "failed to connect",
    "couldn't connect to server",
    "connection timed out",
    "operation timed out",
    "timed out",
    "connection reset",
    "connection was reset",
    "connection refused",
    "network is unreachable",
    "early eof",
    "rpc failed",
    "the remote end hung up unexpectedly",
    "recv failure",
    "send failure",
    "empty reply from server",
    "ssl_read",
    "schannel",
    "the requested url returned error: 50",
)


def is_connection_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _CONNECTION_ERROR_MARKERS)


def create_and_push_tag(repo_path: Path, tag: str, message: str, remote: str = "origin") -> None:
    if tag_exists(repo_path, tag):
        # Коллизия версии (см. п.1.2.2 обсуждения) — git сам отказывает на дубликат тега,
        # отдельную логику сравнения писать не нужно, просто ловим и поднимаем понятную ошибку.
        raise GitError(f"Тег {tag!r} уже существует — коллизия версии, нужно решение пользователя")
    _run(repo_path, "tag", "-a", tag, "-m", message)
    _run(repo_path, "push", remote, tag)


def create_tag(repo_path: Path, tag: str, message: str) -> None:
    """Только локально — создать тег, БЕЗ пуша (используется вместе с push_atomic, чтобы ветка и
    тег уходили на сервер одной командой)."""
    if tag_exists(repo_path, tag):
        raise GitError(f"Тег {tag!r} уже существует — коллизия версии, нужно решение пользователя")
    _run(repo_path, "tag", "-a", tag, "-m", message)


def delete_local_tag(repo_path: Path, tag: str) -> None:
    """Убрать локальный тег, который не удалось (атомарно) запушить — чтобы следующая попытка
    со следующим номером пуша не спотыкалась о него как о 'уже существующий'."""
    try:
        _run(repo_path, "tag", "-d", tag)
    except GitError:
        pass  # тега и так нет — ничего страшного


def push_atomic(repo_path: Path, branch: str, tag: str, remote: str = "origin", force: bool = False) -> None:
    """Пушим ветку и тег ОДНОЙ атомарной командой (git push --atomic): git либо принимает ОБА
    ref-а, либо (при отклонении любого из них — коллизия на сервере, разрыв связи, отставшая
    ветка и т.п.) НЕ принимает НИ ОДНОГО. Так код никогда не окажется на GitHub без версии,
    и наоборот — тег никогда не появится без соответствующего ему кода."""
    args = ["push", "--atomic"]
    if force:
        args.append("--force-with-lease")
    args += [remote, branch, tag]
    _run(repo_path, *args)


def latest_tag_on_branch(repo_path: Path, branch: Optional[str] = None) -> Optional[str]:
    branch = branch or current_branch(repo_path)
    try:
        return _run(repo_path, "describe", "--tags", "--abbrev=0", branch)
    except GitError:
        return None  # тегов ещё нет


def unmerged_files(repo_path: Path) -> List[str]:
    """Файлы, реально находящиеся в состоянии конфликта (unmerged) прямо сейчас. Используется,
    чтобы предупредить пользователя ДО открытия mergetool, если конфликтов нет — тогда mergetool
    откроется и сразу закроется сам, ему нечего показывать (см. watcher.py —
    _process_slияniya_raskhozhdeniy)."""
    out = _run(repo_path, "diff", "--name-only", "--diff-filter=U")
    return out.splitlines() if out else []


def is_tracked(repo_path: Path, relpath: str) -> bool:
    """Отслеживается ли git-ом путь (файл или папка) внутри репозитория. Нужно для случая, когда
    .autosync_data успел закоммититься ДО того, как появилось исключение в .gitignore — само
    .gitignore не снимает с учёта уже отслеживаемые файлы (см. watcher.py —
    _reconcile_repo_data_on_start)."""
    out = _run(repo_path, "ls-files", "--", relpath)
    return bool(out)


def untrack_path(repo_path: Path, relpath: str) -> None:
    """git rm -r --cached: убирает путь из индекса git (переводит в untracked), НЕ трогая сам
    файл/папку на диске. Изменение остаётся застейдженным и уйдёт в git вместе со следующим
    обычным коммитом (см. add_commit в watcher.py)."""
    _run(repo_path, "rm", "-r", "--cached", "--ignore-unmatch", relpath)


def open_mergetool(repo_path: Path) -> None:
    """Открыть настроенный у пользователя инструмент слияния конфликтов.

    Ничего не решает сам — просто передаёт управление git/внешнему diff-инструменту.
    """
    _log_command(repo_path, "mergetool")
    subprocess.run(["git", "-C", str(repo_path), "mergetool"])


def reset_hard(repo_path: Path, ref: str) -> None:
    """ДЕЙСТВИЕ_ОБНОВИТЬ_ЛОКАЛЬНО: отбросить локальные файлы и встать на ref (обычно
    origin/<branch>). Вызывается только после явного подтверждения пользователя."""
    _run(repo_path, "reset", "--hard", ref)


def push_force_with_lease(repo_path: Path, branch: Optional[str] = None, remote: str = "origin") -> None:
    """Для варианта 'оставить локальную версию' в ПРОЦЕСС_СЛИЯНИЯ_РАСХОЖДЕНИЙ. force-with-lease
    (не голый --force) — откажет, если на remote появилось что-то новое уже ПОСЛЕ того, как мы
    в последний раз его видели, вместо того чтобы затереть это молча."""
    branch = branch or current_branch(repo_path)
    _run(repo_path, "push", "--force-with-lease", remote, branch)


# --- Расхождение ветки слежения и версий ветки (11.10.2026) ---------------------------------
# Используются в Class_Watcher.py: _resolve_branch_mismatch (на диске не та ветка) и
# _process_slияniya_raskhozhdeniy (версии одной ветки разошлись).

def _run_rc(repo_path: Path, *args: str) -> int:
    """Как _run, но возвращает код возврата вместо исключения — для команд, где ненулевой код
    это ответ "нет", а не ошибка (git merge-base --is-ancestor, git rev-parse --verify)."""
    _log_command(repo_path, " ".join(args))
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW_FLAGS,
    )
    return result.returncode


def ref_exists(repo_path: Path, ref: str) -> bool:
    return _run_rc(repo_path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}") == 0


def is_ancestor(repo_path: Path, ancestor: str, descendant: str) -> bool:
    """Есть ли коммит ancestor в истории descendant ("эта версия есть в ветке")."""
    return _run_rc(repo_path, "merge-base", "--is-ancestor", ancestor, descendant) == 0


def tag_label(repo_path: Path, ref: str, dirty: bool = False) -> str:
    """Подпись для кнопки выбора: последний тег, достижимый из ref, и "+", если поверх этого
    тега есть ещё коммиты (ref не стоит ровно на теге) или dirty=True (незакоммиченные
    изменения на диске)."""
    tag = latest_tag_on_branch(repo_path, ref)
    if tag is None:
        return "без тега" + ("+" if dirty else "")
    exact = _run_rc(repo_path, "describe", "--tags", "--exact-match", ref) == 0
    return tag + ("+" if (not exact or dirty) else "")


def commits_not_in(repo_path: Path, base: str, head: str) -> List[str]:
    """Коммиты, которые есть в head, но нет в base (git log --oneline base..head)."""
    out = _run(repo_path, "log", "--oneline", f"{base}..{head}")
    return out.splitlines() if out else []


def diff_worktree_name_status(repo_path: Path, ref: str) -> List[str]:
    """Какие файлы на диске (включая незакоммиченные изменения и новые файлы) отличаются от ref."""
    out = _run(repo_path, "diff", "--name-status", ref)
    lines = out.splitlines() if out else []
    status = _run(repo_path, "status", "--porcelain")
    lines += [line for line in (status.splitlines() if status else []) if line.startswith("??")]
    return lines


def switch_branch_keep_files(repo_path: Path, branch: str, remote: str = "origin") -> None:
    """Переключить HEAD на branch, НЕ трогая файлы на диске: после этого файлы с диска видны как
    изменения относительно branch и дальше идут в сверку версий. Коммиты старой ветки остаются
    в ней. Если локальной branch нет — создаётся от <remote>/<branch>."""
    if not ref_exists(repo_path, f"refs/heads/{branch}"):
        if not ref_exists(repo_path, f"{remote}/{branch}"):
            raise GitError(f"ветки {branch!r} нет ни на компьютере, ни на {remote}")
        _run(repo_path, "branch", branch, f"{remote}/{branch}")
    _run(repo_path, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    _run(repo_path, "reset", "-q")  # индекс = новая ветка, рабочие файлы не меняются


def create_branch(repo_path: Path, name: str, ref: str) -> None:
    _run(repo_path, "branch", name, ref)


def stash_all(repo_path: Path, message: str) -> bool:
    """Сохранить незакоммиченные изменения (включая новые файлы) в git stash. True — если было
    что сохранять."""
    if not has_local_changes(repo_path):
        return False
    _run(repo_path, "stash", "push", "-u", "-m", message)
    return True


def commit_all(repo_path: Path, message: str) -> bool:
    """git add -A + commit, если есть что коммитить. True — если коммит создан."""
    if not has_local_changes(repo_path):
        return False
    _run(repo_path, "add", "-A")
    _run(repo_path, "commit", "-m", message)
    return True


def commit_tree_on_top(repo_path: Path, base_ref: str, message: str) -> str:
    """Новый коммит поверх base_ref, в котором файлы ровно как в текущем HEAD ("наша версия как
    основная" без force-push: история base_ref сохраняется). Возвращает sha нового коммита."""
    tree = _run(repo_path, "rev-parse", "HEAD^{tree}")
    return _run(repo_path, "commit-tree", tree, "-p", base_ref, "-m", message)


def move_current_branch_to(repo_path: Path, sha: str) -> None:
    """Передвинуть текущую ветку на sha, не трогая файлы на диске (они уже совпадают с sha)."""
    branch = current_branch(repo_path)
    _run(repo_path, "update-ref", f"refs/heads/{branch}", sha)
    _run(repo_path, "reset", "-q")


def merge_ref(repo_path: Path, ref: str) -> bool:
    """git merge --no-edit ref. True — слияние прошло без конфликтов; False — есть конфликты
    (файлы в состоянии unmerged ждут mergetool). Прочие ошибки — GitError."""
    try:
        _run(repo_path, "merge", "--no-edit", ref)
        return True
    except GitError:
        if unmerged_files(repo_path):
            return False
        raise


def finish_merge(repo_path: Path) -> None:
    _run(repo_path, "commit", "--no-edit")
