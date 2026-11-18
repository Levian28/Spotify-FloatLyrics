from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from dotenv import load_dotenv

APP_NAME = "SpotifyFloatingLyrics"
PROJECT_DIR = Path(__file__).resolve().parent


def app_dir() -> Path:
    """Folder data pengguna (token, cache, settings). Windows: %APPDATA%\\SpotifyFloatingLyrics."""
    base = os.getenv("APPDATA")
    if not base:
        base = os.path.join(Path.home(), ".config") if sys.platform != "darwin" else \
            os.path.join(Path.home(), "Library", "Application Support")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class AppConfig:
    client_id: str
    redirect_uri: str
    poll_interval: float
    user_agent: str
    source: str = "smtc"          
    smtc_app_filter: str = "spotify"

    @classmethod
    def from_env(cls) -> "AppConfig":
        load_dotenv(PROJECT_DIR / ".env")
        source = os.getenv("PLAYBACK_SOURCE", "smtc").strip().lower()
        if source not in ("smtc", "spotify"):
            source = "smtc"
        default_poll = "1" if source == "smtc" else "3"
        try:
            poll = float(os.getenv("POLL_INTERVAL_SECONDS", default_poll))
        except ValueError:
            poll = float(default_poll)
        floor = 0.5 if source == "smtc" else 2.0
        return cls(
            client_id=os.getenv("SPOTIFY_CLIENT_ID", "").strip(),
            redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback").strip(),
            poll_interval=max(floor, poll),
            user_agent=os.getenv("LRCLIB_USER_AGENT", "SpotifyFloatingLyricsOverlay/1.0").strip(),
            source=source,
            smtc_app_filter=os.getenv("SMTC_APP_FILTER", "spotify").strip(),
        )


@dataclass
class Settings:
    bg_opacity: int = 55          # persen, 5..95
    font_family: str = "Segoe UI"
    font_size: int = 20           # point
    lyrics_offset_ms: int = 0   
    screen_name: str = ""       
    click_through: bool = True    # pass mouse 
    max_active_cards: int = 3     # entering
    
    def sanitize(self) -> None:
        self.bg_opacity = max(5, min(int(self.bg_opacity), 95))
        self.font_size = max(10, min(int(self.font_size), 64))
        self.lyrics_offset_ms = max(-5000, min(int(self.lyrics_offset_ms), 5000))
        self.screen_name = str(self.screen_name)
        self.click_through = bool(self.click_through)
        self.max_active_cards = max(1, min(int(self.max_active_cards), 6))

    @classmethod
    def load(cls) -> "Settings":
        path = app_dir() / "settings.json"
        data: dict = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        known = {f.name for f in fields(cls)}
        try:
            obj = cls(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            obj = cls()
        try:
            obj.sanitize()
        except (TypeError, ValueError):
            obj = cls()
        return obj

    def save(self) -> None:
        path = app_dir() / "settings.json"
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass
