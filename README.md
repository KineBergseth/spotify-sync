# spotify-sync

Every script below also works standalone with its own flags. `python cli.py`
gives you one menu for the common weekly, monthly, and structure-management paths
if you don't want to remember commands.

Sub-playlists are the only thing I edit. Masters are derived views, rebuilt nightly
by GitHub Actions to be exactly the union of their sources.

## Model

```
INBOX          manual write    feeds nothing
SUB-PLAYLISTS  manual write    read-only to sync
MASTERS        write-only by sync — never edit by hand
```

**Invariant: a track belongs to exactly one enabled sub.** A sub may feed many
masters. Sync validates this before writing and aborts the entire run if it is
violated — nothing is propagated until the structure is correct.

Subs are defined by facts you can look up (language, genre, era, format), never
by an energy or mood judgment. That is what makes filing a three-second decision
instead of a comparison against an imagined scale.

## Master types

| Type | Populated by |
|---|---|
| `union` | The subs listed in `include` |
| `rule` | Every track in any enabled sub matching a feature predicate |

Rule masters are how energy enters the system: computed, consistent, zero filing
cost. They read `features.json`, a local cache built by `bootstrap.py` from the
Skiley export, because Spotify deprecated the `/audio-features` endpoint for new
apps in November 2024. Tracks added after that export have no features and will
not appear in rule masters until you re-export and re-run bootstrap.

`manage_masters.py` manages **union** masters: create/adopt them, rename them, and
replace their feeder lists safely. Rule-master definitions remain configuration
owned; `sync.py` still remains the only thing that populates either master type.

## Descriptions

After each sync, playlist descriptions are rewritten so the wiring is visible
inside Spotify itself:

```
M · VORS (trygg)      1306 spor · Fra: Dance-Pop & Club, Big Room & Mainstage, … · Oppdatert 2026-08-27
FUNK · Funk & Disco   Mater: M · BAKGRUNN (trygg), M · RETRO & OLDIES, M · SUMMER & DRIVE, M · VORS (trygg)
```

Spotify caps descriptions at 300 characters. Full sub names are used when they
fit; otherwise the `DOMAIN ·` prefix is dropped, and past that the list is
truncated with `+N til`. Nothing is ever silently cut mid-name.

`descriptions.json` records what was last written, so a nightly run with no
structural change makes zero description calls. The date is ignored when
comparing, so it does not trigger a rewrite by itself.

Turn it off with `python sync.py --no-descriptions`.

## Reviewing playlist coherence

`export.py` dumps what is actually in Spotify right now — including anything you
moved by hand — grouped by playlist and split into files small enough to review:

```bash
python export.py                    # subs only, ~700 tracks per file
python export.py --masters          # include masters
python export.py --only "POP ·"     # one family at a time
python export.py --max-tracks 400   # smaller files
```

Writes `review/review_01.md`, `review_02.md`, … each with the review prompt
already at the top, plus `review/library_export.csv` for scripting. Chunks break
on whole playlists only, so a reviewer always sees a complete playlist and can
judge whether its contents match its name.

The same export also scans **all enabled source subs** for duplicate/version
candidates, even when `--only` is used for the coherence chunks. Masters are
deliberately excluded because the same source track is allowed in many masters.
It writes:

- `review/duplicate_candidates.csv` — machine-readable candidate groups
- `review/duplicates_review.md` — a separate AI-ready duplicate review

Duplicate evidence is layered. `EXACT_URI` means the same Spotify track URI is
present more than once. `SAME_ISRC` catches different Spotify IDs for the same
recording, which commonly surfaces single-vs-album and reissue copies.
`SAME_SONG_SHAPE` is conservative fuzzy evidence: same primary artist +
normalized base title + duration within five seconds. It strips publication
markers such as remaster, album/single version, and trailing `feat.` metadata,
but deliberately does **not** strip live, remix, acoustic, demo, instrumental,
etc., because those can be genuinely distinct recordings.

Paste one file per conversation. More than one and review quality drops sharply
— the model starts pattern-matching instead of reading. The prompt now requires
one machine-readable CSV response with `FLAG` rows for genuine misfiles and one
`VERDICT` row per playlist. Save each response as, for example,
`review/review_flags_01.csv`, `review/review_flags_02.csv`, and so on. Review
`review/duplicates_review.md` separately and save that AI response as, for
example, `review/duplicate_flags.csv`. When a duplicate group truly needs
manual resolution, that prompt asks the AI to FLAG **all variants in the group**
so they move to holding together and you choose the canonical copy yourself.

### Extracting AI-flagged tracks

Create one separate Spotify playlist outside `config.json`, for example
`UNSORTED · Must be processed`. It is a holding pen only: it must not be an
INBOX, sub, or master. Keep its Spotify playlist ID handy.

Preview every AI flag before allowing writes:

```bash
python review_apply.py "review/review_flags_*.csv" review/duplicate_flags.csv --holding-id YOUR_PLAYLIST_ID
```

The script refuses to trust the AI output by itself. Every `FLAG` must match the
exact `track_uri + current_playlist` pair in the `review/library_export.csv`
created by the same export run, and the track must still be in that sub live on
Spotify. If the preview is correct:

```bash
python review_apply.py "review/review_flags_*.csv" review/duplicate_flags.csv --holding-id YOUR_PLAYLIST_ID --apply
python sync.py
```

On apply, all flagged tracks are added to the holding playlist first, then
removed from their current subs. This ordering makes an interrupted run safe to
retry: a failed add cannot strand a track with no playlist, and duplicate adds
are avoided on the next run. Masters are not edited directly; `sync.py` removes
the extracted tracks from derived masters after their source-sub removal.

You can avoid repeating the ID by setting:

```bash
export SPOTIFY_REVIEW_HOLDING_ID=YOUR_PLAYLIST_ID
```

Then `--holding-id` can be omitted.

## Re-classifying

The holding playlist is the manual processing queue for review mistakes. Once a
track has been extracted there, decide its real destination. Move it to INBOX
and run the [inbox filing workflow](#routine) again, or move it manually into
exactly one sub. Do not use `bootstrap.py` to re-label tracks: bootstrap only
adds and can create a one-track-two-subs violation if used for refiling.

## API version

This targets the Web API as it stands after the
[February 2026 Development Mode migration](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide).
If you see a bare `403 Forbidden` on a write, check these first — every one of
them was renamed and the old form now 403s rather than 404s:

| Removed | Current |
|---|---|
| `POST /users/{id}/playlists` | `POST /me/playlists` |
| `GET/POST/DELETE /playlists/{id}/tracks` | `.../items` |
| DELETE body key `tracks` | `items` |
| Response row `items[].track` | `items[].item` |

Development Mode also requires the app owner to hold an active Premium
subscription. If Premium lapses, the app stops working until it resumes.

## Files

| File | Purpose |
|---|---|
| `cli.py` | One menu for the whole toolchain. Runs the scripts below for you; nothing it does can't also be typed by hand. |
| `spotify_api.py` | API client: auth, pagination, retries |
| `bootstrap.py` | Creates playlists and fills subs from the CSV. Re-runnable. |
| `sync.py` | Nightly sync + validation |
| `config.json` | Structure consumed by sync. Generated by bootstrap; normally changed through the structure-management scripts rather than by hand. |
| `features.json` | Local audio-feature cache for rule masters |
| `descriptions.json` | Last descriptions written, so sync does not rewrite them nightly |
| `playlist_ids.json` | name → id. **Commit this.** Prevents duplicate creation. |
| `inbox_export.py` | Read-only look at INBOX, and `--prompt` to generate the AI filing prompt below |
| `apply_inbox_recommendations.py` | Applies the resulting `MOVE`/`KEEP_INBOX` recommendation CSV to INBOX. Destinations must already be enabled subs in `config.json` — it never creates playlists, and aborts the whole run with nothing written if any row targets something invalid (a master, INBOX, a typo). |
| `export.py` | Dumps live playlist contents, chunked, with a machine-action review prompt |
| `review_apply.py` | Validates AI `FLAG` rows and moves them to the external holding playlist |
| `manage_playlists.py` | Add/rename subs consistently across `config.json`, `playlist_ids.json`, `descriptions.json`; `validate` checks all three agree in both directions |
| `manage_masters.py` | Create/adopt or rename union masters, replace their feeder lists, rebuild `subs[].in_masters`, and validate master wiring |
| `track_assignments.csv` | Initial assignment of every track to a sub |

## First run

```bash
pip install -r requirements.txt
export SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... SPOTIFY_REFRESH_TOKEN=...

python bootstrap.py --dry-run    # read-only: shows every playlist it would create
python bootstrap.py              # creates playlists, fills subs, writes config
python sync.py --report          # validate structure, write nothing
python sync.py                   # fill the masters
git add playlist_ids.json config.json features.json && git commit -m "bootstrap"
```

`bootstrap.py` only ever touches playlists recorded in `playlist_ids.json`, or
adopts an existing playlist whose name matches exactly. Your current library is
never modified. Interrupted runs are safe to repeat — creation and track-adding
are both idempotent.

Scopes needed: `playlist-read-private playlist-modify-private playlist-modify-public`.

## Config

```jsonc
{
  "settings": {
    "abort_if_union_empty": true,   // never let a failed read empty a master
    "fail_on_duplicate_track": true,// enforce one-track-one-sub
    "min_sub_size": 5,             // warn below: merge it
    "max_sub_size": 150             // warn above: split it
  },
  "inbox":  { "id": "...", "name": "INBOX" },
  "subs":   [ { "name": "KR · Modern K-Pop", "id": "...", "enabled": true } ],
  "masters": [
    { "name": "M · ØSTASIA", "id": "...", "enabled": true,
      "type": "union", "include": ["KR · Modern K-Pop", "JP · J-Pop"] },
    { "name": "M · PEAK (auto)", "id": "...", "enabled": true,
      "type": "rule", "rule": { "energy_min": 0.91 } }
  ]
}
```

`enabled` means one thing in each place, and only one:

- on a **sub** — is this playlist part of the system at all
- on a **master** — should this master be rebuilt tonight

To take a sub out of one union master but leave it in others, change that
master's feeder list. Prefer `python manage_masters.py set-feeders ...` (or the
interactive menu) instead of editing `include` by hand; it also rebuilds every
affected sub's denormalized `in_masters` list. Do not use `enabled` for this.

Rule keys: `energy_min/max`, `valence_min/max`, `tempo_min/max`,
`danceability_min/max`. All conditions must match.

## Routine

**Weekly, ten minutes.** Empty INBOX. One question per track: *what is this?*
Inbox size is the only health metric that matters.

Just want to look, no filing decisions yet?

```bash
python inbox_export.py            # prints the list
python inbox_export.py --csv      # also writes review/inbox_export.csv
```

Either drag by hand, or generate filing recommendations and apply the ones
you're confident in:

```bash
python inbox_export.py --prompt   # writes review/inbox_placement_prompt.md
```

Paste that file into a conversation. It already tells the AI the exact CSV
schema to reply with and the exact list of valid destination sub-playlists —
never a master, never INBOX itself, since `apply_inbox_recommendations.py`
rejects both. Save the reply as `spotify_inbox_placement_recommendations.csv`,
then:

```bash
python apply_inbox_recommendations.py --dry-run    # preview, no writes
python apply_inbox_recommendations.py              # apply MOVE rows above the
                                                    # confidence threshold
python sync.py                                     # push to masters
```

Expected CSV columns: `track_uri, recommended_playlist, action, confidence_score`
(`artist`/`track`/`confidence_label` are read if present, for logging).
`action` must be `MOVE` or `KEEP_INBOX`; rows below `--min-confidence`
(default 0.65) are treated the same as `KEEP_INBOX` and left in place.

Every row's destination is validated **before any Spotify call is made**: if
even one row targets something that isn't an enabled sub — a master, INBOX,
a disabled sub, a typo — the entire run aborts with nothing written, rather
than silently applying the good rows and skipping the bad ones. Add a
genuinely new destination with `manage_playlists.py add-sub` first, then
re-run. Separately, a live one-track-one-sub check runs against every sub
right before writing: if a track has already been filed elsewhere since the
prompt was generated, it's left in INBOX and reported, never duplicated.

This never files anything on its own — you choose whether to run the apply
step after reading the dry-run output. Auto-filing would let one wrong AI
guess become permanent structure, which is the whole reason the dry-run step
exists.

**Monthly.** Read the sync warnings. Act on them:

| Warning | Meaning |
|---|---|
| feeds no master | Misfiled, or a holding pen you forgot |
| under the 5 minimum | Not a playlist yet — merge it upward |
| over the 150 maximum | No longer has an identity — split it |
| feeds 4 masters | Too broad, it's a genre not a playlist |
| duplicate track(s) inside the sub | Clean up |

**Adding or renaming a sub.** Use `python manage_playlists.py` (interactive) or
its `add-sub` / `rename-sub` commands instead of hand-editing. It keeps
`config.json`, `playlist_ids.json`, master `include` lists, and cached description
keys consistent. Run `python manage_playlists.py validate` after any manual
structure edit. A playlist that only exists in `playlist_ids.json` is invisible
to `sync.py`, `export.py`, and `apply_inbox_recommendations.py`: it feeds no
master, is never checked for one-track-one-sub, and never shows up in review.

**Managing union masters.** Run `python manage_masters.py` with no arguments for
an interactive menu, or use the commands directly:

```bash
# Create/adopt a new union master and wire its source subs.
python manage_masters.py add-master "M · LATIN" \
  --sub "LATIN · Latin Pop" \
  --sub "LATIN · Reggaeton & Latin Urban" \
  --sub "LATIN · Salsa, Cumbia & Tropical"

# Rename everywhere; the Spotify playlist itself is renamed by default.
python manage_masters.py rename-master "M · OLD" "M · NEW"

# Replace a union master's COMPLETE feeder list. Repeat --sub for every feeder.
python manage_masters.py set-feeders "M · LATIN" \
  --sub "LATIN · Latin Pop" \
  --sub "LATIN · Reggaeton & Latin Urban"

# Deliberately give a union master no feeders.
python manage_masters.py set-feeders "M · LATIN" --clear

# Read-only consistency check.
python manage_masters.py validate
```

All three write commands accept `--dry-run`. `add-master` creates a private
playlist by default (use `--public` if wanted) and adopts an existing Spotify
playlist with the exact same name when possible. `rename-master` updates
`config.json`, `playlist_ids.json`, `descriptions.json`, and affected
`subs[].in_masters`; use `--no-rename-on-spotify` if you already renamed the
playlist yourself. `set-feeders` applies only to union masters and replaces the
whole `include` list atomically, then rebuilds `subs[].in_masters`.

After any applied master-structure change, run:

```bash
python sync.py
```

That is what actually rebuilds the derived master contents and refreshes Spotify
descriptions. The CLI exposes the same structure tools as options 6 and 7, so
`python cli.py` is enough if you do not want to remember the standalone commands.

## Troubleshooting

Every script authenticates via `Spotify.from_env()`, which holds onto your
client id/secret/refresh token and silently re-authenticates once if the
access token expires mid-run (they last about an hour, which a large
`bootstrap.py` or `export.py` run across many playlists can approach). You will see
a one-line `Access token expired mid-run, refreshing...` log when this
happens — it is not an error.

| Symptom | Cause |
|---|---|
| `401` even after a fresh run | Refresh token itself is invalid or revoked — re-run `get_token.py` |
| `404` | An id in `config.json` is wrong or the playlist was deleted |
| Run exits 1, nothing changed | Invariant violation. Read the error, fix the sub. Working as intended. |
| Master untouched, "EMPTY target" | Every source was empty or unreadable. Guard prevented a wipe. |
| Rule master too small | Tracks missing from `features.json`. Re-export and re-bootstrap. |
| Bootstrap made a second copy | `playlist_ids.json` was not committed. |