"""
Export the live contents of every source playlist for AI review.

Reads what is actually in Spotify right now — not the bootstrap CSV — so it
includes anything moved by hand.

Writes to ./review/:
    review_01.md, review_02.md, ...   playlist-coherence review chunks
    library_export.csv                every exported track, one row, for scripting
    duplicate_candidates.csv          machine-readable duplicate candidate groups
    duplicates_review.md              cross-library duplicate review for AI

Duplicate detection deliberately ignores masters: a source track may feed many
masters, but a song should have one canonical source-sub placement.

Usage:
    python export.py                    # coherence review + global duplicate review
    python export.py --duplicates-only  # ONLY global duplicate review
    python export.py --masters          # also show union masters in coherence review
    python export.py --max-tracks 400   # smaller coherence chunks
    python export.py --only "POP ·"     # limit coherence review to a family
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

from spotify_api import BASE, Spotify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONFIG_PATH = "config.json"
OUT_DIR = "review"
DURATION_TOLERANCE_MS = 5000

PROMPT = """\
You are auditing a structured Spotify library. Below are source playlists with
complete track lists. Your job is to identify tracks that factually do not
belong in the playlist where they currently sit.

This output is machine-actionable: every track you FLAG will later be removed
from its current source playlist and moved to a separate holding playlist for
manual re-processing. Therefore false positives are more costly than misses.

CSV FILE PREFERENCE:
- Prefer creating an actual downloadable .csv file rather than only printing CSV
  in the chat. The preferred filename for this review is stated below the prompt.
- Use UTF-8, comma delimiter, and preserve Unicode exactly (including playlist
  names such as Ø, ·, and accented artist/title text).
- Use standard CSV quoting: wrap a field in double quotes when it contains a
  comma, double quote, or newline; escape an embedded double quote as two quotes.
- Do not use semicolons, tabs, Markdown tables, JSON, or spreadsheet formulas.
- Do not add extra columns or change the column order.
- Keep `reason` short and on one line when possible.
- If you cannot create/attach a CSV file, then output ONLY the raw CSV text with
  no Markdown fences, introduction, explanation, or notes after it.
- For reliability, wrap EVERY non-empty text field in double quotes, even when it
  contains no commas. Escape an embedded double quote as two double quotes.
- Every output row MUST contain exactly 8 CSV columns.

Use this exact header:

record_type,current_playlist,track_uri,artist,track,suggested_playlist,confidence,reason

Emit two kinds of rows:

1. FLAG — one row for each track that is genuinely misfiled.
   - current_playlist: copy the playlist name exactly as shown below.
   - track_uri: copy the Spotify URI exactly as shown below.
   - artist / track: copy exactly enough to identify the song.
   - suggested_playlist: advisory only. Use an exact playlist name visible in
     this file if there is a clearly better destination; otherwise use UNSORTED.
   - confidence: high or medium only. If confidence would be low, DO NOT FLAG.
   - reason: short factual reason (wrong genre, language, era, format, etc.).

2. VERDICT — exactly one row per playlist, including clean playlists.
   - current_playlist: the exact playlist name.
   - leave track_uri, artist, track, suggested_playlist, confidence blank.
   - reason must be one of: clean | mostly clean, N strays | incoherent: <short reason>

Rules:
- Judge by factual identity: genre, language, artist context, era, or format.
  Do NOT judge by energy, mood, danceability, tempo, or personal preference.
- Only FLAG tracks you actually recognise or can identify with enough confidence.
  Never infer from a title alone.
- A playlist name is not evidence that its contents are correct.
- Do not FLAG a track merely because another playlist might also fit. FLAG only
  when the current source playlist is materially wrong.
- Never FLAG tracks from playlists whose name begins with "M ·". Masters are
  derived views; source corrections happen in subs.
- Duplicate/version auditing is handled in duplicates_review.md. Do not spend
  this review looking for duplicates unless they are also clearly misfiled.
- Do not output correctly filed tracks as FLAG rows.
- CSV-quote any field containing a comma, quote, or newline.
"""

DUPLICATE_PROMPT = """\
You are reviewing possible duplicate songs in a structured Spotify library.

Library invariant: a song has ONE canonical source-sub placement. It may then
appear in any number of derived master playlists. The groups below contain only
SOURCE SUBS; masters have already been excluded.

Spotify can assign different track IDs to what is effectively the same song or
recording: a single release versus the album release, a reissue/deluxe release,
a remaster, regional relinking, or metadata changes such as a featured artist.
So do NOT decide only from track_uri.

Evidence labels:
- EXACT_URI: identical Spotify track URI occurs more than once. This is a hard duplicate.
- SAME_ISRC: different Spotify track URIs share an ISRC. Strong evidence of the
  same recording, including many single/album/reissue duplicates.
- SAME_SONG_SHAPE: same main artist + normalized base title + duration within
  five seconds. This is only a candidate and needs judgment. It intentionally
  catches things such as remaster labels and feat. metadata differences.

CSV FILE PREFERENCE:
- Prefer creating an actual downloadable CSV file named `duplicate_flags.csv`.
- Use UTF-8, comma delimiter, and preserve Unicode exactly.
- Use standard CSV quoting: quote fields containing commas, double quotes, or
  newlines; escape an embedded double quote as two quotes.
- Do not use semicolons, tabs, Markdown tables, JSON, spreadsheet formulas, or
  extra columns.
- If you cannot create/attach the file, output ONLY raw CSV text with no Markdown
  fences or surrounding prose.
- For reliability, wrap EVERY non-empty text field in double quotes, even when it
  contains no commas. Escape an embedded double quote as two double quotes.
- Every output row MUST contain exactly 8 CSV columns.

Use this exact header:

record_type,current_playlist,track_uri,artist,track,suggested_playlist,confidence,reason

For each group:
- If it is genuinely duplicate/redundant and should be manually resolved, emit
  FLAG rows for ALL source occurrences in that group. All flagged variants will
  be moved to the holding playlist together so I can choose the canonical copy
  and re-file it myself. Use suggested_playlist=UNSORTED.
- For an EXACT_URI group, normally FLAG it unless the evidence shown is malformed.
- For SAME_ISRC, normally FLAG when the titles/artist context support that it is
  the same recording.
- For SAME_SONG_SHAPE, FLAG only when you are confident the entries are the same
  underlying song/recording or redundant release variants.
- Do NOT collapse genuinely distinct versions just because the base title is the
  same: live, acoustic, remix, instrumental, demo, edit, cover, rerecording, and
  substantially different featured-artist versions may be intentional.
- A remaster/reissue or single-vs-album publication of the same recording SHOULD
  be treated as a duplicate candidate even when Spotify IDs differ.
- If uncertain, output nothing for that group. False positives cost more than misses.
- Do not emit VERDICT rows in this duplicate review.
- CSV-quote any field containing a comma, quote, or newline.
"""


def load_config():
    if not os.path.exists(CONFIG_PATH):
        log.error("%s not found. Run bootstrap.py first.", CONFIG_PATH)
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def fetch(sp: Spotify, playlist_id: str) -> list[dict]:
    """Track metadata used by both coherence review and duplicate matching."""
    out = []
    url = f"{BASE}/playlists/{playlist_id}/items"
    params = {
        "limit": 100,
        "fields": (
            "next,items(is_local,item(uri,id,type,name,duration_ms,external_ids(isrc),"
            "artists(id,name),album(id,name,release_date)))"
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
            external_ids = t.get("external_ids") or {}
            out.append({
                "uri": t.get("uri", ""),
                "track_id": t.get("id", ""),
                "artist": ", ".join(a.get("name", "") for a in artists),
                "artist_ids": ";".join(a.get("id", "") for a in artists if a.get("id")),
                "primary_artist": artists[0].get("name", "") if artists else "",
                "primary_artist_id": artists[0].get("id", "") if artists else "",
                "track": t.get("name", ""),
                "album": album.get("name", ""),
                "album_id": album.get("id", ""),
                "release_date": album.get("release_date", ""),
                "year": (album.get("release_date") or "")[:4],
                "duration_ms": t.get("duration_ms") or 0,
                "isrc": external_ids.get("isrc", ""),
            })
        url = data.get("next")
        params = None
    return out


def write_chunk(path: str, groups: list[tuple[str, list[dict]]], index: int, total: int) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(PROMPT)
        f.write(
            f"\nPreferred output filename: `review_flags_{index:02d}.csv`\n"
            f"This is file {index} of {total}. It contains {len(groups)} complete playlists.\n\n"
        )
        for name, tracks in groups:
            f.write(f"\n## {name}  ({len(tracks)} tracks)\n\n")
            for t in tracks:
                year = f" [{t['year']}]" if t["year"] else ""
                album = f" · album: {t['album']}" if t["album"] else ""
                f.write(f"- {t['artist']} — {t['track']}{year}{album}  `{t['uri']}`\n")


# --------------------------------------------------------- duplicate matching


def _ascii_fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def normalize_artist(text: str) -> str:
    text = _ascii_fold(text).lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


# Only remove labels that commonly describe publication/metadata variants of the
# same recording. Deliberately DO NOT strip live/remix/acoustic/demo/instrumental.
VERSION_SUFFIX_RE = re.compile(
    r"(?:\s*[-–—:]\s*|\s*[\[(])(?:"
    r"(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?"
    r"|remaster(?:ed)?(?:\s+\d{4})?"
    r"|album\s+version|single\s+version"
    r"|feat(?:uring)?\.?\s+[^\])]+|ft\.?\s+[^\])]+"
    r")\s*[\])]?\s*$",
    re.IGNORECASE,
)


def normalize_title(text: str) -> str:
    s = _ascii_fold(text).strip()
    # Peel multiple trailing publication markers, e.g. "Song - 2011 Remaster (feat. X)".
    while True:
        newer = VERSION_SUFFIX_RE.sub("", s).strip()
        if newer == s:
            break
        s = newer
    s = s.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def duration_text(ms: int) -> str:
    if not ms:
        return ""
    sec = int(round(ms / 1000))
    return f"{sec // 60}:{sec % 60:02d}"


def duplicate_groups(source_groups: list[tuple[str, list[dict]]]) -> list[dict]:
    """Return connected duplicate-candidate groups across source subs only."""
    nodes: dict[tuple[str, str], dict] = {}
    occurrence_count: Counter[tuple[str, str]] = Counter()

    for playlist, tracks in source_groups:
        for t in tracks:
            key = (playlist, t["uri"])
            occurrence_count[key] += 1
            if key not in nodes:
                row = dict(t)
                row["playlist"] = playlist
                row["base_title"] = normalize_title(t["track"])
                row["primary_artist_key"] = t["primary_artist_id"] or normalize_artist(t["primary_artist"])
                nodes[key] = row

    keys = list(nodes)
    parent = {k: k for k in keys}
    reasons: defaultdict[tuple[str, str], set[str]] = defaultdict(set)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b, reason):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
        reasons[a].add(reason)
        reasons[b].add(reason)

    # Same URI across source locations is an exact duplicate. Repeated occurrence
    # inside one source also becomes a one-node candidate group below.
    by_uri: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
    for k, row in nodes.items():
        by_uri[row["uri"]].append(k)
    for members in by_uri.values():
        if len(members) > 1:
            for k in members:
                reasons[k].add("EXACT_URI")
            for k in members[1:]:
                union(members[0], k, "EXACT_URI")

    for k, count in occurrence_count.items():
        if count > 1:
            reasons[k].add("EXACT_URI")

    # ISRC identifies a recording, so this catches many single/album/reissue IDs.
    by_isrc: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
    for k, row in nodes.items():
        if row["isrc"]:
            by_isrc[row["isrc"].upper()].append(k)
    for members in by_isrc.values():
        if len({nodes[k]["uri"] for k in members}) < 2:
            continue
        for k in members:
            reasons[k].add("SAME_ISRC")
        for k in members[1:]:
            union(members[0], k, "SAME_ISRC")

    # Fuzzy version layer: same main artist + base title + nearly same duration.
    by_shape: defaultdict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for k, row in nodes.items():
        if row["primary_artist_key"] and row["base_title"]:
            by_shape[(row["primary_artist_key"], row["base_title"])].append(k)

    for members in by_shape.values():
        if len({nodes[k]["uri"] for k in members}) < 2:
            continue
        for i, a in enumerate(members):
            da = nodes[a]["duration_ms"]
            for b in members[i + 1:]:
                db = nodes[b]["duration_ms"]
                if da and db and abs(da - db) <= DURATION_TOLERANCE_MS:
                    union(a, b, "SAME_SONG_SHAPE")
                    reasons[a].add("SAME_SONG_SHAPE")
                    reasons[b].add("SAME_SONG_SHAPE")

    components: defaultdict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for k in keys:
        if reasons[k]:
            components[find(k)].append(k)

    result = []
    for members in components.values():
        # A single node is only valid when the identical URI occurs multiple times
        # inside that same source playlist.
        if len(members) == 1 and occurrence_count[members[0]] <= 1:
            continue
        evidence = set()
        for k in members:
            evidence.update(reasons[k])
        # High only when every member has hard evidence. If a SAME_SONG_SHAPE
        # edge pulled an extra version into an otherwise-hard group, keep the
        # whole group medium so the AI does not over-trust the fuzzy member.
        hard = {"EXACT_URI", "SAME_ISRC"}
        confidence = "high" if all(reasons[k] & hard for k in members) else "medium"
        result.append({
            "evidence": sorted(evidence),
            "confidence": confidence,
            "members": sorted(
                [(nodes[k], occurrence_count[k], sorted(reasons[k])) for k in members],
                key=lambda x: (x[0]["primary_artist"].lower(), x[0]["track"].lower(), x[0]["playlist"].lower()),
            ),
        })

    result.sort(key=lambda g: (
        0 if "EXACT_URI" in g["evidence"] else 1 if "SAME_ISRC" in g["evidence"] else 2,
        g["members"][0][0]["primary_artist"].lower(),
        g["members"][0][0]["track"].lower(),
    ))
    return result


def write_duplicate_outputs(out_dir: str, groups: list[dict]) -> None:
    csv_path = os.path.join(out_dir, "duplicate_candidates.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "group_id", "evidence", "member_evidence", "confidence", "playlist", "occurrences",
            "track_uri", "isrc", "primary_artist", "artists", "track", "base_title",
            "album", "year", "duration_ms", "duration",
        ])
        for i, group in enumerate(groups, 1):
            gid = f"DUP-{i:04d}"
            evidence = "+".join(group["evidence"])
            for row, count, member_evidence in group["members"]:
                w.writerow([
                    gid, evidence, "+".join(member_evidence), group["confidence"],
                    row["playlist"], count, row["uri"], row["isrc"], row["primary_artist"], row["artist"],
                    row["track"], row["base_title"], row["album"], row["year"],
                    row["duration_ms"], duration_text(row["duration_ms"]),
                ])

    md_path = os.path.join(out_dir, "duplicates_review.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(DUPLICATE_PROMPT)
        f.write(f"\nCandidate groups: {len(groups)}\n\n")
        for i, group in enumerate(groups, 1):
            gid = f"DUP-{i:04d}"
            f.write(f"\n## {gid} — {' + '.join(group['evidence'])} — {group['confidence']}\n\n")
            for row, count, member_evidence in group["members"]:
                count_text = f" · occurrences in this sub: {count}" if count > 1 else ""
                member_ev = f" · evidence: {'+'.join(member_evidence)}"
                isrc = f" · ISRC {row['isrc']}" if row["isrc"] else ""
                album = f" · album: {row['album']} ({row['year']})" if row["album"] else ""
                dur = f" · {duration_text(row['duration_ms'])}" if row["duration_ms"] else ""
                f.write(
                    f"- [{row['playlist']}] {row['artist']} — {row['track']}"
                    f"{album}{dur}{isrc}{member_ev}{count_text}  `{row['uri']}`\n"
                )

    log.info("  wrote %s — %d candidate group(s)", csv_path, len(groups))
    log.info("  wrote %s", md_path)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--duplicates-only",
        action="store_true",
        help="only write duplicate_candidates.csv and duplicates_review.md; skip coherence review",
    )
    ap.add_argument("--masters", action="store_true", help="include union masters in coherence review")
    ap.add_argument("--max-tracks", type=int, default=700,
                    help="soft cap per coherence-review file (default 700)")
    ap.add_argument("--only", default="", help="only coherence-review playlists starting with this")
    ap.add_argument("--out", default=OUT_DIR)
    return ap


def _ask_yes_no(prompt: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{d}]: ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


def interactive(config: dict):
    """Ask a few plain-language questions instead of requiring flags to be
    remembered, and return a Namespace matching what build_parser() produces."""
    print("What do you want to export?")
    print("  1. Full review (coherence + duplicates) — the usual weekly/monthly run")
    print("  2. Duplicates only")
    print("  3. Coherence review for one family only, e.g. 'POP ·'")
    print("  4. Quit")
    choice = input("> ").strip()

    if choice == "4" or not choice:
        return None

    ns = argparse.Namespace(duplicates_only=False, masters=False, max_tracks=700, only="", out=OUT_DIR)

    if choice == "2":
        ns.duplicates_only = True
        return ns

    if choice == "3":
        prefix = input("Family prefix (must match the start of playlist names, e.g. 'POP ·'): ").strip()
        if not prefix:
            print("No prefix entered, cancelling.")
            return None
        ns.only = prefix
    elif choice != "1":
        print("Not a valid choice.")
        return None

    ns.masters = _ask_yes_no("Also include union masters in the coherence review?", default=False)
    raw_max = input(f"Soft track cap per review file [{ns.max_tracks}]: ").strip()
    if raw_max:
        try:
            ns.max_tracks = int(raw_max)
        except ValueError:
            print(f"  Not a number, keeping default {ns.max_tracks}.")
    return ns


def run(args) -> None:
    config = load_config()
    source_specs = [s for s in config["subs"] if s.get("enabled", True)]

    # Duplicate audit must always see ALL enabled source subs, independent of --only,
    # because cross-family duplicates are exactly what we want to catch.
    sp = Spotify.from_env()
    log.info("Authenticated as: %s", sp.me().get("display_name"))
    log.info("Fetching %d source sub(s)...", len(source_specs))

    source_groups: list[tuple[str, list[dict]]] = []
    source_cache: dict[str, list[dict]] = {}
    for pl in sorted(source_specs, key=lambda p: p["name"]):
        tracks = fetch(sp, pl["id"])
        source_cache[pl["name"]] = tracks
        source_groups.append((pl["name"], tracks))
        log.info("  %-42s %4d", pl["name"], len(tracks))

    os.makedirs(args.out, exist_ok=True)

    if args.duplicates_only:
        if args.masters or args.only or args.max_tracks != 700:
            log.info("--duplicates-only: coherence options --masters/--only/--max-tracks are ignored")
        dupes = duplicate_groups(source_groups)
        write_duplicate_outputs(args.out, dupes)
        total = sum(len(t) for _, t in source_groups)
        log.info("")
        log.info("Duplicate-only scan: %d source-track occurrences across %d enabled subs",
                 total, len(source_groups))
        log.info("Duplicate candidate groups: %d", len(dupes))
        log.info("Review %s", os.path.join(args.out, "duplicates_review.md"))
        return

    # Build the normal coherence review selection, reusing already-fetched sources.
    wanted = list(source_specs)
    if args.masters:
        wanted += [m for m in config["masters"] if m.get("type") != "rule" and m.get("enabled", True)]
    if args.only:
        wanted = [p for p in wanted if p["name"].startswith(args.only)]
    if not wanted:
        log.error("No playlists matched coherence review selection.")
        sys.exit(1)

    groups = []
    for pl in sorted(wanted, key=lambda p: p["name"]):
        if pl["name"] in source_cache:
            tracks = source_cache[pl["name"]]
        else:
            tracks = fetch(sp, pl["id"])
            log.info("  %-42s %4d", pl["name"], len(tracks))
        if tracks:
            groups.append((pl["name"], tracks))

    chunks, cur, n = [], [], 0
    for name, tracks in groups:
        if cur and n + len(tracks) > args.max_tracks:
            chunks.append(cur)
            cur, n = [], 0
        cur.append((name, tracks))
        n += len(tracks)
    if cur:
        chunks.append(cur)

    for i, chunk in enumerate(chunks, 1):
        path = os.path.join(args.out, f"review_{i:02d}.md")
        write_chunk(path, chunk, i, len(chunks))
        log.info("  wrote %s — %d playlists, %d tracks",
                 path, len(chunk), sum(len(t) for _, t in chunk))

    csv_path = os.path.join(args.out, "library_export.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "playlist", "artist", "primary_artist", "track", "album", "year",
            "duration_ms", "isrc", "track_uri", "track_id", "artist_ids", "album_id",
        ])
        # library_export is SOURCE SUBS only so review_apply cannot accidentally
        # validate a master occurrence as a source correction.
        for name, tracks in source_groups:
            for t in tracks:
                w.writerow([
                    name, t["artist"], t["primary_artist"], t["track"], t["album"], t["year"],
                    t["duration_ms"], t["isrc"], t["uri"], t["track_id"], t["artist_ids"], t["album_id"],
                ])

    dupes = duplicate_groups(source_groups)
    write_duplicate_outputs(args.out, dupes)

    total = sum(len(t) for _, t in source_groups)
    log.info("")
    log.info("%d source-track occurrences across %d subs -> %d review file(s)",
             total, len(source_groups), len(chunks))
    log.info("Duplicate candidate groups: %d", len(dupes))
    log.info("Review coherence files one at a time, then review duplicates_review.md separately.")


def main() -> None:
    if len(sys.argv) == 1:
        config = load_config()
        args = interactive(config)
        if args is None:
            return
    else:
        args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()