# spotify-sync

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
track has been extracted there, decide its real destination. Move it to INBOX and
use `suggest.py`, or move it manually into exactly one sub. Do not use `bootstrap.py` to re-label tracks: bootstrap only adds and can
create a one-track-two-subs violation if used for refiling.

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
| `spotify_api.py` | API client: auth, pagination, retries |
| `bootstrap.py` | Creates playlists and fills subs from the CSV. Re-runnable. |
| `sync.py` | Nightly sync + validation |
| `config.json` | Structure. Generated by bootstrap, edited by hand after. |
| `features.json` | Local audio-feature cache for rule masters |
| `descriptions.json` | Last descriptions written, so sync does not rewrite them nightly |
| `playlist_ids.json` | name → id. **Commit this.** Prevents duplicate creation. |
| `suggest.py` | Suggests a sub for each inbox track, then files the ones you confirm |
| `suggest_model.json` | Learned artist/album/genre history. Regenerated on demand. |
| `export.py` | Dumps live playlist contents, chunked, with a machine-action review prompt |
| `review_apply.py` | Validates AI `FLAG` rows and moves them to the external holding playlist |
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

To take a sub out of one master but leave it in others, remove it from that
master's `include`. Do not use `enabled` for that.

Rule keys: `energy_min/max`, `valence_min/max`, `tempo_min/max`,
`danceability_min/max`. All conditions must match.

## Routine

**Weekly, ten minutes.** Empty INBOX. One question per track: *what is this?*
Inbox size is the only health metric that matters.

Either drag by hand, or use the assistant:

```bash
python suggest.py            # writes inbox_suggestions.csv, top pick pre-filled
#   edit the "chosen" column: accept, change, or blank to leave in inbox
python suggest.py --apply    # files the confirmed rows, clears them from inbox
python sync.py               # push to masters
```

Rows it is unsure about are marked `confident=no`. List just those, without
touching the API:

```bash
python suggest.py --unsure
```

Confidence requires positive evidence — at least three tracks by that artist
already filed in the winning sub, a clear margin over the runner-up, and a
higher bar again for `POP · <era>` destinations, since those are where unknown
tracks drift. It flags roughly two thirds of a typical inbox and is right on
the ones it does not flag.

`inbox_suggestions.csv` is a plain CSV, so slice it however you like. In
PowerShell:

```powershell
# same as --unsure, but as a table
Import-Csv inbox_suggestions.csv |
  Where-Object confident -eq 'no' |
  Format-Table artist,track,suggestion_1,suggestion_2

# everything heading for an era bucket — the usual source of mistakes
Import-Csv inbox_suggestions.csv |
  Where-Object chosen -like 'POP*' |
  Format-Table artist,track,chosen

# how many are going to each sub
Import-Csv inbox_suggestions.csv |
  Group-Object chosen | Sort-Object Count -Descending |
  Format-Table Count,Name
```
It suggests, you confirm; it never files on its own. Auto-filing would let one
wrong Spotify tag become permanent structure, which is the failure mode the
manual step exists to prevent.

`--apply` re-checks one-track-one-sub across every sub before writing, and
aborts without writing anything if a track is already filed elsewhere.

Run `python suggest.py --refresh` after a big refiling session to rebuild the
model from current state. It learns from your corrections.

**Monthly.** Read the sync warnings. Act on them:

| Warning | Meaning |
|---|---|
| feeds no master | Misfiled, or a holding pen you forgot |
| under the 15 minimum | Not a playlist yet — merge it upward |
| over the 120 maximum | No longer has an identity — split it |
| feeds 4 masters | Too broad, it's a genre not a playlist |
| duplicate track(s) inside the sub | Clean up |

**Adding a sub.** Create it in Spotify, add it to `subs`, list it in the
`include` of every master it belongs to, commit.

## Troubleshooting

Every script authenticates via `Spotify.from_env()`, which holds onto your
client id/secret/refresh token and silently re-authenticates once if the
access token expires mid-run (they last about an hour, which a long
`suggest.py` model build or a large `bootstrap.py` can approach). You will see
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