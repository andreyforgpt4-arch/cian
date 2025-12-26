from __future__ import annotations

import os
import sys
from typing import Any


def configure_console_utf8() -> None:
    """
    Best-effort: make Windows console output UTF-8 to avoid mojibake for Cyrillic.
    Works in most conhost/Windows Terminal cases.
    """
    if os.name != "nt":
        return

    # Try to set Windows console codepages to UTF-8.
    try:
        import ctypes  # type: ignore

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass

    # Reconfigure Python streams to UTF-8 (Python 3.7+).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[attr-defined]
    except Exception:
        pass


# Configure on import so regular print() also benefits.
configure_console_utf8()


def safe_str(x: Any) -> str:
    s = str(x)
    # Avoid Windows cp1251 console crashes / mojibake in logs.
    try:
        sys.stdout.write("")  # touch
        return s
    except Exception:
        return s.encode("utf-8", "backslashreplace").decode("ascii", "backslashreplace")


def safe_print(*args: Any, sep: str = " ", end: str = "\n", flush: bool = True) -> None:
    out = sep.join(safe_str(a) for a in args) + end
    try:
        sys.stdout.write(out)
        if flush:
            sys.stdout.flush()
    except Exception:
        sys.stdout.buffer.write(out.encode("utf-8", "backslashreplace"))
        if flush:
            sys.stdout.flush()

