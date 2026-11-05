#!/usr/bin/env python3
"""
Google Drive Cookies Setup & Verification Tool
Use this script to verify your exported Google Drive cookies before adding them to GitHub Actions.

Usage:
    python scripts/setup_gdrive_cookies.py [path/to/cookies.txt]
"""

import sys
import os
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.downloader import load_gdrive_cookies

def main():
    print("=" * 65)
    print("🍪 Google Drive Authentication & Quota Bypass Verification")
    print("=" * 65)

    cookie_file = None
    if len(sys.argv) > 1:
        custom_path = Path(sys.argv[1])
        if custom_path.is_file():
            cookie_file = custom_path
            # Copy to cookies.txt in workspace
            target = PROJECT_ROOT / "cookies.txt"
            target.write_bytes(custom_path.read_bytes())
            print(f"✓ Copied '{custom_path}' to '{target.name}'.")

    if not cookie_file:
        cookie_file = load_gdrive_cookies()

    if not cookie_file:
        print("\n❌ No 'cookies.txt' found!")
        print("\n👉 How to get your Google Drive cookies:")
        print("1. In Google Chrome, install 'Get cookies.txt LOCALLY' extension")
        print("   (or 'Cookie-Editor').")
        print("2. Go to https://drive.google.com/ while signed in.")
        print("3. Click the extension icon and click 'Export' -> save as 'cookies.txt'.")
        print("4. Place 'cookies.txt' in this project folder, or run:")
        print("   python scripts/setup_gdrive_cookies.py path/to/your/cookies.txt")
        print("=" * 65)
        return

    print(f"\n📂 Found cookies file: {cookie_file.resolve()}")
    raw_content = cookie_file.read_text(encoding="utf-8", errors="replace")
    
    # Check for essential Google auth cookies
    has_auth = any(token in raw_content for token in ["SID", "HSID", "SSID", "APISID", "SAPISID", "NID"])
    if has_auth:
        print("✅ Valid Google authentication tokens detected in cookie file.")
    else:
        print("⚠️ Warning: Standard Google authentication tokens (SID, HSID) not found.")

    # Test download against one of the quota-restricted files
    test_id = "1yqLsjKXVBIJUbJblBmEcmjRwNKvpXsN9"
    test_url = f"https://drive.usercontent.google.com/download?id={test_id}&export=download&authuser=0"
    print(f"\n🌐 Testing download with cookies on restricted file (ID: {test_id})...")

    try:
        import requests
        session = requests.Session()
        session.trust_env = False
        load_gdrive_cookies(session)
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        res = session.get(test_url, headers=headers, stream=True, timeout=15)
        
        content_type = res.headers.get("content-type", "").lower()
        if "text/html" in content_type:
            # Check if virus scan warning form is presented
            html = next(res.iter_content(65536), b"").decode("utf-8", errors="replace")
            import re
            form_inputs = {}
            for m in re.finditer(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html):
                form_inputs[m.group(1)] = m.group(2)
            action_m = re.search(r'<form[^>]+action="([^"]+)"', html)
            action = action_m.group(1) if action_m else "https://drive.usercontent.google.com/download"
            
            if form_inputs and "confirm" in form_inputs:
                form_inputs.setdefault("authuser", "0")
                res = session.get(action, params=form_inputs, headers=headers, stream=True, timeout=15)
                content_type = res.headers.get("content-type", "").lower()

        if "text/html" not in content_type:
            first_chunk = next(res.iter_content(65536), b"")
            print(f"🎉 SUCCESS! Successfully authenticated and streamed {len(first_chunk)} bytes of video data.")
            print("\n📋 Next Step for GitHub Actions:")
            print("1. Go to your GitHub Repository -> Settings -> Secrets and variables -> Actions.")
            print("2. Click 'New repository secret'.")
            print("3. Name: GDRIVE_COOKIES")
            print("4. Value: Copy and paste the entire text inside 'cookies.txt'.")
            print("5. Click 'Add secret'. GitHub Actions will now bypass all Google Drive download limits!")
        else:
            print("❌ Google still returned HTML page. Please verify your cookies are fresh from a logged-in Google Drive tab.")
    except Exception as e:
        print(f"❌ Test encountered an error: {e}")

    print("=" * 65)

if __name__ == "__main__":
    main()

