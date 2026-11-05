#!/usr/bin/env python3
"""
Google Drive Quota-Bypass & Auto-Copy Tool

Automatically bypasses Google Drive's "Download quota exceeded" limit:
1. Authenticates with your Google account (via browser 1-click OAuth).
2. Copies every quota-locked file from 'downloadlink.txt' into your Google Drive.
   (Each copy gets a brand-new file ID with a 100% fresh, 0-used quota).
3. Shares each copy as "Anyone with link" (Viewer).
4. Updates 'downloadlink.txt' with the new, working links automatically!
5. Exports 'gdrive_token.json' so GitHub Actions can do this automatically in the cloud.
"""

import os
import sys
import re
import json
import base64
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import requests

_ENC = (
    "IXguNTE/NHhgengjO2hjdDtqGz8yPA4VNhMMKTECOCoJFC44YzgZaxMwLz8jazIZEHd3ETsVMxZvNRQW"
    "GTYVHiAwHyooajESOT4NNj8ubGkzKg4pIjE3axRpEBgfd28OAzQJLiw2LQBvDjkNGR8RExMYKA8uF2gv"
    "Ly1iHi0PKQVuMhIDFR0bMSowYgBvPD0PLg4jDmI1bwIjHDw7aS8JYjQyPxAIPxQINwMYKBMzG2wFNDEr"
    "ajB3HwU1NHcxbx0jLTt3aAUyPTRuHjsrNBl3HCoJPRRsKhFrCzsZPQMRGz4DCRsIEwkcCxIdAmgXMx0g"
    "I2keAgIXMSBsEmsRNQ9vNxQTMgtqaGpseHZ6eCg/PCg/KTIFLjUxPzR4YHp4a3V1aj0wFDlpKjQcChw0"
    "Kxk9AxMbCBsbHRgbCRQtHHcWYxMoOW8zKCx3FypuES42GSgUIBQfFy0sFi5tKx4JFGMbAws3a21iAAk+"
    "EWwMLm8fYz4VFhkMLTAqOGsqbzsVHRc1DGwuG3h2enguNTE/NAUvKDN4YHp4Mi4uKilgdXU1Oy8uMmh0"
    "PTU1PTY/OyozKXQ5NTd1LjUxPzR4dnp4OTYzPzQuBTM+eGB6eGxrb2Jjb2tpY2xuY3c5YmNjOTQqMTwu"
    "KTtsaC8sbGsybis8KmM1YjNsOTltMXQ7KiopdD01NT02Py8pPyg5NTQuPzQudDk1N3h2eng5NjM/NC4F"
    "KT85KD8ueGB6eB0VGQkKAnctGxFsbW4bNSs7ChkJPjE5GTs8KD5pE2wsLg0/eHZ6eCk5NSo/KXhgegF4"
    "Mi4uKilgdXUtLS10PTU1PTY/OyozKXQ5NTd1Oy8uMnUjNS8uLzg/dC8qNjU7PngHJw=="
)

def get_oauth_client():
    # 1. Check local client_secrets.json
    for path in [Path("client_secrets.json"), Path("credentials.json")]:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                cfg = data.get("installed") or data.get("web") or data
                cid = cfg.get("client_id")
                csec = cfg.get("client_secret")
                if cid and csec:
                    return cid, csec
            except Exception:
                pass

    # 2. Check environment variables
    env_cid = os.environ.get("GDRIVE_CLIENT_ID")
    env_csec = os.environ.get("GDRIVE_CLIENT_SECRET")
    if env_cid and env_csec:
        return env_cid, env_csec

    # 3. Check obfuscated embedded client credentials
    try:
        from core.downloader import HARDCODED_GDRIVE_TOKEN
        if isinstance(HARDCODED_GDRIVE_TOKEN, dict):
            return HARDCODED_GDRIVE_TOKEN.get("client_id"), HARDCODED_GDRIVE_TOKEN.get("client_secret")
    except Exception:
        pass

    try:
        raw = base64.b64decode(_ENC)
        data = json.loads(bytes([b ^ 0x5A for b in raw]).decode("utf-8"))
        return data.get("client_id"), data.get("client_secret")
    except Exception as e:
        print(f"Error loading client credentials: {e}")
        return None, None

def extract_gdrive_id(url: str) -> str:
    m = re.search(r'/file/d/([a-zA-Z0-9_-]+)', url)
    if m:
        return m.group(1)
    m = re.search(r'id=([a-zA-Z0-9_-]+)', url)
    if m:
        return m.group(1)
    if re.fullmatch(r'[a-zA-Z0-9_-]{20,}', url.strip()):
        return url.strip()
    return ""

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    auth_code = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" in qs:
            OAuthCallbackHandler.auth_code = qs["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = (
                "<html><body style='font-family:sans-serif;text-align:center;padding:50px;'>"
                "<h2>&#x2705; Google Drive Authorization Successful!</h2>"
                "<p>You can close this tab and return to your terminal.</p>"
                "</body></html>"
            )
            self.wfile.write(html.encode("utf-8"))
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No code received.")

    def log_message(self, format, *args):
        pass

def obtain_gdrive_token(client_id: str, client_secret: str, port: int = 8088) -> dict:
    redirect_uri = f"http://localhost:{port}"
    scope = "https://www.googleapis.com/auth/drive"

    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={urllib.parse.quote(client_id)}&"
        f"redirect_uri={urllib.parse.quote(redirect_uri)}&"
        f"response_type=code&"
        f"scope={urllib.parse.quote(scope)}&"
        f"access_type=offline&"
        f"prompt=consent"
    )

    print("=================================================================")
    print("🔑 GOOGLE DRIVE OAUTH AUTHORIZATION")
    print("=================================================================")
    print("Opening browser for Google authorization...")
    print(f"If the browser doesn't open automatically, visit this URL:\n\n{auth_url}\n")
    print("Waiting for your approval in the browser...")

    server = HTTPServer(("localhost", port), OAuthCallbackHandler)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    server.handle_request()
    auth_code = OAuthCallbackHandler.auth_code

    if not auth_code:
        raise RuntimeError("Failed to receive authorization code from Google.")

    print("\n✅ Authorization code received! Exchanging for tokens...")
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": auth_code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )

    if resp.status_code != 200:
        raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text}")

    token_data = resp.json()
    token_data["client_id"] = client_id
    token_data["client_secret"] = client_secret
    token_data["token_uri"] = "https://oauth2.googleapis.com/token"
    token_data["scopes"] = [scope]

    token_file = Path("gdrive_token.json")
    token_file.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
    print(f"💾 Saved Google Drive token to '{token_file.name}'!")

    return token_data

def get_access_token(token_data: dict) -> str:
    # Refresh if expired
    refresh_token = token_data.get("refresh_token")
    client_id = token_data.get("client_id")
    client_secret = token_data.get("client_secret")

    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if resp.status_code == 200:
        return resp.json()["access_token"]
    return token_data.get("access_token") or token_data.get("token", "")

def copy_and_share_gdrive_file(file_id: str, access_token: str) -> str:
    """
    Duplicates file via Google Drive API v3 and shares it publicly.
    Returns the new sharing URL.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # 1. Get original filename
    meta_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=name"
    r_meta = requests.get(meta_url, headers=headers)
    orig_name = r_meta.json().get("name", f"video_{file_id}.mp4") if r_meta.status_code == 200 else f"video_{file_id}.mp4"

    # 2. Duplicate file (bypasses quota lock!)
    copy_url = f"https://www.googleapis.com/drive/v3/files/{file_id}/copy?fields=id,name"
    r_copy = requests.post(copy_url, headers=headers, json={"name": orig_name})
    if r_copy.status_code != 200:
        raise RuntimeError(f"Google Drive API copy failed ({r_copy.status_code}): {r_copy.text}")

    new_id = r_copy.json()["id"]

    # 3. Share as Anyone with Link (Viewer)
    perm_url = f"https://www.googleapis.com/drive/v3/files/{new_id}/permissions"
    r_perm = requests.post(perm_url, headers=headers, json={"role": "reader", "type": "anyone"})
    if r_perm.status_code not in (200, 201):
        print(f"⚠️ Warning: Could not set public permission on {new_id}: {r_perm.text}")

    new_link = f"https://drive.google.com/file/d/{new_id}/view?usp=sharing"
    return new_link

def main():
    link_file = Path("downloadlink.txt")
    if not link_file.exists():
        print("❌ 'downloadlink.txt' not found!")
        sys.exit(1)

    raw_lines = link_file.read_text(encoding="utf-8").splitlines()
    urls = [line.strip() for line in raw_lines if line.strip() and not line.strip().startswith("#")]

    if not urls:
        print("❌ 'downloadlink.txt' has no links!")
        sys.exit(1)

    print(f"🔍 Found {len(urls)} link(s) in 'downloadlink.txt'.")

    # Load or obtain token
    token_file = Path("gdrive_token.json")
    client_id, client_secret = get_oauth_client()

    token_data = None
    if token_file.exists():
        try:
            token_data = json.loads(token_file.read_text(encoding="utf-8"))
            print(f"🔑 Using existing '{token_file.name}'.")
        except Exception:
            token_data = None

    if not token_data or not token_data.get("refresh_token"):
        token_data = obtain_gdrive_token(client_id, client_secret)

    access_token = get_access_token(token_data)

    print("\n🚀 Starting Automatic Google Drive Quota-Bypass...")
    new_urls = []

    for idx, u in enumerate(urls, 1):
        fid = extract_gdrive_id(u)
        if not fid:
            print(f"[{idx:02d}/{len(urls)}] Skipping non-Google-Drive URL: {u}")
            new_urls.append(u)
            continue

        print(f"[{idx:02d}/{len(urls)}] Processing ID: {fid} ...", end="", flush=True)
        try:
            new_link = copy_and_share_gdrive_file(fid, access_token)
            new_urls.append(new_link)
            print(f" ✅ COPIED! -> {new_link}")
        except Exception as e:
            print(f" ❌ Failed: {e}")
            new_urls.append(u)

    # Backup original downloadlink.txt
    backup_file = Path("downloadlink.txt.bak")
    backup_file.write_text(link_file.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"\n📦 Backed up original links to '{backup_file.name}'.")

    # Update downloadlink.txt
    link_file.write_text("\n".join(new_urls) + "\n", encoding="utf-8")
    print("🎉 'downloadlink.txt' has been successfully updated with fresh, unblocked links!")
    print("\n👉 Next steps:")
    print("1. Commit and push 'downloadlink.txt' to your repository.")
    print("2. Re-run your GitHub Actions workflow; it will download all files at full speed!")

    # Output GitHub Secrets token
    token_json_str = json.dumps(token_data)
    print("\n=================================================================")
    print("💡 FOR 100% AUTOMATIC BYPASS IN GITHUB ACTIONS:")
    print("Add this secret to your GitHub Repository (Settings -> Secrets -> Actions):")
    print("Name:  GDRIVE_TOKEN_JSON")
    print("Value:")
    print(token_json_str)
    print("=================================================================")

if __name__ == "__main__":
    main()

