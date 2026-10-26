"""
Окно AutoSync (tkinter). Один прокручиваемый список, одна строка на репозиторий:

    [путь слежения — клик открывает проводник] [ссылка git — клик открывает ветку на GitHub]
    [версия, последнее число выделено цветом — его одного двигает автопуш]
    [время последнего сохранения] [обратный отсчёт до принудительной проверки]

Правая кнопка мыши на пути/git-ссылке — контекстное меню (Открыть/Копировать/Изменить).
Правая кнопка мыши на обратном отсчёте — задать свой интервал проверки для этого репозитория.

Если репозиторию нужно решение пользователя — строка красная, наверху появляется значок "!"
(фильтр: показать только красные строки). Диалог с вопросом МОЖНО закрыть крестиком, не отвечая —
это НЕ считается ответом "нет", репозиторий просто остаётся "красным", а вопрос — открытым;
кликом по красной строке диалог открывается заново. Это отличие от обычного модального окна
сделано специально: пользователь не обязан отвечать сразу, если сейчас не до этого.

Про потоки: watcher.py делает git-операции в ФОНОВЫХ потоках, а tkinter обязан жить в главном.
Поэтому обновление виджетов из фона идёт через self.root.after(0, ...), а блокирующий вопрос —
через threading.Event: фоновый поток вызывает ask_yes_no/ask_choice и стоит на event.wait(),
пока по этому вопросу не нажмут кнопку (хоть сразу, хоть после переоткрытия по клику на строку).
"""

import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import simpledialog, ttk
from typing import Callable, List, Optional

from Class_Notifier import SyncChoice

_MAX_LOG_LINES = 300

# Журнал теперь не только в окне (которое каждый раз при перезапуске начинается с чистого листа),
# но и в файле — жалоба "вывод в консоль не сохраняется из сессии в сессию". Этот файл лежит в
# Class/ — сам журнал (Save/) находится уровнем выше, рядом с Class/ (см. переезд файлов
# программы в Class/, а данных — в Save/, 22.09).
_LOG_FILE_PATH = Path(__file__).resolve().parent.parent / "Save" / "autosync.log"

# Сюда же (в тот же файл, куда AutoSync.pyw пишет ошибки старта) дописываем и ошибки, возникшие
# ВНУТРИ Tkinter-колбэков уже во время работы окна — см. _handle_callback_exception ниже.
_CRASH_LOG_FILE_PATH = Path(__file__).resolve().parent.parent / "Save" / "autosync_crash.log"

# Коды физических клавиш C/V/X/A на Windows (event.keycode) — В ОТЛИЧИЕ от event.keysym они НЕ
# зависят от текущей раскладки клавиатуры. На русской раскладке Ctrl+C/V/X даёт keysym вроде
# 'Cyrillic_es'/'Cyrillic_em'/'Cyrillic_che', а не 'c'/'v'/'x' — из-за этого и не работали
# копирование/вставка. keycode у физической клавиши остаётся тем же независимо от раскладки.
_VK_A, _VK_C, _VK_V, _VK_X = 65, 67, 86, 88


def _last_n_path_parts(path: Path, n: int = 3) -> str:
    parts = path.parts[-n:] if len(path.parts) >= n else path.parts
    return str(Path(*parts))


def remote_to_github_web_url(remote_url: str, branch: str) -> Optional[str]:
    """git@github.com:owner/repo.git или https://github.com/owner/repo.git -> страница ветки."""
    if not remote_url:
        return None
    url = remote_url.strip()
    if url.startswith("git@"):
        try:
            host_and_path = url.split("git@", 1)[1]
            host, path = host_and_path.split(":", 1)
        except ValueError:
            return None
    else:
        without_scheme = re.sub(r"^https?://", "", url)
        host, _, path = without_scheme.partition("/")
        if not path:
            return None
    path = path[:-4] if path.endswith(".git") else path
    return f"https://{host}/{path}/tree/{branch}"


@dataclass
class PendingQuestion:
    kind: str  # "yes_no" | "choice" | "repo_data_conflict" | "branch_mismatch"
    message: str = ""
    branch: str = ""
    diff_lines: List[str] = field(default_factory=list)
    # Для "choice" и "branch_mismatch" (11.10.2026): описание ситуации и подписи кнопок с тегами
    # ("0.0,0.0,0.0,2+" — "+" значит, что поверх этого тега есть ещё изменения).
    situation: str = ""
    labels: dict = field(default_factory=dict)
    central: dict = field(default_factory=dict)   # для "repo_data_conflict" — данные из config.json
    folder: dict = field(default_factory=dict)     # для "repo_data_conflict" — данные из .autosync_data
    event: threading.Event = field(default_factory=threading.Event)
    result: object = None


def _finalize_toplevel(win: tk.Toplevel) -> None:
    """Известная особенность Tk на Windows: свежесозданный Toplevel иногда повисает БЕЗ рамки
    вообще (ни свернуть, ни развернуть, ни крестика) в углу экрана — оконный менеджер просто не
    перерисовывает рамку, пока не получит сигнал об изменении геометрии. Вручную это лечится
    попыткой изменить размер окна мышью — здесь делаем то же самое программно: пересчитываем
    размер под содержимое и переустанавливаем геометрию (заодно и по центру — раньше окно
    оставалось там, где Windows его изначально поставило, обычно в верхнем левом/правом углу),
    что форсирует Windows нарисовать нормальную рамку сразу, без участия пользователя."""
    win.update_idletasks()
    width = win.winfo_reqwidth()
    height = win.winfo_reqheight()

    # Центрируем относительно главного окна программы (а не относительно всего экрана — так
    # диалог появляется рядом с тем окном, из которого его открыли, даже на нескольких мониторах).
    owner = win.master.winfo_toplevel()
    x = owner.winfo_rootx() + (owner.winfo_width() - width) // 2
    y = owner.winfo_rooty() + (owner.winfo_height() - height) // 2
    # На случай, если главное окно свёрнуто/за пределами экрана — не даём диалогу уйти в минус.
    x, y = max(0, x), max(0, y)

    win.geometry(f"{width}x{height}+{x}+{y}")
    win.lift()
    win.focus_force()


def _add_entry_context_menu(entry: tk.Entry) -> None:
    """Два независимых исправления для полей ввода:

    1. Не было меню по правой кнопке мыши ("Вырезать/Копировать/Вставить") — добавляем его,
       как в остальных программах Windows.
    2. Ctrl+C/Ctrl+V/Ctrl+X сами по себе НЕ РАБОТАЛИ на русской раскладке клавиатуры — встроенные
       привязки tkinter для копирования/вставки завязаны на keysym (символ с учётом раскладки:
       физическая клавиша C на русской раскладке даёт keysym 'Cyrillic_es', а не 'c'), поэтому
       ни разу не срабатывали. Перехватываем по event.keycode (код физической клавиши — от
       раскладки не зависит) и вызываем копирование/вставку/вырезание сами, независимо от того,
       что там думает раскладка."""
    menu = tk.Menu(entry, tearoff=0)
    menu.add_command(label="Вырезать", command=lambda: entry.event_generate("<<Cut>>"))
    menu.add_command(label="Копировать", command=lambda: entry.event_generate("<<Copy>>"))
    menu.add_command(label="Вставить", command=lambda: entry.event_generate("<<Paste>>"))

    def show_menu(event):
        entry.focus_set()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    entry.bind("<Button-3>", show_menu)

    def on_key(event):
        if not (event.state & 0x4):  # Ctrl не зажат — это не наш случай
            return None
        if event.keycode == _VK_C:
            entry.event_generate("<<Copy>>")
        elif event.keycode == _VK_V:
            entry.event_generate("<<Paste>>")
        elif event.keycode == _VK_X:
            entry.event_generate("<<Cut>>")
        elif event.keycode == _VK_A:
            entry.select_range(0, "end")
        else:
            return None
        return "break"

    entry.bind("<Key>", on_key)


def _open_in_explorer(path: Path) -> None:
    if sys.platform == "win32":
        try:
            import os
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception:
            pass
    else:
        subprocess.run(["xdg-open", str(path)])


class RepoRow(tk.Frame):
    """Одна строка списка — один репозиторий."""

    def __init__(self, parent, watcher, app: "AutoSyncGUI"):
        super().__init__(parent, bd=1, relief="solid", padx=6, pady=4)
        self.watcher = watcher
        self.app = app

        # Колонки растягиваются РАВНОМЕРНО (uniform) вместе с окном — раньше ширина была
        # фиксированной в символах, и при узком окне текст последних колонок просто уезжал за
        # пределы видимой области (не было ни переноса, ни горизонтальной прокрутки).
        # Колонка 5 — кнопка работает/ожидает/остановлена.
        for col in range(6):
            self.grid_columnconfigure(col, weight=1, uniform="repo_row_cols")

        self.path_label = tk.Label(self, cursor="hand2", anchor="w")
        self.path_label.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.path_label.bind("<Button-1>", lambda e: _open_in_explorer(self.watcher.full_watch_path))
        self.path_label.bind("<Button-3>", self._path_menu)

        self.git_label = tk.Label(self, cursor="hand2", anchor="w", fg="#1a5fb4")
        self.git_label.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.git_label.bind("<Button-1>", self._open_git)
        self.git_label.bind("<Button-3>", self._git_menu)

        self.version_frame = tk.Frame(self)
        self.version_frame.grid(row=0, column=2, sticky="ew", padx=(0, 8))
        self.version_prefix_label = tk.Label(self.version_frame, anchor="w")
        self.version_prefix_label.pack(side="left")
        self.version_push_label = tk.Label(self.version_frame, anchor="w", fg="#1a5fb4", font=("TkDefaultFont", 9, "bold"))
        self.version_push_label.pack(side="left")
        for widget in (self.version_frame, self.version_prefix_label, self.version_push_label):
            widget.bind("<Button-3>", self._version_menu)

        # Последние две колонки (время/отсчёт) — текст прижат вправо, к краю строки, а не влево.
        self.saved_label = tk.Label(self, anchor="e")
        self.saved_label.grid(row=0, column=3, sticky="ew", padx=(0, 8))

        self.countdown_label = tk.Label(self, anchor="e", cursor="hand2")
        self.countdown_label.grid(row=0, column=4, sticky="ew", padx=(0, 8))
        self.countdown_label.bind("<Button-3>", self._interval_menu)

        # Работает (зелёная) / Ожидает (жёлтая — есть вопрос, требующий ответа, клик открывает
        # тот же диалог, что и клик по красной строке) / Остановлена (красная — слежение и
        # проверки для этого репозитория выключены пользователем, до повторного включения).
        self.run_button = tk.Button(self, width=12, command=self._on_run_button_click)
        self.run_button.grid(row=0, column=5, sticky="ew")

        self.status_label = tk.Label(self, anchor="w")
        self.status_label.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(2, 0))

        for widget in (self, self.status_label, self.saved_label):
            widget.bind("<Button-1>", self._row_click, add="+")
            widget.bind("<Button-3>", self._row_menu, add="+")

        self.pack(fill="x", padx=6, pady=3)

    # --- контекстные меню --------------------------------------------------------

    def _path_menu(self, event):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Открыть", command=lambda: _open_in_explorer(self.watcher.full_watch_path))
        menu.add_command(label="Копировать", command=lambda: self._copy(str(self.watcher.full_watch_path)))
        menu.add_command(label="Изменить путь...", command=self._edit_path)
        menu.tk_popup(event.x_root, event.y_root)

    def _git_menu(self, event):
        menu = tk.Menu(self, tearoff=0)
        url = self.watcher.git_web_url()
        menu.add_command(label="Открыть", command=self._open_git_now)
        menu.add_command(label="Копировать", command=lambda: self._copy(url or self.watcher.branch))
        menu.add_command(label="Изменить ветку...", command=self._edit_branch)
        menu.tk_popup(event.x_root, event.y_root)

    def _version_menu(self, event):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Изменить версию...", command=self._edit_version)
        menu.tk_popup(event.x_root, event.y_root)

    def _edit_version(self) -> None:
        from Class_Version import split_prefix_and_push

        base = self.watcher.known_version or self.watcher.current_tag
        if base and base != "тегов ещё нет":
            default_prefix, _ = split_prefix_and_push(base)
        else:
            default_prefix = str(self.watcher.dev_id)

        win = tk.Toplevel(self)
        win.title(f"Изменить версию — {self.watcher.name}")
        tk.Label(
            win,
            text="Версия — любой текст, никакого формата не требуется. Номер пуша в конце вы не\n"
                 "задаёте — он обнулится и дальше снова будет расти сам с каждым пушем.\n"
                 "Применяется сразу: коммит + пуш поверх того, что сейчас на git.",
            justify="left",
        ).pack(padx=12, pady=(12, 6), anchor="w")

        entry = tk.Entry(win, width=48)
        entry.insert(0, default_prefix)
        entry.pack(padx=12, pady=(0, 12), fill="x")
        entry.select_range(0, "end")
        entry.focus_set()
        _add_entry_context_menu(entry)

        def submit():
            # rstrip(",") — висячая запятая на конце (например "0.0,0.0,0.7,0,") иначе даёт тег
            # с двойной запятой на конце (version.build_tag сама добавляет ",0"): работать он бы
            # работал, но выглядит как опечатка, поэтому просто убираем лишнее.
            new_prefix = entry.get().strip().rstrip(",")
            if not new_prefix:
                return
            self.app.manual_version_change(self.watcher, new_prefix)
            win.destroy()

        entry.bind("<Return>", lambda e: submit())
        tk.Button(win, text="Применить (коммит + пуш сразу)", command=submit).pack(pady=(0, 12))
        _finalize_toplevel(win)

    def _interval_menu(self, event):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Задать интервал проверки...", command=self._set_interval)
        menu.tk_popup(event.x_root, event.y_root)

    def _copy(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)

    def _open_git(self, _event=None) -> None:
        self._open_git_now()

    def _open_git_now(self) -> None:
        url = self.watcher.git_web_url()
        if url:
            webbrowser.open(url)

    def _edit_path(self) -> None:
        new_value = simpledialog.askstring(
            "Изменить путь", "Относительный путь слежения (watch_paths[0]):",
            initialvalue=self.watcher.watch_paths[0], parent=self,
        )
        if new_value:
            self.app.on_edit_repo(self.watcher, watch_path=new_value)

    def _edit_branch(self) -> None:
        new_value = simpledialog.askstring(
            "Изменить ветку", "Ветка git:", initialvalue=self.watcher.branch, parent=self,
        )
        if new_value:
            self.app.on_edit_repo(self.watcher, branch=new_value)

    def _set_interval(self) -> None:
        # В секундах и обязательно целым числом — проще, чем возиться с разделителем дробной
        # части (запятая/точка путаются в разных региональных настройках Windows).
        seconds = simpledialog.askinteger(
            "Интервал проверки", f"Через сколько секунд проверять «{self.watcher.name}»?",
            initialvalue=self.watcher.check_interval_seconds, minvalue=1, parent=self,
        )
        if seconds:
            # Раньше правилось только в памяти (не сохранялось в config.json) — при следующем
            # запуске программы значение терялось и подставлялся общий интервал по умолчанию, из-
            # за чего мог заново возникать конфликт "данные разошлись" (см. edit_repo_runtime и
            # main() в watcher.py). Теперь идёт через on_edit_repo — так же сохраняется, как и
            # остальные изменения репозитория.
            self.app.on_edit_repo(self.watcher, interval_seconds=seconds)

    def _on_run_button_click(self) -> None:
        w = self.watcher
        if w.pending_question is not None:
            # Жёлтое состояние — это не переключатель, а напоминание об открытом вопросе: клик
            # открывает тот же диалог заново, ровно как клик по красной строке.
            self.app.reopen_pending(w)
        elif w.problem is not None:
            # Оранжевое "Проблема" — проверка не прошла, таймер стоит: клик показывает причину и
            # кнопку "Повторить проверку".
            self.app.show_problem(w)
        elif w.running:
            self.app.on_toggle_run(w, False)
        else:
            self.app.on_toggle_run(w, True)

    def _row_click(self, _event=None) -> None:
        if self.watcher.pending_question is not None:
            self.app.reopen_pending(self.watcher)
        elif self.watcher.problem is not None:
            self.app.show_problem(self.watcher)

    def _row_menu(self, event) -> None:
        menu = tk.Menu(self, tearoff=0)
        if self.watcher.problem is not None:
            menu.add_command(label="Повторить проверку", command=lambda: self.app.retry_check(self.watcher))
        menu.add_command(label="Изменить репозиторий...", command=self._edit_repo_full)
        menu.tk_popup(event.x_root, event.y_root)

    def _edit_repo_full(self) -> None:
        """Правая кнопка мыши на строке (не на конкретной ссылке/версии/интервале — там свои
        отдельные меню) — открывает окно, аналогичное добавлению репозитория, но с уже
        подставленными данными этой строки, включая имя (раньше имя было неизменяемым)."""
        w = self.watcher
        win = tk.Toplevel(self)
        win.title(f"Изменить репозиторий — {w.name}")

        rows = [
            ("path", "Папка хранения (где лежит .git)", str(w.repo_path)),
            ("branch", "Ветка для push", w.branch),
            ("interval", "Через сколько секунд проверять git", str(w.check_interval_seconds)),
            ("name", "Имя", w.name),
            ("watch_path", "Папка слежения внутри репозитория (пусто — вся папка)", w.watch_paths[0]),
        ]
        fields = {}
        for i, (key, label, current) in enumerate(rows):
            tk.Label(win, text=label, wraplength=260, justify="left").grid(
                row=i, column=0, sticky="w", padx=8, pady=4
            )
            entry = tk.Entry(win, width=40)
            entry.insert(0, current)
            entry.grid(row=i, column=1, padx=8, pady=4)
            _add_entry_context_menu(entry)
            fields[key] = entry

        def submit():
            path = fields["path"].get().strip()
            branch = fields["branch"].get().strip()
            name = fields["name"].get().strip()
            watch_path = fields["watch_path"].get().strip()
            if not (path and branch and name):
                return
            try:
                interval_seconds = int(fields["interval"].get().strip())
            except ValueError:
                interval_seconds = w.check_interval_seconds
            self.app.on_edit_repo(
                w, name=name, path=path, branch=branch,
                watch_path=watch_path, interval_seconds=interval_seconds,
            )
            win.destroy()

        tk.Button(win, text="Сохранить", command=submit).grid(
            row=len(rows), column=0, columnspan=2, pady=10
        )
        tk.Button(
            win, text="Удалить репозиторий...", fg="#a4000f",
            command=lambda: self._confirm_delete_step1(win, w),
        ).grid(row=len(rows) + 1, column=0, columnspan=2, pady=(0, 10))
        _finalize_toplevel(win)

    def _confirm_delete_step1(self, edit_win: tk.Toplevel, w) -> None:
        """Удаление — с двойным подтверждением (случайный клик не должен убрать репозиторий из
        слежения): первое окно объясняет, что именно произойдёт, второе — просто "точно?"."""
        win = tk.Toplevel(self)
        win.title(f"Удалить репозиторий — {w.name}")
        tk.Label(
            win,
            text=f"Убрать «{w.name}» из слежения AutoSync?\n\n"
                 "Сама папка и git-репозиторий на диске НЕ удаляются — пропадёт только запись\n"
                 "в этой программе (строка в окне, config.json). Добавить обратно можно будет\n"
                 "кнопкой «+», как и любой другой репозиторий.",
            justify="left", wraplength=380,
        ).pack(padx=16, pady=16)
        buttons = tk.Frame(win)
        buttons.pack(pady=(0, 12))
        tk.Button(
            buttons, text="Удалить", width=12, fg="#a4000f",
            command=lambda: (win.destroy(), self._confirm_delete_step2(edit_win, w)),
        ).pack(side="left", padx=8)
        tk.Button(buttons, text="Отмена", width=12, command=win.destroy).pack(side="left", padx=8)
        _finalize_toplevel(win)

    def _confirm_delete_step2(self, edit_win: tk.Toplevel, w) -> None:
        win = tk.Toplevel(self)
        win.title("Подтвердите ещё раз")
        tk.Label(
            win, text=f"Точно удалить «{w.name}»?",
            justify="left", fg="#a4000f", font=("TkDefaultFont", 10, "bold"),
        ).pack(padx=16, pady=16)
        buttons = tk.Frame(win)
        buttons.pack(pady=(0, 12))

        def confirm():
            win.destroy()
            edit_win.destroy()
            self.app.on_delete_repo(w)

        tk.Button(buttons, text="Да, удалить", width=14, fg="#a4000f", command=confirm).pack(
            side="left", padx=8
        )
        tk.Button(buttons, text="Отмена", width=12, command=win.destroy).pack(side="left", padx=8)
        _finalize_toplevel(win)

    # --- обновление вида -----------------------------------------------------------

    def refresh(self) -> None:
        w = self.watcher
        self.path_label.configure(text=w.name)
        self.git_label.configure(text=_last_n_path_parts(Path(w.name) / w.branch, 3))

        tag = w.current_tag
        if tag and tag != "тегов ещё нет" and "," in tag:
            prefix, last = tag.rsplit(",", 1)
            self.version_prefix_label.configure(text=prefix + ",")
            self.version_push_label.configure(text=last)
        else:
            self.version_prefix_label.configure(text=tag or "—")
            self.version_push_label.configure(text="")

        self.saved_label.configure(text=w.last_saved_at)
        remaining = max(0, int(w.next_check_at - time.time()))
        mm, ss = divmod(remaining, 60)
        timer_on = w.running and w.problem is None  # при "Проблеме" таймер стоит
        self.countdown_label.configure(text=f"{mm:02d}:{ss:02d}" if timer_on else "—:—")
        self.status_label.configure(text=f"{w.last_action} ({w.last_check_time})")

        needs_attention = w.pending_question is not None or w.problem is not None
        if w.pending_question is not None:
            self.run_button.configure(text="Ожидает", bg="#f6c343", activebackground="#f6c343")
        elif w.problem is not None:
            self.run_button.configure(text="Проблема", bg="#e8833a", activebackground="#e8833a")
        elif w.running:
            self.run_button.configure(text="Работает", bg="#4caf50", activebackground="#4caf50")
        else:
            self.run_button.configure(text="Остановлена", bg="#e05252", activebackground="#e05252")
        bg = "#f8d7da" if needs_attention else self.app.default_bg
        for widget in (self, self.status_label, self.saved_label, self.countdown_label,
                       self.version_frame, self.version_prefix_label, self.version_push_label,
                       self.path_label, self.git_label):
            widget.configure(bg=bg)


class AutoSyncGUI:
    def __init__(self, watchers: list, on_add_repo: Callable[[dict], object],
                 on_edit_repo: Callable[..., None],
                 on_manual_version_change: Callable[..., None],
                 on_toggle_run: Callable[..., None],
                 on_delete_repo: Callable[..., None],
                 on_retry_check: Optional[Callable[..., None]] = None):
        self.watchers = watchers
        self._on_add_repo_cb = on_add_repo
        self._on_edit_repo_cb = on_edit_repo
        self._on_manual_version_change_cb = on_manual_version_change
        self._on_toggle_run_cb = on_toggle_run
        self._on_delete_repo_cb = on_delete_repo
        self._on_retry_check_cb = on_retry_check

        self.root = tk.Tk()
        self.root.title("AutoSync")
        self.root.geometry("900x520")
        self.default_bg = self.root.cget("bg")
        # По умолчанию Tk гасит исключения из колбэков (кнопки, .after(), диалоги) сама — просто
        # печатает в stderr, и такая ошибка НИКОГДА не долетает ни до верхнеуровневого try/except
        # в AutoSync.pyw, ни до Save/autosync_crash.log (обнаружено 22.09 при расследовании
        # "краша" без единой строки в логах). Перехватываем сами, чтобы такие ошибки тоже попадали
        # в журнал и в crash-лог.
        self.root.report_callback_exception = self._handle_callback_exception

        top_bar = tk.Frame(self.root)
        top_bar.pack(fill="x", padx=6, pady=(6, 0))
        tk.Button(top_bar, text="+", width=3, command=self._open_add_dialog).pack(side="left")
        # Пока нет отдельной иконки — просто текстовая кнопка "ПРОБЛЕМЫ (n)", хорошо видна и без
        # значка. Нажатие — фильтр: показать только строки, где нужен ответ.
        self.alert_button = tk.Button(top_bar, text="", command=self._toggle_filter, fg="#a4000f")
        self.alert_button.pack(side="left", padx=8)

        # Список репозиториев и лог — в PanedWindow, чтобы можно было перетащить границу между
        # ними мышью и увеличить лог, если строк репозиториев мало, а лога нужно много видно
        # (жалоба "только 5 строчек лога и не видно больше").
        paned = ttk.PanedWindow(self.root, orient="vertical")
        paned.pack(fill="both", expand=True, padx=6, pady=6)

        list_container = tk.Frame(paned)
        canvas = tk.Canvas(list_container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
        self.rows_frame = tk.Frame(canvas)
        self.rows_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        rows_window = canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        # Ширина содержимого канваса всегда равна ширине окна — иначе при изменении размера окна
        # строки не растягивались вслед за ним, и правые колонки "уезжали" за пределы видимой
        # области без возможности прокрутить (только вертикальная прокрутка и была нужна).
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(rows_window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        paned.add(list_container, weight=3)

        log_container = tk.Frame(paned)
        log_bar = tk.Frame(log_container)
        log_bar.pack(fill="x")
        tk.Label(log_bar, text="Журнал событий").pack(side="left", padx=(2, 0))
        tk.Button(log_bar, text="Копировать весь лог", command=self._copy_log).pack(side="right")
        log_body = tk.Frame(log_container)
        log_body.pack(fill="both", expand=True)
        # state="disabled" в некоторых сборках Tk на Windows заодно ломает и обычное выделение
        # мышью (работала только кнопка "Копировать весь лог") — вместо этого держим текст
        # "normal" всегда, а от ручного редактирования защищаемся отдельно, блокируя клавиши
        # (см. _block_log_editing ниже); выделение и Ctrl+C/Ctrl+A при этом работают как обычно.
        self.log_text = tk.Text(log_body, wrap="word")
        self.log_text.bind("<Key>", self._block_log_editing)
        log_scrollbar = ttk.Scrollbar(log_body, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scrollbar.pack(side="right", fill="y")
        paned.add(log_container, weight=2)

        self.filter_mode = False
        self._rows: dict = {}
        self._log_lock = threading.Lock()
        for w in watchers:
            self._add_row(w)

        self._load_log_history()
        self._tick()

    _LOG_NAV_KEYSYMS = {
        "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
        "Shift_L", "Shift_R", "Control_L", "Control_R",
    }

    def _block_log_editing(self, event) -> Optional[str]:
        """Лог должен оставаться читаемым мышью (выделение/Ctrl+C/Ctrl+A), но не редактируемым
        с клавиатуры.

        ВАЖНО про раскладку клавиатуры: раньше здесь проверялось event.keysym.lower() in
        ("c", "a") — а keysym это СИМВОЛ, который получается с учётом текущей раскладки. На
        русской раскладке физическая клавиша C выдаёт keysym вроде 'Cyrillic_es', а не 'c', и
        проверка никогда не срабатывала — отсюда и "Ctrl+C не работает". event.keycode — это код
        ФИЗИЧЕСКОЙ клавиши (в Windows он не зависит от раскладки), проверяем по нему. Также не
        полагаемся на встроенную привязку Tk к Ctrl+C (она тоже завязана на keysym и тоже сломана
        на нелатинской раскладке) — копирование/выделение всего вызываем сами явно."""
        if event.keysym in self._LOG_NAV_KEYSYMS:
            return None
        ctrl_pressed = bool(event.state & 0x4)
        if ctrl_pressed and event.keycode == _VK_C:
            self.log_text.event_generate("<<Copy>>")
            return "break"
        if ctrl_pressed and event.keycode == _VK_A:
            self.log_text.tag_add("sel", "1.0", "end")
            return "break"
        return "break"

    def _handle_callback_exception(self, exc_type, exc_value, exc_tb) -> None:
        """Ставится в self.root.report_callback_exception (см. __init__). Вызывается самим Tk,
        когда исключение вылетело внутри любого колбэка (нажатие кнопки, .after(), логика
        диалога) уже во время работы mainloop() — то есть ПОСЛЕ старта окна, когда верхнеуровневый
        try/except в AutoSync.pyw уже не смотрит за кодом. Без этого такая ошибка просто печаталась
        бы в stderr (не виден в .pyw-запуске без консоли) и исчезала бесследно."""
        error_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            _CRASH_LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_CRASH_LOG_FILE_PATH, "a", encoding="utf-8") as f:
                f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"(ошибка внутри колбэка Tkinter, окно продолжает работать) ---\n")
                f.write(error_text)
        except OSError:
            pass
        self.log("AutoSync", f"ОШИБКА В КОЛБЭКЕ (полный текст дописан в Save/autosync_crash.log): "
                              f"{exc_type.__name__}: {exc_value}")

    # --- вызывается из фоновых потоков (watcher.py) -----------------------------

    def log(self, repo_name: str, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] [{repo_name}] {message}"
        self._append_log_file(line)
        self.root.after(0, self._append_log, line)

    def _append_log_file(self, line: str) -> None:
        with self._log_lock:
            try:
                _LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(_LOG_FILE_PATH, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass  # запись в файл — не критично, окно всё равно должно работать дальше

    def _load_log_history(self) -> None:
        """При старте подтягиваем хвост лога с прошлых сессий, чтобы окно не начиналось с
        пустоты — раньше весь журнал терялся при каждом перезапуске программы."""
        try:
            lines = _LOG_FILE_PATH.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if not lines:
            return
        tail = lines[-_MAX_LOG_LINES:]
        self.log_text.insert("end", "\n".join(tail) + "\n")
        self.log_text.insert("end", "── новый запуск программы ──\n")
        self.log_text.see("end")

    def ask_yes_no(self, watcher, message: str) -> bool:
        pq = PendingQuestion(kind="yes_no", message=message)
        watcher.pending_question = pq
        self.root.after(0, self._show_pending_dialog, watcher, pq)
        pq.event.wait()
        watcher.pending_question = None
        return bool(pq.result)

    def ask_choice(self, watcher, branch: str, diff_lines: List[str], situation: str = "",
                   local_label: str = "", git_label: str = "") -> "SyncChoice":
        """Расхождение версий одной ветки — три стандартных варианта (см. _show_pending_dialog)."""
        pq = PendingQuestion(
            kind="choice", branch=branch, diff_lines=diff_lines, situation=situation,
            labels={"local": local_label, "git": git_label},
        )
        watcher.pending_question = pq
        self.root.after(0, self._show_pending_dialog, watcher, pq)
        pq.event.wait()
        watcher.pending_question = None
        return pq.result  # type: ignore[return-value]

    def ask_branch_mismatch(self, watcher, disk_branch: str, tracked_branch: str, disk_label: str,
                            tracked_label: str, commits: List[str]) -> str:
        """Расхождение ветки слежения (11.10.2026): на диске одна ветка, AutoSync следит за другой.
        Возвращает "disk" (оставить ветку с диска — сменить ветку слежения) или "tracked"
        (перейти на указанную ветку — дальше сверка версий)."""
        pq = PendingQuestion(
            kind="branch_mismatch", branch=tracked_branch, diff_lines=commits,
            labels={"disk_branch": disk_branch, "disk": disk_label, "tracked": tracked_label},
        )
        watcher.pending_question = pq
        self.root.after(0, self._show_pending_dialog, watcher, pq)
        pq.event.wait()
        watcher.pending_question = None
        return pq.result  # type: ignore[return-value]

    def ask_repo_data_conflict(self, watcher, central: dict, folder: dict) -> str:
        """Данные репозитория разошлись между общим config.json и .autosync_data в папке
        проекта (см. repo_data.py) — блокирующий вопрос, как ask_choice. Возвращает
        "central" или "folder"."""
        pq = PendingQuestion(kind="repo_data_conflict", central=central, folder=folder)
        watcher.pending_question = pq
        self.root.after(0, self._show_pending_dialog, watcher, pq)
        pq.event.wait()
        watcher.pending_question = None
        return pq.result  # type: ignore[return-value]

    def reopen_pending(self, watcher) -> None:
        pq = watcher.pending_question
        if pq is not None:
            self._show_pending_dialog(watcher, pq)

    def add_row_for(self, watcher) -> None:
        self.root.after(0, self._add_row, watcher)

    # --- внутреннее (главный поток) ---------------------------------------------

    def _force_focus(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _show_pending_dialog(self, watcher, pq: PendingQuestion) -> None:
        self._force_focus()
        win = tk.Toplevel(self.root)
        win.title(f"AutoSync — {watcher.name}")
        win.transient(self.root)

        def close_without_answer():
            win.destroy()  # НЕ трогаем pq.event — вопрос остаётся открытым, строка красная

        win.protocol("WM_DELETE_WINDOW", close_without_answer)

        if pq.kind == "yes_no":
            tk.Label(win, text=pq.message, wraplength=420, justify="left").pack(padx=16, pady=16)
            buttons = tk.Frame(win)
            buttons.pack(pady=(0, 12))

            def answer(value: bool):
                pq.result = value
                pq.event.set()
                win.destroy()

            tk.Button(buttons, text="Да", width=10, command=lambda: answer(True)).pack(side="left", padx=8)
            tk.Button(buttons, text="Нет", width=10, command=lambda: answer(False)).pack(side="left", padx=8)
        elif pq.kind == "repo_data_conflict":
            def fmt(d: dict) -> str:
                return (
                    f"ветка: {d.get('branch', '—')}\n"
                    f"интервал проверки: {d.get('check_interval_minutes', '—')} мин\n"
                    f"последняя версия: {d.get('known_version') or '—'}"
                )

            tk.Label(
                win,
                text=(f"Данные репозитория {watcher.name!r} в общем config.json и в папке "
                      f"проекта (.autosync_data) разошлись. Какие данные верны?"),
                wraplength=440, justify="left",
            ).pack(padx=16, pady=(16, 8))

            info = tk.Frame(win)
            info.pack(padx=16, pady=(0, 8))
            tk.Label(info, text="Общий config.json:", font=("", 9, "bold"), justify="left").grid(
                row=0, column=0, sticky="w", padx=(0, 24)
            )
            tk.Label(info, text="Папка проекта (.autosync_data):", font=("", 9, "bold"), justify="left").grid(
                row=0, column=1, sticky="w"
            )
            tk.Label(info, text=fmt(pq.central), justify="left").grid(
                row=1, column=0, sticky="nw", padx=(0, 24)
            )
            tk.Label(info, text=fmt(pq.folder), justify="left").grid(row=1, column=1, sticky="nw")

            buttons = tk.Frame(win)
            buttons.pack(pady=(4, 16))

            def answer_repo_data(value: str):
                pq.result = value
                pq.event.set()
                win.destroy()

            tk.Button(buttons, text="Взять из config.json", width=20,
                      command=lambda: answer_repo_data("central")).pack(side="left", padx=6)
            tk.Button(buttons, text="Взять из папки проекта", width=22,
                      command=lambda: answer_repo_data("folder")).pack(side="left", padx=6)
        elif pq.kind == "branch_mismatch":
            disk_branch = pq.labels.get("disk_branch", "?")
            tk.Label(
                win,
                text=(f"Расхождение ветки слежения в {watcher.name!r}: на диске открыта ветка "
                      f"{disk_branch!r}, а AutoSync следит за {pq.branch!r}. Какая ветка правильная?"),
                wraplength=520, justify="left",
            ).pack(padx=16, pady=(16, 6), anchor="w")
            if pq.diff_lines:
                tk.Label(
                    win,
                    text=(f"Коммиты на {disk_branch!r}, которых нет в {pq.branch!r} (при выборе "
                          f"{pq.branch!r} они останутся в ветке {disk_branch!r}, а файлы с диска "
                          f"пойдут в сверку версий):"),
                    wraplength=520, justify="left",
                ).pack(padx=16, pady=(0, 4), anchor="w")
                commits_box = tk.Text(win, height=min(10, max(3, len(pq.diff_lines))), width=70)
                commits_box.insert("1.0", "\n".join(pq.diff_lines))
                commits_box.configure(state="disabled")
                commits_box.pack(padx=16, pady=4)

            buttons = tk.Frame(win)
            buttons.pack(pady=(8, 16))

            def answer_branch(value: str):
                pq.result = value
                pq.event.set()
                win.destroy()

            tk.Button(
                buttons, text=f"Оставить {disk_branch} — {pq.labels.get('disk', '')}",
                command=lambda: answer_branch("disk"),
            ).pack(side="left", padx=6)
            tk.Button(
                buttons, text=f"Перейти на {pq.branch} — {pq.labels.get('tracked', '')}",
                command=lambda: answer_branch("tracked"),
            ).pack(side="left", padx=6)
        else:
            # Расхождение версий одной ветки — три стандартных варианта (11.10.2026). На кнопках —
            # к какому тегу ведёт выбор; "+" — поверх тега есть ещё изменения.
            tk.Label(
                win, text=pq.situation or f"Расхождение в ветке {pq.branch!r}:",
                wraplength=520, justify="left",
            ).pack(padx=16, pady=(16, 4), anchor="w")
            tk.Label(win, text="Файлы, которые различаются (диск ↔ git):", justify="left").pack(
                padx=16, pady=(4, 0), anchor="w"
            )
            diff_box = tk.Text(win, height=min(10, max(3, len(pq.diff_lines))), width=70)
            diff_box.insert("1.0", "\n".join(pq.diff_lines) if pq.diff_lines else "(нет данных о разнице)")
            diff_box.configure(state="disabled")
            diff_box.pack(padx=16, pady=4)

            buttons = tk.Frame(win)
            buttons.pack(pady=(4, 16))

            def answer(value: "SyncChoice"):
                pq.result = value
                pq.event.set()
                win.destroy()

            tk.Button(buttons, text=f"Взять всё с git — {pq.labels.get('git', '')}",
                      command=lambda: answer(SyncChoice.TAKE_GIT)).pack(side="left", padx=6)
            tk.Button(buttons, text=f"Отправить нашу версию как основную — {pq.labels.get('local', '')}",
                      command=lambda: answer(SyncChoice.TAKE_LOCAL)).pack(side="left", padx=6)
            tk.Button(buttons, text="Избирательно (слияние + mergetool)",
                      command=lambda: answer(SyncChoice.OPEN_MERGETOOL)).pack(side="left", padx=6)

        _finalize_toplevel(win)

    def _append_log(self, line: str) -> None:
        self.log_text.insert("end", line + "\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > _MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{line_count - _MAX_LOG_LINES}.0")
        self.log_text.see("end")

    def _copy_log(self) -> None:
        # Отдельная кнопка "скопировать весь лог целиком" — быстрее, чем выделять мышью весь
        # текст. Выделение конкретного куска мышью + Ctrl+C теперь тоже работает (см.
        # _block_log_editing выше — раньше state="disabled" на некоторых сборках Tk блокировало
        # и это тоже, оставляя рабочей только эту кнопку).
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_text.get("1.0", "end-1c"))

    def _add_row(self, watcher) -> None:
        row = RepoRow(self.rows_frame, watcher, self)
        # Ключ — сам объект watcher (id), а не watcher.name: имя теперь можно менять через
        # "Изменить репозиторий...", и по строковому имени строка была бы потеряна после смены.
        self._rows[id(watcher)] = row

    def _toggle_filter(self) -> None:
        self.filter_mode = not self.filter_mode
        self._apply_filter()

    def _apply_filter(self) -> None:
        for w in self.watchers:
            row = self._rows.get(id(w))
            if row is None:
                continue
            show = (not self.filter_mode) or (w.pending_question is not None) or (w.problem is not None)
            if show:
                row.pack(fill="x", padx=6, pady=3)
            else:
                row.pack_forget()

    def _open_add_dialog(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Добавить репозиторий")

        # Порядок и подписи — по вашему списку (папка хранения / ветка для push / интервал),
        # плюс два необязательных поля с понятным объяснением, что это и зачем — заполнять их
        # нужно не всегда:
        #   "Имя" — просто подпись репозитория в списке и в файле состояния (state.json). Если
        #       оставить пустым — возьмём имя папки автоматически, ничего вводить не обязательно.
        #   "Папка слежения внутри репозитория" — НЕ путь к самому репозиторию (это отдельное
        #       поле выше), а конкретная подпапка ВНУТРИ него, изменения в которой должны сразу
        #       пушиться (например у вас — "Godot_Template_Life_Operator", а не весь проект
        #       целиком, где много не относящихся к делу файлов). Если оставить пустым — будет
        #       следить за всей папкой репозитория.
        # Git-адрес (origin) отдельно не спрашиваем — берём как уже настроено в самой папке
        # (git remote), спрашиваем только ветку, потому что именно её вы выбираете сами.
        fields = {}
        rows = [
            ("path", "Папка хранения (где лежит .git)", True),
            ("branch", "Ветка для push", True),
            ("interval", "Через сколько секунд проверять git", True),
            ("name", "Имя (необязательно — по умолчанию из папки)", False),
            ("watch_path", "Папка слежения внутри репозитория (необязательно — по умолчанию вся папка)", False),
        ]
        for i, (key, label, _required) in enumerate(rows):
            tk.Label(win, text=label, wraplength=260, justify="left").grid(
                row=i, column=0, sticky="w", padx=8, pady=4
            )
            entry = tk.Entry(win, width=40)
            entry.grid(row=i, column=1, padx=8, pady=4)
            _add_entry_context_menu(entry)
            fields[key] = entry
        fields["interval"].insert(0, "1800")

        def submit():
            path = fields["path"].get().strip()
            branch = fields["branch"].get().strip()
            if not (path and branch):
                return
            # Если имя не задано вручную — по умолчанию берём последние 3 части пути (как при
            # выборе папки в проводнике), а не только имя самой папки: так строка в таблице
            # остаётся узнаваемой даже когда конечные папки у разных проектов называются одинаково
            # (например "Сам проект").
            name = fields["name"].get().strip() or _last_n_path_parts(Path(path), 3)
            watch_path = fields["watch_path"].get().strip()  # пусто — следим за всей папкой репозитория
            repo_cfg = {
                "name": name,
                "path": path,
                "branch": branch,
                "watch_paths": [watch_path],
            }
            try:
                interval_seconds = int(fields["interval"].get().strip() or "1800")
            except ValueError:
                interval_seconds = 1800
            watcher = self._on_add_repo_cb(repo_cfg, interval_seconds)
            self.watchers.append(watcher)
            self._add_row(watcher)
            win.destroy()

        tk.Button(win, text="Добавить", command=submit).grid(row=len(rows), column=0, columnspan=2, pady=10)
        _finalize_toplevel(win)

    def on_edit_repo(self, watcher, **changes) -> None:
        self._on_edit_repo_cb(watcher, **changes)

    def manual_version_change(self, watcher, new_version) -> None:
        self._on_manual_version_change_cb(watcher, new_version)

    def on_toggle_run(self, watcher, running: bool) -> None:
        self._on_toggle_run_cb(watcher, running)

    def on_delete_repo(self, watcher) -> None:
        self._on_delete_repo_cb(watcher)

    def retry_check(self, watcher) -> None:
        if self._on_retry_check_cb is not None:
            self._on_retry_check_cb(watcher)

    def show_problem(self, watcher) -> None:
        """Окно с причиной "Проблемы" (проверка не прошла, таймер репозитория остановлен) и
        кнопкой "Повторить проверку". Сам по себе ничего не блокирует — в отличие от вопросов
        ask_yes_no/ask_choice, фоновый поток проверки его не ждёт."""
        if watcher.problem is None:
            return
        win = tk.Toplevel(self.root)
        win.title(f"AutoSync — проблема: {watcher.name}")
        win.transient(self.root)
        tk.Label(
            win, text="Проверка не прошла — таймер и слежение для этого репозитория остановлены.",
            font=("TkDefaultFont", 9, "bold"), justify="left",
        ).pack(padx=16, pady=(16, 6), anchor="w")
        text = tk.Text(win, wrap="word", width=80, height=12)
        text.insert("1.0", watcher.problem)
        # Текст можно выделить и скопировать (например, чтобы переслать ошибку git); правка его
        # ни на что не влияет — это только копия watcher.problem для показа.
        text.pack(padx=16, pady=(0, 8), fill="both", expand=True)
        buttons = tk.Frame(win)
        buttons.pack(pady=(0, 12))

        def retry():
            win.destroy()
            self.retry_check(watcher)

        tk.Button(buttons, text="Повторить проверку", command=retry).pack(side="left", padx=6)
        tk.Button(buttons, text="Закрыть", command=win.destroy).pack(side="left", padx=6)
        _finalize_toplevel(win)

    def remove_row_for(self, watcher) -> None:
        """Вызывается из фонового потока (watcher.py: delete_repo_runtime) после того, как
        слежение снято — сама уборка виджета и списка watchers идёт в главном потоке."""
        self.root.after(0, self._remove_row, watcher)

    def _remove_row(self, watcher) -> None:
        row = self._rows.pop(id(watcher), None)
        if row is not None:
            row.destroy()
        if watcher in self.watchers:
            self.watchers.remove(watcher)

    def _tick(self) -> None:
        pending_count = sum(
            1 for w in self.watchers if w.pending_question is not None or w.problem is not None
        )
        # Раньше при 0 проблем кнопка вообще исчезала ("ПРОБЛЕМЫ (0)" не было видно совсем) —
        # теперь кнопка всегда на месте, просто со счётчиком.
        self.alert_button.configure(text=f"ПРОБЛЕМЫ ({pending_count})")
        if self.filter_mode and pending_count == 0:
            self.filter_mode = False
        self._apply_filter()
        for w in self.watchers:
            row = self._rows.get(id(w))
            if row is not None:
                row.refresh()
        self.root.after(500, self._tick)

    def run(self) -> None:
        """Блокирует до закрытия окна — должно вызываться из ГЛАВНОГО потока программы."""
        self.root.mainloop()
