"""
One-time (but re-runnable) bootstrap.

Reads track_assignments.csv and:
  1. Creates every sub-playlist and master playlist that does not exist yet.
  2. Fills each sub-playlist with its assigned tracks.
  3. Writes config.json (the structure sync.py reads).
  4. Writes features.json (local audio-feature cache for rule-based masters).
  5. Writes playlist_ids.json (name -> id, so re-runs never create duplicates).

It NEVER touches playlists that are not in playlist_ids.json, so your existing
library is left completely alone.

Masters are created empty on purpose. Run sync.py afterwards to fill them.

Usage:
    python bootstrap.py --dry-run     # show what would happen, change nothing
    python bootstrap.py               # do it
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from collections import OrderedDict, defaultdict

from spotify_api import Spotify, token_from_env

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CSV_PATH = "track_assignments.csv"
IDS_PATH = "playlist_ids.json"
CONFIG_PATH = "config.json"
FEATURES_PATH = "features.json"

INBOX_NAME = "INBOX"
INBOX_DESC = "Unfiled. Empty me weekly. Feeds no master."

# Subs listed here are holding pens: created, but wired to no master.
NON_SOURCE_SUBS = {"POP · Unsorted Inbox"}

# Rule-based masters: computed from the local feature cache, not from subs.
# Every rule master is the union of tracks matching its predicate, drawn from
# all enabled subs. These give you energy-sliced views with zero filing cost.
RULE_MASTERS = OrderedDict(
    [
        (
            "M · PEAK (auto)",
            {
                "description": "Auto: top-decile energy. Rule-generated, never filed by hand.",
                "rule": {"energy_min": 0.91},
            },
        ),
        (
            "M · SPRINT 160+ (auto)",
            {
                "description": "Auto: fast and loud. Rule-generated, never filed by hand.",
                "rule": {"tempo_min": 158, "energy_min": 0.80},
            },
        ),
        (
            "M · CALM & CONTENT (auto)",
            {
                "description": "Auto: low energy, positive mood. Rule-generated.",
                "rule": {"energy_max": 0.45, "valence_min": 0.50},
            },
        ),
    ]
)

MASTER_DESCRIPTIONS = {
    "M · POP GJENNOM TIÅRENE": "The Anglo-pop spine, 1960s to now. Synced. Do not edit by hand.",
    "M · PARTY & PREGAME": "Vors and singalong. Synced. Do not edit by hand.",
    "M · SUMMER & DRIVE": "Warm, forward motion. Synced. Do not edit by hand.",
    "M · ØSTASIA (Asia Pop)": "Korea, Japan, and the rest of Asia. Synced. Do not edit by hand.",
    "M · ROCK & HEAVY": "Guitars, all languages. Synced. Do not edit by hand.",
    "M · NORSK & NORDISK": "Norwegian first, Scandi neighbours after. Synced. Do not edit by hand.",
    "M · RETRO & OLDIES": "Mostly pre-1995. Synced. Do not edit by hand.",
    "M · SLOW & SOFT": "Quiet but still songs. Synced. Do not edit by hand.",
    "M · HIP HOP": "Rap in every language. Synced. Do not edit by hand.",
    "M · RAVE & HARD": "Hardstyle, phonk, hypertechno, metal. Synced. Do not edit by hand.",
    "M · SHITPOST & KOS": "Deliberately silly. Synced. Do not edit by hand.",
    "M · FOCUS & BAKGRUNN": "Instrumental background. Synced. Do not edit by hand.",
    "M · VINTAGE SWING & JAZZ": "The swing lineage, old and electro. Synced. Do not edit by hand.",
    "M · JUL (seasonal)": "Seasonal. Disable 11 months a year. Synced. Do not edit by hand.",
}

# Seasonal masters start disabled so they do not clutter the library in August.
SEASONAL_MASTERS = {"M · JUL (seasonal)"}


# --------------------------------------------------------------------- CSV


def read_csv(path: str):
    if not os.path.exists(path):
        log.error("%s not found. Put the assignment CSV next to this script.", path)
        sys.exit(1)

    sub_tracks: dict[str, list[str]] = defaultdict(list)
    master_sources: dict[str, list[str]] = defaultdict(list)
    features: dict[str, dict] = {}
    seen: dict[str, str] = {}
    dupes: list[tuple[str, str, str]] = []

    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            uri = (row.get("track_uri") or "").strip()
            sub = (row.get("sub_playlist") or "").strip()
            if not uri or not sub:
                continue

            # Invariant: one track, one sub. First occurrence wins; rest reported.
            if uri in seen:
                if seen[uri] != sub:
                    dupes.append((uri, seen[uri], sub))
                continue
            seen[uri] = sub
            sub_tracks[sub].append(uri)

            for master in (row.get("masters") or "").split(";"):
                master = master.strip()
                if not master:
                    continue
                if sub not in master_sources[master]:
                    master_sources[master].append(sub)

            def num(key):
                try:
                    return float(row[key])
                except (KeyError, TypeError, ValueError):
                    return None

            features[uri] = {
                "energy": num("energy"),
                "valence": num("valence"),
                "tempo": num("tempo"),
                "danceability": num("danceability"),
            }

    return sub_tracks, master_sources, features, dupes


# ----------------------------------------------------------------- helpers


def load_ids() -> dict[str, str]:
    if os.path.exists(IDS_PATH):
        with open(IDS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def ensure_playlist(sp: Spotify, ids: dict, existing: dict, name: str, desc: str) -> str:
    """Return the id for `name`, creating the playlist only if we must.

    Order of preference:
      1. An id we already recorded in playlist_ids.json (ours, definitely).
      2. An existing playlist of ours with exactly that name (adoption, so a
         half-finished run does not create a second copy).
      3. Create it.
    """
    if name in ids:
        return ids[name]

    if name in existing:
        log.info("  Adopting existing playlist %r (%s)", name, existing[name])
        ids[name] = existing[name]
        return existing[name]

    pid = sp.create_playlist(name, desc)
    log.info("  Created %r -> %s", name, pid)
    ids[name] = pid
    return pid


# -------------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="change nothing")
    ap.add_argument("--csv", default=CSV_PATH)
    ap.add_argument(
        "--public", action="store_true", help="create playlists as public (default private)"
    )
    args = ap.parse_args()

    sub_tracks, master_sources, features, dupes = read_csv(args.csv)

    if dupes:
        log.error("INVARIANT VIOLATION: %d track(s) assigned to more than one sub.", len(dupes))
        for uri, a, b in dupes[:20]:
            log.error("  %s -> %r and %r", uri, a, b)
        log.error("Fix the CSV before bootstrapping. Nothing was created.")
        sys.exit(1)

    total = sum(len(v) for v in sub_tracks.values())
    log.info(
        "CSV: %d unique tracks, %d subs, %d union-masters",
        total,
        len(sub_tracks),
        len(master_sources),
    )

    sp = Spotify(token_from_env(), dry_run=args.dry_run)
    log.info("Authenticated as: %s", sp.me().get("display_name"))

    ids = load_ids()
    existing = sp.my_playlists()

    # 1. Inbox --------------------------------------------------------------
    log.info("Ensuring inbox...")
    ensure_playlist(sp, ids, existing, INBOX_NAME, INBOX_DESC)

    # 2. Subs ---------------------------------------------------------------
    log.info("Ensuring %d sub-playlists...", len(sub_tracks))
    for name in sorted(sub_tracks):
        feeds = [m for m, s in master_sources.items() if name in s]
        desc = ("Feeds: " + ", ".join(feeds)) if feeds else "Holding pen. Feeds no master."
        ensure_playlist(sp, ids, existing, name, desc)

    # 3. Masters ------------------------------------------------------------
    log.info("Ensuring %d union-masters and %d rule-masters...",
             len(master_sources), len(RULE_MASTERS))
    for name in sorted(master_sources):
        ensure_playlist(sp, ids, existing, name,
                        MASTER_DESCRIPTIONS.get(name, "Synced. Do not edit by hand."))
    for name, spec in RULE_MASTERS.items():
        ensure_playlist(sp, ids, existing, name, spec["description"])

    if not args.dry_run:
        save_json(IDS_PATH, ids)
        log.info("Wrote %s (%d playlists tracked)", IDS_PATH, len(ids))

    # 4. Fill subs ----------------------------------------------------------
    log.info("Filling sub-playlists...")
    for name in sorted(sub_tracks):
        pid = ids[name]
        wanted = sub_tracks[name]

        current = [] if args.dry_run and pid.startswith("DRYRUN_") else sp.playlist_track_uris(pid)
        missing = [u for u in wanted if u not in set(current)]

        if not missing:
            log.info("  %-42s %4d tracks, already complete", name, len(wanted))
            continue

        log.info("  %-42s adding %d of %d", name, len(missing), len(wanted))
        sp.add_tracks(pid, missing)

    # 5. config.json --------------------------------------------------------
    config = {
        "_comment": "Generated by bootstrap.py. Subs are declared once; masters "
                    "reference them by name. Edit by hand after generation.",
        "settings": {
            "abort_if_union_empty": True,
            "fail_on_duplicate_track": True,
            "min_sub_size": 5,
            "max_sub_size": 150,
        },
        "inbox": {"id": ids[INBOX_NAME], "name": INBOX_NAME},
        "subs": [
            {
                "name": name,
                "id": ids[name],
                "enabled": True,
                "in_masters": sorted(m for m, s in master_sources.items() if name in s),
            }
            for name in sorted(sub_tracks)
        ],
        "masters": [
            {
                "name": name,
                "id": ids[name],
                "enabled": name not in SEASONAL_MASTERS,
                "type": "union",
                "include": sorted(master_sources[name]),
            }
            for name in sorted(master_sources)
        ]
        + [
            {
                "name": name,
                "id": ids[name],
                "enabled": True,
                "type": "rule",
                "rule": spec["rule"],
            }
            for name, spec in RULE_MASTERS.items()
        ],
    }

    if args.dry_run:
        log.info("[dry-run] would write %s and %s", CONFIG_PATH, FEATURES_PATH)
    else:
        if os.path.exists(CONFIG_PATH):
            os.replace(CONFIG_PATH, CONFIG_PATH + ".bak")
            log.info("Backed up existing config to %s.bak", CONFIG_PATH)
        save_json(CONFIG_PATH, config)
        save_json(FEATURES_PATH, features)
        log.info("Wrote %s and %s (%d feature rows)", CONFIG_PATH, FEATURES_PATH, len(features))

    log.info("")
    log.info("Bootstrap complete. %d write calls made.", sp.writes)
    log.info("Masters are still empty. Run:  python sync.py")
    if SEASONAL_MASTERS:
        log.info("Note: %s created disabled.", ", ".join(sorted(SEASONAL_MASTERS)))


if __name__ == "__main__":
    main()