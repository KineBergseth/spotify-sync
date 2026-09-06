"""
Apply AI review flags safely.

The review AI identifies tracks that do not belong in their current source
sub-playlist. This script moves every FLAG row into a separate holding playlist
(e.g. "UNSORTED · Must be processed") and removes it from the current sub.

Safety model:
  - preview only by default; --apply is required for writes
  - every FLAG must match review/library_export.csv exactly by URI + playlist
  - current_playlist must be a configured sub, never a master/inbox
  - holding playlist must be outside the managed subs/masters/inbox
  - live Spotify membership is re-checked before writing
  - all tracks are added to the holding playlist BEFORE any source removal

Usage:
    python review_apply.py review/review_flags_01.csv --holding-id PLAYLIST_ID
    python review_apply.py review/review_flags_*.csv --holding-id PLAYLIST_ID --apply

You can also set SPOTIFY_REVIEW_HOLDING_ID instead of passing --holding-id.
"""

from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import logging
import os
import shlex
import sys
from collections import defaultdict
from pathlib import Path

from spotify_api import Spotify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONFIG_PATH = "config.json"
DEFAULT_LIBRARY_EXPORT = os.path.join("review", "library_export.csv")
ENV_HOLDING_ID = "SPOTIFY_REVIEW_HOLDING_ID" #2h0CpWZWmR5iFKFtWqghYe
FLAG_TYPES = {"FLAG", "MISFILED"}


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        log.error("%s not found. Run bootstrap.py first.", path)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def expand_inputs(patterns: list[str]) -> list[str]:
    """Expand shell-independent globs and preserve first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if not matches and os.path.isfile(pattern):
            matches = [pattern]
        if not matches:
            log.error("No review file matched %r", pattern)
            sys.exit(1)
        for path in matches:
            if path not in seen:
                out.append(path)
                seen.add(path)
    return out


def read_csv_rows(path: str) -> list[dict[str, str]]:
    """Read AI CSV, tolerating BOM, markdown fences, or a tiny prose preamble."""
    text = Path(path).read_text(encoding="utf-8-sig")
    lines = text.splitlines()

    # Prefer the new schema, but accept the old export prompt schema too.
    header_index = None
    for i, line in enumerate(lines):
        normalized = line.strip().lstrip("\ufeff")
        if normalized.startswith("record_type,") or normalized.startswith("track_uri,"):
            header_index = i
            break
    if header_index is None:
        raise ValueError(
            f"{path}: could not find CSV header. Expected 'record_type,...' "
            "or legacy 'track_uri,...'."
        )

    payload_lines = []
    for line in lines[header_index:]:
        if line.strip().startswith("```"):
            break
        payload_lines.append(line)

    reader = csv.DictReader(io.StringIO("\n".join(payload_lines)))
    if not reader.fieldnames:
        raise ValueError(f"{path}: empty CSV")

    cleaned: list[dict[str, str]] = []
    for line_no, row in enumerate(reader, start=header_index + 2):
        if None in row:
            raise ValueError(
                f"{path}:{line_no}: too many CSV columns. A field containing a comma "
                "was probably not quoted."
            )
        cleaned.append({
            (k or "").strip(): (v or "").strip()
            for k, v in row.items()
        })
    return cleaned


def load_export(path: str) -> dict[tuple[str, str], dict[str, str]]:
    if not os.path.exists(path):
        raise ValueError(
            f"{path} not found. Run export.py first; the export is the safety proof "
            "that the AI flag came from the reviewed library state."
        )
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    required = {"playlist", "track_uri"}
    missing = required - set(rows[0].keys() if rows else [])
    if missing:
        raise ValueError(f"{path}: missing column(s): {', '.join(sorted(missing))}")

    index: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        uri = (row.get("track_uri") or "").strip()
        playlist = (row.get("playlist") or "").strip()
        if uri and playlist:
            index[(uri, playlist)] = row
    return index


def collect_flags(paths: list[str]) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for path in paths:
        rows = read_csv_rows(path)
        for row in rows:
            record_type = (row.get("record_type") or "").upper()
            uri = (row.get("track_uri") or "").strip()
            current = (row.get("current_playlist") or "").strip()

            # New schema: only explicit FLAG/MISFILED rows are actionable.
            # Legacy schema: every row with a URI is a misfiled-track row.
            if "record_type" in row and record_type not in FLAG_TYPES:
                continue
            if not uri and not current:
                continue
            if not uri or not current:
                raise ValueError(f"{path}: actionable row is missing track_uri/current_playlist: {row}")
            if not uri.startswith("spotify:track:"):
                raise ValueError(f"{path}: invalid Spotify track URI: {uri!r}")

            key = (uri, current)
            if key in seen:
                continue
            seen.add(key)
            row["_source_file"] = path
            flags.append(row)

    return flags


def validate_flags(
    flags: list[dict[str, str]],
    config: dict,
    export_index: dict[tuple[str, str], dict[str, str]],
    holding_id: str,
) -> dict[str, str]:
    sub_ids = {s["name"]: s["id"] for s in config["subs"]}
    managed_ids = {config["inbox"]["id"]}
    managed_ids.update(sub_ids.values())
    managed_ids.update(m["id"] for m in config["masters"])

    if holding_id in managed_ids:
        raise ValueError(
            "The holding playlist ID is one of the managed inbox/sub/master playlists. "
            "Use a separate playlist that is not part of config.json."
        )

    errors: list[str] = []
    for row in flags:
        uri = row["track_uri"]
        current = row["current_playlist"]
        source = row.get("_source_file", "AI review")

        if current not in sub_ids:
            errors.append(f"{source}: {uri} -> unknown/non-sub playlist {current!r}")
            continue
        if (uri, current) not in export_index:
            errors.append(
                f"{source}: {uri} is not listed in {current!r} in the matching library export"
            )

    if errors:
        preview = "\n".join(f"  - {e}" for e in errors[:25])
        more = f"\n  ... and {len(errors) - 25} more" if len(errors) > 25 else ""
        raise ValueError(f"Review validation failed; nothing will be written:\n{preview}{more}")

    return sub_ids


def summarize(flags: list[dict[str, str]]) -> None:
    by_playlist: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in flags:
        by_playlist[row["current_playlist"]].append(row)

    log.info("AI flags accepted: %d track(s) across %d source playlist(s)", len(flags), len(by_playlist))
    for name in sorted(by_playlist):
        log.info("  %-42s -%d", name, len(by_playlist[name]))

    # Show enough titles to make a preview useful without flooding the terminal.
    for row in flags[:20]:
        artist = row.get("artist", "")
        track = row.get("track", "")
        log.info("    %s — %s [%s]", artist, track, row["current_playlist"])
    if len(flags) > 20:
        log.info("    ... and %d more", len(flags) - 20)


def live_validate(sp: Spotify, flags: list[dict[str, str]], sub_ids: dict[str, str]) -> dict[str, list[str]]:
    planned: dict[str, list[str]] = defaultdict(list)
    for row in flags:
        planned[row["current_playlist"]].append(row["track_uri"])

    errors: list[str] = []
    for name, uris in sorted(planned.items()):
        live = set(sp.playlist_track_uris(sub_ids[name]))
        missing = [u for u in uris if u not in live]
        if missing:
            for uri in missing[:10]:
                errors.append(f"{uri} is no longer in {name!r}")
            if len(missing) > 10:
                errors.append(f"{len(missing) - 10} more track(s) are no longer in {name!r}")

    if errors:
        raise ValueError(
            "Spotify changed since export/review; nothing will be written. Re-run export.py and review again:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
    return planned


def main() -> None:
    ap = argparse.ArgumentParser(description="Move AI-flagged misfiles to a holding playlist safely.")
    ap.add_argument("review_files", nargs="+", help="AI-produced CSV file(s); globs are supported")
    ap.add_argument("--holding-id", default=os.environ.get(ENV_HOLDING_ID, ""),
                    help=f"holding playlist ID (or set {ENV_HOLDING_ID})")
    ap.add_argument("--library-export", default=DEFAULT_LIBRARY_EXPORT,
                    help=f"matching export CSV (default: {DEFAULT_LIBRARY_EXPORT})")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--apply", action="store_true", help="perform the move; default is preview only")
    args = ap.parse_args()

    holding_id = args.holding_id.strip()
    if not holding_id:
        log.error("Missing holding playlist ID. Pass --holding-id ID or set %s.", ENV_HOLDING_ID)
        sys.exit(2)

    try:
        paths = expand_inputs(args.review_files)
        config = load_config(args.config)
        export_index = load_export(args.library_export)
        flags = collect_flags(paths)
        if not flags:
            log.info("No FLAG rows found. Nothing to do.")
            return
        sub_ids = validate_flags(flags, config, export_index, holding_id)
    except ValueError as exc:
        log.error("%s", exc)
        sys.exit(1)

    summarize(flags)

    sp = Spotify.from_env()
    log.info("Authenticated as: %s", sp.me().get("display_name"))

    try:
        planned = live_validate(sp, flags, sub_ids)
    except ValueError as exc:
        log.error("%s", exc)
        sys.exit(1)

    holding_live = set(sp.playlist_track_uris(holding_id))
    all_uris = list(dict.fromkeys(row["track_uri"] for row in flags))
    to_add = [uri for uri in all_uris if uri not in holding_live]
    already_holding = len(all_uris) - len(to_add)

    log.info("Holding playlist: +%d new track(s), %d already present", len(to_add), already_holding)

    if not args.apply:
        log.info("")
        log.info("PREVIEW ONLY — no Spotify writes were made.")
        rerun = [sys.executable, os.path.basename(__file__), *paths,
                 "--holding-id", holding_id, "--library-export", args.library_export, "--apply"]
        log.info("If this plan is correct, run:")
        log.info("  %s", " ".join(shlex.quote(part) for part in rerun))
        return

    # Transaction-like ordering: put every flagged track somewhere safe first.
    # If an add fails, no source removals have happened yet. A retry is idempotent.
    if to_add:
        sp.add_tracks(holding_id, to_add)
        log.info("Added %d track(s) to holding playlist.", len(to_add))

    removed = 0
    for name, uris in sorted(planned.items()):
        sp.remove_tracks(sub_ids[name], uris)
        removed += len(uris)
        log.info("  %-42s -%d", name, len(uris))

    log.info("")
    log.info("Moved %d flagged track(s) out of source subs into the holding playlist.", removed)
    log.info("Run python sync.py to rebuild masters without these tracks.")
    log.info("Process the holding playlist later: move a track to INBOX for suggest.py, or file it manually.")


if __name__ == "__main__":
    main()
