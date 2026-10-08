from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def write_json(data: dict, output_path: Path) -> Path:
    """Salva JSON UTF-8 com escrita atômica."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=output_path.parent,
            prefix=f".{output_path.stem}-",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            temporary_path = Path(tmp.name)
            json.dump(data, tmp, ensure_ascii=False, indent=2)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())

        temporary_path.replace(output_path)
        return output_path
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Não foi possível salvar o JSON em '{output_path}': {exc}") from exc
    finally:
        if temporary_path and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass
