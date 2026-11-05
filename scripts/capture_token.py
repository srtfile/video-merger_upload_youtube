#!/usr/bin/env python3
import urllib.parse
import webbrowser
import requests
import json
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

try:
    from core.downloader import HARDCODED_GDRIVE_TOKEN
    CLIENT_ID = HARDCODED_GDRIVE_TOKEN.get("client_id", "")
    CLIENT_SECRET = HARDCODED_GDRIVE_TOKEN.get("client_secret", "")
except Exception:
    CLIENT_ID = ""
    CLIENT_SECRET = ""
PORT = 8088
REDIRECT_URI = f"http://localhost:{PORT}"
SCOPE = "https://www.googleapis.com/auth/drive"

class OAuthHandler(BaseHTTPRequestHandler):
    code = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" in qs:
            OAuthHandler.code = qs["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body style='font-family:sans-serif;text-align:center;padding:50px;'><h2>&#x2705; Token Captured Successfully!</h2><p>You can close this tab and return to your terminal.</p></body></html>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No code received.")

    def log_message(self, format, *args):
        pass

def main():
    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={urllib.parse.quote(CLIENT_ID)}&"
        f"redirect_uri={urllib.parse.quote(REDIRECT_URI)}&"
        f"response_type=code&"
        f"scope={urllib.parse.quote(SCOPE)}&"
        f"access_type=offline&"
        f"prompt=consent"
    )

    print("=================================================================")
    print("🔑 ONE-TIME GOOGLE DRIVE AUTHORIZATION")
    print("=================================================================")
    print(f"Opening browser to authorize Google Drive...\n")
    print(f"URL: {auth_url}\n")
    print("Waiting for you to click 'Allow' in the browser...")

    server = HTTPServer(("localhost", PORT), OAuthHandler)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    server.handle_request()
    auth_code = OAuthHandler.code

    if not auth_code:
        print("❌ No authorization code received.")
        return

    print("✅ Authorization code received! Exchanging for permanent refresh token...")
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": auth_code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"❌ Token exchange failed: {resp.text}")
        return

    data = resp.json()
    token_dict = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": data.get("refresh_token"),
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": [SCOPE],
    }

    # Save to gdrive_token.json
    Path("gdrive_token.json").write_text(json.dumps(token_dict, indent=2), encoding="utf-8")
    print("💾 Saved to gdrive_token.json!")

    # Now directly embed inside video_joiner.py and core/downloader.py
    token_json_str = json.dumps(token_dict)
    
    for filepath in [Path("core/downloader.py"), Path("video_joiner.py")]:
        content = filepath.read_text(encoding="utf-8")
        # Replace HARDCODED_GDRIVE_TOKEN
        pattern = r'HARDCODED_GDRIVE_TOKEN = (None|\{.*?\})'
        replacement = f'HARDCODED_GDRIVE_TOKEN = {json.dumps(token_dict)}'
        if "HARDCODED_GDRIVE_TOKEN = " in content:
            new_content = re.sub(pattern, replacement, content)
        else:
            new_content = f'HARDCODED_GDRIVE_TOKEN = {json.dumps(token_dict)}\n' + content
        filepath.write_text(new_content, encoding="utf-8")
        print(f"✅ Blended permanent token directly into {filepath.name}!")

    print("\n🎉 ALL DONE! Your Google Drive credentials are now permanently embedded in the codebase.")
    print("You can commit and push to GitHub, and GitHub Actions will run without any secrets or logins!")

if __name__ == "__main__":
    main()

