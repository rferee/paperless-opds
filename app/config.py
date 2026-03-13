import json
import os
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings


def _load_config_json() -> dict[str, Any]:
    """Load config.json and flatten the nested paperless structure."""
    config_path = os.environ.get("CONFIG_PATH", "config.json")
    path = Path(config_path)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    flat: dict[str, Any] = {}
    paperless = data.get("paperless", {})
    if "url" in paperless:
        flat["paperless_url"] = paperless["url"]
    if "api_key" in paperless:
        flat["paperless_api_key"] = paperless["api_key"]
    return flat


class Settings(BaseSettings):
    paperless_url: str = ""
    paperless_api_key: str = ""
    opds_title: str = "Paperless-OPDS"
    opds_page_size: int = 25
    cache_ttl: int = 300
    opds_username: str | None = None
    opds_password: str | None = None

    # Thumbnail / preview cache
    thumbnail_dir: str = "/data/thumbnails"
    thumb_max_size: int = 300
    preview_max_size: int = 1000
    thumbnail_quality: int = 85
    thumbnail_batch_size: int = 50
    thumbnail_max_retries: int = 3
    thumbnail_pregenerate: bool = True

    # OPDS content
    opds_include_content: bool = False

    def model_post_init(self, __context: Any) -> None:
        json_values = _load_config_json()
        for field, value in json_values.items():
            if not getattr(self, field):
                object.__setattr__(self, field, value)

        if not self.paperless_url:
            raise ValueError("paperless_url is required (set PAPERLESS_URL or provide config.json)")
        if not self.paperless_api_key:
            raise ValueError("paperless_api_key is required (set PAPERLESS_API_KEY or provide config.json)")

        # Normalize URL: strip trailing slash
        object.__setattr__(self, "paperless_url", self.paperless_url.rstrip("/"))

        # Create thumbnail cache directories
        thumb_root = Path(self.thumbnail_dir)
        (thumb_root / "thumb").mkdir(parents=True, exist_ok=True)
        (thumb_root / "preview").mkdir(parents=True, exist_ok=True)


settings = Settings()
