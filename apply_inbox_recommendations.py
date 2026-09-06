#!/usr/bin/env python3
"""
Apply Spotify inbox placement recommendations safely.

Reads spotify_inbox_placement_recommendations.csv and:
  1. Processes only rows where action == MOVE.
  2. Leaves KEEP_INBOX rows untouched.
  3. Processes only tracks that are still in the live INBOX.
  4. Adds each track to its recommended destination playlist.
  5. Creates missing destination playlists (private by default).
  6. Removes a track from INBOX only after its destination add succeeds
     (or if the track was already present in the destination).
  7. Updates playlist_ids.json only for newly created/adopted playlists.
  8. Retries transient GET/read timeouts without risking duplicate write retries.

The script uses the current Spotify Web API playlist /items endpoints (2026),
while reusing token_from_env() from this repo's spotify_api module.

Recommended first run:
    python apply_inbox_recommendations.py --dry-run

Apply:
    python apply_inbox_recommendations.py

Stricter confidence cutoff, if desired:
    python apply_inbox_recommendations.py --min-confidence 0.80

Do not create missing destination playlists:
    python apply_inbox_recommendations.py --no-create-missing
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from spotify_api import token_from_env


API_BASE = "https://api.spotify.com/v1"

DEFAULT_CSV = "spotify_inbox_placement_recommendations.csv"
IDS_PATH = "playlist_ids.json"
INBOX_NAME = "INBOX"

# These are the two additions identified in the recommendation pass.
NEW_PLAYLIST_DESCRIPTIONS = {
    "CHILL · Trip Hop & Downtempo":
        "Trip hop, downtempo and leftfield chill. Created from inbox recommendations.",
    "JAZZ · Standards, Bebop & Cool":
        "Jazz standards, bebop and cool jazz. Created from inbox recommendations.",
}

log = logging.getLogger("apply_inbox_recommendations")


class SpotifyApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class Recommendation:
    artist: str
    track: str
    uri: str
    destination: str
    action: str
    confidence: float
    confidence_label: str

    @property
    def label(self) -> str:
        return f"{self.artist} — {self.track}"


def chunks(values: list[str], size: int = 100) -> Iterable[list[str]]:
    for i in range(0, len(values), size):
        yield values[i:i + size]


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json_atomic(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def api_request(
    token: str,
    method: str,
    path: str,
    *,
    body=None,
    params: dict | None = None,
    retries: int = 5,
    timeout: float = 60.0,
):
    if path.startswith("http://") or path.startswith("https://"):
        url = path
    else:
        url = API_BASE + path

    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        url += ("&" if "?" in url else "?") + query

    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")

            if exc.code == 429 and attempt < retries:
                retry_after = exc.headers.get("Retry-After", "2")
                try:
                    delay = max(1.0, float(retry_after))
                except ValueError:
                    delay = 2.0
                log.warning("Spotify rate limit; retrying in %.1fs", delay)
                time.sleep(delay)
                continue

            if exc.code >= 500 and attempt < retries:
                delay = min(2 ** attempt, 10)
                log.warning("Spotify %s; retrying in %ss", exc.code, delay)
                time.sleep(delay)
                continue

            raise SpotifyApiError(
                f"{method} {url} failed with HTTP {exc.code}: {raw}"
            ) from exc
        except TimeoutError as exc:
            # Python 3.14 can raise TimeoutError directly from resp.read(),
            # rather than wrapping it in urllib.error.URLError.
            #
            # Retrying reads is safe. For writes, an HTTP request may have
            # reached Spotify even if our response read timed out, so do not
            # blindly retry and risk adding duplicate playlist items. The
            # caller will leave the track in INBOX; the next script run will
            # detect whether it is already present at the destination.
            if method.upper() in {"GET", "HEAD"} and attempt < retries:
                delay = min(2 ** attempt, 10)
                log.warning(
                    "Spotify read timed out; retrying in %ss (%d/%d)",
                    delay, attempt + 1, retries,
                )
                time.sleep(delay)
                continue
            raise SpotifyApiError(
                f"{method} {url} timed out after {timeout:.0f}s"
            ) from exc

        except urllib.error.URLError as exc:
            if attempt < retries:
                delay = min(2 ** attempt, 10)
                log.warning("Network error; retrying in %ss: %s", delay, exc)
                time.sleep(delay)
                continue
            raise SpotifyApiError(f"{method} {url} failed: {exc}") from exc

    raise SpotifyApiError(f"{method} {url} failed after retries")


def read_recommendations(path: str, min_confidence: float):
    if not os.path.exists(path):
        raise SystemExit(f"Recommendation CSV not found: {path}")

    moves: list[Recommendation] = []
    keep_inbox = 0
    below_threshold = 0
    invalid = 0

    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"track_uri", "recommended_playlist", "action", "confidence_score"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(
                "Recommendation CSV is missing required columns: "
                + ", ".join(sorted(missing))
            )

        for row in reader:
            action = (row.get("action") or "").strip().upper()
            uri = (row.get("track_uri") or "").strip()
            destination = (row.get("recommended_playlist") or "").strip()

            try:
                confidence = float(row.get("confidence_score") or 0)
            except ValueError:
                confidence = 0.0

            if action != "MOVE":
                keep_inbox += 1
                continue

            if confidence < min_confidence:
                below_threshold += 1
                continue

            if not uri.startswith("spotify:track:") or not destination or destination == INBOX_NAME:
                invalid += 1
                continue

            moves.append(
                Recommendation(
                    artist=(row.get("artist") or "").strip(),
                    track=(row.get("track") or "").strip(),
                    uri=uri,
                    destination=destination,
                    action=action,
                    confidence=confidence,
                    confidence_label=(row.get("confidence_label") or "").strip(),
                )
            )

    # One URI should never be sent to multiple destination playlists.
    seen: dict[str, str] = {}
    conflicts: list[tuple[str, str, str]] = []
    unique_moves: list[Recommendation] = []

    for rec in moves:
        previous = seen.get(rec.uri)
        if previous is None:
            seen[rec.uri] = rec.destination
            unique_moves.append(rec)
        elif previous != rec.destination:
            conflicts.append((rec.uri, previous, rec.destination))

    if conflicts:
        log.error("Recommendation invariant violation: a track has multiple destinations.")
        for uri, a, b in conflicts[:20]:
            log.error("  %s -> %r and %r", uri, a, b)
        raise SystemExit("Fix recommendation conflicts before applying.")

    return unique_moves, keep_inbox, below_threshold, invalid


def my_playlists(token: str) -> dict[str, list[str]]:
    """Return exact playlist name -> list of owned/collaborative playlist IDs."""
    by_name: dict[str, list[str]] = defaultdict(list)
    offset = 0
    limit = 50

    while True:
        payload = api_request(
            token, "GET", "/me/playlists",
            params={"limit": limit, "offset": offset},
        )
        items = (payload or {}).get("items") or []

        for playlist in items:
            name = playlist.get("name")
            pid = playlist.get("id")
            if name and pid:
                by_name[name].append(pid)

        if len(items) < limit:
            break
        offset += len(items)

    return dict(by_name)


def resolve_existing_playlist(
    name: str,
    *,
    by_name: dict[str, list[str]],
    tracked_ids: dict[str, str],
) -> str | None:
    ids = by_name.get(name, [])

    if not ids:
        return tracked_ids.get(name)

    if len(ids) == 1:
        return ids[0]

    tracked = tracked_ids.get(name)
    if tracked and tracked in ids:
        return tracked

    raise SpotifyApiError(
        f"Multiple playlists are named {name!r} and playlist_ids.json does not "
        "disambiguate them. Rename one or record the intended ID before running."
    )


def get_playlist_uris(token: str, playlist_id: str) -> list[str]:
    uris: list[str] = []
    offset = 0
    limit = 100

    while True:
        payload = api_request(
            token, "GET", f"/playlists/{playlist_id}/items",
            params={"limit": limit, "offset": offset},
        )
        items = (payload or {}).get("items") or []

        for entry in items:
            # Current API uses "item"; "track" is kept as a compatibility fallback.
            obj = entry.get("item") or entry.get("track") or {}
            uri = obj.get("uri")
            if isinstance(uri, str) and uri.startswith("spotify:track:"):
                uris.append(uri)

        if len(items) < limit:
            break
        offset += len(items)

    return uris


def create_playlist(
    token: str,
    name: str,
    description: str,
    *,
    public: bool,
) -> str:
    payload = api_request(
        token,
        "POST",
        "/me/playlists",
        body={
            "name": name,
            "public": public,
            "description": description[:300],
        },
    )
    pid = (payload or {}).get("id")
    if not pid:
        raise SpotifyApiError(f"Spotify did not return an ID for new playlist {name!r}")
    return pid


def add_items(token: str, playlist_id: str, uris: list[str]) -> None:
    for batch in chunks(uris, 100):
        api_request(
            token,
            "POST",
            f"/playlists/{playlist_id}/items",
            body={"uris": batch},
        )


def remove_items(token: str, playlist_id: str, uris: list[str]) -> None:
    for batch in chunks(uris, 100):
        api_request(
            token,
            "DELETE",
            f"/playlists/{playlist_id}/items",
            body={"items": [{"uri": uri} for uri in batch]},
        )


def ensure_destination(
    token: str,
    name: str,
    *,
    by_name: dict[str, list[str]],
    tracked_ids: dict[str, str],
    create_missing: bool,
    public_new: bool,
    dry_run: bool,
) -> tuple[str | None, bool]:
    pid = resolve_existing_playlist(name, by_name=by_name, tracked_ids=tracked_ids)
    if pid:
        return pid, False

    if not create_missing:
        return None, False

    desc = NEW_PLAYLIST_DESCRIPTIONS.get(
        name,
        "Created from Spotify inbox placement recommendations.",
    )

    if dry_run:
        log.info("  [dry-run] would create missing playlist %r", name)
        return None, True

    pid = create_playlist(token, name, desc, public=public_new)
    tracked_ids[name] = pid
    by_name.setdefault(name, []).append(pid)
    log.info("  Created %r -> %s", name, pid)
    return pid, True


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Move confident recommendation rows out of Spotify INBOX."
    )
    ap.add_argument("--csv", default=DEFAULT_CSV, help="recommendation CSV path")
    ap.add_argument("--dry-run", action="store_true", help="show changes; write nothing")
    ap.add_argument(
        "--min-confidence",
        type=float,
        default=0.65,
        help="minimum confidence_score for MOVE rows (default: 0.65)",
    )
    ap.add_argument(
        "--no-create-missing",
        action="store_true",
        help="do not create destination playlists that are missing",
    )
    ap.add_argument(
        "--public-new",
        action="store_true",
        help="create missing destination playlists as public (default: private)",
    )
    ap.add_argument(
        "--keep-inbox-copy",
        action="store_true",
        help="add to destinations but do not remove successfully placed tracks from INBOX",
    )
    args = ap.parse_args()

    if not 0 <= args.min_confidence <= 1:
        ap.error("--min-confidence must be between 0 and 1")

    recs, manual_count, below_threshold, invalid = read_recommendations(
        args.csv, args.min_confidence
    )

    log.info(
        "CSV: %d MOVE candidate(s); %d KEEP_INBOX; %d below threshold; %d invalid.",
        len(recs), manual_count, below_threshold, invalid,
    )

    token = token_from_env()
    playlists = my_playlists(token)
    tracked_ids: dict[str, str] = load_json(IDS_PATH, {})

    inbox_id = resolve_existing_playlist(
        INBOX_NAME, by_name=playlists, tracked_ids=tracked_ids
    )
    if not inbox_id:
        log.error("Could not find playlist named %r.", INBOX_NAME)
        return 2

    inbox_uris = get_playlist_uris(token, inbox_id)
    inbox_set = set(inbox_uris)
    log.info("Live INBOX contains %d track occurrence(s), %d unique.", len(inbox_uris), len(inbox_set))

    # Only act on recommendations whose tracks are still in the live inbox.
    live_recs = [r for r in recs if r.uri in inbox_set]
    stale_recs = [r for r in recs if r.uri not in inbox_set]

    if stale_recs:
        log.info(
            "Skipping %d recommended MOVE track(s) no longer present in live INBOX.",
            len(stale_recs),
        )

    grouped: dict[str, list[Recommendation]] = defaultdict(list)
    for rec in live_recs:
        grouped[rec.destination].append(rec)

    safe_to_remove: set[str] = set()
    failed: list[tuple[str, str]] = []
    created_names: list[str] = []

    for destination in sorted(grouped):
        rows = grouped[destination]
        uris = list(dict.fromkeys(r.uri for r in rows))

        log.info("%s: %d inbox track(s)", destination, len(uris))

        try:
            pid, created = ensure_destination(
                token,
                destination,
                by_name=playlists,
                tracked_ids=tracked_ids,
                create_missing=not args.no_create_missing,
                public_new=args.public_new,
                dry_run=args.dry_run,
            )
        except SpotifyApiError as exc:
            log.error("  Destination resolution failed: %s", exc)
            failed.extend((r.uri, str(exc)) for r in rows)
            continue

        if created:
            created_names.append(destination)
            # Persist a newly created ID immediately so an interrupted run cannot
            # create a duplicate playlist on the next run.
            if not args.dry_run:
                save_json_atomic(IDS_PATH, tracked_ids)

        if pid is None:
            if args.dry_run and created:
                existing_set: set[str] = set()
            else:
                msg = "destination does not exist and creation is disabled"
                log.warning("  %s", msg)
                failed.extend((r.uri, msg) for r in rows)
                continue
        else:
            try:
                existing_set = set(get_playlist_uris(token, pid))
            except SpotifyApiError as exc:
                log.error("  Could not read destination: %s", exc)
                failed.extend((r.uri, str(exc)) for r in rows)
                continue

        missing = [uri for uri in uris if uri not in existing_set]
        already_there = [uri for uri in uris if uri in existing_set]

        if already_there:
            log.info("  %d already present", len(already_there))

        if missing:
            if args.dry_run:
                log.info("  [dry-run] would add %d", len(missing))
                safe_to_remove.update(missing)
            else:
                try:
                    add_items(token, pid, missing)
                    log.info("  added %d", len(missing))
                    safe_to_remove.update(missing)
                except SpotifyApiError as exc:
                    # If an add fails, leave those tracks in INBOX.
                    log.error("  add failed; leaving %d track(s) in INBOX: %s", len(missing), exc)
                    failed.extend((uri, str(exc)) for uri in missing)

        # If already in destination, it is also safe to clear from inbox.
        safe_to_remove.update(already_there)

    # Intersect again with live inbox for clarity/idempotency.
    safe_to_remove &= inbox_set

    if args.keep_inbox_copy:
        log.info(
            "--keep-inbox-copy: %d successfully placed track(s) will remain in INBOX.",
            len(safe_to_remove),
        )
    elif safe_to_remove:
        if args.dry_run:
            log.info("[dry-run] would remove %d successfully placed track(s) from INBOX.", len(safe_to_remove))
        else:
            try:
                remove_items(token, inbox_id, sorted(safe_to_remove))
                log.info("Removed %d successfully placed track(s) from INBOX.", len(safe_to_remove))
            except SpotifyApiError as exc:
                # Destination adds have already happened, but leaving inbox copies is safe.
                log.error(
                    "Could not remove placed tracks from INBOX. Nothing was lost; "
                    "destination adds remain and INBOX copies remain: %s",
                    exc,
                )
                return 3

    if not args.dry_run and created_names:
        log.info(
            "Recorded %d newly created playlist ID(s) in %s.",
            len(created_names), IDS_PATH,
        )

    log.info("")
    log.info("Summary")
    log.info("  MOVE rows eligible by confidence : %d", len(recs))
    log.info("  Still present in live INBOX      : %d", len(live_recs))
    log.info("  Successfully placed / safe       : %d", len(safe_to_remove))
    log.info("  KEEP_INBOX rows untouched         : %d", manual_count)
    log.info("  Failed placements kept in INBOX   : %d", len({u for u, _ in failed}))
    log.info("  Missing playlists created/planned : %d", len(created_names))

    if failed:
        log.warning("Some placements failed. Their tracks were deliberately left in INBOX.")
        return 1

    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    sys.exit(main())
