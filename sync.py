"""
Spotify master playlist sync.

Subs are the only source of truth. Masters are derived views and are never
edited by hand. Every master is rebuilt to be exactly:

  - union masters: the union of the enabled subs it includes
  - rule masters:  every track in any enabled sub matching a feature predicate

Before writing anything, the run validates the structure and refuses to
proceed if a track appears in more than one enabled sub. That check is the
whole point: sync propagates whatever it is given, so the invariant has to be
enforced here or nowhere.

Usage:
    python sync.py --dry-run    # report only, write nothing
    python sync.py              # sync
    python sync.py --report     # validate and print health, no writes
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict

from spotify_api import Spotify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONFIG_PATH = "config.json"
FEATURES_PATH = "features.json"
DESC_PATH = "descriptions.json"   # what we last wrote, so we do not rewrite nightly

DESC_LIMIT = 300


# ------------------------------------------------------------------ loading


def load_config(path: str = CONFIG_PATH) -> dict:
    if not os.path.exists(path):
        log.error("%s not found. Run bootstrap.py first.", path)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")


def load_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_features(path: str = FEATURES_PATH) -> dict:
    if not os.path.exists(path):
        log.warning("%s not found. Rule-based masters will be skipped.", path)
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_subs(sp: Spotify, config: dict) -> dict[str, list[str]]:
    """Read every enabled sub once. Order preserved, duplicates preserved."""
    contents: dict[str, list[str]] = {}
    for sub in config["subs"]:
        if not sub.get("enabled", True):
            log.info("  %-42s skipped (disabled)", sub["name"])
            continue
        try:
            uris = sp.playlist_track_uris(sub["id"])
        except Exception as exc:
            log.error("  %-42s READ FAILED: %s", sub["name"], exc)
            raise
        contents[sub["name"]] = uris
        log.info("  %-42s %4d tracks", sub["name"], len(uris))
    return contents


# --------------------------------------------------------------- validation


def validate(config: dict, contents: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors block the run; warnings do not."""
    errors: list[str] = []
    warnings: list[str] = []
    settings = config.get("settings", {})

    sub_names = {s["name"] for s in config["subs"]}
    enabled_names = set(contents)

    # 1. One track, one sub.
    owner: dict[str, str] = {}
    collisions: dict[str, list[str]] = defaultdict(list)
    for name, uris in contents.items():
        for uri in set(uris):
            if uri in owner and owner[uri] != name:
                if owner[uri] not in collisions[uri]:
                    collisions[uri].append(owner[uri])
                collisions[uri].append(name)
            else:
                owner[uri] = name

    if collisions:
        msg = f"{len(collisions)} track(s) appear in more than one enabled sub"
        for uri, names in list(collisions.items())[:15]:
            log.error("    %s -> %s", uri, " + ".join(names))
        if len(collisions) > 15:
            log.error("    ... and %d more", len(collisions) - 15)
        (errors if settings.get("fail_on_duplicate_track", True) else warnings).append(msg)

    # 2. Duplicates within a single sub.
    for name, uris in contents.items():
        if len(uris) != len(set(uris)):
            warnings.append(f"{name}: {len(uris) - len(set(uris))} duplicate track(s) inside the sub")

    # 3. Masters reference subs that exist.
    for master in config["masters"]:
        for ref in master.get("include", []):
            if ref not in sub_names:
                errors.append(f"{master['name']} includes unknown sub {ref!r}")

    # 4. Subs feeding nothing.
    referenced = {r for m in config["masters"] for r in m.get("include", [])}
    for sub in config["subs"]:
        if sub.get("enabled", True) and sub["name"] not in referenced:
            warnings.append(f"{sub['name']}: feeds no master")

    # 5. Size rules.
    lo = settings.get("min_sub_size", 0)
    hi = settings.get("max_sub_size", 10**9)
    for name, uris in contents.items():
        if len(uris) < lo:
            warnings.append(f"{name}: {len(uris)} tracks, under the {lo} minimum (merge or grow it)")
        if len(uris) > hi:
            warnings.append(f"{name}: {len(uris)} tracks, over the {hi} maximum (split it)")

    # 6. Subs in many masters have weak identity.
    fan = defaultdict(int)
    for master in config["masters"]:
        for ref in master.get("include", []):
            fan[ref] += 1
    for name, count in fan.items():
        if count >= 4 and name in enabled_names:
            warnings.append(f"{name}: feeds {count} masters, identity may be too broad")

    return errors, warnings


# -------------------------------------------------------------- descriptions


def _short(sub: str) -> str:
    """Drop the DOMAIN prefix: 'EDM · Big Room' -> 'Big Room'."""
    _, sep, rest = sub.partition(" · ")
    return rest if sep else sub


def _join(names: list[str], prefix: str, suffix: str) -> str:
    """Fit as many names as possible inside the 300-char limit."""
    for render in (lambda n: n, _short):
        body = ", ".join(render(n) for n in names)
        if len(prefix) + len(body) + len(suffix) <= DESC_LIMIT:
            return prefix + body + suffix
    # still too long: list what fits, then say how many were dropped
    kept, used = [], 0
    for name in names:
        piece = _short(name)
        extra = len(piece) + (2 if kept else 0)
        if len(prefix) + used + extra + len(suffix) + 12 > DESC_LIMIT:
            break
        kept.append(piece)
        used += extra
    more = len(names) - len(kept)
    return prefix + ", ".join(kept) + (f" +{more} til" if more else "") + suffix


def describe_master(master: dict, count: int, stamp: str) -> str:
    if master.get("type") == "rule":
        rule = ", ".join(f"{k}={v}" for k, v in sorted(master.get("rule", {}).items()))
        return f"{count} spor · Auto: {rule} · Oppdatert {stamp}"[:DESC_LIMIT]
    return _join(sorted(master.get("include", [])),
                 f"{count} spor · Fra: ", f" · Oppdatert {stamp}")


def describe_sub(name: str, masters: list[str]) -> str:
    if not masters:
        return "Mater ingen master."
    return _join(sorted(masters), "Mater: ", "")


def update_descriptions(sp: Spotify, config: dict, sizes: dict, contents: dict) -> None:
    """Write source lists into playlist descriptions, but only when they change."""
    from datetime import date
    stamp = date.today().isoformat()
    cache = load_json(DESC_PATH, {}) or {}
    written = 0

    for master in config["masters"]:
        if not master.get("enabled", True) or master["name"] not in sizes:
            continue
        text = describe_master(master, sizes[master["name"]], stamp)
        # ignore the date when deciding whether anything really changed
        key = text.rsplit(" · Oppdatert", 1)[0]
        if cache.get(master["name"]) == key:
            continue
        try:
            sp.set_description(master["id"], text)
            cache[master["name"]] = key
            written += 1
        except Exception as exc:
            log.warning("  %s: description not set (%s)", master["name"], exc)

    feeds = defaultdict(list)
    for master in config["masters"]:
        for ref in master.get("include", []):
            feeds[ref].append(master["name"])
    for sub in config["subs"]:
        if sub["name"] not in contents:
            continue
        text = describe_sub(sub["name"], feeds.get(sub["name"], []))
        if cache.get("sub:" + sub["name"]) == text:
            continue
        try:
            sp.set_description(sub["id"], text)
            cache["sub:" + sub["name"]] = text
            written += 1
        except Exception as exc:
            log.warning("  %s: description not set (%s)", sub["name"], exc)

    if written and not sp.dry_run:
        save_json(DESC_PATH, cache)
    log.info("  %d description(s) updated", written)


# ----------------------------------------------------------------- resolving


def matches(feat: dict | None, rule: dict) -> bool:
    if not feat:
        return False
    checks = (
        ("energy", "energy_min", "energy_max"),
        ("valence", "valence_min", "valence_max"),
        ("tempo", "tempo_min", "tempo_max"),
        ("danceability", "danceability_min", "danceability_max"),
    )
    for key, lo_key, hi_key in checks:
        value = feat.get(key)
        if lo_key in rule:
            if value is None or value < rule[lo_key]:
                return False
        if hi_key in rule:
            if value is None or value > rule[hi_key]:
                return False
    return True


def resolve(master: dict, contents: dict[str, list[str]], features: dict) -> list[str] | None:
    """Target track list for a master, in a stable order. None = skip."""
    if master.get("type", "union") == "rule":
        if not features:
            log.warning("  %s: no feature cache, skipped", master["name"])
            return None
        rule = master.get("rule", {})
        out, seen = [], set()
        for name in sorted(contents):
            for uri in contents[name]:
                if uri not in seen and matches(features.get(uri), rule):
                    seen.add(uri)
                    out.append(uri)
        return out

    out, seen = [], set()
    for name in master.get("include", []):
        for uri in contents.get(name, []):
            if uri not in seen:
                seen.add(uri)
                out.append(uri)
    return out


# --------------------------------------------------------------------- main


def sync_master(sp: Spotify, master: dict, target: list[str], settings: dict) -> None:
    name = master["name"]

    if not target:
        if settings.get("abort_if_union_empty", True):
            log.error("  %-42s EMPTY target, master left untouched", name)
            return
        log.warning("  %-42s target is empty", name)

    current = sp.playlist_track_uris(master["id"])
    cur_set, tgt_set = set(current), set(target)

    to_add = [u for u in target if u not in cur_set]
    to_remove = [u for u in set(current) if u not in tgt_set]
    dupes_in_master = len(current) - len(cur_set)

    if not to_add and not to_remove and not dupes_in_master:
        log.info("  %-42s %4d tracks, unchanged", name, len(current))
        return

    if dupes_in_master:
        # Removing by URI removes every copy, so re-add once afterwards.
        log.info("  %-42s cleaning %d duplicate(s)", name, dupes_in_master)
        overlap = [u for u in tgt_set if current.count(u) > 1]
        sp.remove_tracks(master["id"], overlap)
        to_add = [u for u in target if u not in (cur_set - set(overlap))]

    if to_remove:
        sp.remove_tracks(master["id"], to_remove)
    if to_add:
        sp.add_tracks(master["id"], to_add)

    log.info(
        "  %-42s %4d tracks (+%d / -%d)", name, len(target), len(to_add), len(to_remove)
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    ap.add_argument("--report", action="store_true", help="validate only, then exit")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--no-descriptions", action="store_true",
                    help="do not write source lists into playlist descriptions")
    args = ap.parse_args()

    config = load_config(args.config)
    features = load_features()
    settings = config.get("settings", {})

    sp = Spotify.from_env(dry_run=args.dry_run or args.report)
    log.info("Authenticated as: %s", sp.me().get("display_name"))

    log.info("")
    log.info("Reading sub-playlists")
    contents = read_subs(sp, config)

    log.info("")
    log.info("Validating")
    errors, warnings = validate(config, contents)

    for warning in warnings:
        log.warning("  %s", warning)
    for error in errors:
        log.error("  %s", error)

    if errors:
        log.error("")
        log.error("%d error(s). No masters were modified.", len(errors))
        sys.exit(1)

    if not warnings:
        log.info("  All checks passed")

    # Inbox is the health metric.
    inbox = config.get("inbox")
    if inbox:
        size = len(sp.playlist_track_uris(inbox["id"]))
        (log.warning if size else log.info)("  Inbox: %d unfiled track(s)", size)

    if args.report:
        total = len({u for uris in contents.values() for u in uris})
        log.info("")
        log.info("%d unique tracks across %d enabled subs", total, len(contents))
        return

    log.info("")
    log.info("Syncing masters")
    failed = 0
    sizes: dict[str, int] = {}
    for master in config["masters"]:
        if not master.get("enabled", True):
            log.info("  %-42s skipped (disabled)", master["name"])
            continue
        target = resolve(master, contents, features)
        if target is None:
            continue
        try:
            sync_master(sp, master, target, settings)
            sizes[master["name"]] = len(target)
        except Exception as exc:
            failed += 1
            log.error("  %-42s FAILED: %s", master["name"], exc)

    if not args.no_descriptions:
        log.info("")
        log.info("Updating descriptions")
        update_descriptions(sp, config, sizes, contents)

    log.info("")
    log.info("Done. %d write call(s), %d master(s) failed.", sp.writes, failed)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()