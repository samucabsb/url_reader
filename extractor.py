from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from cleaner import deduplicate_strings, normalize_text, text_key

try:
    import trafilatura
except ImportError:  # fallback local: BeautifulSoup continua disponível
    trafilatura = None  # type: ignore[assignment]


BOILERPLATE_TAGS = {
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "object", "embed", "form", "nav", "aside", "footer",
}

BOILERPLATE_HINTS = {
    "ad", "ads", "advert", "advertisement", "banner", "breadcrumb", "cookie",
    "consent", "dialog", "drawer", "menu", "modal", "newsletter", "pagination",
    "popup", "promo", "search", "share", "sidebar", "social", "sponsor",
    "subscribe", "toolbar",
}

SEMANTIC_ROOT_SELECTORS = [
    "main", "article", '[role="main"]', '[itemprop="articleBody"]',
    ".article-body", ".article-content", ".post-content", ".entry-content",
    ".post-body", ".content-body", "#article-body", "#article-content", "#main-content",
]

HEADING_TAGS = {f"h{i}" for i in range(1, 7)}
BLOCK_CONTAINER_TAGS = {
    "html", "body", "main", "article", "section", "div", "header", "figure", "figcaption",
}

IRRELEVANT_IMAGE_TOKENS = {
    "avatar", "gravatar", "tracking", "tracker", "pixel", "spacer", "placeholder",
}


@dataclass(frozen=True)
class Block:
    tipo: str
    texto: str | None = None
    nivel: int | None = None
    itens: tuple[str, ...] = ()
    cabecalho: tuple[str, ...] = ()
    linhas: tuple[tuple[str, ...], ...] = ()


def _class_tokens(tag: Tag | None) -> set[str]:
    """Lê classes/IDs sem quebrar em elementos já decompostos."""
    if tag is None:
        return set()
    attrs = getattr(tag, "attrs", None)
    if not isinstance(attrs, dict):
        return set()

    raw: list[str] = []
    classes = attrs.get("class") or []
    if isinstance(classes, str):
        raw.append(classes)
    else:
        raw.extend(str(value) for value in classes)

    element_id = attrs.get("id")
    if element_id:
        raw.append(str(element_id))

    tokens: set[str] = set()
    for value in raw:
        tokens.update(token for token in re.split(r"[^a-z0-9]+", value.lower()) if token)
    return tokens


def _inside_semantic_content(tag: Tag) -> bool:
    return bool(tag.find_parent(["main", "article"]))


def remove_boilerplate(root: Tag) -> None:
    """Remove ruído estrutural sem invalidar a lista de elementos em iteração."""
    for tag in list(root.find_all(BOILERPLATE_TAGS)):
        if getattr(tag, "attrs", None) is not None:
            tag.decompose()

    for tag in list(root.find_all(["header", "footer"])):
        if getattr(tag, "attrs", None) is None:
            continue
        if tag.name == "header" and _inside_semantic_content(tag):
            continue
        tag.decompose()

    # Decompor um ancestral invalida seus descendentes. O guard abaixo é intencional.
    for tag in list(root.find_all(True)):
        if getattr(tag, "attrs", None) is None:
            continue
        if tag.name in {"body", "html", "main", "article", "section"}:
            continue
        if _class_tokens(tag).intersection(BOILERPLATE_HINTS):
            tag.decompose()


def _candidate_score(tag: Tag) -> float:
    text = normalize_text(tag.get_text(" ", strip=True))
    if len(text) < 80:
        return 0.0

    link_text = normalize_text(" ".join(a.get_text(" ", strip=True) for a in tag.find_all("a")))
    link_ratio = len(link_text) / max(len(text), 1)
    score = len(text) * (1.0 - min(link_ratio, 0.85))

    if tag.name == "main":
        score += 2500
    elif tag.name == "article":
        score += 2200
    elif tag.get("role") == "main":
        score += 2000
    if tag.get("itemprop") == "articleBody":
        score += 1800

    score += 700 * len(_class_tokens(tag).intersection({"article", "content", "body", "post", "entry"}))
    return score


def select_main_root(soup: BeautifulSoup) -> Tag:
    candidates: list[Tag] = []
    seen_ids: set[int] = set()

    for selector in SEMANTIC_ROOT_SELECTORS:
        try:
            found = soup.select(selector)
        except Exception:
            found = []
        for tag in found:
            if id(tag) not in seen_ids:
                candidates.append(tag)
                seen_ids.add(id(tag))

    for tag in soup.find_all("div"):
        if _class_tokens(tag).intersection({"content", "article", "post", "entry", "main"}):
            if id(tag) not in seen_ids:
                candidates.append(tag)
                seen_ids.add(id(tag))

    best = max(candidates, key=_candidate_score, default=None)
    if best is not None and _candidate_score(best) > 100:
        return best
    return soup.body or soup


def _text_of(tag: Tag) -> str:
    return normalize_text(tag.get_text(" ", strip=True))


def _extract_table(tag: Tag) -> Block | None:
    rows: list[tuple[str, ...]] = []
    header: tuple[str, ...] = ()
    has_header = False

    for tr in tag.find_all("tr"):
        cells = [normalize_text(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"], recursive=False)]
        cells = [cell for cell in cells if cell]
        if not cells:
            continue
        rows.append(tuple(cells))
        if tr.find("th", recursive=False) and not has_header:
            header = tuple(cells)
            has_header = True

    if not rows:
        return None
    data_rows = rows[1:] if has_header and len(rows) > 1 else rows if not has_header else ()
    return Block(tipo="tabela", cabecalho=header, linhas=tuple(data_rows))


def _extract_html_blocks(node: Tag) -> list[Block]:
    blocks: list[Block] = []

    def visit(parent: Tag) -> None:
        for child in parent.children:
            if not isinstance(child, Tag):
                continue
            name = child.name.lower()

            if name in HEADING_TAGS:
                text = _text_of(child)
                if text:
                    blocks.append(Block(tipo="titulo", texto=text, nivel=int(name[1])))
                continue

            if name == "p":
                text = _text_of(child)
                if text:
                    blocks.append(Block(tipo="paragrafo", texto=text))
                continue

            if name == "blockquote":
                text = _text_of(child)
                if text:
                    blocks.append(Block(tipo="citacao", texto=text))
                continue

            if name in {"ul", "ol"}:
                items = tuple(text for text in (_text_of(li) for li in child.find_all("li", recursive=False)) if text)
                if items:
                    blocks.append(Block(tipo="lista", itens=items))
                continue

            if name == "table":
                table_block = _extract_table(child)
                if table_block:
                    blocks.append(table_block)
                continue

            if name in {"br", "hr"}:
                continue

            if name in BLOCK_CONTAINER_TAGS or child.find(["p", *HEADING_TAGS, "blockquote", "ul", "ol", "table"]):
                visit(child)
                continue

            if not child.find(["p", *HEADING_TAGS, "blockquote", "ul", "ol", "table"]):
                text = _text_of(child)
                if len(text) >= 40:
                    blocks.append(Block(tipo="paragrafo", texto=text))

    visit(node)
    return _dedupe_blocks(blocks)


def _dedupe_blocks(blocks: Iterable[Block]) -> list[Block]:
    result: list[Block] = []
    seen: set[str] = set()

    for block in blocks:
        comparable: str | None = block.texto
        if block.tipo == "lista":
            comparable = "\n".join(block.itens)
        elif block.tipo == "tabela":
            comparable = "\n".join(" | ".join(row) for row in block.linhas)
            comparable += "\n" + " | ".join(block.cabecalho)

        if comparable:
            key = f"{block.tipo}:{text_key(comparable)}"
            if key in seen:
                continue
            seen.add(key)
        result.append(block)
    return result


def _iter_jsonld_values(value: object) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _iter_jsonld_values(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_jsonld_values(item)


def _first_jsonld_value(soup: BeautifulSoup, field_names: set[str]) -> object | None:
    scripts = soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)})
    for script in scripts:
        raw = script.string or script.get_text()
        raw = raw.strip() if raw else ""
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        for item in _iter_jsonld_values(data):
            for field in field_names:
                candidate = item.get(field)
                if candidate is not None and candidate != "":
                    return candidate
    return None


def _normalize_author(value: object | None) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        return normalize_text(value) or None
    if isinstance(value, dict):
        return normalize_text(str(value.get("name") or value.get("alternateName") or "")) or None
    if isinstance(value, list):
        names = [_normalize_author(item) for item in value]
        return ", ".join(deduplicate_strings(name for name in names if name)) or None
    return None


def _normalize_keywords(value: object | None) -> list[str]:
    if not value:
        return []
    raw = value if isinstance(value, list) else re.split(r"[,;]", str(value))
    return deduplicate_strings(str(item) for item in raw)


def _meta_content(soup: BeautifulSoup, *selectors: tuple[str, dict[str, str]]) -> str | None:
    for tag_name, attrs in selectors:
        tag = soup.find(tag_name, attrs=attrs)
        if tag and tag.get("content"):
            return normalize_text(str(tag["content"])) or None
    return None


def extract_metadata(soup: BeautifulSoup, base_url: str) -> dict:
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else None
    title = title or _meta_content(
        soup,
        ("meta", {"property": "og:title"}),
        ("meta", {"name": "twitter:title"}),
    )

    description = _meta_content(
        soup,
        ("meta", {"name": "description"}),
        ("meta", {"property": "og:description"}),
        ("meta", {"name": "twitter:description"}),
    )

    author = _meta_content(
        soup,
        ("meta", {"name": "author"}),
        ("meta", {"property": "article:author"}),
    ) or _normalize_author(_first_jsonld_value(soup, {"author", "creator"}))

    publication_date = _meta_content(
        soup,
        ("meta", {"property": "article:published_time"}),
        ("meta", {"name": "datePublished"}),
        ("meta", {"itemprop": "datePublished"}),
    )
    publication_date = publication_date or normalize_text(
        str(_first_jsonld_value(soup, {"datePublished", "dateCreated"}) or "")
    ) or None
    if not publication_date:
        time_tag = soup.find("time", attrs={"datetime": True})
        publication_date = normalize_text(str(time_tag.get("datetime"))) if time_tag else None

    categories = _normalize_keywords(_meta_content(soup, ("meta", {"property": "article:section"})))
    keywords = _normalize_keywords(_meta_content(soup, ("meta", {"name": "keywords"})))
    categories = deduplicate_strings([
        *categories,
        *(_normalize_keywords(_first_jsonld_value(soup, {"articleSection"}))),
    ])
    keywords = deduplicate_strings([
        *keywords,
        *(_normalize_keywords(_first_jsonld_value(soup, {"keywords"}))),
    ])

    language = _meta_content(soup, ("meta", {"http-equiv": "content-language"}))
    if soup.html and soup.html.get("lang"):
        language = language or normalize_text(str(soup.html["lang"])) or None
    language = language or normalize_text(str(_first_jsonld_value(soup, {"inLanguage"}) or "")) or None

    if not title:
        jsonld_headline = _first_jsonld_value(soup, {"headline", "name"})
        if isinstance(jsonld_headline, str):
            title = normalize_text(jsonld_headline) or None

    canonical = None
    canonical_tag = soup.find("link", attrs={"rel": re.compile(r"canonical", re.I), "href": True})
    if canonical_tag:
        canonical = urljoin(base_url, str(canonical_tag["href"]).strip())
        canonical = urldefrag(canonical).url or None

    return {
        "titulo": title,
        "descricao": description,
        "autor": author,
        "data_publicacao": publication_date,
        "idioma": language,
        "categorias": categories,
        "palavras_chave": keywords,
        "url_canonica": canonical,
        "site_nome": _meta_content(soup, ("meta", {"property": "og:site_name"})),
    }


def _is_same_document(url: str, base_url: str) -> bool:
    return urldefrag(url).url == urldefrag(base_url).url


def _image_is_irrelevant(image: Tag, absolute_url: str) -> bool:
    parsed = urlparse(absolute_url)
    haystack = " ".join([
        parsed.netloc,
        parsed.path,
        " ".join(_class_tokens(image)),
    ]).lower()
    if any(token in haystack for token in IRRELEVANT_IMAGE_TOKENS):
        return True

    width = image.get("width")
    height = image.get("height")
    try:
        if width is not None and int(str(width)) <= 1:
            return True
        if height is not None and int(str(height)) <= 1:
            return True
    except (TypeError, ValueError):
        pass

    return False


def extract_resources(root: Tag, base_url: str, max_links: int, max_images: int) -> tuple[list[dict], list[dict]]:
    links: list[dict] = []
    seen_links: set[str] = set()
    for anchor in root.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href:
            continue
        parsed_href = urlparse(href)
        if parsed_href.scheme.lower() in {"javascript", "mailto", "tel", "data"}:
            continue

        absolute = urldefrag(urljoin(base_url, href)).url
        if not absolute or urlparse(absolute).scheme.lower() not in {"http", "https"}:
            continue
        if _is_same_document(absolute, base_url):
            continue
        if absolute in seen_links:
            continue

        text = _text_of(anchor) or normalize_text(str(anchor.get("title") or ""))
        if not text:
            continue

        seen_links.add(absolute)
        links.append({"texto": text, "url": absolute})
        if len(links) >= max_links:
            break

    images: list[dict] = []
    seen_images: set[str] = set()
    for image in root.find_all("img"):
        source = next(
            (
                str(image.get(name)).strip()
                for name in ("src", "data-src", "data-lazy-src", "data-original")
                if image.get(name)
            ),
            "",
        )
        if not source or source.lower().startswith("data:"):
            continue

        absolute = urljoin(base_url, source)
        if urlparse(absolute).scheme.lower() not in {"http", "https"}:
            continue
        if _image_is_irrelevant(image, absolute) or absolute in seen_images:
            continue

        alt = normalize_text(str(image.get("alt") or "")) or None
        caption = None
        figure = image.find_parent("figure")
        if figure:
            caption_tag = figure.find("figcaption")
            caption = _text_of(caption_tag) if caption_tag else None

        seen_images.add(absolute)
        images.append({"url": absolute, "alt": alt, "legenda": caption})
        if len(images) >= max_images:
            break

    return links, images


def _trafilatura_blocks(html: bytes, url: str) -> list[Block]:
    if trafilatura is None:
        return []

    try:
        xml = trafilatura.extract(
            html,
            url=url,
            output_format="xml",
            include_comments=False,
            include_tables=True,
            include_images=False,
            include_links=False,
            deduplicate=True,
            with_metadata=True,
            favor_precision=False,
        )
    except (TypeError, ValueError, RuntimeError):
        return []

    if not xml:
        return []

    from xml.etree import ElementTree

    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []

    def local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    def text_content(element: ElementTree.Element) -> str:
        return normalize_text(" ".join(element.itertext()))

    blocks: list[Block] = []

    def walk(parent: ElementTree.Element) -> None:
        for child in list(parent):
            name = local_name(child.tag)
            if name == "head":
                text = text_content(child)
                rend = child.attrib.get("rend") or child.attrib.get("level") or ""
                match = re.search(r"([1-6])", rend)
                level = int(match.group(1)) if match else None
                if text:
                    blocks.append(Block(tipo="titulo", texto=text, nivel=level))
            elif name == "p":
                text = text_content(child)
                if text:
                    blocks.append(Block(tipo="paragrafo", texto=text))
            elif name in {"quote", "ab"}:
                text = text_content(child)
                if text:
                    blocks.append(Block(tipo="citacao", texto=text))
            elif name == "list":
                items = tuple(
                    text_content(item)
                    for item in child.iter()
                    if local_name(item.tag) == "item" and text_content(item)
                )
                if items:
                    blocks.append(Block(tipo="lista", itens=items))
            elif name == "table":
                rows: list[tuple[str, ...]] = []
                for row in child.iter():
                    if local_name(row.tag) != "row":
                        continue
                    cells = tuple(
                        text_content(cell)
                        for cell in list(row)
                        if local_name(cell.tag) == "cell" and text_content(cell)
                    )
                    if cells:
                        rows.append(cells)
                if rows:
                    blocks.append(Block(tipo="tabela", linhas=tuple(rows)))
            else:
                walk(child)

    walk(root)
    return _dedupe_blocks(blocks)


def _blocks_to_sections(blocks: list[Block], fallback_title: str | None) -> tuple[str | None, list[dict]]:
    title = None
    sections: list[dict] = []
    current: dict | None = None

    def ensure_current() -> dict:
        nonlocal current
        if current is None:
            current = {"titulo": None, "nivel": None, "conteudo": []}
        return current

    for block in blocks:
        if block.tipo == "titulo":
            heading = block.texto or ""
            if block.nivel == 1 and title is None:
                title = heading
                continue

            if current is not None and current["conteudo"]:
                sections.append(current)
            current = {"titulo": heading, "nivel": block.nivel, "conteudo": []}
            continue

        section = ensure_current()
        if block.tipo == "paragrafo" and block.texto:
            section["conteudo"].append({"tipo": "paragrafo", "texto": block.texto})
        elif block.tipo == "citacao" and block.texto:
            section["conteudo"].append({"tipo": "citacao", "texto": block.texto})
        elif block.tipo == "lista" and block.itens:
            section["conteudo"].append({"tipo": "lista", "itens": list(block.itens)})
        elif block.tipo == "tabela":
            section["conteudo"].append({
                "tipo": "tabela",
                "cabecalho": list(block.cabecalho),
                "linhas": [list(row) for row in block.linhas],
            })

    if current is not None and current["conteudo"]:
        sections.append(current)

    return title or fallback_title, sections


def _plain_text_from_sections(sections: list[dict], max_chars: int) -> str:
    chunks: list[str] = []
    for section in sections:
        if section.get("titulo"):
            chunks.append(section["titulo"])
        for block in section.get("conteudo", []):
            if block["tipo"] in {"paragrafo", "citacao"}:
                chunks.append(block["texto"])
            elif block["tipo"] == "lista":
                chunks.extend(block["itens"])
            elif block["tipo"] == "tabela":
                if block.get("cabecalho"):
                    chunks.append(" | ".join(block["cabecalho"]))
                chunks.extend(" | ".join(row) for row in block.get("linhas", []))

    text = "\n\n".join(deduplicate_strings(chunks))
    return text[:max_chars].rstrip()


def _quality_score(metadata: dict, plain_text: str, sections: list[dict], method: str) -> dict:
    score = 0
    factors: list[str] = []
    if plain_text:
        score += 40
        factors.append("conteudo_principal_encontrado")
    if len(plain_text) >= 500:
        score += 20
    elif len(plain_text) >= 150:
        score += 10
    if sections:
        score += 10
    if metadata.get("titulo"):
        score += 10
    if metadata.get("descricao") or metadata.get("autor") or metadata.get("data_publicacao"):
        score += 10
    if method == "trafilatura_xml":
        score += 10
        factors.append("extracao_estruturada_trafilatura")
    else:
        factors.append("fallback_beautifulsoup")

    score = min(score, 100)
    nivel = "alto" if score >= 80 else "medio" if score >= 55 else "baixo"
    return {"pontuacao": score, "nivel": nivel, "fatores": factors}


def extract_document(
    html: bytes | str,
    final_url: str,
    *,
    requested_url: str,
    status_code: int,
    encoding: str | None,
    max_links: int = 200,
    max_images: int = 100,
    max_text_chars: int = 2_000_000,
) -> dict:
    """Extrai uma página em formato estável para ingestão posterior em RAG."""
    raw_html = html.encode("utf-8") if isinstance(html, str) else html
    soup = BeautifulSoup(raw_html, "html.parser")
    metadata = extract_metadata(soup, final_url)

    main_root = select_main_root(soup)
    remove_boilerplate(main_root)
    fallback_blocks = _extract_html_blocks(main_root)
    main_blocks = _trafilatura_blocks(raw_html, final_url)

    method = "trafilatura_xml" if main_blocks else "beautifulsoup_fallback"
    blocks = main_blocks or fallback_blocks
    title, sections = _blocks_to_sections(blocks, metadata.get("titulo"))
    metadata["titulo"] = title or metadata.get("titulo")

    links, images = extract_resources(main_root, final_url, max_links, max_images)
    plain_text = _plain_text_from_sections(sections, max_text_chars)
    document_id = hashlib.sha256(final_url.encode("utf-8")).hexdigest()[:16]
    content_hash = hashlib.sha256(plain_text.encode("utf-8")).hexdigest()
    warning = "Nenhum conteúdo textual principal foi identificado na página." if not plain_text else None

    document = {
        "schema_version": "1.0",
        "document_id": document_id,
        "metadata": {
            "url": requested_url,
            "url_final": final_url,
            "url_canonica": metadata.get("url_canonica"),
            "titulo": metadata.get("titulo"),
            "descricao": metadata.get("descricao"),
            "autor": metadata.get("autor"),
            "data_publicacao": metadata.get("data_publicacao"),
            "idioma": metadata.get("idioma"),
            "site_nome": metadata.get("site_nome"),
            "categorias": metadata.get("categorias", []),
            "palavras_chave": metadata.get("palavras_chave", []),
            "encoding": encoding,
        },
        "conteudo": {
            "titulo_principal": metadata.get("titulo"),
            "secoes": sections,
            "texto_plano": plain_text,
        },
        "recursos": {
            "links": links,
            "imagens": images,
        },
        "extracao": {
            "metodo": method,
            "metodo_aquisicao": None,
            "conteudo_encontrado": bool(plain_text),
            "status_http": status_code,
            "hash_conteudo": content_hash,
            "observacoes": [warning] if warning else [],
            "qualidade": _quality_score(metadata, plain_text, sections, method),
        },
    }
    return document


def render_with_playwright(url: str, settings, *, respect_robots: bool = True) -> tuple[bytes, str, int, tuple[str, ...]]:
    """Renderiza páginas que dependem de JavaScript; Playwright é opcional."""
    clean_url = url
    warnings: list[str] = []

    if respect_robots:
        import requests
        from fetcher import check_robots, validate_host_safety, validate_url

        clean_url = validate_url(url)
        validate_host_safety(clean_url, allow_private_hosts=settings.allow_private_hosts)
        session = requests.Session()
        session.trust_env = False
        try:
            allowed, warning = check_robots(
                session,
                clean_url,
                settings.user_agent,
                settings.request_timeout,
                max_bytes=settings.max_robots_bytes,
            )
        finally:
            session.close()
        if warning:
            warnings.append(warning)
        if not allowed:
            raise RuntimeError(warning or "Acesso bloqueado por robots.txt.")
    else:
        from fetcher import validate_host_safety, validate_url
        clean_url = validate_url(url)
        validate_host_safety(clean_url, allow_private_hosts=settings.allow_private_hosts)

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "O modo de renderização requer Playwright. "
            "Instale com 'python -m pip install -r requirements-render.txt' "
            "e depois execute 'playwright install chromium'."
        ) from exc

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=settings.user_agent)

                def route_handler(route) -> None:
                    if route.request.resource_type in {"image", "media", "font"}:
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/*", route_handler)
                response = page.goto(
                    clean_url,
                    wait_until="domcontentloaded",
                    timeout=int(settings.read_timeout * 1000),
                )
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except PlaywrightTimeoutError:
                    pass

                html = page.content().encode("utf-8")
                if len(html) > settings.max_response_bytes:
                    raise RuntimeError(
                        f"O HTML renderizado excedeu o limite de {settings.max_response_bytes:,} bytes."
                    )

                final_url = page.url
                validate_host_safety(final_url, allow_private_hosts=settings.allow_private_hosts)
                status_code = response.status if response is not None else 200
                return html, final_url, status_code, tuple(warnings)
            finally:
                browser.close()
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("Tempo limite excedido durante a renderização JavaScript.") from exc
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"Falha durante a renderização JavaScript: {exc}") from exc
