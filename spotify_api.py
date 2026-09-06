"""
Shared Spotify Web API helpers: auth, pagination, rate-limit handling,
and playlist read/write. Used by both bootstrap.py and sync.py.

Targets the Web API as of the February 2026 Development Mode migration:
  - playlist items live at /playlists/{id}/items, not /tracks
  - the DELETE body key is "items", not "tracks"
  - each row is items[].item, not items[].track
  - playlists are created at POST /me/playlists; POST /users/{id}/playlists
    is gone and returns 403
  - GET /me no longer returns country, email, product, or followers
https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide
"""

from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger(__name__)

BASE = "https://api.spotify.com/v1"
MAX_RETRIES = 5


class SpotifyError(RuntimeError):
    pass


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def token_from_env() -> str:
    """Kept for backward compatibility. Prefer Spotify.from_env(), which also
    lets the client refresh its own access token if it expires mid-run."""
    return refresh_access_token(
        os.environ["SPOTIFY_CLIENT_ID"],
        os.environ["SPOTIFY_CLIENT_SECRET"],
        os.environ["SPOTIFY_REFRESH_TOKEN"],
    )


class Spotify:
    """Thin API client with retry-aware request handling.

    Access tokens last about an hour. Long runs (a big bootstrap, or suggest.py
    fetching genres artist-by-artist across a large inbox) can outlive that, so
    the client re-authenticates itself on a 401 if it was built with
    Spotify.from_env() and has credentials on hand to do so.
    """

    def __init__(self, token: str, dry_run: bool = False, credentials: tuple[str, str, str] | None = None):
        self.token = token
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.writes = 0
        self._credentials = credentials  # (client_id, client_secret, refresh_token) or None

    @classmethod
    def from_env(cls, dry_run: bool = False) -> "Spotify":
        client_id = os.environ["SPOTIFY_CLIENT_ID"]
        client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]
        refresh_token = os.environ["SPOTIFY_REFRESH_TOKEN"]
        token = refresh_access_token(client_id, client_secret, refresh_token)
        return cls(token, dry_run=dry_run, credentials=(client_id, client_secret, refresh_token))

    def _reauth(self) -> None:
        if not self._credentials:
            raise SpotifyError(
                "Access token was rejected (401) and this client has no "
                "credentials on hand to refresh it. Build it with Spotify.from_env()."
            )
        client_id, client_secret, refresh_token = self._credentials
        log.warning("Access token expired mid-run, refreshing...")
        self.token = refresh_access_token(client_id, client_secret, refresh_token)
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    # ---------------------------------------------------------------- core

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        reauthed = False
        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
            except requests.exceptions.RequestException as exc:
                wait = 2 ** attempt
                log.warning("Network error (%s), retrying in %ss", exc, wait)
                time.sleep(wait)
                continue

            if resp.status_code == 401 and not reauthed:
                # Token expired mid-run rather than being wrong outright.
                # Refresh once; if it 401s again after that, something else
                # is wrong and it should surface as a real error below.
                self._reauth()
                reauthed = True
                continue

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 5)) + 1
                log.warning("Rate limited, sleeping %ss (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
                continue

            if resp.status_code in (500, 502, 503, 504):
                wait = 2 ** attempt
                log.warning("Server %s, retrying in %ss", resp.status_code, wait)
                time.sleep(wait)
                continue

            if not resp.ok:
                raise SpotifyError(f"{method} {url} -> {resp.status_code} {resp.text[:300]}")
            return resp

        raise SpotifyError(f"{method} {url} failed after {MAX_RETRIES} attempts")

    def _write(self, method: str, url: str, **kwargs) -> requests.Response | None:
        if self.dry_run:
            log.info("  [dry-run] %s %s", method, url.replace(BASE, ""))
            return None
        self.writes += 1
        return self._request(method, url, **kwargs)

    # -------------------------------------------------------------- reads

    def me(self) -> dict:
        return self._request("GET", f"{BASE}/me").json()

    def playlist_track_uris(self, playlist_id: str) -> list[str]:
        """All track URIs in a playlist, in order, including duplicates.

        Local files and podcast episodes are skipped: they cannot be synced
        reliably by URI.
        """
        uris: list[str] = []
        url = f"{BASE}/playlists/{playlist_id}/items"
        params = {"limit": 100, "fields": "next,items(is_local,item(uri,type))"}

        while url:
            data = self._request("GET", url, params=params).json()
            for row in data.get("items", []):
                if row.get("is_local"):
                    continue
                # "item" is the Feb-2026 name; "track" kept for older responses.
                track = row.get("item") or row.get("track")
                if not isinstance(track, dict):
                    continue
                if track.get("type") != "track" or not track.get("uri"):
                    continue
                uris.append(track["uri"])
            url = data.get("next")
            params = None
        return uris

    def my_playlists(self) -> dict[str, str]:
        """Map of playlist name -> id for playlists owned by the current user."""
        user_id = self.me()["id"]
        out: dict[str, str] = {}
        url = f"{BASE}/me/playlists"
        params = {"limit": 50}
        while url:
            data = self._request("GET", url, params=params).json()
            for pl in data.get("items", []):
                if pl and pl.get("owner", {}).get("id") == user_id:
                    out[pl["name"]] = pl["id"]
            url = data.get("next")
            params = None
        return out

    # ------------------------------------------------------------- writes

    def create_playlist(self, name: str, description: str = "", public: bool = False) -> str:
        if self.dry_run:
            log.info("  [dry-run] create playlist %r", name)
            return f"DRYRUN_{abs(hash(name)) % 10**16:016d}"
        # POST /users/{id}/playlists was removed in Feb 2026 and now 403s.
        resp = self._request(
            "POST",
            f"{BASE}/me/playlists",
            json={"name": name, "description": description[:300], "public": public},
        )
        self.writes += 1
        return resp.json()["id"]

    def set_description(self, playlist_id: str, description: str) -> None:
        self._write(
            "PUT",
            f"{BASE}/playlists/{playlist_id}",
            json={"description": description[:300]},
        )

    def rename_playlist(self, playlist_id: str, name: str) -> None:
        self._write(
            "PUT",
            f"{BASE}/playlists/{playlist_id}",
            json={"name": name},
        )

    def add_tracks(self, playlist_id: str, uris: list[str]) -> None:
        for i in range(0, len(uris), 100):
            batch = uris[i : i + 100]
            self._write(
                "POST",
                f"{BASE}/playlists/{playlist_id}/items",
                json={"uris": batch},
            )

    def remove_tracks(self, playlist_id: str, uris: list[str]) -> None:
        """Remove tracks by URI. Removes every occurrence of each URI."""
        for i in range(0, len(uris), 100):
            batch = uris[i : i + 100]
            self._write(
                "DELETE",
                f"{BASE}/playlists/{playlist_id}/items",
                json={"items": [{"uri": u} for u in batch]},
            )