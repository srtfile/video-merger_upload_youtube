"""
Google Drive & URL Downloader Engine
Parses Google Drive links, extracts file/folder IDs, and downloads videos with resume,
large-file confirmation, folder batch-downloading, and automatic format detection.
"""

import os
import re
import sys
import json
import time
import html
import tempfile
import urllib.parse
import subprocess
import shutil
import random
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Callable, Dict, Any, Tuple, Union

from core.sorter import SUPPORTED_EXTENSIONS, is_html_or_empty_file
from core.ffmpeg_utils import find_binary
from core.probe import probe_file


def extract_ffmpeg_error(err_str: str) -> str:
    """Extract last error line from ffmpeg output snippet."""
    if not err_str:
        return "Unknown error"
    lines = [line.strip() for line in err_str.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "Unknown error"


# Try importing requests, bs4, tqdm, and gdown
try:
    import requests
except ImportError:
    requests = None

try:
    import gdown
except ImportError:
    gdown = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


def extract_gdrive_id(url: str) -> Optional[str]:
    """
    Extract Google Drive File or Folder ID from various URL formats.
    Supported patterns:
    - https://drive.google.com/file/d/FILE_ID/view?usp=sharing
    - https://drive.google.com/open?id=FILE_ID
    - https://drive.google.com/uc?id=FILE_ID&export=download
    - https://drive.google.com/drive/folders/FOLDER_ID
    - https://drive.google.com/drive/u/0/folders/FOLDER_ID
    - Raw ID string (alphanumeric, length >= 25)
    """
    url = url.strip()
    
    # 1. /file/d/ID/ or /d/ID/ or /folders/ID
    m = re.search(r"(?:/file/d/|/d/|/folders/)([a-zA-Z0-9_-]{25,})", url)
    if m:
        return m.group(1)
        
    # 2. ?id=ID or &id=ID
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]{25,})", url)
    if m:
        return m.group(1)
        
    # 3. Direct raw ID string (alphanumeric with dash/underscore, length >= 25)
    if re.match(r"^[a-zA-Z0-9_-]{25,}$", url):
        return url
        
    return None


def is_gdrive_folder(url: str) -> bool:
    """Check if the URL points to a Google Drive folder."""
    return bool(re.search(r"(?:/drive/(?:u/\d+/)?folders/|/folders/)", url.strip()))


def extract_urls_from_text(text: str) -> List[str]:
    """
    Extract video and Google Drive URLs from multiline, comma-separated,
    semicolon-separated, or space-separated strings.
    """
    if not text:
        return []
        
    urls: List[str] = []
    # Normalize common separators
    normalized = text.replace(",", "\n").replace(";", "\n")
    for raw_line in normalized.splitlines():
        line = raw_line.strip().strip('"').strip("'").strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
            
        # Support space-separated items on single line
        tokens = line.split()
        for token in tokens:
            token = token.strip().strip('"').strip("'").strip()
            if not token:
                continue
            if token.startswith("http://") or token.startswith("https://") or extract_gdrive_id(token):
                if token not in urls:
                    urls.append(token)
                    
    return urls


@dataclass
class BatchGroup:
    name: str           # Clean filename stem, e.g. 'angel_moy'
    raw_label: str      # Original label, e.g. 'this is angel moy'
    urls: List[str]     # List of video/gdrive URLs
    index: int = 1      # 1-based batch index


def normalize_group_label(label: str) -> str:
    """Normalize label for comparison (strips leading numbers, extra spaces, and 'this is')."""
    cleaned = re.sub(r"\s+", " ", label.strip().lower())
    cleaned = re.sub(r"^\d+[\s\.\)\-_:]*", "", cleaned)
    cleaned = re.sub(r"^(?:this\s+is\s+|video\s+is\s+)", "", cleaned)
    return cleaned.strip()


def sanitize_group_name(label: str) -> str:
    """
    Sanitize group label to a clean snake_case filename stem.
    e.g. '1  this is angel moy' -> 'angel_moy'
         'this is bonka koy' -> 'bonka_koy'
    """
    cleaned = label.strip()
    cleaned = re.sub(r"^\d+[\s\.\)\-_:]*", "", cleaned)
    cleaned = re.sub(r"^(?:this\s+is\s+|video\s+is\s+)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[\s\-]+", "_", cleaned.strip())
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "", cleaned).strip("_")
    return cleaned.lower() or "merged_video"


def parse_batch_groups(file_path_or_content: Union[str, Path]) -> List[BatchGroup]:
    """
    Parse a links file or raw text into distinct batch groups.
    Supports patterns where each group has an opening and optional closing label, e.g.:
                 1    this is angel moy
                 <url 1>
                 <url 2>
                      this is angel moy
                 2    this is bonka koy
                 <url 1>
                 ...
    """
    if isinstance(file_path_or_content, Path) or (isinstance(file_path_or_content, str) and "\n" not in file_path_or_content and Path(file_path_or_content).is_file()):
        content = Path(file_path_or_content).read_text(encoding="utf-8", errors="replace")
    else:
        content = str(file_path_or_content)

    lines = content.splitlines()
    groups: List[BatchGroup] = []
    current_label: Optional[str] = None
    current_urls: List[str] = []

    def is_link(token: str) -> bool:
        return token.startswith("http://") or token.startswith("https://") or bool(extract_gdrive_id(token))

    for raw_line in lines:
        line = raw_line.strip().strip('"').strip("'").strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue

        # Check if line is a URL or Drive ID
        # Extract potential URLs from the line
        extracted = []
        tokens = line.split()
        for token in tokens:
            t = token.strip().strip('"').strip("'").strip()
            if is_link(t):
                extracted.append(t)

        if extracted:
            for u in extracted:
                if u not in current_urls:
                    current_urls.append(u)
        else:
            # Line is a text label/delimiter
            label_text = line
            # Check if this matches the current group label (closing marker)
            if current_label is not None and normalize_group_label(label_text) == normalize_group_label(current_label) and current_urls:
                groups.append(BatchGroup(
                    name=sanitize_group_name(current_label),
                    raw_label=current_label,
                    urls=list(current_urls)
                ))
                current_label = None
                current_urls = []
            elif current_label is not None and current_urls:
                # Started a new group without an explicit closing marker
                groups.append(BatchGroup(
                    name=sanitize_group_name(current_label),
                    raw_label=current_label,
                    urls=list(current_urls)
                ))
                current_label = label_text
                current_urls = []
            else:
                # Start or update current label
                current_label = label_text

    # Tail group if any URLs were accumulated
    if current_label is not None and current_urls:
        groups.append(BatchGroup(
            name=sanitize_group_name(current_label),
            raw_label=current_label,
            urls=list(current_urls)
        ))
    elif not groups and current_urls:
        groups.append(BatchGroup(
            name="merged_video",
            raw_label="merged_video",
            urls=list(current_urls)
        ))

    for idx, g in enumerate(groups, start=1):
        g.index = idx

    return groups


def parse_range_selection(target_str: str, total_count: int) -> List[int]:
    """
    Parse a range or list of batch indices (1-based).
    Supports formats:
      - 'all' or '' -> all indices [1, 2, ..., total_count]
      - '1' -> [1]
      - '1 to 3' or '1-3' or '1..3' -> [1, 2, 3]
      - '1, 3, 5' -> [1, 3, 5]
      - '1-2, 4' -> [1, 2, 4]
    """
    target = target_str.strip().lower()
    if not target or target == "all":
        return list(range(1, total_count + 1))

    normalized = target.replace(" to ", "-").replace("..", "-")
    selected = set()
    parts = [p.strip() for p in re.split(r"[,;]+", normalized) if p.strip()]
    for part in parts:
        if "-" in part:
            sub = part.split("-", 1)
            try:
                start = int(sub[0].strip())
                end = int(sub[1].strip())
                if start > end:
                    start, end = end, start
                for idx in range(start, end + 1):
                    if 1 <= idx <= total_count:
                        selected.add(idx)
            except ValueError:
                pass
        else:
            try:
                idx = int(part)
                if 1 <= idx <= total_count:
                    selected.add(idx)
            except ValueError:
                pass
    return sorted(list(selected)) if selected else list(range(1, total_count + 1))


def parse_links_file(file_path: Path) -> List[str]:
    """
    Read URLs from a text file, ignoring comments (#) and blank lines.
    """
    if not file_path.is_file():
        raise FileNotFoundError(f"Links file not found: {file_path}")
        
    content = file_path.read_text(encoding="utf-8", errors="replace")
    return extract_urls_from_text(content)


def detect_video_extension(path: Path) -> str:
    """
    Determine appropriate video file extension by inspecting container magic bytes.
    Defaults to '.mp4' if unrecognized video stream.
    """
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return ".mp4"
        with open(path, "rb") as f:
            header = f.read(188 * 4)
            if len(header) >= 8:
                if len(header) >= 12 and header[4:8] == b"ftyp":
                    return ".mp4"
                if header.startswith(b"\x1a\x45\xdf\xa3"):
                    return ".mkv"
                if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"AVI ":
                    return ".avi"
                if header.startswith(b"FLV"):
                    return ".flv"
                if header[0] == 0x47:
                    if (len(header) >= 376 and header[188] == 0x47 and header[376] == 0x47) or \
                       (len(header) >= 384 and header[192] == 0x47) or \
                       (len(header) >= 408 and header[204] == 0x47):
                        return ".ts"
    except Exception:
        pass
    return ".mp4"


def ensure_video_extension(file_path: Path, default_ext: str = ".mp4") -> Path:
    """
    Ensure the downloaded file has a valid video file extension recognized by the joiner.
    If extension is missing or generic (e.g. .bin, .tmp), auto-detects container format
    and renames the file accordingly.
    """
    if file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
        return file_path
        
    detected_ext = detect_video_extension(file_path) or default_ext
    target_path = file_path.with_name(f"{file_path.name}{detected_ext}")
    
    # If target already exists, append unique counter
    counter = 1
    while target_path.exists() and target_path != file_path:
        target_path = file_path.with_name(f"{file_path.stem}_{counter}{detected_ext}")
        counter += 1
        
    try:
        file_path.rename(target_path)
        return target_path
    except Exception:
        return file_path


def format_size(size_bytes: int) -> str:
    """Format bytes into MB or GB."""
    mb = size_bytes / (1024 * 1024)
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.1f} MB"


def is_ts_file(file_path: Path) -> bool:
    """Check if file is an MPEG-TS video by extension or transport packet sync bytes."""
    if not file_path.is_file():
        return False
    if file_path.suffix.lower() in (".ts", ".mts", ".m2ts"):
        return True
    try:
        if file_path.stat().st_size < 188:
            return False
        with open(file_path, "rb") as f:
            chunk = f.read(188 * 4)
            if len(chunk) >= 188 and chunk[0] == 0x47:
                if (len(chunk) >= 376 and chunk[188] == 0x47 and chunk[376] == 0x47) or \
                   (len(chunk) >= 384 and chunk[192] == 0x47) or \
                   (len(chunk) >= 408 and chunk[204] == 0x47):
                    return True
    except Exception:
        pass
    return False


def convert_ts_to_mp4(
    file_path: Path,
    ffmpeg_exe: Optional[Path] = None,
    ffprobe_exe: Optional[Path] = None,
    delete_original: bool = True
) -> Path:
    """
    Convert a .ts (MPEG Transport Stream) file to .mp4 format with 5-stage resilience:
    1. Fast lossless stream remuxing (-c copy + aac_adtstoasc).
    2. Smart Remux (-c:v copy -c:a aac): preserves 100% video quality with AAC audio.
    3. Visually lossless transcode fallback (CRF 17, H.264/AAC).
    4. Deep Scan Transcode (-f mpegts -probesize 1000M + auto-mapping).
    5. Video-only recovery (-an) if audio is hopelessly corrupted.
    Deletes the original .ts file upon successful conversion.
    """
    if not is_ts_file(file_path):
        return file_path

    if not file_path.is_file() or file_path.stat().st_size == 0:
        return file_path

    if is_html_or_empty_file(file_path):
        print(f"⚠️ Skipping '{file_path.name}': file is empty or an HTML error page.")
        return file_path

    if not ffmpeg_exe:
        ffmpeg_exe = find_binary("ffmpeg")
    if not ffprobe_exe:
        ffprobe_exe = find_binary("ffprobe")

    if not ffmpeg_exe or not ffmpeg_exe.is_file():
        print(f"⚠️ FFmpeg binary not found; keeping original TS file: {file_path.name}")
        return file_path

    target_mp4 = file_path.with_suffix(".mp4")
    if target_mp4 == file_path or target_mp4.exists():
        counter = 1
        while target_mp4.exists() and target_mp4 != file_path:
            target_mp4 = file_path.with_name(f"{file_path.stem}_{counter}.mp4")
            counter += 1

    print(f"🔄 Detected MPEG-TS video ('{file_path.name}'). Converting to MP4 format...")

    # Probe file to detect streams and codecs
    audio_is_aac = False
    video_is_compatible = True
    try:
        info = probe_file(file_path, ffprobe_exe, ffmpeg_exe)
        if info and info.audio and info.audio.codec:
            audio_is_aac = ("aac" in info.audio.codec.lower())
        if info and info.video and info.video.codec:
            v_codec = info.video.codec.lower()
            video_is_compatible = any(k in v_codec for k in ("264", "avc", "265", "hevc", "av1", "vp9", "mp4v"))
    except Exception:
        pass

    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    ts_input_opts = [
        "-hide_banner",
        "-probesize", "500M",
        "-analyzeduration", "500M",
        "-fflags", "+genpts+discardcorrupt+igndts",
        "-err_detect", "ignore_err",
        "-scan_all_pmts", "1",
        "-resync_size", "100M",
    ]

    remux_ok = False
    last_err_msg = ""

    # Attempt 1: True Lossless Stream Copy (Remuxing)
    if video_is_compatible:
        remux_cmd = [str(ffmpeg_exe), "-y"] + ts_input_opts + [
            "-i", str(file_path),
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-c:v", "copy",
            "-c:a", "copy"
        ]
        remux_cmd.extend(["-bsf:a", "aac_adtstoasc"])
        remux_cmd.extend([
            "-avoid_negative_ts", "make_zero",
            "-max_muxing_queue_size", "4096",
            "-movflags", "+faststart",
            str(target_mp4)
        ])

        try:
            proc = subprocess.run(
                remux_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                startupinfo=startupinfo,
                timeout=300
            )
            if proc.returncode == 0 and target_mp4.is_file() and target_mp4.stat().st_size > 0:
                remux_ok = True
            else:
                last_err_msg = f"{proc.stderr.strip()}\n{proc.stdout.strip()}"
        except Exception as e:
            last_err_msg = str(e)

    # Attempt 2: Smart Remux (Lossless Video Copy + Audio Transcode to AAC)
    if not remux_ok and video_is_compatible:
        if target_mp4.exists():
            target_mp4.unlink(missing_ok=True)
        smart_cmd = [str(ffmpeg_exe), "-y"] + ts_input_opts + [
            "-i", str(file_path),
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-avoid_negative_ts", "make_zero",
            "-max_muxing_queue_size", "4096",
            "-movflags", "+faststart",
            str(target_mp4)
        ]
        try:
            proc = subprocess.run(
                smart_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                startupinfo=startupinfo,
                timeout=300
            )
            if proc.returncode == 0 and target_mp4.is_file() and target_mp4.stat().st_size > 0:
                remux_ok = True
            else:
                last_err_msg = f"{proc.stderr.strip()}\n{proc.stdout.strip()}"
        except Exception as e:
            last_err_msg = str(e)

    # Attempt 3: Visually Lossless Transcode (CRF 17 fallback for MPEG-2 or damaged streams)
    if not remux_ok:
        if target_mp4.exists():
            target_mp4.unlink(missing_ok=True)
        print(f"  ⚡ Remuxing direct copy not applicable/failed; transcoding '{file_path.name}' to MP4 (CRF 17)...")
        transcode_cmd = [str(ffmpeg_exe), "-y"] + ts_input_opts + [
            "-i", str(file_path),
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-crf", "17",
            "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-af", "aresample=async=1:first_pts=0,pan=stereo|c0=c0|c1=c1",
            "-c:a", "aac",
            "-b:a", "192k",
            "-avoid_negative_ts", "make_zero",
            "-max_muxing_queue_size", "4096",
            "-movflags", "+faststart",
            str(target_mp4)
        ]
        try:
            proc = subprocess.run(
                transcode_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                startupinfo=startupinfo,
                timeout=600
            )
            if proc.returncode == 0 and target_mp4.is_file() and target_mp4.stat().st_size > 0:
                remux_ok = True
            else:
                last_err_msg = f"{proc.stderr.strip()}\n{proc.stdout.strip()}"
        except Exception as e:
            last_err_msg = str(e)

    # Attempt 4: Deep Scan Transcode (-f mpegts with auto stream mapping and 1GB probe buffers)
    if not remux_ok:
        if target_mp4.exists():
            target_mp4.unlink(missing_ok=True)
        print(f"  ⚡ Deep scan transcode fallback for '{file_path.name}'...")
        deep_cmd = [
            str(ffmpeg_exe), "-y",
            "-hide_banner",
            "-f", "mpegts",
            "-probesize", "1000M",
            "-analyzeduration", "1000M",
            "-fflags", "+genpts+discardcorrupt+igndts",
            "-err_detect", "ignore_err",
            "-scan_all_pmts", "1",
            "-resync_size", "200M",
            "-i", str(file_path),
            "-c:v", "libx264",
            "-crf", "17",
            "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-avoid_negative_ts", "make_zero",
            "-max_muxing_queue_size", "4096",
            "-movflags", "+faststart",
            str(target_mp4)
        ]
        try:
            proc = subprocess.run(
                deep_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                startupinfo=startupinfo,
                timeout=600
            )
            if proc.returncode == 0 and target_mp4.is_file() and target_mp4.stat().st_size > 0:
                remux_ok = True
            else:
                last_err_msg = f"{proc.stderr.strip()}\n{proc.stdout.strip()}"
        except Exception as e:
            last_err_msg = str(e)

    # Attempt 5: Emergency Video-Only Recovery (-an) if audio stream packets are completely corrupt
    if not remux_ok:
        if target_mp4.exists():
            target_mp4.unlink(missing_ok=True)
        v_only_cmd = [
            str(ffmpeg_exe), "-y",
            "-hide_banner",
            "-f", "mpegts",
            "-probesize", "1000M",
            "-analyzeduration", "1000M",
            "-fflags", "+genpts+discardcorrupt+igndts",
            "-err_detect", "ignore_err",
            "-scan_all_pmts", "1",
            "-resync_size", "200M",
            "-i", str(file_path),
            "-an",
            "-c:v", "libx264",
            "-crf", "17",
            "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-avoid_negative_ts", "make_zero",
            "-max_muxing_queue_size", "4096",
            "-movflags", "+faststart",
            str(target_mp4)
        ]
        try:
            proc = subprocess.run(
                v_only_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                startupinfo=startupinfo,
                timeout=600
            )
            if proc.returncode == 0 and target_mp4.is_file() and target_mp4.stat().st_size > 0:
                remux_ok = True
                print(f"  ⚠️ Audio corrupted or missing; recovered video track for '{file_path.name}'")
            else:
                last_err_msg = f"{proc.stderr.strip()}\n{proc.stdout.strip()}"
        except Exception as e:
            last_err_msg = str(e)

    if remux_ok and target_mp4.is_file() and target_mp4.stat().st_size > 0:
        print(f"✓ Converted '{file_path.name}' -> '{target_mp4.name}' ({format_size(target_mp4.stat().st_size)})")
        if delete_original and file_path != target_mp4:
            try:
                file_path.unlink()
            except Exception:
                pass
        return target_mp4
    else:
        err_snippet = extract_ffmpeg_error(last_err_msg)
        print(f"❌ Failed to convert '{file_path.name}' to MP4 ({err_snippet}). Retaining original file.")
        if target_mp4.exists() and target_mp4 != file_path:
            try:
                target_mp4.unlink(missing_ok=True)
            except Exception:
                pass
        return file_path


def get_filename_from_cd(cd_header: Optional[str]) -> Optional[str]:
    """Extract filename from Content-Disposition header."""
    if not cd_header:
        return None
    m = re.search(r'filename\*=UTF-8\'\'([^;]+)', cd_header, re.IGNORECASE)
    if m:
        return urllib.parse.unquote(m.group(1))
    m = re.search(r'filename="?([^";]+)"?', cd_header, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def is_valid_video_file(file_path: Union[str, Path]) -> bool:
    """
    Check if a file exists, is not empty/corrupted/HTML error page,
    and is a valid video file of substantial size (> 10KB).
    """
    if not file_path:
        return False
    p = Path(file_path) if isinstance(file_path, str) else file_path
    if not p.is_file():
        return False
    try:
        sz = p.stat().st_size
        if sz < 10240:  # Less than 10KB is an error stub or incomplete file
            return False
        if is_html_or_empty_file(p):
            return False
        return True
    except Exception:
        return False


DOWNLOAD_MANIFEST_FILENAME = "download_manifest.json"


def load_download_manifest(dest_dir: Path) -> Dict[str, Any]:
    manifest_path = dest_dir / DOWNLOAD_MANIFEST_FILENAME
    if manifest_path.is_file():
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_download_manifest(dest_dir: Path, data: Dict[str, Any]):
    dest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = dest_dir / DOWNLOAD_MANIFEST_FILENAME
    try:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def find_cached_video(
    url_or_id: str,
    dest_dir: Path,
    index: int,
    expected_title: Optional[str] = None
) -> Optional[Path]:
    """
    Multi-stage check if video for url_or_id or batch item index is already downloaded and valid.
    Checks:
    1. Persistent manifest (dest_dir/download_manifest.json) by URL / file_id
    2. Expected title filename on disk
    3. Google Drive file_id in existing filename in dest_dir
    4. Deterministic index prefixes on disk: 01_*.mp4, 01_*.ts, etc.
    5. Standard fallback patterns: video_01.mp4, drive_video_01.mp4, etc.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_download_manifest(dest_dir)
    file_id = extract_gdrive_id(url_or_id)
    key = file_id if file_id else url_or_id

    # 1. Check in manifest by key
    if key in manifest:
        cached_raw = manifest[key].get("path", "")
        if cached_raw:
            cached_path = Path(cached_raw)
            if is_valid_video_file(cached_path):
                return cached_path
        # Also check relative to dest_dir in case workspace directory moved between CI runs
        cached_name = manifest[key].get("name", "")
        if cached_name:
            cand = dest_dir / cached_name
            if is_valid_video_file(cand):
                manifest[key]["path"] = str(cand.resolve())
                save_download_manifest(dest_dir, manifest)
                return cand

    # 2. Check by expected title if provided
    if expected_title:
        safe = sanitize_filename(expected_title)
        for cand_name in [f"{index:02d}_{safe}.mp4", f"{index}_{safe}.mp4", f"{safe}.mp4"]:
            cand = dest_dir / cand_name
            if is_valid_video_file(cand):
                manifest[key] = {"path": str(cand.resolve()), "name": cand.name, "size": cand.stat().st_size}
                save_download_manifest(dest_dir, manifest)
                return cand

    # 3. Check by Google Drive file_id in filename in dest_dir
    if file_id:
        for f in dest_dir.iterdir():
            if f.is_file() and file_id in f.name and is_valid_video_file(f):
                manifest[key] = {"path": str(f.resolve()), "name": f.name, "size": f.stat().st_size}
                save_download_manifest(dest_dir, manifest)
                return f

    # 4. Check by deterministic index-based prefixes: 01_*.mp4, 01_*.ts, etc.
    prefix_2d = f"{index:02d}_"
    prefix_1d = f"{index}_"
    for f in dest_dir.iterdir():
        if f.is_file() and is_valid_video_file(f):
            if f.name.startswith(prefix_2d) or f.name.startswith(prefix_1d):
                manifest[key] = {"path": str(f.resolve()), "name": f.name, "size": f.stat().st_size}
                save_download_manifest(dest_dir, manifest)
                return f

    # 5. Check standard legacy patterns
    for pattern in [f"video_{index:02d}.mp4", f"drive_video_{index:02d}.mp4", f"video_{index}.mp4", f"drive_video_{index}.mp4"]:
        cand = dest_dir / pattern
        if is_valid_video_file(cand):
            manifest[key] = {"path": str(cand.resolve()), "name": cand.name, "size": cand.stat().st_size}
            save_download_manifest(dest_dir, manifest)
            return cand

    return None


def download_with_gdown(
    url_or_id: str,
    dest_dir: Path,
    index: int = 1,
    quiet: bool = False
) -> Path:
    """
    Download single Google Drive file using gdown package with caching.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    cached = find_cached_video(url_or_id, dest_dir, index)
    if cached and is_valid_video_file(cached):
        return cached

    file_id = extract_gdrive_id(url_or_id)
    url = f"https://drive.google.com/uc?id={file_id}" if file_id else url_or_id
    
    # Destination ending in separator tells gdown to preserve remote filename in dest_dir
    dest_param = str(dest_dir.resolve()) + os.sep
    
    res = None
    err_notes = []
    try:
        res = gdown.download(url=url, output=dest_param, quiet=quiet)
    except Exception as e:
        err_notes.append(str(e))
        
    if (not res or not Path(res).is_file()) and file_id:
        try:
            res = gdown.download(id=file_id, output=dest_param, quiet=quiet)
        except Exception as e:
            err_notes.append(str(e))
            
    if not res or not Path(res).is_file():
        fallback_file = dest_dir / f"drive_video_{index:02d}.mp4"
        try:
            res = gdown.download(url=url, output=str(fallback_file), quiet=quiet)
        except Exception as e:
            err_notes.append(str(e))
        
    if not res or not Path(res).is_file():
        full_err = " | ".join(err_notes)
        if "Cannot retrieve the public link" in full_err or "permission" in full_err.lower():
            raise PermissionError(
                f"Google Drive access restricted for: {url_or_id}\n"
                f"File is PRIVATE or requires Google Sign-in.\n"
                f"👉 Fix: In Google Drive, right-click file -> Share -> Change 'General access' to 'Anyone with the link' (Viewer)."
            )
        raise RuntimeError(f"Failed to download Google Drive file: {url_or_id} ({full_err})")
        
    res_path = ensure_video_extension(Path(res).resolve())
    manifest = load_download_manifest(dest_dir)
    key = file_id if file_id else url_or_id
    manifest[key] = {"path": str(res_path.resolve()), "name": res_path.name, "size": res_path.stat().st_size}
    save_download_manifest(dest_dir, manifest)
    return res_path


def download_gdrive_folder(
    url: str,
    dest_dir: Path,
    quiet: bool = False
) -> List[Path]:
    """
    Download all video files from a public Google Drive folder using gdown with cache check.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not gdown:
        raise ImportError("The 'gdown' package is required to download Google Drive folders.")
        
    folder_id = extract_gdrive_id(url)
    folder_dest = dest_dir / f"folder_{folder_id or 'shared'}"
    folder_dest.mkdir(parents=True, exist_ok=True)

    # Check if folder already contains valid downloaded videos
    existing_videos: List[Path] = []
    for p in folder_dest.rglob("*"):
        if p.is_file() and is_valid_video_file(p):
            p_fixed = ensure_video_extension(p)
            if p_fixed.suffix.lower() in SUPPORTED_EXTENSIONS:
                if is_ts_file(p_fixed):
                    p_fixed = convert_ts_to_mp4(p_fixed)
                if p_fixed not in existing_videos:
                    existing_videos.append(p_fixed)

    if existing_videos:
        print(f"⏩ [Cache Hit] Found {len(existing_videos)} already downloaded video file(s) in folder '{folder_dest.name}'. Skipping folder download.")
        return existing_videos

    print(f"\n📁 Batch downloading Google Drive shared folder: {url} (ID: {folder_id or 'N/A'})...")
    
    res_list = None
    try:
        res_list = gdown.download_folder(url=url, output=str(folder_dest.resolve()), quiet=quiet)
    except Exception as e:
        if folder_id:
            try:
                res_list = gdown.download_folder(id=folder_id, output=str(folder_dest.resolve()), quiet=quiet)
            except Exception as e2:
                print(f"⚠️ Failed to download folder: {e2}")
        else:
            print(f"⚠️ Failed to download folder: {e}")
            
    # Collect all video files downloaded in folder_dest
    found_videos: List[Path] = []
    for p in folder_dest.rglob("*"):
        if p.is_file():
            p_fixed = ensure_video_extension(p)
            if p_fixed.suffix.lower() in SUPPORTED_EXTENSIONS and is_valid_video_file(p_fixed):
                if is_ts_file(p_fixed):
                    p_fixed = convert_ts_to_mp4(p_fixed)
                if p_fixed not in found_videos:
                    found_videos.append(p_fixed)
                
    print(f"✓ Retrieved {len(found_videos)} video files from Google Drive folder.")
    return found_videos


def download_with_requests(
    url_or_id: str,
    dest_dir: Path,
    index: int = 1,
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Path:
    """
    Resilient Google Drive downloader using requests.Session with
    virus scan confirmation token handling, atomic part files, and permission validation.
    """
    if not requests:
        raise ImportError("The 'requests' package is required. Install via: pip install requests")

    cached = find_cached_video(url_or_id, dest_dir, index)
    if cached and is_valid_video_file(cached):
        return cached

    file_id = extract_gdrive_id(url_or_id)
    if not file_id:
        download_url = url_or_id
    else:
        download_url = f"https://drive.google.com/uc?id={file_id}&export=download"

    session = requests.Session()
    session.trust_env = False  # Direct connection without proxy/VPN for Google Drive
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    response = session.get(download_url, headers=headers, stream=True, allow_redirects=True)

    # Check for authentication redirect (Restricted / Private file)
    if "accounts.google.com" in response.url or "/signin" in response.url or "ServiceLogin" in response.url:
        raise PermissionError(
            f"Google Drive access restricted for: {url_or_id}\n"
            f"File is PRIVATE or requires Google Sign-in.\n"
            f"👉 Fix: In Google Drive, right-click file -> Share -> Change 'General access' to 'Anyone with the link' (Viewer)."
        )

    content_type = response.headers.get("content-type", "").lower()
    if "text/html" in content_type:
        html_text = next(response.iter_content(65536), b"").decode("utf-8", errors="replace")

        # 1. Check for sign-in / restricted access
        if "accounts.google.com" in response.url or "accounts.google.com" in html_text or "ServiceLogin" in html_text:
            raise PermissionError(
                f"Google Drive access restricted for: {url_or_id}\n"
                f"File is PRIVATE or requires Google Sign-in.\n"
                f"👉 Fix: In Google Drive, right-click file -> Share -> Change 'General access' to 'Anyone with the link' (Viewer)."
            )

        # 2. Check for quota exceeded
        if "quota" in html_text.lower() or "too many users" in html_text.lower():
            raise RuntimeError(f"Google Drive download quota exceeded for: {url_or_id}")

        # 3. Check for Google Drive Virus Scan Warning form (for files > 100MB)
        form_inputs = {}
        for m in re.finditer(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html_text):
            form_inputs[m.group(1)] = m.group(2)

        action_match = re.search(r'<form[^>]+action="([^"]+)"', html_text)
        action_url = action_match.group(1) if action_match else "https://drive.usercontent.google.com/download"

        if form_inputs and "confirm" in form_inputs:
            response = session.get(action_url, params=form_inputs, headers=headers, stream=True, allow_redirects=True)
        else:
            # Fallback legacy token search
            m_token = re.search(r'confirm=([0-9A-Za-z_-]+)', html_text)
            if m_token and file_id:
                token = m_token.group(1)
                confirm_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm={token}"
                response = session.get(confirm_url, headers=headers, stream=True, allow_redirects=True)
            else:
                raise RuntimeError(
                    f"Google Drive returned an HTML page instead of video data for: {url_or_id}\n"
                    f"Please verify the file sharing permission is set to 'Anyone with the link'."
                )

    # Determine filename
    cd = response.headers.get("content-disposition", "")
    filename = get_filename_from_cd(cd)
    
    if not filename:
        parsed = urllib.parse.urlparse(url_or_id)
        path_name = os.path.basename(parsed.path)
        if path_name and "." in path_name:
            filename = path_name
        else:
            filename = f"drive_video_{index:02d}.mp4"

    dest_path = dest_dir / filename
    if is_valid_video_file(dest_path):
        return ensure_video_extension(dest_path)

    total_size = int(response.headers.get("content-length", 0))
    downloaded = 0
    chunk_size = 4 * 1024 * 1024  # 4 MB chunks
    temp_path = dest_path.with_name(dest_path.name + ".part")
    
    try:
        with open(temp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total_size, filename)
        if dest_path.exists():
            dest_path.unlink(missing_ok=True)
        temp_path.rename(dest_path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise

    # Validation: Ensure file is not 0 bytes and not an HTML error document
    if not is_valid_video_file(dest_path):
        if dest_path.exists():
            dest_path.unlink(missing_ok=True)
        raise RuntimeError(f"Download produced an invalid or empty file: {url_or_id}")

    final_res = ensure_video_extension(dest_path)
    manifest = load_download_manifest(dest_dir)
    key = file_id if file_id else url_or_id
    manifest[key] = {"path": str(final_res.resolve()), "name": final_res.name, "size": final_res.stat().st_size}
    save_download_manifest(dest_dir, manifest)
    return final_res


def parse_resolution_height(label: str, url: str) -> int:
    """Accurately extracts numeric height (e.g. 1080 for '1080p FHD' or '3194554_1080p.mp4') for sorting."""
    label_str = (label or "").strip()
    path = urllib.parse.urlparse(url).path

    # 1. Check label for 4k / 2k
    if re.search(r"\b4k\b", label_str, re.I):
        return 2160
    if re.search(r"\b2k\b", label_str, re.I):
        return 1440

    # 2. Check label for numeric resolution
    num_match = re.search(r"(?:^|[\s_/-])(2160|1440|1080|720|480|360|240|144)p?(?:$|[\s_/-])", label_str, re.I)
    if num_match:
        return int(num_match.group(1))

    num_any = re.search(r"(2160|1440|1080|720|480|360|240|144)", label_str)
    if num_any:
        return int(num_any.group(1))

    # 3. Check URL path only (ignoring query parameters & security tokens)
    if re.search(r"[-_.]4k[-_.]", path, re.I):
        return 2160
    path_match = re.search(r"[-_.](\d{3,4})p?[-_.]", path)
    if path_match:
        val = int(path_match.group(1))
        if val in (2160, 1440, 1080, 720, 480, 360, 240, 144):
            return val

    return 0


def format_quality_label(height: int, raw_label: str) -> str:
    """Formats quality name nicely."""
    if height == 2160:
        return "2160p (4K UHD)"
    if height == 1440:
        return "1440p (2K QHD)"
    if height == 1080:
        return "1080p FHD"
    if height == 720:
        return "720p HD"
    if height > 0:
        return f"{height}p"
    return raw_label if raw_label else "Default"


WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def sanitize_filename(name: str) -> str:
    """Removes invalid filename characters for Windows/Linux."""
    clean = re.sub(r'[\\/*?:"<>|]', "_", name)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def is_webpage_url(url_or_id: str) -> bool:
    """Check if the string is an HTTP/HTTPS webpage URL rather than a Google Drive link or direct video file."""
    if not (url_or_id.startswith("http://") or url_or_id.startswith("https://")):
        return False
    if extract_gdrive_id(url_or_id):
        return False
    clean = url_or_id.split("?")[0].rstrip("/")
    ext = os.path.splitext(clean)[1].lower()
    if ext in SUPPORTED_EXTENSIONS:
        return False
    return True


def fetch_webpage_html(url: str, referer: Optional[str] = None) -> str:
    """
    Fetches HTML content via requests with curl fallback.
    """
    domain = urllib.parse.urlparse(url).netloc
    ref = referer or f"https://{domain}/"

    req_headers = WEB_HEADERS.copy()
    req_headers["Referer"] = ref

    # 1. Try requests.Session
    if requests:
        try:
            s = requests.Session()
            resp = s.get(url, headers=req_headers, timeout=25)
            if resp.status_code == 200 and resp.text:
                return resp.text
        except Exception as e:
            print(f"⚠️ requests fetch note: {e}")

    # 2. Resilient fallback to curl
    curl_bin = "curl.exe" if sys.platform == "win32" else "curl"
    curl_cmd = [
        curl_bin, "-sSL",
        "-A", req_headers["User-Agent"],
        "-H", f"Referer: {ref}",
        "-H", f"Accept: {req_headers['Accept']}",
        "-H", f"Accept-Language: {req_headers['Accept-Language']}",
        "--compressed",
        url
    ]
    try:
        res = subprocess.run(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", timeout=30)
        if res.returncode == 0 and res.stdout and ("<html" in res.stdout.lower() or "<video" in res.stdout.lower() or "mp4" in res.stdout.lower()):
            return res.stdout
    except Exception as e:
        print(f"⚠️ curl fallback note: {e}")

    raise RuntimeError(f"Failed to fetch webpage: {url}")


def extract_webpage_video_info(page_url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Extracts the highest quality direct video URL, quality label, and title from a webpage.
    Supports PornTrex (flashvars), TNAFlix (HTML5 size attributes), 4KPorno, and generic video pages.
    Returns: (video_stream_url, quality_label, title)
    """
    html_content = fetch_webpage_html(page_url)

    # 1. Extract Title
    title = None
    if BeautifulSoup:
        soup = BeautifulSoup(html_content, "html.parser")
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            title = h1.get_text(strip=True)
        elif soup.title and soup.title.string:
            title = soup.title.string.strip()
    else:
        title_m = re.search(r"<h1[^>]*>(.*?)</h1>", html_content, re.DOTALL | re.IGNORECASE)
        if title_m:
            title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip()

    if not title:
        og_m = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']', html_content, re.IGNORECASE)
        if og_m:
            title = og_m.group(1).strip()

    streams = []

    def add_candidate(raw_url: str, label_hint: str = ""):
        if not raw_url:
            return
        clean_url = html.unescape(raw_url.strip())
        full_url = urllib.parse.urljoin(page_url, clean_url)

        # Skip non-video files (images/posters)
        clean_path = full_url.lower().split("?")[0]
        if any(ext in clean_path for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".vtt", ".srt"]):
            return

        res_h = parse_resolution_height(label_hint, full_url)
        lbl = format_quality_label(res_h, label_hint)

        if not any(s["url"] == full_url for s in streams):
            streams.append({
                "label": lbl,
                "height": res_h,
                "url": full_url
            })

    # Strategy A: Check flashvars JavaScript object (PornTrex / Kernel Video Sharing sites)
    fv_block_match = re.search(r"flashvars\s*=\s*\{([^\}]+)\}", html_content, re.DOTALL)
    if fv_block_match:
        block = fv_block_match.group(1)
        pairs = re.findall(r"([a-zA-Z0-9_]+)\s*:\s*['\"]([^'\"]*)['\"]", block)
        flashvars_raw = dict(pairs)

        url_keys = [k for k in flashvars_raw.keys() if k == "video_url" or k.startswith("video_alt_url")]
        for k in url_keys:
            if k.endswith("_text") or k.endswith("_hd"):
                continue
            v_url = flashvars_raw.get(k)
            if v_url and v_url.startswith("http"):
                lbl_key = f"{k}_text"
                label = flashvars_raw.get(lbl_key, "")
                add_candidate(v_url, label)

    # Strategy B: Check HTML5 <video> and <source> elements (TNAFlix, 4KPorno, HTML5)
    if BeautifulSoup:
        soup = BeautifulSoup(html_content, "html.parser")
        video_tag = soup.find("video", id=lambda x: x and "html5_api" in x) or soup.find("video")
        if video_tag:
            for source in video_tag.find_all("source"):
                src = source.get("src")
                size_attr = source.get("size") or source.get("label") or source.get("data-res") or source.get("title") or ""
                add_candidate(src, size_attr)
            if video_tag.get("src"):
                add_candidate(video_tag["src"], "Source Video")

        for source in soup.find_all("source"):
            src = source.get("src")
            size_attr = source.get("size") or source.get("label") or source.get("data-res") or ""
            add_candidate(src, size_attr)

    # Strategy C: Regex fallback for <source> and get_file / .mp4 links
    if not streams:
        for s_match in re.finditer(r'<source\s+[^>]*src=["\']([^"\']+)["\'](?:\s+[^>]*size=["\']([^"\']*)["\'])?', html_content, re.IGNORECASE):
            add_candidate(s_match.group(1), s_match.group(2) or "")

        get_files = re.findall(r"https?://[^\s'\"<>\\]+/get_file/[^\s'\"<>\\]+", html_content)
        for gf in get_files:
            add_candidate(gf, "")

        mp4_matches = re.findall(r'(https?://[^"\'\s>]+\.(?:mp4|m4v|ts|webm)(?:/[^"\'\s>]*)?)', html_content, re.IGNORECASE)
        for m in mp4_matches:
            add_candidate(m, "")

    if streams:
        # Sort strictly by resolution height descending (highest first)
        streams.sort(key=lambda x: x["height"], reverse=True)
        best = streams[0]
        return best["url"], best["label"], title

    return None, None, title


def download_stream_file(
    stream_url: str,
    target_path: Path,
    referer: str,
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> None:
    """Download video stream with high-speed multi-engine: aria2c (16 parallel connections) -> requests (4MB buffer) -> curl."""
    temp_path = target_path.with_name(target_path.name + ".part")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }

    download_ok = False

    # 1. Ultra Fast: aria2c multi-segmented downloader (16 connections split)
    aria2_bin = "aria2c.exe" if sys.platform == "win32" else "aria2c"
    aria2_path = shutil.which(aria2_bin) or shutil.which("aria2c")
    if aria2_path:
        try:
            print(f"⚡ [Multi-Threaded Download] Using aria2c (16 parallel streams)...")
            aria2_cmd = [
                str(aria2_path),
                "-x", "16",
                "-s", "16",
                "-j", "16",
                "-k", "1M",
                "--file-allocation=none",
                "--header", f"Referer: {referer}",
                "--header", f"User-Agent: {headers['User-Agent']}",
                "--header", f"Accept: {headers['Accept']}",
                "--dir", str(temp_path.parent.resolve()),
                "--out", temp_path.name,
                "--allow-overwrite=true",
                "--auto-file-renaming=false",
                "--summary-interval=1",
                "--max-tries=3",
                "--retry-wait=2",
                "--connect-timeout=15",
                "--timeout=30",
                stream_url
            ]
            res = subprocess.run(aria2_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0 and temp_path.is_file() and temp_path.stat().st_size > 1024:
                download_ok = True
                print(f"⚡ Download finished with aria2c.")
        except Exception as e:
            print(f"⚠️ aria2c note: {e}")
            download_ok = False

    # 2. Fast Streaming requests (4MB buffer)
    if not download_ok and requests:
        try:
            with requests.get(stream_url, headers=headers, stream=True, timeout=60) as resp:
                if resp.status_code == 200:
                    total_size = int(resp.headers.get("content-length", 0))
                    downloaded = 0
                    chunk_size = 4 * 1024 * 1024
                    with open(temp_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=chunk_size):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                if progress_callback:
                                    progress_callback(downloaded, total_size, target_path.name)
                    if temp_path.is_file() and temp_path.stat().st_size > 1024:
                        download_ok = True
        except Exception as e:
            print(f"⚠️ requests download note: {e}")
            download_ok = False

    # 3. Fallback to curl
    if not download_ok:
        curl_bin = "curl.exe" if sys.platform == "win32" else "curl"
        curl_cmd = [
            curl_bin, "-L",
            "-A", headers["User-Agent"],
            "-H", f"Referer: {referer}",
            "-o", str(temp_path),
            "--retry", "3",
            "--retry-delay", "2",
            stream_url
        ]
        res = subprocess.run(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0 and temp_path.is_file() and temp_path.stat().st_size > 1024:
            download_ok = True

    if not download_ok or not temp_path.is_file() or temp_path.stat().st_size == 0:
        if temp_path.exists():
            temp_path.unlink()
        raise RuntimeError(f"Failed to download video stream from: {stream_url}")

    if target_path.exists():
        target_path.unlink()
    temp_path.rename(target_path)


def download_webpage_video(
    page_url: str,
    dest_dir: Path,
    index: int = 1,
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Path:
    """
    Scrapes video page, resolves best stream URL, and downloads with cache verification and referer.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 1. Quick check cache first
    cached = find_cached_video(page_url, dest_dir, index)
    if cached and is_valid_video_file(cached):
        print(f"⏩ [Cache Hit] '{cached.name}' ({format_size(cached.stat().st_size)}) already downloaded. Skipping.")
        if progress_callback:
            sz = cached.stat().st_size
            progress_callback(sz, sz, cached.name)
        return cached

    stream_url, quality, title = extract_webpage_video_info(page_url)
    if not stream_url:
        raise ValueError(f"Could not locate playable video stream from: {page_url}")

    if title:
        safe = sanitize_filename(title)
        filename = f"{index:02d}_{safe}.mp4"
    else:
        raw_path = urllib.parse.urlparse(stream_url).path.rstrip("/")
        stem = os.path.basename(raw_path) or f"video_{index:02d}.mp4"
        filename = f"{index:02d}_{stem}" if not stem.startswith(f"{index:02d}_") else stem
        if not filename.endswith(".mp4"):
            filename += ".mp4"

    dest_path = dest_dir / filename

    # 2. Check if dest_path is already completely downloaded and valid
    if is_valid_video_file(dest_path):
        print(f"⏩ [Cache Hit] '{dest_path.name}' ({format_size(dest_path.stat().st_size)}) already downloaded. Skipping.")
        manifest = load_download_manifest(dest_dir)
        manifest[page_url] = {"path": str(dest_path.resolve()), "name": dest_path.name, "size": dest_path.stat().st_size}
        save_download_manifest(dest_dir, manifest)
        if progress_callback:
            sz = dest_path.stat().st_size
            progress_callback(sz, sz, dest_path.name)
        return dest_path

    # Check by index / expected title in case filename formatting differed
    cached_cand = find_cached_video(page_url, dest_dir, index, expected_title=title)
    if cached_cand and is_valid_video_file(cached_cand):
        print(f"⏩ [Cache Hit] '{cached_cand.name}' ({format_size(cached_cand.stat().st_size)}) already downloaded. Skipping.")
        if progress_callback:
            sz = cached_cand.stat().st_size
            progress_callback(sz, sz, cached_cand.name)
        return cached_cand

    print(f"📥 Downloading: {dest_path.name} (Quality: {quality.upper() if quality else 'Auto'})")
    download_stream_file(stream_url, dest_path, referer=page_url, progress_callback=progress_callback)
    
    manifest = load_download_manifest(dest_dir)
    manifest[page_url] = {"path": str(dest_path.resolve()), "name": dest_path.name, "size": dest_path.stat().st_size}
    save_download_manifest(dest_dir, manifest)
    return dest_path



def download_video(
    url_or_id: str,
    dest_dir: Path,
    index: int = 1,
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Path:
    """
    Download video from Webpage (4KPorno/HTML5), Google Drive, or direct URL into dest_dir with caching.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 1. Check download cache first before any network requests
    cached = find_cached_video(url_or_id, dest_dir, index)
    if cached and is_valid_video_file(cached):
        print(f"⏩ [Download Cache Hit] '{cached.name}' ({format_size(cached.stat().st_size)}) already downloaded. Skipping.")
        if progress_callback:
            sz = cached.stat().st_size
            progress_callback(sz, sz, cached.name)
        if is_ts_file(cached):
            return convert_ts_to_mp4(cached)
        return cached

    # 2. Webpage URL
    if is_webpage_url(url_or_id):
        res_path = download_webpage_video(url_or_id, dest_dir=dest_dir, index=index, progress_callback=progress_callback)
        if res_path and res_path.is_file():
            if is_ts_file(res_path):
                res_path = convert_ts_to_mp4(res_path)
            manifest = load_download_manifest(dest_dir)
            manifest[url_or_id] = {"path": str(res_path.resolve()), "name": res_path.name, "size": res_path.stat().st_size}
            save_download_manifest(dest_dir, manifest)
        return res_path

    file_id = extract_gdrive_id(url_or_id)
    last_err: Optional[Exception] = None
    res_path = None
    
    # 3. Google Drive via gdown
    if gdown and file_id:
        try:
            res_path = download_with_gdown(url_or_id, dest_dir=dest_dir, index=index, quiet=False)
            if res_path and is_valid_video_file(res_path):
                if progress_callback:
                    sz = res_path.stat().st_size
                    progress_callback(sz, sz, res_path.name)
                if is_ts_file(res_path):
                    res_path = convert_ts_to_mp4(res_path)
                return res_path
        except PermissionError:
            raise
        except Exception as e:
            last_err = e
            print(f"⚠️ gdown attempt note: {e}, falling back to requests session...")
            
    # 4. Fallback direct requests
    try:
        res_path = download_with_requests(
            url_or_id=url_or_id,
            dest_dir=dest_dir,
            index=index,
            progress_callback=progress_callback
        )
    except Exception as req_err:
        if last_err and isinstance(req_err, PermissionError):
            raise req_err
        raise req_err

    if res_path and res_path.is_file():
        if is_ts_file(res_path):
            res_path = convert_ts_to_mp4(res_path)
        manifest = load_download_manifest(dest_dir)
        key = file_id if file_id else url_or_id
        manifest[key] = {"path": str(res_path.resolve()), "name": res_path.name, "size": res_path.stat().st_size}
        save_download_manifest(dest_dir, manifest)

    return res_path



def download_all_videos(
    urls: List[str],
    dest_dir: Path,
    overall_callback: Optional[Callable[[int, int, str], None]] = None,
    max_runtime_minutes: Optional[int] = None,
    job_start_time: Optional[float] = None
) -> Tuple[List[Path], bool]:
    """
    Download a sequence of videos from a list of URLs (supporting file links and folder links) with safety time checkpointing.
    Returns (list of downloaded file paths, resume_needed boolean).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    downloaded_files: List[Path] = []
    failed_downloads: List[Tuple[str, str]] = []
    total_count = len(urls)

    for i, url in enumerate(urls, start=1):
        if max_runtime_minutes and job_start_time:
            elapsed_mins = (time.time() - job_start_time) / 60.0
            if elapsed_mins >= max_runtime_minutes:
                pct = ((i - 1) / total_count) * 100.0 if total_count else 0.0
                print(f"\n\n⚠️ ========================================================")
                print(f"⚠️ TIME LIMIT BUDGET REACHED DURING DOWNLOAD ({elapsed_mins:.1f}m >= {max_runtime_minutes}m)!")
                print(f"💾 Checkpoint safely preserved in cache: {len(downloaded_files)}/{total_count} files downloaded ({pct:.1f}%).")
                print(f"🔄 Setting resumed_needed=true for GitHub Actions auto-continuation.")
                print(f"========================================================\n")
                final_list = [convert_ts_to_mp4(p) if is_ts_file(p) else p for p in downloaded_files]
                return final_list, True

        if is_gdrive_folder(url):
            if overall_callback:
                overall_callback(i, total_count, f"Downloading Google Drive folder [{i}/{total_count}]...")
            try:
                folder_vids = download_gdrive_folder(url, dest_dir=dest_dir)
                downloaded_files.extend(folder_vids)
            except Exception as e:
                failed_downloads.append((url, str(e)))
        else:
            if overall_callback:
                overall_callback(i, total_count, f"Starting download {i}/{total_count}...")
                
            def item_progress(curr, tot, fname):
                if overall_callback:
                    overall_callback(i, total_count, f"Downloading [{i}/{total_count}]: {fname}")

            try:
                # Use deterministic 1-based index 'i' matching URL position
                p = download_video(
                    url_or_id=url,
                    dest_dir=dest_dir,
                    index=i,
                    progress_callback=item_progress
                )
                if p and p.is_file() and p not in downloaded_files:
                    downloaded_files.append(p)
            except Exception as e:
                print(f"❌ Error downloading [{i}/{total_count}]: {e}")
                failed_downloads.append((url, str(e)))

    if failed_downloads:
        print("\n" + "=" * 62)
        print("❌ DOWNLOAD ERRORS DETECTED:")
        for u, err in failed_downloads:
            print(f"  • {u}\n    {err}")
        print("=" * 62 + "\n")
        raise RuntimeError(f"{len(failed_downloads)} download(s) failed. See error details above.")

    final_list = []
    for p in downloaded_files:
        if is_ts_file(p):
            final_list.append(convert_ts_to_mp4(p))
        else:
            final_list.append(p)

    return final_list, False
