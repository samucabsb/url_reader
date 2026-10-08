from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import extractor
from cleaner import deduplicate_strings, normalize_text, sanitize_filename
from extractor import extract_document, extract_resources, remove_boilerplate
from fetcher import FetchError, validate_url
from json_exporter import write_json
from bs4 import BeautifulSoup


HTML_FIXTURE = """
<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <title>Minha Página de Teste</title>
  <meta name="description" content="Descrição de teste.">
  <meta name="author" content="João da Silva">
  <meta property="og:site_name" content="Exemplo">
  <meta name="keywords" content="teste, conteúdo">
  <link rel="canonical" href="https://example.com/final">
  <script type="application/ld+json">[null, {"headline": "Conteúdo principal", "articleSection": ["Tecnologia"], "keywords": ["IA"]}]</script>
</head>
<body>
  <nav><a href="/menu">Menu</a></nav>
  <main>
    <article>
      <h1>Conteúdo principal</h1>
      <p>Primeiro parágrafo com acentuação: ação, informação e coração.</p>
      <p>Segundo parágrafo. Este texto aparece uma vez.</p>
      <div class="ad-banner">COMPRE AGORA</div>
      <h2>Seção A</h2>
      <p>Texto da seção A.</p>
      <p>Texto da seção A.</p>
      <ul><li>Item um</li><li>Item dois</li></ul>
      <table>
        <tr><th>Nome</th><th>Valor</th></tr>
        <tr><td>A</td><td>10</td></tr>
      </table>
      <a href="#">Continuar lendo</a>
      <a href="/relacionado">Conteúdo relacionado</a>
      <img src="https://secure.gravatar.com/avatar/x?s=1" alt="Autor">
      <figure><img src="/foto.jpg" alt="Foto"><figcaption>Legenda da foto</figcaption></figure>
    </article>
  </main>
  <footer>Rodapé repetitivo</footer>
  <script>console.log('não deve aparecer');</script>
  <style>.x { display:none; }</style>
</body>
</html>
""".encode("utf-8")


TRAFILATURA_XML = """
<doc>
  <head rend="h1">Conteúdo principal</head>
  <p>Primeiro parágrafo com acentuação: ação, informação e coração.</p>
  <p>Segundo parágrafo. Este texto aparece uma vez.</p>
  <head rend="h2">Seção A</head>
  <p>Texto da seção A.</p>
  <list><item>Item um</item><item>Item dois</item></list>
</doc>
"""


def test_validate_url_accepts_http_https_and_removes_fragment():
    assert validate_url("https://example.com/a#frag") == "https://example.com/a"
    assert validate_url("http://example.com") == "http://example.com"


def test_validate_url_rejects_invalid_inputs():
    for value in ["", "ftp://example.com", "https://user:secret@example.com", "https://example.com:99999"]:
        with pytest.raises(FetchError):
            validate_url(value)


def test_normalization_and_filename_safety():
    assert normalize_text(" ação\r\n  e  informação \xa0 ") == "ação\ne informação"
    assert deduplicate_strings(["A", " A ", "B", "B"]) == ["A", "B"]
    filename = sanitize_filename('Título: teste / inválido?.json'.removesuffix('.json'))
    assert filename.endswith(".json")
    assert "/" not in filename and "?" not in filename


def test_decomposed_children_do_not_crash():
    soup = BeautifulSoup("<main><div class='ad'><span>ruído</span></div><p>Texto</p></main>", "html.parser")
    remove_boilerplate(soup.main)
    assert "Texto" in soup.main.get_text(" ", strip=True)


def test_extract_resources_filters_noise_and_preserves_caption():
    soup = BeautifulSoup(HTML_FIXTURE, "html.parser")
    main = soup.main
    links, images = extract_resources(main, "https://example.com/final", 20, 20)
    assert links == [{"texto": "Conteúdo relacionado", "url": "https://example.com/relacionado"}]
    assert images == [{
        "url": "https://example.com/foto.jpg",
        "alt": "Foto",
        "legenda": "Legenda da foto",
    }]


def test_extracts_metadata_and_structure_with_structured_extractor(monkeypatch):
    fake_trafilatura = SimpleNamespace(extract=lambda *args, **kwargs: TRAFILATURA_XML)
    monkeypatch.setattr(extractor, "trafilatura", fake_trafilatura)

    result = extract_document(
        HTML_FIXTURE,
        "https://example.com/final",
        requested_url="https://example.com/original",
        status_code=200,
        encoding="UTF-8",
    )

    assert result["schema_version"] == "1.0"
    assert result["metadata"]["titulo"] == "Conteúdo principal"
    assert result["metadata"]["descricao"] == "Descrição de teste."
    assert result["metadata"]["autor"] == "João da Silva"
    assert result["metadata"]["url_canonica"] == "https://example.com/final"
    assert result["metadata"]["categorias"] == ["Tecnologia"]
    assert result["metadata"]["palavras_chave"] == ["teste", "conteúdo", "IA"]
    assert "ação" in result["conteudo"]["texto_plano"]
    assert "COMPRE AGORA" not in result["conteudo"]["texto_plano"]
    assert result["extracao"]["metodo"] == "trafilatura_xml"
    assert result["conteudo"]["secoes"][0]["nivel"] is None
    assert result["recursos"]["links"] == [{"texto": "Conteúdo relacionado", "url": "https://example.com/relacionado"}]


def test_fallback_works_without_trafilatura(monkeypatch):
    monkeypatch.setattr(extractor, "trafilatura", None)
    result = extract_document(
        HTML_FIXTURE,
        "https://example.com/final",
        requested_url="https://example.com/final",
        status_code=200,
        encoding="UTF-8",
    )
    text = result["conteudo"]["texto_plano"]
    assert result["extracao"]["metodo"] == "beautifulsoup_fallback"
    assert "Primeiro parágrafo" in text
    assert "Seção A" in text
    assert "Rodapé repetitivo" not in text


def test_titleless_first_section_has_nullable_level(monkeypatch):
    fake_trafilatura = SimpleNamespace(extract=lambda *args, **kwargs: "<doc><p>Texto inicial.</p><head rend='h3'>Seção</head><p>Corpo.</p></doc>")
    monkeypatch.setattr(extractor, "trafilatura", fake_trafilatura)
    result = extract_document(
        HTML_FIXTURE,
        "https://example.com/final",
        requested_url="https://example.com/final",
        status_code=200,
        encoding="UTF-8",
    )
    assert result["conteudo"]["secoes"][0]["nivel"] is None
    assert result["conteudo"]["secoes"][1]["nivel"] == 3


def test_page_without_main_content(monkeypatch):
    monkeypatch.setattr(extractor, "trafilatura", None)
    html = b"<html><head><title>Vazia</title></head><body><script>1</script><style>x{}</style></body></html>"
    result = extract_document(
        html,
        "https://example.com/vazia",
        requested_url="https://example.com/vazia",
        status_code=200,
        encoding="UTF-8",
    )
    assert result["extracao"]["conteudo_encontrado"] is False
    assert result["extracao"]["observacoes"]
    assert result["extracao"]["qualidade"]["nivel"] == "baixo"


def test_write_json_round_trip(tmp_path: Path):
    data = {"texto": "Ação e informação", "ok": True}
    target = tmp_path / "resultado.json"
    written = write_json(data, target)
    loaded = json.loads(written.read_text(encoding="utf-8"))
    assert loaded == data


def test_cli_module_compiles_and_json_is_utf8(tmp_path: Path):
    target = tmp_path / "saida.json"
    write_json({"mensagem": "Olá, mundo!"}, target)
    raw = target.read_bytes().decode("utf-8")
    assert "Olá" in raw
    assert "\n  \"mensagem\"" in raw
