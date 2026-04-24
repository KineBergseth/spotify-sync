"""
Spotify Bucket Master Playlist Sync
Reads all sub-playlists per bucket, computes the union of items,
and syncs each master playlist to match exactly.
"""

import json
import time
import logging
import os
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BASE = "https://api.spotify.com/v1"
ABORT_IF_UNION_EMPTY = True


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def get_playlist_items(token: str, playlist_id: str) -> set[str]:
    """Return set of track URIs in a playlist (handles pagination)."""
    uris = set()
    url = f"{BASE}/playlists/{playlist_id}/items"
    params = {"limit": 100}
    while url:
        resp = requests.get(url, headers=headers(token), params=params)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 5))
            log.warning(f"Rate limited. Waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items", []):
            track = item.get("item") or item.get("track")
            if track and isinstance(track, dict) and track.get("type") == "track":
                uris.add(track["uri"])
        url = data.get("next")
        params = {}
    return uris


def add_items(token: str, playlist_id: str, uris: list[str]):
    """Add items to a playlist in batches of 100."""
    for i in range(0, len(uris), 100):
        batch = uris[i : i + 100]
        resp = requests.post(
            f"{BASE}/playlists/{playlist_id}/items",
            headers=headers(token),
            json={"uris": batch},
        )
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 5))
            time.sleep(wait)
            resp = requests.post(
                f"{BASE}/playlists/{playlist_id}/items",
                headers=headers(token),
                json={"uris": batch},
            )
        resp.raise_for_status()
        log.info(f"  Added {len(batch)} items")


def remove_items(token: str, playlist_id: str, uris: list[str]):
    """Remove items from a playlist in batches of 100."""
    for i in range(0, len(uris), 100):
        batch = uris[i : i + 100]
        resp = requests.delete(
            f"{BASE}/playlists/{playlist_id}/items",
            headers=headers(token),
            json={"items": [{"uri": u} for u in batch]},
        )
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 5))
            time.sleep(wait)
            resp = requests.delete(
                f"{BASE}/playlists/{playlist_id}/items",
                headers=headers(token),
                json={"items": [{"uri": u} for u in batch]},
            )
        resp.raise_for_status()
        log.info(f"  Removed {len(batch)} items")


def sync_bucket(token: str, bucket_name: str, bucket_config: dict):
    master_id = bucket_config["master"]["id"]
    master_name = bucket_config["master"]["name"]
    sources = bucket_config["sources"]

    log.info(f"\n{'─'*50}")
    log.info(f"Syncing bucket: {bucket_name} -> master: {master_name}")

    union: set[str] = set()
    for source in sources:
        if not source.get("enabled", True):
            log.info(f"  [{source['name']}]: skipped (disabled)")
            continue
        pl_id = source["id"]
        pl_name = source["name"]
        try:
            items = get_playlist_items(token, pl_id)
            log.info(f"  [{pl_name}]: {len(items)} items")
            union |= items
        except Exception as e:
            log.error(f"  Failed to read [{pl_name}] ({pl_id}): {e}")

    log.info(f"  Union total: {len(union)} unique items")

    if ABORT_IF_UNION_EMPTY and len(union) == 0:
        log.error(f"  ABORTED: union is empty for bucket {bucket_name}. Master not touched.")
        return

    try:
        current = get_playlist_items(token, master_id)
    except Exception as e:
        log.error(f"  Failed to read master {master_id}: {e}")
        return

    log.info(f"  Master currently: {len(current)} items")

    to_add = list(union - current)
    to_remove = list(current - union)

    if to_add:
        log.info(f"  Adding {len(to_add)} items...")
        add_items(token, master_id, to_add)
    else:
        log.info("  Nothing to add")

    if to_remove:
        log.info(f"  Removing {len(to_remove)} items...")
        remove_items(token, master_id, to_remove)
    else:
        log.info("  Nothing to remove")

    log.info(f"  Done. Master now has {len(union)} items.")


def main():
    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]
    refresh_token = os.environ["SPOTIFY_REFRESH_TOKEN"]

    with open("config.json", encoding="utf-8") as f:
        config = json.load(f)

    log.info("Getting access token...")
    token = refresh_access_token(client_id, client_secret, refresh_token)

    log.info(f"Authenticated as: {requests.get(BASE + '/me', headers=headers(token)).json().get('display_name')}")

    for bucket_name, bucket_config in config["buckets"].items():
        if not bucket_config.get("enabled", True):
            log.info(f"Skipping {bucket_name} (disabled)")
            continue
        try:
            sync_bucket(token, bucket_name, bucket_config)
        except Exception as e:
            log.error(f"Bucket {bucket_name} failed: {e}")

    log.info("\nAll buckets synced.")


if __name__ == "__main__":
    main()