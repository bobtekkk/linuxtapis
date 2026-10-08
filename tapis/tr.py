"""User-facing text in the user's language, from the Mac app's own
Localizable.strings files (keys are the English text)."""

import re
from pathlib import Path

from PyQt6.QtCore import QLocale

_DIR = Path(__file__).parent / "locale"
_table: dict[str, str] | None = None
_ENTRY = re.compile(r'"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"\s*;')


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


def _load() -> dict[str, str]:
    names = []
    for lang in QLocale.system().uiLanguages():
        lang = lang.replace("_", "-")
        names += [lang, lang.split("-")[0]]
        if lang.startswith("pt"):
            names.append("pt-BR")
        if lang.startswith("zh"):
            names.append("zh-Hans")
    for name in names:
        f = _DIR / f"{name}.strings"
        if f.exists():
            text = f.read_text(encoding="utf-8")
            return {_unescape(k): _unescape(v) for k, v in _ENTRY.findall(text)}
    return {}


def tr(key: str) -> str:
    global _table
    if _table is None:
        _table = _load()
    return _table.get(key, key)
