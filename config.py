from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    max_response_bytes: int = 15 * 1024 * 1024
    max_redirects: int = 5
    max_links: int = 200
    max_images: int = 100
    max_robots_bytes: int = 512 * 1024
    max_text_chars: int = 2_000_000
    user_agent: str = "WebTextExtractor/2.0"
    output_dir: Path = Path("output")
    allow_private_hosts: bool = False

    @property
    def request_timeout(self) -> tuple[float, float]:
        return self.connect_timeout, self.read_timeout
