from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from cleaner import sanitize_filename
from config import Settings
from extractor import extract_document, render_with_playwright
from fetcher import FetchError, fetch_page, validate_url
from json_exporter import write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extrai o conteúdo principal de uma página web e gera JSON pronto para RAG."
    )
    parser.add_argument("url", nargs="?", help="URL HTTP(S) da página a processar.")
    parser.add_argument("-o", "--output", help="Nome do JSON de saída.")
    parser.add_argument("--output-dir", default="output", help="Diretório de saída. Padrão: output")
    parser.add_argument("--timeout", type=float, default=30.0, help="Timeout de leitura em segundos.")
    parser.add_argument("--max-bytes", type=int, default=15 * 1024 * 1024, help="Limite do HTML em bytes.")
    parser.add_argument("--max-redirects", type=int, default=5, help="Máximo de redirecionamentos.")
    parser.add_argument("--max-links", type=int, default=200, help="Máximo de links preservados.")
    parser.add_argument("--max-images", type=int, default=100, help="Máximo de imagens preservadas.")
    parser.add_argument("--ignore-robots", action="store_true", help="Não consultar robots.txt.")
    parser.add_argument("--allow-private-hosts", action="store_true", help="Permite destinos não públicos; use somente em ambiente autorizado.")
    parser.add_argument("--render", action="store_true", help="Renderiza a página com Playwright.")
    parser.add_argument("--auto-render", action="store_true", help="Usa Playwright quando o texto normal vier vazio.")
    return parser


def get_url_argument_or_input(value: str | None) -> str:
    if value and value.strip():
        return value.strip()
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
    if args.max_links < 0 or args.max_images < 0:
        raise ValueError("--max-links e --max-images não podem ser negativos.")

    return Settings(
        connect_timeout=min(args.timeout, 15.0),
        read_timeout=args.timeout,
        max_response_bytes=args.max_bytes,
        max_redirects=args.max_redirects,
        max_links=args.max_links,
        max_images=args.max_images,
        output_dir=Path(args.output_dir),
        allow_private_hosts=args.allow_private_hosts,
    )


def finalize_document(document: dict, *, acquisition_method: str, warnings: tuple[str, ...] = ()) -> dict:
    document["metadata"]["data_extracao"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    document["extracao"]["metodo_aquisicao"] = acquisition_method
    if warnings:
        document["extracao"]["observacoes"].extend(warnings)

    plain_text = document.get("conteudo", {}).get("texto_plano", "")
    document["extracao"]["estatisticas"] = {
        "caracteres": len(plain_text),
        "palavras": len(plain_text.split()) if plain_text else 0,
        "secoes": len(document.get("conteudo", {}).get("secoes", [])),
        "links": len(document.get("recursos", {}).get("links", [])),
        "imagens": len(document.get("recursos", {}).get("imagens", [])),
    }
    return document


def _output_path(settings: Settings, requested: str | None, title: str | None) -> Path:
    filename = requested or sanitize_filename(title)
    filename = filename[:-5] if filename.lower().endswith(".json") else filename
    return settings.output_dir / sanitize_filename(filename)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = build_settings(args)
        requested_url = validate_url(get_url_argument_or_input(args.url))

        if args.render:
            html, final_url, status_code, warnings = render_with_playwright(
                requested_url,
                settings,
                respect_robots=not args.ignore_robots,
            )
            document = extract_document(
                html,
                final_url,
                requested_url=requested_url,
                status_code=status_code,
                encoding="utf-8",
                max_links=settings.max_links,
                max_images=settings.max_images,
                max_text_chars=settings.max_text_chars,
            )
            acquisition = "playwright"
        else:
            fetched = fetch_page(requested_url, settings, respect_robots=not args.ignore_robots)
            document = extract_document(
                fetched.content,
                fetched.final_url,
                requested_url=fetched.requested_url,
                status_code=fetched.status_code,
                encoding=fetched.encoding,
                max_links=settings.max_links,
                max_images=settings.max_images,
                max_text_chars=settings.max_text_chars,
            )
            document["extracao"]["redirecionamentos"] = fetched.redirects
            document["extracao"]["tempo_download_s"] = round(fetched.elapsed_seconds, 3)
            acquisition = "requests"
            warnings = fetched.warnings

        if (
            args.auto_render
            and not args.render
            and not document.get("conteudo", {}).get("texto_plano")
        ):
            html, final_url, status_code, render_warnings = render_with_playwright(
                requested_url,
                settings,
                respect_robots=not args.ignore_robots,
            )
            document = extract_document(
                html,
                final_url,
                requested_url=requested_url,
                status_code=status_code,
                encoding="utf-8",
                max_links=settings.max_links,
                max_images=settings.max_images,
                max_text_chars=settings.max_text_chars,
            )
            document["extracao"]["observacoes"].append("Playwright foi usado porque a extração HTTP não encontrou conteúdo textual.")
            acquisition = "playwright_auto_render"
            warnings = render_warnings

        document = finalize_document(document, acquisition_method=acquisition, warnings=warnings)
        output_path = _output_path(
            settings,
            args.output,
            document.get("metadata", {}).get("titulo"),
        )
        written = write_json(document, output_path)

        stats = document["extracao"]["estatisticas"]
        quality = document["extracao"]["qualidade"]
        print(f"OK: {written}")
        print(f"Método: {document['extracao']['metodo']} / {document['extracao']['metodo_aquisicao']}")
        print(f"Caracteres: {stats['caracteres']} | Palavras: {stats['palavras']} | Seções: {stats['secoes']}")
        print(f"Qualidade técnica: {quality['pontuacao']}/100 ({quality['nivel']})")
        return 0

    except (FetchError, RuntimeError, ValueError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
