"""
INBOX tools, now that suggest.py is retired. All read-only — no Spotify writes.

Two things this does:

  1. Look at what's in INBOX right now.

         python inbox_export.py
         python inbox_export.py --csv                 # also review/inbox_export.csv

  2. Generate an AI placement prompt, the input half of the recommendation
     loop that apply_inbox_recommendations.py applies:

         python inbox_export.py --prompt

     Writes review/inbox_placement_prompt.md: the prompt, the exact list of
     valid destination sub-playlists (enabled subs only — masters and INBOX
     are excluded, since apply_inbox_recommendations.py rejects both), and
     every INBOX track. Paste that file into a conversation, save the AI's
     CSV reply as spotify_inbox_placement_recommendations.csv, then:

         python apply_inbox_recommendations.py --dry-run
         python apply_inbox_recommendations.py
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys

from spotify_api import BASE, Spotify
from sync import load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

OUT_DIR = "review"
DEFAULT_CSV = os.path.join(OUT_DIR, "inbox_export.csv")
DEFAULT_PROMPT = os.path.join(OUT_DIR, "inbox_placement_prompt.md")

PROMPT = """\
You are filing tracks out of a Spotify INBOX into existing source
sub-playlists. Below is every track currently in INBOX, plus the exact list
of valid destination sub-playlists.

This output is machine-actionable: it will be read by
apply_inbox_recommendations.py, which moves every MOVE row into its
recommended_playlist and removes it from INBOX. Rows below the confidence
threshold (default 0.65) are left in INBOX untouched, same as KEEP_INBOX.

CSV FILE PREFERENCE:
- Prefer creating an actual downloadable .csv file rather than only printing
  CSV in the chat. Preferred filename:
  spotify_inbox_placement_recommendations.csv
- Use UTF-8, comma delimiter, and preserve Unicode exactly (playlist names
  contain ·, Ø, and accented artist/title text).
- Use standard CSV quoting: wrap a field in double quotes when it contains a
  comma, double quote, or newline; escape an embedded double quote as two.
- Do not use semicolons, tabs, Markdown tables, JSON, or spreadsheet formulas.
- Do not add extra columns or change the column order.
- If you cannot create/attach a CSV file, output ONLY the raw CSV text with
  no Markdown fences, introduction, or notes after it.

Use this exact header:

track_uri,recommended_playlist,action,confidence_score,artist,track

Emit exactly one row per INBOX track listed below:

- track_uri: copy the Spotify URI exactly as shown below.
- action: MOVE or KEEP_INBOX.
- recommended_playlist: required when action is MOVE, and MUST be an EXACT
  name copied from the "Valid destination sub-playlists" list below.
  NEVER a playlist starting with "M ·" — those are derived masters,
  write-only by sync.py, and apply_inbox_recommendations.py will refuse
  the entire batch if one appears here. NEVER "INBOX" itself. Leave blank
  when action is KEEP_INBOX.
- confidence_score: 0.00-1.00, your honest confidence. Rows below the
  threshold are treated the same as KEEP_INBOX, so it is safe to be honest
  rather than optimistic.
- artist / track: copy exactly enough to identify the song.

Rules:
- Judge by factual identity: genre, language, artist context, era, or
  format — the same basis the existing subs were filed on. Do NOT judge by
  energy, mood, danceability, tempo, or personal preference.
- If you do not recognise the track, or nothing in the valid list is a good
  fit, use KEEP_INBOX. A wrong MOVE costs more than a track staying in
  INBOX one more week.
- Never invent a destination name that is not in the valid list.
- CSV-quote any field containing a comma, quote, or newline.
"""


def fetch_inbox_tracks(sp: Spotify, playlist_id: str) -> list[dict]:
    out: list[dict] = []
    url = f"{BASE}/playlists/{playlist_id}/items"
    params = {
        "limit": 100,
        "fields": (
            "next,items(added_at,is_local,item(uri,type,name,duration_ms,"
            "artists(name),album(name,release_date)))"
        ),
    }
    while url:
        data = sp._request("GET", url, params=params).json()
        for row in data.get("items", []):
            if row.get("is_local"):
                continue
            t = row.get("item") or row.get("track")
            if not isinstance(t, dict) or t.get("type") != "track" or not t.get("uri"):
                continue
            album = t.get("album") or {}
            artists = t.get("artists") or []
            out.append({
                "added_at": row.get("added_at", ""),
                "artist": ", ".join(a.get("name", "") for a in artists),
                "track": t.get("name", ""),
                "album": album.get("name", ""),
                "year": (album.get("release_date") or "")[:4],
                "duration_ms": t.get("duration_ms") or 0,
                "track_uri": t.get("uri", ""),
            })
        url = data.get("next")
        params = None
    return out


def duration_text(ms: int) -> str:
    if not ms:
        return ""
    sec = int(round(ms / 1000))
    return f"{sec // 60}:{sec % 60:02d}"


def write_csv(path: str, tracks: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["artist", "track", "album", "year", "duration_ms", "added_at", "track_uri"])
        for t in tracks:
            w.writerow([t["artist"], t["track"], t["album"], t["year"],
                        t["duration_ms"], t["added_at"], t["track_uri"]])


def write_prompt(path: str, tracks: list[dict], sub_names: list[str]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(PROMPT)
        f.write(f"\nValid destination sub-playlists ({len(sub_names)}):\n")
        for name in sub_names:
            f.write(f"- {name}\n")
        f.write(f"\n## INBOX  ({len(tracks)} tracks)\n\n")
        for t in tracks:
            year = f" [{t['year']}]" if t["year"] else ""
            album = f" · album: {t['album']}" if t["album"] else ""
            f.write(f"- {t['artist']} — {t['track']}{year}{album}  `{t['track_uri']}`\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", nargs="?", const=DEFAULT_CSV, default=None,
                     help=f"also write a plain CSV (default path if given with no value: {DEFAULT_CSV})")
    ap.add_argument("--prompt", nargs="?", const=DEFAULT_PROMPT, default=None,
                     help=f"write an AI placement prompt instead of printing (default path: {DEFAULT_PROMPT})")
    args = ap.parse_args()

    config = load_config()
    inbox = config.get("inbox")
    if not inbox:
        log.error("No inbox configured in config.json.")
        sys.exit(1)

    sp = Spotify.from_env()
    log.info("Authenticated as: %s", sp.me().get("display_name"))

    tracks = fetch_inbox_tracks(sp, inbox["id"])

    if not tracks:
        log.info("INBOX is empty.")
        return

    if args.prompt:
        sub_names = sorted(s["name"] for s in config["subs"] if s.get("enabled", True))
        write_prompt(args.prompt, tracks, sub_names)
        log.info("INBOX: %d track(s), %d valid destination sub(s)", len(tracks), len(sub_names))
        log.info("Wrote %s", args.prompt)
        log.info("Paste it into a conversation, save the reply as "
                 "spotify_inbox_placement_recommendations.csv, then run "
                 "apply_inbox_recommendations.py --dry-run")
        return

    log.info("INBOX: %d track(s)\n", len(tracks))
    for t in tracks:
        year = f" [{t['year']}]" if t["year"] else ""
        dur = f" ({duration_text(t['duration_ms'])})" if t["duration_ms"] else ""
        print(f"  {t['artist'][:32]:34s} {t['track'][:40]:42s}{year}{dur}")

    if args.csv:
        write_csv(args.csv, tracks)
        log.info("")
        log.info("Wrote %s", args.csv)

    log.info("")
    log.info("To generate filing recommendations for these, run: python inbox_export.py --prompt")


if __name__ == "__main__":
    main()