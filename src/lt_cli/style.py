"""Terminal styling: ANSI colors + emoji, enabled only when writing to a terminal.

Controls (first match wins):
  --color always|never|auto   CLI flag
  LT_PRETTY=0                 plain output (set in .env; default 1 = pretty)
  NO_COLOR=1                  disable colors (https://no-color.org)
  FORCE_COLOR=1               enable colors even when piped
  LT_EMOJI=0                  keep colors but drop emoji

In auto mode styling is only used when stdout is a terminal, so piped or
redirected output stays plain text.
"""

from __future__ import annotations

import os
import re
import sys

IS_WIN = os.name == "nt"

CODES = {
    "bold": "1", "dim": "2", "italic": "3", "underline": "4",
    "red": "31", "green": "32", "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36",
    "gray": "90", "bred": "91", "bgreen": "92", "byellow": "93", "bblue": "94",
    "bmagenta": "95", "bcyan": "96",
}

_MSYS_PTY = re.compile(r"\\(?:cygwin|msys)-[0-9a-f]+-pty\d+-(?:from|to)-master")


def _is_msys_pty(stream) -> bool:
    """True if `stream` is a Git Bash / Cygwin (mintty) terminal pipe on Windows."""
    if not IS_WIN:
        return False
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class FILE_NAME_INFO(ctypes.Structure):
            _fields_ = [("FileNameLength", wintypes.DWORD), ("FileName", wintypes.WCHAR * 512)]

        handle = msvcrt.get_osfhandle(stream.fileno())
        info = FILE_NAME_INFO()
        ok = ctypes.windll.kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(handle), 2, ctypes.byref(info), ctypes.sizeof(info))  # 2 = FileNameInfo
        if not ok:
            return False
        name = info.FileName[: info.FileNameLength // 2]
        return bool(_MSYS_PTY.search(name))
    except Exception:
        return False


def is_terminal(stream) -> bool:
    """isatty() that also recognises Git Bash's mintty terminal on Windows."""
    try:
        if stream.isatty():
            return True
    except (AttributeError, ValueError):
        return False
    return _is_msys_pty(stream)


def _enable_windows_vt() -> None:
    """Let the classic Windows console interpret ANSI escape codes."""
    if not IS_WIN:
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        for std in (-11, -12):  # stdout, stderr
            h = k32.GetStdHandle(std)
            mode = ctypes.c_uint32()
            if k32.GetConsoleMode(h, ctypes.byref(mode)):
                k32.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


class Style:
    def __init__(self, color: bool, emoji: bool):
        self.color = color
        self.emoji = emoji and color

    def __call__(self, text: str, *styles: str) -> str:
        if not self.color or not styles or not text:
            return text
        codes = ";".join(CODES[s] for s in styles)
        return f"\033[{codes}m{text}\033[0m"

    def icon(self, emoji: str, fallback: str = "") -> str:
        """Emoji followed by a space, or the fallback when emoji are off."""
        if self.emoji:
            return emoji + " "
        return fallback + " " if fallback else ""


def make_style(mode: str = "auto", stream=None) -> Style:
    stream = stream or sys.stdout
    if mode == "always":
        color = True
    elif mode == "never" or not _env_flag("LT_PRETTY", True):
        color = False
    elif os.environ.get("NO_COLOR"):
        color = False
    elif os.environ.get("FORCE_COLOR"):
        color = True
    else:
        color = is_terminal(stream)
    if color:
        _enable_windows_vt()
    return Style(color, _env_flag("LT_EMOJI", True))


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


PLAIN = Style(False, False)
