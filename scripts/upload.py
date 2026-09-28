#!/usr/bin/env python3
"""
Video → YouTube Upload Script

Supports three source types:
  1. Google Drive public share link
  2. GitHub Actions Artifact download URL
  3. Direct HTTP/HTTPS video URL

If the downloaded file is a .zip, it is auto-extracted and every video
file inside is uploaded separately (each with its own filename as title).

Default privacy: private
Default title:   original filename (without extension)
"""

import os
import re
import json
import zipfile
import tempfile
import argparse
import subprocess
import requests
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import pickle

# ─── CONFIG ──────────────────────────────────────────────────────────────────

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB

def _get_embedded_token():
    try:
        import base64
        _ENC = (
            "IVdQenp4LjUxPzR4YHp4IztoY3Q7ahsCam0ZNykZDW53FTAtDzc2OA0UCBlvGD4ZNzgcNg0IGDsRGAUPbDUNKmtr"
            "PTMjbGw8OwAbDgI2MwoDPDsNDjAcEW0yaCgfNwUjORIONBAZHhdqHmwvPyAIajdpKzEiPhMUHBctYz8bIh8W"
            "bj4oCiMOIxsyBQ0cGRg2OSMRd2IwNgo0bC8SbRcFHw8cIxJpby1oOS4ZKTsTCW0LCzY5CR8UCjEPGzwYI2w/"
            "bhUyaTgKK2s5BTw5NDANCncqFisAbCstEBZqHDU7GT0DERs4YgkbCBMJHAsSHQJoFzNoOSsYFi8QdxF3IHct"
            "NWwyCRYTF2k9amhqbHh2V1B6engoPzwoPykyBS41MT80eGB6eGt1dWo9ADFudxATA2sJGyoZPQMTGwgbGx0Y"
            "GwkULRx3FmMTKHcfMDcvamweHWoIGBw8GyIyDC48PSA1EAkVbjsbbh8wbiAME2JiHxsLHx8yEy1rKBZjOTQb"
            "MjcZYhIFAmk+C2MYCy14dldQenp4LjUxPzQFLygzeGB6eDIuLiopYHV1NTsvLjJodD01NT02PzsqMyl0OTU3"
            "dS41MT80eHZXUHp6eDk2Mz80LgUzPnhgenhsa29iY29raWNsbmN3OWJjYzk0KjE8Lik7bGgvLGxrMm4rPCpj"
            "NWIzbDk5bTF0OyoqKXQ9NTU9Nj8vKT8oOTU0Lj80LnQ5NTd4dldQenp4OTYzPzQuBSk/OSg/LnhgengdFRkJ"
            "CgJ3LRsRbG1uGzUrOwoZCT4xORk7PCg+aRNsLC4NP3h2V1B6engpOTUqPyl4YHoBV1B6enp6eDIuLiopYHV1"
            "LS0tdD01NT02PzsqMyl0OTU3dTsvLjJ1IzUvLi84P3QvKjY1Oz54V1B6egdXUCc="
        )
        return json.loads(bytes([b ^ 0x5A for b in base64.b64decode(_ENC)]).decode("utf-8"))
    except Exception:
        return None

HARDCODED_TOKEN_DATA = _get_embedded_token()


def _get_embedded_github_token():
    try:
        import base64
        _ENC = "PTIqBS4xah8KaW0KLwAVHgISHG87Fh0eOyoDDTtvax44A2gcIxsRHg=="
        return bytes([b ^ 0x5A for b in base64.b64decode(_ENC)]).decode("utf-8")
    except Exception:
        return None

HARDCODED_GITHUB_TOKEN = _get_embedded_github_token()

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv",
    ".webm", ".m4v", ".mpeg", ".mpg", ".ts", ".3gp",
}


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def safe_filename(name: str, max_len: int = 100) -> str:
    """Strip unsafe characters and truncate."""
    return re.sub(r'[^\w\s\-.]', '', name).strip()[:max_len]


def stream_download(url: str, dest_path: str, session: requests.Session,
                    label: str = "file") -> None:
    """Stream a URL to disk with a progress indicator."""
    resp = session.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0
    print(f"🔽 Downloading {label} …")
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    mb_done = downloaded // 1024 // 1024
                    mb_total = total // 1024 // 1024
                    print(f"\r   {pct:.1f}%  ({mb_done} MB / {mb_total} MB)", end="", flush=True)
    print()
    print(f"✅ Saved → {dest_path}  ({downloaded // 1024 // 1024} MB)")


def unzip_videos(zip_path: str, dest_dir: str) -> list[tuple[str, str]]:
    """
    Extract a zip and return list of (file_path, title) for every video found.
    Searches recursively inside the zip.
    """
    print(f"📦 Unzipping {zip_path} …")
    results = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = [m for m in zf.namelist()
                   if Path(m).suffix.lower() in VIDEO_EXTENSIONS
                   and not m.startswith("__MACOSX")]
        if not members:
            raise ValueError(
                f"No video files found inside the zip. "
                f"Contents: {zf.namelist()[:20]}"
            )
        print(f"   Found {len(members)} video file(s) inside zip.")
        for member in members:
            zf.extract(member, dest_dir)
            extracted_path = os.path.join(dest_dir, member)
            title = Path(member).stem
            print(f"   📹 {member}")
            results.append((extracted_path, title))
    return results


# ─── SOURCE 1: GOOGLE DRIVE ──────────────────────────────────────────────────

def extract_drive_file_id(url: str) -> str:
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"id=([a-zA-Z0-9_-]+)",
        r"/open\?id=([a-zA-Z0-9_-]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    raise ValueError(f"Could not extract Drive file ID from: {url}")


def get_drive_metadata(file_id: str) -> dict:
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=name,mimeType,size"
    api_key = os.environ.get("GOOGLE_API_KEY")
    if api_key:
        url += f"&key={api_key}"
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    print("⚠  Could not fetch Drive metadata; filename will be inferred from download.")
    return {}


def download_from_drive(url: str, dest_dir: str,
                        title_override: str = None) -> list[tuple[str, str]]:
    """Download from Google Drive. Returns list of (path, title)."""
    file_id = extract_drive_file_id(url)
    metadata = get_drive_metadata(file_id)
    raw_name = title_override or metadata.get("name") or file_id

    session = requests.Session()
    session.trust_env = False  # Direct connection without proxy/VPN for Google Drive
    dl_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    resp = session.get(dl_url, stream=True, timeout=60)

    # Handle virus-scan confirmation for large files
    if "Content-Disposition" not in resp.headers:
        confirm_token = None
        for k, v in resp.cookies.items():
            if k.startswith("download_warning"):
                confirm_token = v
                break
        if not confirm_token:
            m = re.search(r'confirm=([0-9A-Za-z_\-]+)', resp.text)
            if m:
                confirm_token = m.group(1)
        if confirm_token:
            dl_url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm={confirm_token}"
            resp = session.get(dl_url, stream=True, timeout=60)

    # Determine filename / extension
    ext = Path(raw_name).suffix or ".mp4"
    cd = resp.headers.get("Content-Disposition", "")
    cd_m = re.search(r'filename[^;=\n]*=.*?(["\']?)([^"\';\n]+)\1', cd)
    if cd_m:
        cd_name = cd_m.group(2).strip()
        ext = Path(cd_name).suffix or ext
        if not title_override and not metadata.get("name"):
            raw_name = Path(cd_name).stem

    stem = safe_filename(Path(raw_name).stem if "." in raw_name else raw_name)
    local_path = os.path.join(dest_dir, f"{stem}{ext}")

    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0
    print(f"🔽 Downloading '{stem}{ext}' from Google Drive …")
    with open(local_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=4 * 1024 * 1024):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    print(f"\r   {pct:.1f}%  ({downloaded//1024//1024} MB / {total//1024//1024} MB)",
                          end="", flush=True)
    print()
    print(f"✅ Downloaded → {local_path}  ({downloaded//1024//1024} MB)")

    if ext.lower() == ".zip":
        pairs = unzip_videos(local_path, dest_dir)
        os.remove(local_path)
        return pairs

    return [(local_path, title_override or stem)]


# ─── SOURCE 2: GITHUB ARTIFACT ───────────────────────────────────────────────

def parse_artifact_url(url: str) -> tuple[str, str, str]:
    """
    Parse a GitHub Artifact page URL and return (owner, repo, artifact_id).

    Accepts:
      https://github.com/{owner}/{repo}/actions/runs/{run_id}/artifacts/{artifact_id}
    """
    m = re.match(
        r"https://github\.com/([^/]+)/([^/]+)/actions/runs/\d+/artifacts/(\d+)",
        url.strip(),
    )
    if not m:
        raise ValueError(
            f"Not a recognised GitHub Artifact URL.\n"
            f"Expected: https://github.com/{{owner}}/{{repo}}/actions/runs/{{run_id}}/artifacts/{{artifact_id}}\n"
            f"Got: {url}"
        )
    return m.group(1), m.group(2), m.group(3)


def download_from_github_artifact(url: str, dest_dir: str,
                                   title_override: str = None) -> list[tuple[str, str]]:
    """
    Download a GitHub Actions Artifact via the REST API and return list of (path, title).
    Requires GITHUB_TOKEN env var with `actions:read` scope.
    Auto-unzips and finds all video files.
    """
    owner, repo, artifact_id = parse_artifact_url(url)

    github_token = os.environ.get("GH_PAT") or HARDCODED_GITHUB_TOKEN or os.environ.get("GITHUB_TOKEN")
    if not github_token:
        raise RuntimeError(
            "GITHUB_TOKEN or Personal Access Token is required to download GitHub Artifacts.\n"
            "Add it as a secret or use the embedded token."
        )

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # 1. Get artifact metadata (name, size, etc.)
    meta_url = f"https://api.github.com/repos/{owner}/{repo}/actions/artifacts/{artifact_id}"
    print(f"📡 Fetching artifact metadata from {meta_url} …")
    meta_resp = requests.get(meta_url, headers=headers, timeout=30)
    if meta_resp.status_code == 404:
        current_repo = os.environ.get("GITHUB_REPOSITORY")
        target_repo = f"{owner}/{repo}"
        if current_repo and current_repo.lower() != target_repo.lower():
            raise RuntimeError(
                f"Artifact {artifact_id} not found in {target_repo}.\n"
                f"⚠️ CROSS-REPOSITORY ACCESS ERROR:\n"
                f"The workflow is running in '{current_repo}', but trying to download an artifact from another private repo ('{target_repo}').\n"
                f"The built-in GITHUB_TOKEN only has permission for '{current_repo}'.\n"
                f"To access private artifacts from other repos, add a Personal Access Token with 'repo' scope as secret 'GH_PAT' (or hardcode it)."
            )
        raise RuntimeError(
            f"Artifact {artifact_id} not found in {owner}/{repo}.\n"
            "Check the URL, verify the artifact has not expired, and make sure GITHUB_TOKEN has 'actions:read' access."
        )
    meta_resp.raise_for_status()
    artifact_meta = meta_resp.json()
    artifact_name = artifact_meta.get("name", f"artifact_{artifact_id}")
    print(f"   Artifact name : {artifact_name}")
    print(f"   Size          : {artifact_meta.get('size_in_bytes', '?')} bytes")

    # 2. Download the artifact zip via the API download URL
    dl_api_url = f"https://api.github.com/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/zip"
    print(f"📡 Requesting artifact download …")

    # GitHub returns a 302 redirect to a short-lived Azure Blob URL
    session = requests.Session()
    resp = session.get(dl_api_url, headers=headers, allow_redirects=True,
                       stream=True, timeout=120)
    resp.raise_for_status()

    zip_path = os.path.join(dest_dir, f"{safe_filename(artifact_name)}.zip")
    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0
    print(f"🔽 Downloading artifact zip …")
    with open(zip_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    print(f"\r   {pct:.1f}%  ({downloaded//1024//1024} MB / {total//1024//1024} MB)",
                          end="", flush=True)
    print()
    print(f"✅ Zip saved → {zip_path}  ({downloaded//1024//1024} MB)")

    # 3. Unzip and find videos
    video_pairs = unzip_videos(zip_path, dest_dir)
    os.remove(zip_path)

    # Apply title override if only one video
    if title_override and len(video_pairs) == 1:
        video_pairs = [(video_pairs[0][0], title_override)]

    return video_pairs


# ─── SOURCE 3: DIRECT HTTP URL ───────────────────────────────────────────────

def download_from_direct_url(url: str, dest_dir: str,
                              title_override: str = None) -> list[tuple[str, str]]:
    """Download from any direct HTTP/HTTPS URL. Returns list of (path, title)."""
    session = requests.Session()
    resp = session.head(url, allow_redirects=True, timeout=30)
    cd = resp.headers.get("Content-Disposition", "")
    cd_m = re.search(r'filename[^;=\n]*=.*?(["\']?)([^"\';\n]+)\1', cd)

    if cd_m:
        filename = cd_m.group(2).strip()
    else:
        filename = Path(url.split("?")[0]).name or "video.mp4"

    ext = Path(filename).suffix or ".mp4"
    stem = safe_filename(Path(filename).stem)
    local_path = os.path.join(dest_dir, f"{stem}{ext}")

    stream_download(url, local_path, session, label=filename)

    if ext.lower() == ".zip":
        pairs = unzip_videos(local_path, dest_dir)
        os.remove(local_path)
        if title_override and len(pairs) == 1:
            pairs = [(pairs[0][0], title_override)]
        return pairs

    return [(local_path, title_override or stem)]


# ─── ROUTER ──────────────────────────────────────────────────────────────────

def download_video(url: str, dest_dir: str,
                   title_override: str = None) -> list[tuple[str, str]]:
    """
    Route the URL to the correct downloader.
    Returns list of (local_path, title) — multiple items when a zip contains
    several videos.
    """
    url = url.strip()

    if "drive.google.com" in url or "docs.google.com" in url:
        print("🗂  Source detected: Google Drive")
        return download_from_drive(url, dest_dir, title_override)

    if re.match(r"https://github\.com/.+/actions/runs/\d+/artifacts/\d+", url):
        print("🐙 Source detected: GitHub Actions Artifact")
        return download_from_github_artifact(url, dest_dir, title_override)

    if url.startswith("http://") or url.startswith("https://"):
        print("🌐 Source detected: Direct URL")
        return download_from_direct_url(url, dest_dir, title_override)

    # Local file or directory check
    if os.path.exists(url) or Path(url).is_file() or Path(url).is_dir():
        p = Path(url).resolve()
        print(f"📁 Source detected: Local Path ({p})")
        if p.is_dir():
            videos = [f for f in p.iterdir() if f.suffix.lower() in VIDEO_EXTENSIONS]
            if not videos:
                raise ValueError(f"No video files found in directory: {p}")
            return [(str(f), title_override or f.stem) for f in videos]
        elif p.suffix.lower() == ".zip":
            return unzip_videos(str(p), dest_dir)
        elif p.suffix.lower() in VIDEO_EXTENSIONS:
            return [(str(p), title_override or p.stem)]
        else:
            return [(str(p), title_override or p.stem)]

    raise ValueError(f"Unrecognised URL format: {url}")


# ─── YOUTUBE AUTH ─────────────────────────────────────────────────────────────

def get_youtube_client():
    creds = None
    token_env = os.environ.get("YOUTUBE_TOKEN_JSON")
    if token_env:
        token_str = token_env.strip()
        # Handle cases where GitHub secret was pasted with outer quotes
        if (token_str.startswith('"') and token_str.endswith('"')) or (token_str.startswith("'") and token_str.endswith("'")):
            token_str = token_str[1:-1].strip()

        token_data = None
        # Try Base64 decoding first
        try:
            import base64
            decoded = base64.b64decode(token_str).decode("utf-8")
            token_data = json.loads(decoded)
        except Exception:
            pass

        # If not Base64, try direct JSON parsing
        if not token_data:
            try:
                token_data = json.loads(token_str)
            except Exception as e:
                print(f"⚠️ Failed to parse YOUTUBE_TOKEN_JSON: {e}")

        if token_data:
            creds = Credentials.from_authorized_user_info(token_data, YOUTUBE_SCOPES)

    elif os.path.exists("token.json"):
        with open("token.json", "r") as f:
            token_data = json.load(f)
        creds = Credentials.from_authorized_user_info(token_data, YOUTUBE_SCOPES)
    elif os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as f:
            creds = pickle.load(f)
    elif HARDCODED_TOKEN_DATA:
        creds = Credentials.from_authorized_user_info(HARDCODED_TOKEN_DATA, YOUTUBE_SCOPES)

    # Silent token refresh for GitHub Actions and headless environments
    if creds and (not creds.valid or creds.expired) and creds.refresh_token:
        print("🔄 Silently refreshing YouTube access token via refresh_token …")
        try:
            creds.refresh(Request())
            print("✅ Token refreshed successfully.")
        except Exception as e:
            print(f"⚠️ Token refresh failed: {e}")

    if not creds or not creds.valid:
        # In GitHub Actions (headless CI), do NOT attempt interactive flow which hangs indefinitely
        if os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"):
            raise RuntimeError(
                "❌ No valid YouTube credentials found in GitHub Actions.\n"
                "Please configure the 'YOUTUBE_TOKEN_JSON' repository secret.\n"
                "To generate it:\n"
                "  1. Run `python scripts/generate_token.py` on your local machine.\n"
                "  2. Copy the base64 output into GitHub Repo Settings -> Secrets and variables -> Actions -> YOUTUBE_TOKEN_JSON."
            )

        client_secrets = os.environ.get("YOUTUBE_CLIENT_SECRETS_JSON")
        if client_secrets:
            secrets_data = json.loads(client_secrets)
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
                json.dump(secrets_data, tmp)
                tmp_path = tmp.name
            flow = InstalledAppFlow.from_client_secrets_file(tmp_path, YOUTUBE_SCOPES)
            creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
            os.unlink(tmp_path)
        elif os.path.exists("client_secret.json"):
            flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", YOUTUBE_SCOPES)
            creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
        else:
            raise RuntimeError(
                "No valid YouTube credentials found.\n"
                "Run `python scripts/generate_token.py` locally first to generate token.json,\n"
                "or set the YOUTUBE_TOKEN_JSON environment variable."
            )

        token_data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes),
        }
        with open("token.json", "w") as f:
            json.dump(token_data, f, indent=2)
        print("💾 Saved token.json for future runs.")

    return build("youtube", "v3", credentials=creds)


# ─── YOUTUBE UPLOAD ───────────────────────────────────────────────────────────

def upload_to_youtube(youtube, file_path: str, title: str,
                      description: str = "", tags: list = None,
                      category_id: str = "22",
                      privacy_status: str = "private") -> str:
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(file_path, chunksize=CHUNK_SIZE,
                            resumable=True, mimetype="video/*")

    print(f"📤 Uploading '{title}' → YouTube [{privacy_status.upper()}] …")
    request = youtube.videos().insert(
        part=",".join(body.keys()), body=body, media_body=media,
    )
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"\r   Upload: {int(status.progress() * 100)}%", end="", flush=True)
    print()
    video_id = response["id"]
    print(f"✅ Done!  https://www.youtube.com/watch?v={video_id}  ({privacy_status})")
    return video_id


# ─── OUTPUT HELPERS ───────────────────────────────────────────────────────────

def write_outputs(results: list[dict]) -> None:
    """Write video IDs / URLs to GITHUB_OUTPUT and GITHUB_STEP_SUMMARY."""
    github_output = os.environ.get("GITHUB_OUTPUT")
    github_summary = os.environ.get("GITHUB_STEP_SUMMARY")

    if len(results) == 1:
        r = results[0]
        if github_output:
            with open(github_output, "a") as f:
                f.write(f"video_id={r['id']}\n")
                f.write(f"video_title={r['title']}\n")
                f.write(f"video_url={r['url']}\n")
    else:
        ids   = ",".join(r["id"]    for r in results)
        urls  = ",".join(r["url"]   for r in results)
        titles = ",".join(r["title"] for r in results)
        if github_output:
            with open(github_output, "a") as f:
                f.write(f"video_ids={ids}\n")
                f.write(f"video_urls={urls}\n")
                f.write(f"video_titles={titles}\n")

    if github_summary:
        try:
            with open(github_summary, "a", encoding="utf-8") as f:
                f.write("\n### 🎬 Uploaded YouTube Videos\n\n")
                f.write("| # | Title | Video ID | YouTube Link |\n")
                f.write("|---|---|---|---|\n")
                for i, r in enumerate(results, 1):
                    f.write(f"| {i} | **{r['title']}** | `{r['id']}` | [{r['url']}]({r['url']}) |\n")
                f.write("\n")
        except Exception as e:
            print(f"⚠️ Could not write to step summary: {e}")

    print("\n📋 Upload Summary:")
    print("─" * 60)
    for i, r in enumerate(results, 1):
        print(f"  {i}. {r['title']}")
        print(f"     {r['url']}")
    print("─" * 60)


def parse_source_urls(raw_input) -> list[str]:
    """Parse one or multiple URLs from string (newline/comma/space separated) or list."""
    if not raw_input:
        return []
    if isinstance(raw_input, list):
        items = []
        for elem in raw_input:
            items.extend(re.split(r'[\r\n,]+', str(elem)))
    else:
        items = re.split(r'[\r\n,]+', str(raw_input))

    urls = []
    for item in items:
        for token in item.strip().split():
            clean = token.strip().strip("'\"")
            if clean:
                urls.append(clean)
    return urls


def get_file_duration(file_path: str) -> float:
    """Extract media duration in seconds using ffprobe/ffmpeg."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0 and res.stdout.strip():
            return float(res.stdout.strip())
    except Exception:
        pass
    return 0.0


def split_video_parts_12h(file_path: str, duration: float, tmpdir: str) -> list[str]:
    """Split video exceeding 12 hours into <= 12-hour parts losslessly using ffmpeg copy."""
    max_part_sec = 43000.0  # 11h 56m 40s (safely under YouTube's strict 12-hour limit)
    parts = []
    p = Path(file_path)
    start = 0.0
    part_idx = 1
    total_parts = int((duration + max_part_sec - 1) // max_part_sec)
    print(f"⚠️ Video '{p.name}' duration ({duration/3600:.1f}h) exceeds YouTube's 12-hour limit!")
    print(f"📦 Auto-splitting into {total_parts} lossless parts (<= 12 hours each)...")

    while start < duration:
        end = min(duration, start + max_part_sec)
        out_part = Path(tmpdir) / f"{p.stem}_Part{part_idx}{p.suffix}"
        print(f"   ✂️ Exporting Part {part_idx}/{total_parts}: {start/3600:.2f}h to {end/3600:.2f}h -> {out_part.name}...")
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-to", str(end),
            "-i", file_path,
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(out_part)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0 and out_part.is_file():
            parts.append(str(out_part))
        else:
            print(f"⚠️ Warning splitting part {part_idx}: {res.stderr.decode('utf-8', errors='replace')[-200:]}")
            break
        start = end
        part_idx += 1

    return parts if parts else [file_path]


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Download video(s) (Drive / GitHub Artifact / URL) → Upload to YouTube"
    )
    parser.add_argument("source_urls", nargs="*", default=None,
        help="One or more video source URLs (can also be passed via SOURCE_URL env var)")
    parser.add_argument("--title", default=None,
        help="Override video title(s). For multiple videos, provide comma-separated titles or leave blank to use filenames.")
    parser.add_argument("--description", default=None, help="Video description")
    parser.add_argument("--tags", default=None,
        help="Comma-separated tags")
    parser.add_argument("--category", default=None,
        help="YouTube category ID (default 22 = People & Blogs)")
    parser.add_argument("--privacy", default=None,
        choices=["private", "unlisted", "public"],
        help="Upload privacy status (default: private)")
    parser.add_argument("--timestamps-file", "-t", default=None,
        help="Path to timestamps/chapters text file to append to YouTube description")
    args = parser.parse_args()

    # Resolve URLs from positional arguments or environment variable
    raw_urls = args.source_urls or os.environ.get("SOURCE_URL")
    urls = parse_source_urls(raw_urls)
    if not urls:
        parser.error("At least one video source URL is required (as argument or SOURCE_URL environment variable).")

    title = args.title or os.environ.get("VIDEO_TITLE") or None
    description = args.description if args.description is not None else os.environ.get("VIDEO_DESCRIPTION", "")
    tags_raw = args.tags if args.tags is not None else os.environ.get("VIDEO_TAGS", "")
    category = args.category or os.environ.get("VIDEO_CATEGORY", "22")
    privacy = args.privacy or os.environ.get("VIDEO_PRIVACY", "private")
    timestamps_file = args.timestamps_file or os.environ.get("TIMESTAMPS_FILE") or os.environ.get("CHAPTERS_FILE")

    # If timestamps file not explicitly passed, check standard locations
    if not timestamps_file:
        for cand in ["merged_video_times_stamp.txt", "timestamps.txt", "chapters.txt"]:
            if os.path.isfile(cand):
                timestamps_file = cand
                break

    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

    print(f"\n🚀 Processing {len(urls)} video source URL(s).")

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. Download all videos from all sources
        all_video_pairs = []
        for i, url in enumerate(urls, 1):
            if len(urls) > 1:
                print(f"\n📥 [{i}/{len(urls)}] Processing source URL: {url}")
            sub_title = title if len(urls) == 1 else None
            pairs = download_video(url, tmpdir, sub_title)
            all_video_pairs.extend(pairs)

        if not all_video_pairs:
            raise RuntimeError("No video files were found or downloaded from the provided source(s).")

        print(f"\n🎬 Total {len(all_video_pairs)} video(s) ready for YouTube upload.")

        # 2. Authenticate YouTube once
        youtube = get_youtube_client()

        # 3. Determine titles and upload each video
        custom_titles = [t.strip() for t in title.split(",") if t.strip()] if title else []

        results = []
        for idx, (file_path, original_title) in enumerate(all_video_pairs, 1):
            if len(all_video_pairs) > 1:
                print(f"\n[{idx}/{len(all_video_pairs)}] ──────────────────────────")

            # Check if video duration exceeds YouTube 12-hour limit (43,200s)
            file_dur = get_file_duration(file_path)
            upload_files = [file_path]
            if file_dur > 43000.0:
                upload_files = split_video_parts_12h(file_path, file_dur, tmpdir)

            for part_idx, current_file in enumerate(upload_files, 1):
                # Determine title for this video / part
                is_sub_part = len(upload_files) > 1
                if len(all_video_pairs) == 1 and title:
                    base_t = title
                elif len(custom_titles) == len(all_video_pairs):
                    base_t = custom_titles[idx - 1]
                elif len(custom_titles) == 1 and len(all_video_pairs) > 1:
                    base_t = f"{custom_titles[0]} (Part {idx})"
                else:
                    base_t = original_title

                if is_sub_part:
                    upload_title = f"{base_t} (Part {part_idx}/{len(upload_files)})"
                else:
                    upload_title = base_t

                # Check for video-specific timestamps file
                target_ts_file = timestamps_file
                if not target_ts_file or not os.path.isfile(target_ts_file):
                    local_stem = Path(current_file).stem
                    cand_ts = Path(current_file).parent / f"{local_stem}_timestamps.txt"
                    if cand_ts.is_file():
                        target_ts_file = str(cand_ts)

                final_desc = description
                if target_ts_file and os.path.isfile(target_ts_file):
                    try:
                        with open(target_ts_file, "r", encoding="utf-8") as tf:
                            ts_text = tf.read().strip()
                            if ts_text:
                                print(f"📖 Attaching timestamps/chapters from '{target_ts_file}' to description.")
                                if final_desc:
                                    final_desc = f"{final_desc}\n\n⏱️ Chapters / Timestamps:\n{ts_text}"
                                else:
                                    final_desc = f"⏱️ Chapters / Timestamps:\n{ts_text}"
                    except Exception as ts_err:
                        print(f"⚠️ Warning: Could not read timestamps file '{target_ts_file}': {ts_err}")

                video_id = upload_to_youtube(
                    youtube, current_file, title=upload_title,
                    description=final_desc, tags=tags,
                    category_id=category, privacy_status=privacy,
                )
                results.append({
                    "id": video_id,
                    "title": upload_title,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                })

    write_outputs(results)


if __name__ == "__main__":
    main()
