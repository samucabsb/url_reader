from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable

_HTML_SPACE_RE = re.compile(r"[ \t\f\v]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
_MULTI_DASH_RE = re.compile(r"[-_]{2,}")


def normalize_text(value: str | None) -> str:
    """Normaliza Unicode e espaços sem reescrever o conteúdo."""
    if not value:
        return ""

    text = value.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    text = _HTML_SPACE_RE.sub(" ", text)
    text = re.sub(r" *\n *", "\n", text)
    return _MULTI_NEWLINE_RE.sub("\n\n", text).strip()


def text_key(value: str) -> str:
    """Chave estável para deduplicação sem diferenciar maiúsculas/minúsculas."""
    normalized = re.sub(r"\s+", " ", normalize_text(value).casefold()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def deduplicate_strings(values: Iterable[str]) -> list[str]:
    """Remove duplicatas preservando a primeira ocorrência."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = normalize_text(value)
        if not normalized:
            continue
        key = text_key(normalized)
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def sanitize_filename(title: str | None, fallback: str = "resultado") -> str:
    """Gera um nome de arquivo seguro, sem permitir travessia de diretórios."""
    base = normalize_text(title) or fallback
    base = _INVALID_FILENAME_RE.sub("-", base)
    base = re.sub(r"\s+", "-", base.strip(" ._-"))
    base = _MULTI_DASH_RE.sub("-", base)
    base = base.strip(" .") or fallback

    reserved = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
    }
    if base.upper() in reserved:
        base = f"{base}-pagina"

    return f"{base[:100].rstrip(' .')}.json"
