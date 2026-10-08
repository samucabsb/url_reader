# Web Text Extractor — RAG Ready

CLI em Python para transformar uma página HTTP(S) em JSON UTF-8 estruturado, com foco em ingestão posterior por um pipeline de RAG.

## O que entra no JSON

`metadata`: URL original/final, URL canônica quando disponível, título, descrição, autor, data, idioma, site, categorias e palavras-chave.

`conteudo.secoes`: estrutura semântica preservando títulos, parágrafos, citações, listas e tabelas.

`conteudo.texto_plano`: representação linear para pipelines que trabalham diretamente com texto.

`recursos`: somente links e imagens relevantes encontrados no conteúdo principal; fragmentos da própria página, links sem texto e imagens de avatar/tracking são descartados.

`extracao`: método usado, aquisição, status HTTP, hashes, estatísticas, avisos e um indicador técnico de qualidade.

O campo `document_id` é estável para a URL final. `hash_conteudo` muda quando o texto extraído muda, ajudando a deduplicar documentos no índice.

## Instalação

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Testes:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Renderização JavaScript:

```bash
python -m pip install -r requirements-render.txt
playwright install chromium
```

## Uso

```bash
python scraper.py "https://example.com"
```

Modo interativo:

```bash
python scraper.py
```

Renderização direta:

```bash
python scraper.py "https://example.com/app" --render
```

Fallback automático para páginas cujo HTML inicial não contém conteúdo textual:

```bash
python scraper.py "https://example.com/app" --auto-render
```

Arquivo de saída:

```text
output/<titulo-sanitizado>.json
```

## Segurança

O acesso remoto respeita `robots.txt` por padrão, limita tamanho da resposta e redirecionamentos, rejeita credenciais embutidas na URL e bloqueia por padrão destinos que resolvem para endereços não públicos, reduzindo risco de SSRF.

`--allow-private-hosts` existe apenas para ambientes autorizados que realmente precisam acessar recursos internos.

A ferramenta não contorna CAPTCHA, autenticação, paywall ou mecanismos equivalentes.

## Integração com RAG

Para ingestão, use `conteudo.secoes` como fonte semântica e mantenha `metadata`, `document_id` e `extracao.hash_conteudo` junto de cada chunk. `conteudo.texto_plano` é o fallback simples.

O contrato de saída está em `schema.json` e usa `schema_version: "1.0"`. O extrator não cria embeddings, chunks nem armazenamento vetorial: essas decisões pertencem ao pipeline RAG que consumirá este JSON.

Não há chunking, embeddings ou armazenamento vetorial neste projeto de propósito: essas etapas dependem do seu modelo, tamanho de contexto, estratégia de recuperação e banco vetorial.

## Estrutura

```text
web_text_extractor_rag_ready/
├── cleaner.py
├── config.py
├── extractor.py
├── fetcher.py
├── json_exporter.py
├── scraper.py
├── schema.json
├── requirements.txt
├── requirements-dev.txt
├── requirements-render.txt
├── README.md
├── .gitignore
└── tests/
    └── test_scraper.py
```

## Exemplo de comando usado no projeto

```bash
python scraper.py "https://jornaldebrasilia.com.br/brasilia/hran-da-assistencia-a-vitimas-de-animais-peconhentos/"
```

O artigo contém título, subtítulo, autoria, data, parágrafos e uma lista de soros; a página pública também expõe ruído de navegação e avatar, exatamente o tipo de conteúdo que o pipeline procura remover. 

## Validação

A versão empacotada foi verificada com 11 testes automatizados, compilação de todos os módulos Python, fluxo HTTP local ponta a ponta e validação do JSON contra `schema.json`. A integração real com Trafilatura depende da instalação da dependência; quando ela não está disponível, o sistema usa o fallback nativo em BeautifulSoup.
