"""
Apply Spotify inbox placement recommendations safely.

Reads a recommendation CSV (produced elsewhere, e.g. an AI pass over the
inbox) and moves confident MOVE rows out of INBOX into their recommended
destination sub-playlist.

Safety model (matches review_apply.py's fail-closed validation):
  - EVERY row's destination is validated before any Spotify call is made. If
    any row targets something that isn't an enabled sub in config.json — a
    master ("M · ..."), INBOX itself, a disabled sub, or a typo — the entire
    run aborts and nothing is written, not just that one row. This script
    never creates playlists; add a genuinely new sub with
    `python manage_playlists.py add-sub "<name>"` first, then re-run.
  - one-track-one-sub is checked against every live sub before writing: if a
    track is already filed somewhere else, it is left in INBOX and reported,
    never silently duplicated.
  - only rows where action == MOVE and confidence_score >= --min-confidence
    are considered; KEEP_INBOX rows are left untouched.
  - only tracks still present in the live INBOX are processed.
  - a track is removed from INBOX only after its destination add succeeds
    (or if it was already present at the destination).

Recommended first run:
    python apply_inbox_recommendations.py --dry-run

Apply:
    python apply_inbox_recommendations.py

Stricter confidence cutoff:
    python apply_inbox_recommendations.py --min-confidence 0.80
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass

from spotify_api import Spotify
from sync import load_config

DEFAULT_CSV = "spotify_inbox_placement_recommendations.csv"

log = logging.getLogger("apply_inbox_recommendations")


@dataclass(frozen=True)
class Recommendation:
    artist: str
    track: str
    uri: str
    destination: str
    confidence: float

    @property
    def label(self) -> str:
        return f"{self.artist} — {self.track}"


def read_recommendations(path: str, min_confidence: float) -> tuple[list[Recommendation], int, int, int]:
    if not os.path.exists(path):
        raise SystemExit(f"Recommendation CSV not found: {path}")

    moves: list[Recommendation] = []
    keep_inbox = below_threshold = invalid = 0

    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"track_uri", "recommended_playlist", "action", "confidence_score"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"Recommendation CSV is missing column(s): {', '.join(sorted(missing))}")

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
            if not uri.startswith("spotify:track:") or not destination:
                invalid += 1
                continue

            moves.append(Recommendation(
                artist=(row.get("artist") or "").strip(),
                track=(row.get("track") or "").strip(),
                uri=uri,
                destination=destination,
                confidence=confidence,
            ))

    # One URI should never be recommended to two different destinations.
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


def validate_destinations(recs: list[Recommendation], config: dict, sub_ids: dict[str, str]) -> None:
    """Abort the entire run — before any Spotify call is made — if any row
    targets something that isn't an enabled sub.

    This matches review_apply.py's fail-closed validation: everything happens
    up front and completely, so a bad row can never let some tracks get
    written while others silently don't. In particular this is the guard
    against a recommendation CSV naming a master ("M · ...") or INBOX itself
    as a destination — those are never valid and must stop the run, not just
    get skipped while everything else goes through.
    """
    master_names = {m["name"] for m in config["masters"]}
    inbox_name = config["inbox"]["name"]

    bad: dict[str, str] = {}  # destination -> reason
    for rec in recs:
        if rec.destination in sub_ids:
            continue
        if rec.destination in bad:
            continue
        if rec.destination in master_names:
            bad[rec.destination] = "this is a MASTER — masters are write-only by sync.py, never a valid destination"
        elif rec.destination == inbox_name:
            bad[rec.destination] = "this is INBOX itself"
        elif any(s["name"] == rec.destination for s in config["subs"]):
            bad[rec.destination] = "this sub exists but is disabled in config.json"
        else:
            bad[rec.destination] = "not a known sub — typo, or a new sub not yet added"

    if bad:
        log.error("%d recommended destination(s) are not valid. Nothing was written.", len(bad))
        for name, reason in sorted(bad.items()):
            count = sum(1 for r in recs if r.destination == name)
            log.error("  %r (%d track(s)): %s", name, count, reason)
        log.error("Fix the CSV, or run `python manage_playlists.py add-sub` for a genuinely new sub, then re-run.")
        raise SystemExit(1)


def check_one_track_one_sub(sp: Spotify, config: dict) -> dict[str, str]:
    """uri -> sub name, across every enabled sub, read live right now."""
    existing: dict[str, str] = {}
    for sub in config["subs"]:
        if not sub.get("enabled", True):
            continue
        for uri in sp.playlist_track_uris(sub["id"]):
            existing[uri] = sub["name"]
    return existing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=DEFAULT_CSV, help="recommendation CSV path")
    ap.add_argument("--dry-run", action="store_true", help="show changes; write nothing")
    ap.add_argument("--min-confidence", type=float, default=0.65,
                     help="minimum confidence_score for MOVE rows (default: 0.65)")
    ap.add_argument("--keep-inbox-copy", action="store_true",
                     help="add to destinations but do not remove placed tracks from INBOX")
    args = ap.parse_args()

    if not 0 <= args.min_confidence <= 1:
        ap.error("--min-confidence must be between 0 and 1")

    config = load_config()
    inbox = config.get("inbox")
    if not inbox:
        log.error("No inbox configured in config.json.")
        return 2
    sub_ids = {s["name"]: s["id"] for s in config["subs"] if s.get("enabled", True)}

    recs, keep_inbox, below_threshold, invalid = read_recommendations(args.csv, args.min_confidence)
    validate_destinations(recs, config, sub_ids)  # raises SystemExit before any Spotify call if a row is bad

    log.info(
        "CSV: %d usable MOVE candidate(s); %d KEEP_INBOX; %d below threshold; %d invalid.",
        len(recs), keep_inbox, below_threshold, invalid,
    )

    sp = Spotify.from_env(dry_run=args.dry_run)
    log.info("Authenticated as: %s", sp.me().get("display_name"))

    inbox_uris = set(sp.playlist_track_uris(inbox["id"]))
    log.info("Live INBOX: %d unique track(s).", len(inbox_uris))

    live_recs = [r for r in recs if r.uri in inbox_uris]
    stale = len(recs) - len(live_recs)
    if stale:
        log.info("Skipping %d recommended track(s) no longer present in live INBOX.", stale)

    log.info("Checking one-track-one-sub against live subs before writing...")
    existing = check_one_track_one_sub(sp, config)

    conflicts = [(r, existing[r.uri]) for r in live_recs
                 if r.uri in existing and existing[r.uri] != r.destination]
    conflict_uris = {r.uri for r, _ in conflicts}
    actionable = [r for r in live_recs if r.uri not in conflict_uris]

    if conflicts:
        log.warning("%d track(s) already filed elsewhere — left in INBOX, not moved:", len(conflicts))
        for rec, current_sub in conflicts[:20]:
            log.warning("  %s is already in %r, recommendation said %r", rec.label, current_sub, rec.destination)
        if len(conflicts) > 20:
            log.warning("  ... and %d more", len(conflicts) - 20)

    grouped: dict[str, list[Recommendation]] = defaultdict(list)
    for rec in actionable:
        grouped[rec.destination].append(rec)

    safe_to_remove: set[str] = set()
    for destination in sorted(grouped):
        uris = list(dict.fromkeys(r.uri for r in grouped[destination]))
        dest_id = sub_ids[destination]

        current = set(sp.playlist_track_uris(dest_id))
        missing = [u for u in uris if u not in current]
        already_there = [u for u in uris if u in current]

        log.info("%-42s +%d new, %d already there", destination, len(missing), len(already_there))
        if missing:
            sp.add_tracks(dest_id, missing)
        safe_to_remove.update(missing)
        safe_to_remove.update(already_there)

    if args.keep_inbox_copy:
        log.info("--keep-inbox-copy: %d track(s) will remain in INBOX despite being filed.", len(safe_to_remove))
    elif safe_to_remove:
        sp.remove_tracks(inbox["id"], sorted(safe_to_remove))
        log.info("Removed %d track(s) from INBOX.", len(safe_to_remove))

    log.info("")
    log.info("Summary")
    log.info("  Usable MOVE rows                  : %d", len(recs))
    log.info("  Still present in live INBOX       : %d", len(live_recs))
    log.info("  Skipped (already filed elsewhere) : %d", len(conflicts))
    log.info("  Placed / safe to clear            : %d", len(safe_to_remove))
    log.info("  KEEP_INBOX rows untouched         : %d", keep_inbox)
    if args.dry_run:
        log.info("")
        log.info("DRY RUN — no Spotify writes were made.")
    log.info("Run python sync.py afterwards to update the masters.")

    return 1 if conflicts else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())