from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from time import perf_counter
from urllib import robotparser
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from requests import Response

from config import Settings


class FetchError(RuntimeError):
    """Erro recuperável relacionado ao acesso ou validação da página."""


class RobotsDenied(FetchError):
    """O robots.txt proibiu explicitamente o acesso solicitado."""


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    final_url: str
    status_code: int
    content: bytes
    encoding: str | None
    content_type: str | None
    redirects: int
    elapsed_seconds: float
    warnings: tuple[str, ...] = ()


def validate_url(url: str) -> str:
    """Valida e normaliza uma URL HTTP(S)."""
    candidate = (url or "").strip()
    if not candidate:
        raise FetchError("A URL não foi informada.")

    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise FetchError("A URL deve usar http:// ou https://.")
    if not parsed.hostname:
        raise FetchError("A URL não contém um domínio válido.")
    if parsed.username or parsed.password:
        raise FetchError("URLs com usuário ou senha embutidos não são aceitas.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise FetchError("A URL contém uma porta inválida.") from exc
    if port is not None and not (1 <= port <= 65535):
        raise FetchError("A URL contém uma porta inválida.")

    return urlunparse(parsed._replace(fragment=""))


def _host_is_public(url: str) -> bool:
    """Bloqueia destinos não globais para reduzir risco de SSRF no modo padrão."""
    hostname = urlparse(url).hostname
    if not hostname:
        return False

    try:
        literal = ipaddress.ip_address(hostname)
        return literal.is_global
    except ValueError:
        pass

    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(
                hostname,
                None,
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise FetchError(f"Não foi possível resolver o domínio '{hostname}'.") from exc

    if not addresses:
        return False

    return all(ipaddress.ip_address(address).is_global for address in addresses)


def validate_host_safety(url: str, *, allow_private_hosts: bool = False) -> None:
    """Aplica a proteção de destino privado antes de uma requisição."""
    if allow_private_hosts:
        return
    if not _host_is_public(url):
        raise FetchError(
            "O destino resolve para um endereço não público. "
            "Use --allow-private-hosts somente quando esse acesso for intencional."
        )


def _robots_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/robots.txt"


def _check_content_limit(response: Response, max_bytes: int) -> None:
    raw_length = response.headers.get("Content-Length")
    if not raw_length:
        return
    try:
        content_length = int(raw_length)
    except ValueError:
        return
    if content_length > max_bytes:
        raise FetchError(
            f"A resposta excede o limite configurado de {max_bytes:,} bytes."
        )


def _read_limited(response: Response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise FetchError(
                    f"A resposta excedeu o limite de {max_bytes:,} bytes durante o download."
                )
            chunks.append(chunk)
    finally:
        response.close()
    return b"".join(chunks)


def check_robots(
    session: requests.Session,
    url: str,
    user_agent: str,
    timeout: tuple[float, float],
    *,
    max_bytes: int = 512 * 1024,
) -> tuple[bool, str | None]:
    """Consulta robots.txt; indisponibilidade gera aviso e não bloqueia."""
    robots_url = _robots_url(url)
    parser = robotparser.RobotFileParser()
    parser.set_url(robots_url)

    try:
        response = session.get(
            robots_url,
            headers={"User-Agent": user_agent},
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
    except requests.RequestException as exc:
        return True, f"Não foi possível consultar robots.txt ({exc.__class__.__name__}); prosseguindo."

    try:
        if response.status_code == 404:
            return True, None
        if response.status_code >= 400:
            return True, (
                f"robots.txt respondeu HTTP {response.status_code}; "
                "prosseguindo sem decisão do arquivo."
            )
        raw = _read_limited(response, max_bytes)
    except FetchError as exc:
        return True, f"Não foi possível ler robots.txt ({exc}); prosseguindo."

    parser.parse(raw.decode("utf-8", errors="replace").splitlines())
    if parser.can_fetch(user_agent, url):
        return True, None
    return False, f"O robots.txt não permite acesso ao caminho solicitado: {url}"


def fetch_page(
    url: str,
    settings: Settings,
    *,
    respect_robots: bool = True,
) -> FetchedPage:
    """Baixa uma página HTML com limites, redirecionamentos e proteção SSRF."""
    requested_url = validate_url(url)
    validate_host_safety(requested_url, allow_private_hosts=settings.allow_private_hosts)

    session = requests.Session()
    session.trust_env = False
    headers = {
        "User-Agent": settings.user_agent,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
        "Connection": "close",
    }

    warnings: list[str] = []
    current_url = requested_url
    redirect_count = 0
    started = perf_counter()

    try:
        while True:
            validate_host_safety(
                current_url,
                allow_private_hosts=settings.allow_private_hosts,
            )

            if respect_robots:
                allowed, warning = check_robots(
                    session,
                    current_url,
                    settings.user_agent,
                    settings.request_timeout,
                    max_bytes=settings.max_robots_bytes,
                )
                if warning:
                    warnings.append(warning)
                if not allowed:
                    raise RobotsDenied(warning or "Acesso bloqueado por robots.txt.")

            try:
                response = session.get(
                    current_url,
                    headers=headers,
                    timeout=settings.request_timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.Timeout as exc:
                raise FetchError("Tempo limite excedido ao acessar a página.") from exc
            except requests.ConnectionError as exc:
                raise FetchError("Falha de conexão ou DNS ao acessar a página.") from exc
            except requests.RequestException as exc:
                raise FetchError(f"Falha HTTP ao acessar a página: {exc}") from exc

            if 300 <= response.status_code < 400 and response.headers.get("Location"):
                try:
                    if redirect_count >= settings.max_redirects:
                        raise FetchError(
                            f"A página excedeu o limite de {settings.max_redirects} redirecionamentos."
                        )
                    next_url = validate_url(urljoin(current_url, response.headers["Location"]))
                finally:
                    response.close()
                current_url = next_url
                redirect_count += 1
                continue

            try:
                if response.status_code >= 400:
                    status = response.status_code
                    raise FetchError(f"O servidor respondeu com HTTP {status}.")

                _check_content_limit(response, settings.max_response_bytes)
                content_type = response.headers.get("Content-Type")
                if content_type:
                    media_type = content_type.split(";", 1)[0].strip().lower()
                    if media_type not in {"text/html", "application/xhtml+xml"}:
                        raise FetchError(
                            "O recurso acessado não parece ser HTML "
                            f"(Content-Type: {media_type})."
                        )

                content = _read_limited(response, settings.max_response_bytes)
                if not content:
                    raise FetchError("A página retornou um corpo vazio.")

                encoding = response.encoding or getattr(response, "apparent_encoding", None)
                return FetchedPage(
                    requested_url=requested_url,
                    final_url=current_url,
                    status_code=response.status_code,
                    content=content,
                    encoding=encoding,
                    content_type=content_type,
                    redirects=redirect_count,
                    elapsed_seconds=perf_counter() - started,
                    warnings=tuple(warnings),
                )
            except FetchError:
                raise
            except Exception as exc:
                raise FetchError(f"Falha ao ler a resposta HTML: {exc}") from exc
            finally:
                response.close()
    finally:
        session.close()
