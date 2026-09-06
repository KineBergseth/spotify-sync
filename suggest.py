"""
Inbox filing assistant.

Suggests a sub-playlist for each track sitting in INBOX, then — once you have
confirmed the choices — moves them. It never files anything on its own: the
whole point of manual filing is that a wrong tag must not become permanent
structure, so a human confirms every row.

Two-step workflow:

    python suggest.py            # read inbox, write inbox_suggestions.csv
    <edit the "chosen" column>   # accept, change, or blank to skip
    python suggest.py --apply    # move the confirmed tracks

Signals, strongest first:
  1. Artist history  - you already filed this artist somewhere
  2. Album history   - you already filed this album somewhere
  3. Genre profile   - where tracks with these genre tags usually land
  4. Feature nearness- how close energy/valence/tempo sit to each sub's centre

Artist history dominates on purpose. Spotify's genre tags are wrong often
enough to matter (it tags Stray Kids as "Noise Music"), but your own past
decision about an artist is by definition consistent with how you file.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
from collections import Counter, defaultdict

from spotify_api import BASE, Spotify, SpotifyError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONFIG_PATH = "config.json"
FEATURES_PATH = "features.json"
MODEL_PATH = "suggest_model.json"
OUT_PATH = "inbox_suggestions.csv"

W_ARTIST = 100.0
W_ALBUM = 55.0
W_GENRE = 40.0
W_FEATURES = 12.0
TOP_N = 3


# ------------------------------------------------------------------ loading


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")


def playlist_items_full(sp: Spotify, playlist_id: str) -> list[dict]:
    """Full track objects for a playlist (needs artist names, so no field filter)."""
    out: list[dict] = []
    url = f"{BASE}/playlists/{playlist_id}/items"
    params = {"limit": 100}
    while url:
        data = sp._request("GET", url, params=params).json()
        for row in data.get("items", []):
            if row.get("is_local"):
                continue
            track = row.get("item") or row.get("track")
            if isinstance(track, dict) and track.get("type") == "track" and track.get("uri"):
                out.append(track)
        url = data.get("next")
        params = None
    return out


# -------------------------------------------------------------- model build


def build_model(sp: Spotify, config: dict, features: dict) -> dict:
    """Learn from what is already filed. Reads live subs, so it improves every
    time you correct a mistake."""
    artist = defaultdict(Counter)   # artist_id -> Counter(sub)
    album = defaultdict(Counter)    # album_id  -> Counter(sub)
    artist_names: dict[str, str] = {}
    centroid: dict[str, dict] = {}

    for sub in config["subs"]:
        if not sub.get("enabled", True):
            continue
        name = sub["name"]
        tracks = playlist_items_full(sp, sub["id"])
        log.info("  %-42s %4d tracks", name, len(tracks))

        vals = defaultdict(list)
        for track in tracks:
            for art in track.get("artists") or []:
                if art.get("id"):
                    artist[art["id"]][name] += 1
                    artist_names[art["id"]] = art.get("name", "")
            alb = (track.get("album") or {}).get("id")
            if alb:
                album[alb][name] += 1
            feat = features.get(track["uri"])
            if feat:
                for key in ("energy", "valence", "tempo"):
                    if feat.get(key) is not None:
                        vals[key].append(feat[key])

        if vals.get("energy"):
            centroid[name] = {k: sum(v) / len(v) for k, v in vals.items() if v}

    return {
        "artist": {k: dict(v) for k, v in artist.items()},
        "album": {k: dict(v) for k, v in album.items()},
        "artist_names": artist_names,
        "centroid": centroid,
    }


def genre_profile(sp: Spotify, model: dict, cache: dict) -> dict:
    """genre -> Counter(sub), derived from artist history + artist genres."""
    profile = defaultdict(Counter)
    for artist_id, subs in model["artist"].items():
        genres = cache.get(artist_id)
        if genres is None:
            continue
        for genre in genres:
            for sub, count in subs.items():
                profile[genre.lower()][sub] += count
    return {k: dict(v) for k, v in profile.items()}


def fetch_artist_genres(sp: Spotify, artist_ids: list[str], cache: dict) -> None:
    """GET /artists/{id} one at a time. The batch endpoint was removed in the
    February 2026 migration, so this is the only way. Results are cached.

    Only a genuine 404 (artist really has no genre data) is cached as an
    empty list. Anything else — a rate limit that outlasted the client's own
    retries, a dropped connection, a 500 — is left uncached so the next run
    tries again, instead of permanently recording "no genres" for an artist
    we simply failed to ask.
    """
    todo = [a for a in artist_ids if a not in cache]
    if not todo:
        return
    log.info("Fetching genres for %d new artist(s)...", len(todo))
    failed = 0
    for artist_id in todo:
        try:
            data = sp._request("GET", f"{BASE}/artists/{artist_id}").json()
            cache[artist_id] = [g.lower() for g in (data.get("genres") or [])]
        except SpotifyError as exc:
            if " -> 404 " in str(exc):
                cache[artist_id] = []
            else:
                failed += 1
                log.warning("  artist %s: %s (will retry next run)", artist_id, exc)
    if failed:
        log.warning("  %d artist(s) could not be fetched, will retry next run", failed)


# ----------------------------------------------------------------- scoring


def score_track(track: dict, model: dict, gprofile: dict, gcache: dict,
                features: dict) -> list[tuple[str, float, str]]:
    scores: dict[str, float] = defaultdict(float)
    why: dict[str, list[str]] = defaultdict(list)

    evidence: dict[str, int] = defaultdict(int)

    # 1. Artist history
    for art in track.get("artists") or []:
        hist = model["artist"].get(art.get("id") or "")
        if not hist:
            continue
        total = sum(hist.values())
        for sub, count in hist.items():
            scores[sub] += W_ARTIST * (count / total)
            evidence[sub] += count
            why[sub].append(f"{art.get('name')}: {count} filed here")

    # 2. Album history
    alb = (track.get("album") or {}).get("id")
    hist = model["album"].get(alb or "")
    if hist:
        total = sum(hist.values())
        for sub, count in hist.items():
            scores[sub] += W_ALBUM * (count / total)
            why[sub].append("same album")

    # 3. Genre profile
    genres = []
    for art in track.get("artists") or []:
        genres.extend(gcache.get(art.get("id") or "", []))
    for genre in set(genres):
        hist = gprofile.get(genre)
        if not hist:
            continue
        total = sum(hist.values())
        for sub, count in hist.items():
            scores[sub] += (W_GENRE / max(len(set(genres)), 1)) * (count / total)
            why[sub].append(f"genre {genre}")

    # 4. Feature nearness (weak tiebreak only)
    feat = features.get(track["uri"])
    if feat and feat.get("energy") is not None:
        for sub, centre in model["centroid"].items():
            if sub not in scores:
                continue
            dist = 0.0
            for key, span in (("energy", 1.0), ("valence", 1.0), ("tempo", 60.0)):
                if feat.get(key) is not None and centre.get(key) is not None:
                    dist += ((feat[key] - centre[key]) / span) ** 2
            scores[sub] += W_FEATURES * math.exp(-dist)

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:TOP_N]
    return [(sub, round(val, 1), "; ".join(dict.fromkeys(why[sub]))[:90], evidence[sub])
            for sub, val in ranked]


MIN_EVIDENCE = 3      # tracks by this artist already filed in the winning sub
MIN_SCORE = 55.0      # below this, nothing much matched
MIN_MARGIN = 25.0     # clear separation from the runner-up

FALLBACK_PREFIXES = ("POP · ",)  # era buckets: where unknown tracks drift


def judge(ranked) -> tuple[bool, str]:
    """Is the top suggestion trustworthy?

    A single weak clue with no rival is NOT confidence — that was the original
    bug here, and it let one-off artists sail into POP era buckets unflagged.
    Confidence needs positive evidence, not merely an absent competitor.
    """
    if not ranked:
        return False, "nothing matched"

    sub, score, _, evidence = ranked[0]
    margin = score - ranked[1][1] if len(ranked) > 1 else score

    if evidence < MIN_EVIDENCE:
        return False, f"only {evidence} track(s) by this artist filed there"
    if score < MIN_SCORE:
        return False, "weak match"
    if margin < MIN_MARGIN:
        return False, f"close call vs {ranked[1][0]}"
    if sub.startswith(FALLBACK_PREFIXES) and evidence < MIN_EVIDENCE * 2:
        return False, "era-bucket fallback, check genre first"
    return True, ""


# ------------------------------------------------------------------ actions


def do_suggest(sp: Spotify, config: dict, features: dict, refresh: bool) -> None:
    inbox = config.get("inbox")
    if not inbox:
        log.error("No inbox in config.json.")
        sys.exit(1)

    tracks = playlist_items_full(sp, inbox["id"])
    if not tracks:
        log.info("Inbox is empty. Nothing to do.")
        return
    log.info("Inbox: %d track(s)", len(tracks))

    model = load_json(MODEL_PATH) if not refresh else None
    if model is None:
        log.info("Building model from filed subs (this reads every sub once)...")
        model = build_model(sp, config, features)
        model["genre_cache"] = {}
        save_json(MODEL_PATH, model)
    gcache = model.setdefault("genre_cache", {})

    known = {a for t in tracks for a in [x.get("id") for x in (t.get("artists") or [])] if a}
    fetch_artist_genres(sp, sorted(known), gcache)
    save_json(MODEL_PATH, model)

    gprofile = genre_profile(sp, model, gcache)

    rows = []
    log.info("")
    for track in tracks:
        artists = ", ".join(a.get("name", "") for a in (track.get("artists") or []))
        ranked = score_track(track, model, gprofile, gcache, features)

        top = ranked[0][0] if ranked else ""
        confident, reason = judge(ranked)
        flag = " " if confident else "?"

        log.info("%s %-26s %-26s -> %s", flag, artists[:26], track.get("name", "")[:26],
                 " | ".join(f"{s} ({v})" for s, v, _, _ in ranked) or "no suggestion")

        rows.append({
            "chosen": top,
            "confident": "yes" if confident else "no",
            "check_why": "" if confident else reason,
            "artist": artists,
            "track": track.get("name", ""),
            "suggestion_1": ranked[0][0] if len(ranked) > 0 else "",
            "suggestion_2": ranked[1][0] if len(ranked) > 1 else "",
            "suggestion_3": ranked[2][0] if len(ranked) > 2 else "",
            "why": ranked[0][2] if ranked else "",
            "track_uri": track["uri"],
        })

    with open(OUT_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    unsure = sum(1 for r in rows if r["confident"] == "no")
    log.info("")
    log.info("Wrote %s — %d row(s), %d marked '?' for you to check.", OUT_PATH, len(rows), unsure)
    log.info("Edit the 'chosen' column (blank = leave in inbox), then: python suggest.py --apply")


def do_apply(sp: Spotify, config: dict) -> None:
    if not os.path.exists(OUT_PATH):
        log.error("%s not found. Run without --apply first.", OUT_PATH)
        sys.exit(1)

    # Disabled subs are out of the system; treat them the same as an unknown
    # name so a stray or stale "chosen" value can't quietly refile into one.
    sub_ids = {s["name"]: s["id"] for s in config["subs"] if s.get("enabled", True)}
    inbox = config["inbox"]

    with open(OUT_PATH, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    planned = defaultdict(list)
    skipped = 0
    for row in rows:
        chosen = (row.get("chosen") or "").strip()
        uri = (row.get("track_uri") or "").strip()
        if not chosen or not uri:
            skipped += 1
            continue
        if chosen not in sub_ids:
            log.error("Unknown sub %r for %s — %s", chosen, row.get("artist"), row.get("track"))
            sys.exit(1)
        if uri not in planned[chosen]:      # same URI listed twice in the CSV
            planned[chosen].append(uri)

    if not planned:
        log.info("Nothing chosen. %d row(s) left in inbox.", skipped)
        return

    # Invariant guard: never create a track that lives in two subs.
    log.info("Checking one-track-one-sub before writing...")
    existing: dict[str, str] = {}
    for sub in config["subs"]:
        for uri in sp.playlist_track_uris(sub["id"]):
            existing[uri] = sub["name"]

    conflicts = [(u, existing[u], dest) for dest, uris in planned.items()
                 for u in uris if u in existing and existing[u] != dest]
    if conflicts:
        log.error("%d track(s) already filed elsewhere. Nothing was written.", len(conflicts))
        for uri, have, want in conflicts[:15]:
            log.error("  %s is in %r, would also go to %r", uri, have, want)
        sys.exit(1)

    added = []
    already = []
    for dest, uris in sorted(planned.items()):
        fresh = [u for u in uris if u not in existing]
        dupes = [u for u in uris if u in existing]
        already.extend(dupes)
        if not fresh:
            log.info("  %-42s all %d already there", dest, len(dupes))
            continue
        sp.add_tracks(sub_ids[dest], fresh)
        added.extend(fresh)
        log.info("  %-42s +%d%s", dest, len(fresh),
                 f" ({len(dupes)} already there)" if dupes else "")

    # Clear the inbox of everything now filed — including tracks that were
    # already in their destination. Otherwise they linger and resurface in
    # every future suggest run.
    clear = added + already
    if clear:
        sp.remove_tracks(inbox["id"], clear)
        log.info("Cleared %d track(s) from inbox (%d newly added, %d already filed).",
                 len(clear), len(added), len(already))

    log.info("")
    log.info("Filed %d, left %d in inbox. Run sync.py to update the masters.", len(added), skipped)
    log.info("Delete %s, or leave it — it is regenerated each run.", OUT_PATH)


def show_unsure() -> None:
    """Print rows from the last run that need a human look. No API calls."""
    if not os.path.exists(OUT_PATH):
        log.error("%s not found. Run suggest.py first.", OUT_PATH)
        sys.exit(1)
    with open(OUT_PATH, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("confident") or "") == "no"]
    if not rows:
        print("Nothing flagged.")
        return
    print(f"{len(rows)} row(s) to check:\n")
    for r in rows:
        print(f"  {r['artist'][:30]:32s} {r['track'][:34]:36s}")
        print(f"    -> {r['suggestion_1']}")
        for key in ("suggestion_2", "suggestion_3"):
            if r.get(key):
                print(f"       or {r[key]}")
        if r.get("check_why"):
            print(f"       ({r['check_why']})")
        print()
    print(f"Edit the 'chosen' column in {OUT_PATH}, then: python suggest.py --apply")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="move the tracks you confirmed")
    ap.add_argument("--refresh", action="store_true", help="rebuild the model from scratch")
    ap.add_argument("--unsure", action="store_true",
                    help="print the rows marked '?' from the last run and exit")
    ap.add_argument("--dry-run", action="store_true", help="with --apply: write nothing")
    args = ap.parse_args()

    config = load_json(CONFIG_PATH)
    if not config:
        log.error("%s not found. Run bootstrap.py first.", CONFIG_PATH)
        sys.exit(1)
    features = load_json(FEATURES_PATH, {}) or {}

    if args.unsure:
        show_unsure()
        return

    sp = Spotify.from_env(dry_run=args.dry_run)
    log.info("Authenticated as: %s", sp.me().get("display_name"))

    if args.apply:
        do_apply(sp, config)
    else:
        do_suggest(sp, config, features, args.refresh)


if __name__ == "__main__":
    main()