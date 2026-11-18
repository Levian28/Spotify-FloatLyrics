from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
SCOPES = "user-read-currently-playing user-read-playback-state"
HTTP_TIMEOUT = 8


class SpotifyError(Exception):
    pass


class SpotifyAuthError(SpotifyError):
    """Token tidak valid / belum login: perlu otorisasi ulang."""


class SpotifyNetworkError(SpotifyError):
    """Gagal terhubung ke internet / Spotify."""


class SpotifyRateLimit(SpotifyError):
    def __init__(self, retry_after: float):
        super().__init__(f"Rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


@dataclass(frozen=True)
class Track:
    id: str | None
    title: str
    artist: str          # artis utama
    artists_all: str
    album: str
    duration_ms: int
    is_local: bool = False
    artwork_id: str | None = None
    artwork_data: bytes | None = None

    @property
    def key(self) -> str:
        """Kunci unik untuk cache (file lokal tidak punya ID)."""
        if self.id:
            return self.id
        raw = f"{self.artist}|{self.title}|{self.duration_ms}".lower().encode("utf-8")
        return "local-" + hashlib.sha1(raw).hexdigest()[:16]


@dataclass
class PlaybackState:
    status: str                     # "playing" | "paused" | "idle" | "unsupported"
    track: Track | None = None
    progress_ms: int = 0
    sampled_at: float = 0.0         # time.monotonic() saat progress_ms valid (perkiraan)


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != self.server.callback_path:  # type: ignore[attr-defined]
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.server.result = params  # type: ignore[attr-defined]
        ok = "code" in params
        body = (
            "<html><head><meta charset='utf-8'><title>Lyrics Overlay</title></head>"
            "<body style='font-family:Segoe UI,sans-serif;background:#111;color:#eee;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            f"<div style='text-align:center'><h2>{'Berhasil terhubung' if ok else 'Otorisasi gagal'}</h2>"
            "<p>Anda dapat menutup tab ini dan kembali ke aplikasi.</p></div></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # diamkan log HTTP
        pass


class SpotifyClient:
    def __init__(self, client_id: str, redirect_uri: str, token_path: Path):
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.token_path = token_path
        self._tokens: dict = {}
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._load_tokens()

    # ------------------------------------------------------------------ token
    def _load_tokens(self) -> None:
        try:
            self._tokens = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._tokens = {}

    def _save_tokens(self) -> None:
        try:
            self.token_path.write_text(json.dumps(self._tokens), encoding="utf-8")
        except OSError:
            pass

    def has_token(self) -> bool:
        return bool(self._tokens.get("refresh_token") or self._tokens.get("access_token"))

    def logout(self) -> None:
        with self._lock:
            self._tokens = {}
            try:
                self.token_path.unlink()
            except OSError:
                pass

    def _apply_token_response(self, data: dict) -> None:
        self._tokens["access_token"] = data["access_token"]
        self._tokens["expires_at"] = time.time() + int(data.get("expires_in", 3600))
        if data.get("refresh_token"):
            self._tokens["refresh_token"] = data["refresh_token"]
        self._save_tokens()

    def _post_token(self, payload: dict) -> dict:
        try:
            r = self._session.post(TOKEN_URL, data=payload, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            raise SpotifyNetworkError(str(exc)) from exc
        if r.status_code in (400, 401):
            raise SpotifyAuthError(r.text[:200])
        if r.status_code == 429:
            raise SpotifyRateLimit(float(r.headers.get("Retry-After", 5)))
        if not r.ok:
            raise SpotifyError(f"Token endpoint {r.status_code}")
        return r.json()

    def _refresh(self) -> None:
        refresh = self._tokens.get("refresh_token")
        if not refresh:
            raise SpotifyAuthError("Tidak ada refresh token")
        data = self._post_token({
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": self.client_id,
        })
        self._apply_token_response(data)

    def _access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            if not self._tokens:
                raise SpotifyAuthError("Belum login")
            expired = time.time() > float(self._tokens.get("expires_at", 0)) - 60
            if force_refresh or expired:
                self._refresh()
            return self._tokens["access_token"]

    # -------------------------------------------------------------- authorize
    def authorize(self, timeout: float = 180.0) -> None:
        """Alur PKCE lengkap: buka browser, tunggu callback lokal, tukar code -> token. Blocking."""
        verifier = secrets.token_urlsafe(64)[:128]
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        state = secrets.token_urlsafe(16)

        redirect = urlparse(self.redirect_uri)
        host = redirect.hostname or "127.0.0.1"
        port = redirect.port or 80
        try:
            server = HTTPServer((host, port), _CallbackHandler)
        except OSError as exc:
            raise SpotifyError(f"Port {port} tidak dapat dipakai: {exc}") from exc
        server.callback_path = redirect.path or "/"  # type: ignore[attr-defined]
        server.result = None  # type: ignore[attr-defined]
        server.timeout = 1

        query = urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        })
        webbrowser.open(f"{AUTH_URL}?{query}")

        deadline = time.monotonic() + timeout
        try:
            while server.result is None and time.monotonic() < deadline:  # type: ignore[attr-defined]
                server.handle_request()
        finally:
            server.server_close()

        result = server.result  # type: ignore[attr-defined]
        if not result:
            raise SpotifyAuthError("Waktu otorisasi habis")
        if result.get("state") != state:
            raise SpotifyAuthError("State tidak cocok")
        if "code" not in result:
            raise SpotifyAuthError(result.get("error", "Otorisasi ditolak"))

        data = self._post_token({
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": verifier,
        })
        with self._lock:
            self._tokens = {}
            self._apply_token_response(data)

    # --------------------------------------------------------------- playback
    def get_playback(self) -> PlaybackState:
        t0 = time.monotonic()
        resp = self._get("/me/player/currently-playing", {"additional_types": "track,episode"})
        t1 = time.monotonic()
        sampled_at = (t0 + t1) / 2  # kompensasi latensi jaringan

        if resp.status_code == 204 or not resp.content:
            return PlaybackState(status="idle", sampled_at=sampled_at)
        try:
            data = resp.json()
        except ValueError:
            return PlaybackState(status="idle", sampled_at=sampled_at)

        item = data.get("item")
        if not item or data.get("currently_playing_type") != "track":
            return PlaybackState(status="unsupported", sampled_at=sampled_at)

        artists = [a.get("name", "") for a in item.get("artists", []) if a.get("name")]
        track = Track(
            id=item.get("id"),
            title=item.get("name", ""),
            artist=artists[0] if artists else "",
            artists_all=", ".join(artists),
            album=(item.get("album") or {}).get("name", ""),
            duration_ms=int(item.get("duration_ms") or 0),
            is_local=bool(item.get("is_local")),
        )
        return PlaybackState(
            status="playing" if data.get("is_playing") else "paused",
            track=track,
            progress_ms=int(data.get("progress_ms") or 0),
            sampled_at=sampled_at,
        )

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        for attempt in range(2):
            token = self._access_token(force_refresh=(attempt == 1))
            try:
                r = self._session.get(
                    API_BASE + path, params=params, timeout=HTTP_TIMEOUT,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except requests.RequestException as exc:
                raise SpotifyNetworkError(str(exc)) from exc
            if r.status_code == 401 and attempt == 0:
                continue
            if r.status_code == 401:
                raise SpotifyAuthError("Token ditolak")
            if r.status_code == 429:
                raise SpotifyRateLimit(float(r.headers.get("Retry-After", 5)))
            if r.status_code == 403:
                raise SpotifyAuthError("Akses ditolak (403): cek mode Development / user allowlist / Premium")
            if r.status_code >= 500:
                raise SpotifyNetworkError(f"Spotify server {r.status_code}")
            if not r.ok and r.status_code != 204:
                raise SpotifyError(f"HTTP {r.status_code}")
            return r
        raise SpotifyAuthError("Token ditolak")
