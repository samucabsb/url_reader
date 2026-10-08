from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from cleaner import sanitize_filename
from config import Settings
import extractor as extractor_module
from extractor import extract_document, render_with_playwright
from fetcher import FetchError, fetch_page, validate_url
from json_exporter import write_json


def _safe_first_jsonld_value(soup, field_names):
    """Versão defensiva do leitor de JSON-LD.

    Alguns sites publicam JSON-LD irregular/incompleto. O extrator original
    espera dicionários em todos os nós; aqui ignoramos valores nulos ou
    estruturas que não sejam objetos antes de usar .get().
    """
    for script in soup.find_all(
        "script",
        attrs={"type": re.compile(r"application/ld\+json", re.I)},
    ):
        raw = script.string or script.get_text()
        raw = raw.strip() if raw else ""
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue

        stack = [data]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                for field in field_names:
                    candidate = value.get(field)
                    if candidate is not None and candidate != "":
                        return candidate
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)

    return None


# Corrige páginas cujo JSON-LD contém valores nulos ou estruturas incomuns.
# O restante do pipeline continua sendo o extractor.py original.
extractor_module._first_jsonld_value = _safe_first_jsonld_value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extrai conteúdo textual principal de uma página web e salva em JSON."
    )
    parser.add_argument("url", nargs="?", help="URL HTTP(S) da página a ser processada.")
    parser.add_argument(
        "-o", "--output",
        help="Nome do arquivo JSON de saída. Padrão: título da página sanitizado.",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Diretório onde o JSON será salvo (padrão: output).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Timeout de leitura por requisição em segundos (padrão: 30).",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=15 * 1024 * 1024,
        help="Tamanho máximo da resposta HTML (padrão: 15 MiB).",
    )
    parser.add_argument(
        "--max-redirects",
        type=int,
        default=5,
        help="Máximo de redirecionamentos (padrão: 5).",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="Não consultar robots.txt antes do acesso.",
    )
    parser.add_argument(
        "--render",
        action="store_true",
        help="Usa Playwright/Chromium para renderizar a página antes da extração.",
    )
    parser.add_argument(
        "--auto-render",
        action="store_true",
        help="Tenta primeiro HTTP normal e usa Playwright se o conteúdo extraído for vazio.",
    )
    return parser


def get_url_argument_or_input(value: str | None) -> str:
    url = value.strip() if value else ""
    if url:
        return url
    try:
        return input("Informe a URL: ").strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise FetchError("Entrada cancelada pelo usuário.") from exc


def build_settings(args: argparse.Namespace) -> Settings:
    if args.timeout <= 0:
        raise ValueError("--timeout deve ser maior que zero.")
    if args.max_bytes <= 0:
        raise ValueError("--max-bytes deve ser maior que zero.")
    if args.max_redirects < 0:
        raise ValueError("--max-redirects não pode ser negativo.")

    return Settings(
        connect_timeout=min(args.timeout, 15.0),
        read_timeout=args.timeout,
        max_response_bytes=args.max_bytes,
        max_redirects=args.max_redirects,
        output_dir=Path(args.output_dir),
    )


def add_extraction_timestamp(document: dict) -> None:
    document["metadata"]["data_extracao"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    plain_text = document.get("conteudo", {}).get("texto_plano", "")
    words = len(plain_text.split()) if plain_text else 0
    chars = len(plain_text)
    sections = len(document.get("conteudo", {}).get("secoes", []))
    document["extracao"]["estatisticas"] = {
        "caracteres": chars,
        "palavras": words,
        "secoes": sections,
        "links": len(document.get("recursos", {}).get("links", [])),
        "imagens": len(document.get("recursos", {}).get("imagens", [])),
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = build_settings(args)
        raw_url = get_url_argument_or_input(args.url)
        url = validate_url(raw_url)

        render_warnings: tuple[str, ...] = ()

        if args.render:
            html, final_url, status_code, render_warnings = render_with_playwright(
                url,
                settings,
                respect_robots=not args.ignore_robots,
            )
            document = extract_document(
                html,
                final_url,
                requested_url=url,
                status_code=status_code,
                encoding="utf-8",
                max_links=settings.max_links,
                max_images=settings.max_images,
            )
            document["extracao"]["metodo_aquisicao"] = "playwright"
        else:
            fetched = fetch_page(url, settings, respect_robots=not args.ignore_robots)
            document = extract_document(
                fetched.content,
                fetched.final_url,
                requested_url=fetched.requested_url,
                status_code=fetched.status_code,
                encoding=fetched.encoding,
                max_links=settings.max_links,
                max_images=settings.max_images,
            )
            document["extracao"]["metodo_aquisicao"] = "requests"
            document["extracao"]["status_http"] = fetched.status_code
            document["extracao"]["redirecionamentos"] = fetched.redirects
            document["extracao"]["tempo_download_s"] = round(fetched.elapsed_seconds, 3)
            document["extracao"]["observacoes"].extend(fetched.warnings)

        if (
            args.auto_render
            and not args.render
            and not document.get("conteudo", {}).get("texto_plano")
        ):
            html, final_url, status_code, render_warnings = render_with_playwright(
                url,
                settings,
                respect_robots=not args.ignore_robots,
            )
            document = extract_document(
                html,
                final_url,
                requested_url=url,
                status_code=status_code,
                encoding="utf-8",
                max_links=settings.max_links,
                max_images=settings.max_images,
            )
            document["extracao"]["metodo_aquisicao"] = "playwright_auto_render"
            document["extracao"]["observacoes"].extend(render_warnings)

        add_extraction_timestamp(document)
        title = document.get("metadata", {}).get("titulo")
        filename = args.output or sanitize_filename(title)
        filename = sanitize_filename(filename[:-5] if filename.lower().endswith(".json") else filename)
        output_path = settings.output_dir / filename
        written = write_json(document, output_path)

        print(f"OK: conteúdo extraído e salvo em: {written}")
        print(f"Método: {document['extracao'].get('metodo')} / {document['extracao'].get('metodo_aquisicao')}")
        print(f"Caracteres: {document['extracao']['estatisticas']['caracteres']}")
        print(f"Seções: {document['extracao']['estatisticas']['secoes']}")
        return 0

    except (FetchError, RuntimeError, ValueError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"ERRO inesperado: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        print("\nDetalhes técnicos:", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
