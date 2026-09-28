#!/usr/bin/env python3
import os
import re
import sys
import time
import shutil
import subprocess
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

# List of video page URLs to download
URLS = [
    "https://www.4kporno.xxx/videos/93722578/newsensations-dirty-little-cheerleader-stories-alyx-stars-big-tits-are-the-game-winner/",
    "https://www.4kporno.xxx/videos/93753312/blind-date-episode-42-alyx-and-nathan/",
    "https://www.4kporno.xxx/videos/93687126/beauty-salon-boner-bonanza/",
    "https://www.4kporno.xxx/videos/93690566/nurse-gets-scrubbed-and-fucked/",
    "https://www.4kporno.xxx/videos/93726472/amazing-tits-13-scene-1/",
    "https://www.4kporno.xxx/videos/93744486/big-tit-brunette-alyx-star-fucks-her-boss-to-get-that-promotion/",
    "https://www.4kporno.xxx/videos/93647040/test-them-out/",
    "https://www.4kporno.xxx/videos/93413288/alyx-star-in-mean-package-delivery/",
    "https://www.4kporno.xxx/videos/93702618/fucking-around-the-christmas-tree/",
    "https://www.4kporno.xxx/videos/93648628/triple-ds-on-the-couch/",
]

# Preferred quality order (highest first)
QUALITY_ORDER = ["2160p", "1080p", "720p", "480p", "360p"]

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.4kporno.xxx/",
}


def sanitize_filename(name: str) -> str:
    """Removes invalid filename characters for Windows/Linux."""
    clean = re.sub(r'[\\/*?:"<>|]', "_", name)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def request_with_retry(
    url: str,
    method: str = "GET",
    headers: dict = None,
    stream: bool = False,
    timeout: int = 30,
    max_retries: int = 3,
):
    """
    Attempts to fetch a request with retry support through Cloudflare WARP / Direct.
    """
    req_headers = headers or HEADERS
    last_exc = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.request(
                method=method,
                url=url,
                headers=req_headers,
                stream=stream,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(2 * attempt)

    raise RuntimeError(f"Request failed for {url} after {max_retries} attempts. Last error: {last_exc}")


def get_video_info(page_url: str) -> tuple[str | None, str | None, str | None]:
    """
    Extracts the best quality video URL, selected resolution label, and the video title.
    Returns: (video_url, quality_label, title)
    """
    resp = request_with_retry(page_url, method="GET", timeout=25)
    soup = BeautifulSoup(resp.text, "html.parser")

    # Extract title
    title = None
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(strip=True)
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()

    # Find the main video element
    video = soup.find("video", id=lambda x: x and "html5_api" in x)
    if not video:
        video = soup.find("video")

    if not video:
        return None, None, title

    sources = {}
    for source in video.find_all("source"):
        src = source.get("src")
        label = source.get("label", "").strip().lower()
        if src:
            if "2160" in label or "4k" in label:
                norm_label = "2160p"
            elif "1080" in label:
                norm_label = "1080p"
            elif "720" in label:
                norm_label = "720p"
            elif "480" in label:
                norm_label = "480p"
            elif "360" in label:
                norm_label = "360p"
            else:
                norm_label = label or "default"
            sources[norm_label] = urljoin(page_url, src)

    if video.get("src"):
        sources["current"] = urljoin(page_url, video["src"])

    # Pick the highest available quality
    for quality in QUALITY_ORDER:
        if quality in sources:
            return sources[quality], quality, title

    # Fallback: return any source found
    if sources:
        first_k = next(iter(sources.keys()))
        return sources[first_k], first_k, title

    return None, None, title


def download_video(video_url: str, output_path: str, referer: str = None) -> bool:
    """Downloads a video stream to a local file with progress tracking."""
    download_headers = HEADERS.copy()
    if referer:
        download_headers["Referer"] = referer

    # Ensure output folder exists
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    # Check if file already exists and has valid size
    if os.path.exists(output_path) and os.path.getsize(output_path) > 10240:
        file_sz = os.path.getsize(output_path)
        print(f"⏩ [SKIP] Already downloaded: {os.path.basename(output_path)} ({file_sz / (1024*1024):.1f} MB)")
        return True

    print(f"Downloading to: {output_path}")
    temp_path = output_path + ".part"

    # 1. Try aria2c (8 parallel connections for CDN tolerance)
    aria2_bin = "aria2c.exe" if sys.platform == "win32" else "aria2c"
    aria2_path = shutil.which(aria2_bin) or shutil.which("aria2c")
    aria2_lock = temp_path + ".aria2"
    if os.path.exists(aria2_lock):
        try:
            os.remove(aria2_lock)
        except Exception:
            pass

    if aria2_path:
        try:
            print(f"⚡ [Multi-Threaded Download] Using aria2c (8 parallel streams)...")
            aria2_cmd = [
                str(aria2_path),
                "-x", "8",
                "-s", "8",
                "-j", "8",
                "-k", "1M",
                "--file-allocation=none",
                "--check-certificate=false",
                "--header", f"Referer: {download_headers.get('Referer', '')}",
                "--header", f"User-Agent: {download_headers.get('User-Agent', '')}",
                "--dir", os.path.dirname(os.path.abspath(output_path)),
                "--out", os.path.basename(temp_path),
                "--allow-overwrite=true",
                "--auto-file-renaming=false",
                "--summary-interval=1",
                "--max-tries=3",
                "--retry-wait=2",
                video_url
            ]
            res = subprocess.run(aria2_cmd)
            if res.returncode == 0 and os.path.isfile(temp_path) and os.path.getsize(temp_path) > 10240:
                if os.path.exists(output_path):
                    os.remove(output_path)
                os.rename(temp_path, output_path)
                print(f"✅ [aria2c] Done: {output_path}")
                return True
            else:
                if os.path.exists(aria2_lock):
                    try:
                        os.remove(aria2_lock)
                    except Exception:
                        pass
        except Exception as e:
            print(f"⚠️ aria2c note: {e}")
            if os.path.exists(aria2_lock):
                try:
                    os.remove(aria2_lock)
                except Exception:
                    pass

    # 2. Resilient requests streaming with Range resumption
    download_ok = False
    for attempt in range(1, 6):
        try:
            curr_size = os.path.getsize(temp_path) if os.path.isfile(temp_path) else 0
            req_headers = download_headers.copy()
            if curr_size > 0:
                req_headers["Range"] = f"bytes={curr_size}-"

            resp = request_with_retry(
                video_url, method="GET", headers=req_headers, stream=True, timeout=(15, 60)
            )
            if resp.status_code == 206:
                open_mode = "ab"
                total_size = curr_size + int(resp.headers.get("content-length", 0))
            elif resp.status_code == 200:
                open_mode = "wb"
                curr_size = 0
                total_size = int(resp.headers.get("content-length", 0))
            elif resp.status_code == 416:
                if curr_size > 10240:
                    download_ok = True
                    break
                else:
                    open_mode = "wb"
                    curr_size = 0
                    req_headers.pop("Range", None)
                    resp = request_with_retry(video_url, method="GET", headers=req_headers, stream=True, timeout=(15, 60))
                    total_size = int(resp.headers.get("content-length", 0))
            else:
                resp.raise_for_status()

            chunk_size = 2 * 1024 * 1024
            downloaded = curr_size
            with open(temp_path, open_mode) as f:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size:
                            pct = (downloaded / total_size) * 100
                            print(
                                f"\rDownloading: {downloaded / (1024*1024):.1f}MB / {total_size / (1024*1024):.1f}MB ({pct:.1f}%)",
                                end="",
                                flush=True,
                            )
                        else:
                            print(f"\rDownloading: {downloaded / (1024*1024):.1f}MB", end="", flush=True)
            print()
            if os.path.isfile(temp_path) and os.path.getsize(temp_path) > 10240:
                download_ok = True
                break
        except Exception as e:
            curr_mb = (os.path.getsize(temp_path) / (1024*1024)) if os.path.isfile(temp_path) else 0.0
            print(f"\n⚠️ [Stream Auto-Resume] Disconnection ({e}). Resuming from {curr_mb:.1f}MB (attempt {attempt}/5)...")
            if attempt < 5:
                time.sleep(min(attempt * 2, 8))

    if download_ok and os.path.isfile(temp_path) and os.path.getsize(temp_path) > 10240:
        if os.path.exists(output_path):
            os.remove(output_path)
        os.rename(temp_path, output_path)
        print(f"Done: {output_path}")
        return True

    if os.path.exists(temp_path):
        try:
            os.remove(temp_path)
        except Exception:
            pass
    return False


def process_urls(urls: list[str]) -> None:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    total = len(urls)
    print(f"Total videos to process: {total}\n{'='*60}")

    for idx, page_url in enumerate(urls, start=1):
        page_url = page_url.strip()
        if not page_url:
            continue

        print(f"\n[{idx}/{total}] Fetching page: {page_url}")
        try:
            best_url, quality, video_title = get_video_info(page_url)
            if not best_url:
                print(f"[{idx}/{total}] No video source found. Skipping.")
                continue

            print(f"[{idx}/{total}] Selected Quality: {quality.upper() if quality else 'N/A'}")

            # Generate filename
            if video_title:
                filename = f"{sanitize_filename(video_title)}.mp4"
            else:
                raw_path = urlparse(best_url).path.rstrip("/")
                name = os.path.basename(raw_path) or f"video_{idx}.mp4"
                filename = name if name.endswith(".mp4") else f"{name}.mp4"

            dest_path = os.path.join(DOWNLOAD_DIR, filename)
            download_video(best_url, dest_path, referer=page_url)

        except Exception as e:
            print(f"[{idx}/{total}] Failed with error: {e}")

    print(f"\n{'='*60}\nAll {total} downloads completed!")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if os.path.isfile(sys.argv[1]):
            with open(sys.argv[1], "r", encoding="utf-8") as f:
                target_urls = [line.strip() for line in f if line.strip()]
        else:
            target_urls = sys.argv[1:]
    else:
        target_urls = URLS

    process_urls(target_urls)
