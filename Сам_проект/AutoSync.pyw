"""
Запуск AutoSync БЕЗ окна консоли: двойной клик по этому файлу (или ярлык на него) на Windows
открывает только окно программы — pythonw.exe, который стоит за .pyw-файлами, консоль не создаёт.

Раньше это было главной причиной, почему без консоли было бы неудобно: если что-то падает ДО того,
как открылось окно программы (например, битый config.json), консоли нет — и ошибку было бы вообще
не увидеть, программа просто "не запустилась" молча. Поэтому здесь любая ошибка на старте:
  1) записывается в файл Save/autosync_crash.log рядом с программой (можно скопировать и показать),
  2) сразу же показывается всплывающим окном (messagebox) — его видно, даже если консоли нет.

Сама программа (22.09) разложена по папкам: код — в Class/ (файлы Class_*.py), данные и логи —
в Save/ (config.json, state.json, autosync.log и т.п., по аналогии с папкой Save/ в Godot_Template).
Этот файл и сам остаётся на верхнем уровне — как точка входа (по аналогии с project.godot).
"""

import sys
import traceback
from pathlib import Path

_SELF_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SELF_DIR))
sys.path.insert(0, str(_SELF_DIR / "Class"))


def _show_fatal_error(exc: BaseException) -> None:
    error_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    crash_log_path = _SELF_DIR / "Save" / "autosync_crash.log"

    try:
        crash_log_path.write_text(error_text, encoding="utf-8")
    except OSError:
        pass

    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        # Полный текст — в autosync_crash.log, в окне показываем достаточно, чтобы понять суть.
        messagebox.showerror(
            "AutoSync — ошибка запуска",
            "Программа не смогла запуститься.\n\n"
            f"Подробности сохранены в:\n{crash_log_path}\n\n"
            + error_text[-1200:],
        )
    except Exception:
        pass  # если даже messagebox не поднялся — хотя бы файл уже записан


if __name__ == "__main__":
    try:
        import Class_Watcher as watcher

        watcher.main()
    except BaseException as exc:  # ловим всё, включая ошибки конфигурации/импорта при старте
        _show_fatal_error(exc)
