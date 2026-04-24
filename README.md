# spotify-sync

Automated sync of Spotify sub-playlists into bucket master playlists. Runs nightly via GitHub Actions.

## How it works

My Spotify library is organised into an energy-based bucket system:

| Bucket | Energy | Vibe |
|---|---|---|
| Void | 0–0.35 | Silence, stillness, classical, lo-fi |
| Orbit | 0.35–0.60 | Moving but unhurried, emotional pop, trip-hop |
| Atmosphere | 0.60–0.78 | Familiar and alive, pop, hip-hop, house |
| Velocity | 0.78–0.90 | Tempo takes over, K-pop, rock, EDM |
| Impact | 0.90+ | No ceiling no mercy |

Each bucket contains several focused sub-playlists by genre, mood, or era. This script reads all enabled sub-playlists in a bucket, computes the union of their tracks, and syncs a master playlist for that bucket to match exactly.

- Add a track to any sub-playlist → it appears in the master
- Remove a track from a sub-playlist → it disappears from the master
- The master is never edited manually

Runs every night at 04:00 UTC (05:00 Oslo winter, 06:00 Oslo summer).

---

## Files

| File | Purpose |
|---|---|
| `sync.py` | Main sync script |
| `config.json` | Bucket structure — playlist IDs, names, and toggles |
| `requirements.txt` | Python dependencies |
| `get_token.py` | One-time script to get Spotify refresh token |
| `.github/workflows/sync.yml` | GitHub Actions workflow |

---

## Config

`config.json` defines which playlists belong to each bucket and which is the master:

```json
{
  "buckets": {
    "Void": {
      "enabled": true,
      "master": {"id": "PLAYLIST_ID", "name": "Void"},
      "sources": [
        {"id": "PLAYLIST_ID", "name": "Cinematic Reverie", "enabled": true},
        {"id": "PLAYLIST_ID", "name": "Lo-Fi", "enabled": false}
      ]
    }
  }
}
```

**To add a new sub-playlist:** add it to `sources` with its ID, name, and `"enabled": true`. Commit and it will be included in the next sync.

**To exclude a playlist from the master temporarily:** set `"enabled": false` on that source. The playlist still exists in Spotify — it just won't feed into the master until you flip it back.

**To disable an entire bucket:** set `"enabled": false` at the bucket level. The master won't be touched.

**Widescreen** is set to `"enabled": false` by default — it is a parking lot playlist, not a finished list.

---

## Setup

### 1. Create a Spotify app
Go to https://developer.spotify.com/dashboard, create an app with:
- Redirect URI: `https://localhost:8888/callback`
- API: Web API

Copy your **Client ID** and **Client Secret**.

### 2. Get your refresh token
Fill in your credentials in `get_token.py` and run it once locally:
```
pip install requests
python get_token.py
```
It opens Spotify in your browser, you approve access, paste the redirect URL back into the terminal, and it prints your refresh token.

### 3. Add GitHub secrets
Go to **Settings → Secrets and variables → Actions** and add:
- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
- `SPOTIFY_REFRESH_TOKEN`

### 4. Fill in config.json
Replace every `PLAYLIST_ID_*` placeholder with the real Spotify playlist ID.

To find a playlist ID: right-click any playlist in Spotify → Share → Copy link to playlist. The ID is the string after `/playlist/` in the URL.

### 5. Test it
Go to **Actions → Spotify Bucket Sync → Run workflow** to trigger a manual run and check the logs.

---

## Troubleshooting

| Error | Fix |
|---|---|
| 401 Unauthorized | Refresh token expired — re-run `get_token.py` |
| 404 Not Found | A playlist ID in `config.json` is wrong |
| Rate limited | Handled automatically with retries |
| Playlist not updating | Check that both the bucket and the source have `"enabled": true` |