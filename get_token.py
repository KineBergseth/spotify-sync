"""
Run this ONCE to get your Spotify refresh token.

Usage:
  pip install requests
  python get_token.py

Before running, set these environment variables
(see your shell profile ~/.zshrc, ~/.bashrc, or PowerShell $PROFILE):
  SPOTIFY_CLIENT_ID
  SPOTIFY_CLIENT_SECRET
"""

import os
import re
import sys
import urllib.parse
import requests

# ── Pulled from environment variables ──────────────────────────────────────────
try:
    CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
    CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
except KeyError as missing:
    print(f"\nMissing environment variable: {missing}")
    print("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET before running this script.")
    sys.exit(1)

REDIRECT_URI = "http://127.0.0.1:8888/callback"
# ──────────────────────────────────────────────────────────────────────────────

SCOPES = "playlist-read-private playlist-modify-private playlist-modify-public"


def main():
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
    }
    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params)

    print("\n1. Open this URL in your browser:\n")
    print(auth_url)
    print("\n2. Log in and approve access.")
    print("3. You will be redirected to a page that won't load — that is expected.")
    print("4. Copy the full URL from the address bar and paste it below.\n")

    redirected = input("Paste the full redirect URL here:\n> ").strip()

    code_match = re.search(r"[?&]code=([^&]+)", redirected)
    if not code_match:
        print("\nCould not find 'code' in the URL. Make sure you copied the full URL.")
        return

    code = code_match.group(1)

    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )

    if not resp.ok:
        print(f"\nError: {resp.status_code} {resp.text}")
        return

    data = resp.json()

    print("\n✅ Success! Add these as GitHub secrets:\n")
    print(f"SPOTIFY_CLIENT_ID     = {CLIENT_ID}")
    print(f"SPOTIFY_CLIENT_SECRET = {CLIENT_SECRET}")
    print(f"SPOTIFY_REFRESH_TOKEN = {data['refresh_token']}")


if __name__ == "__main__":
    main()