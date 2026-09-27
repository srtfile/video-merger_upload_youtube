#!/usr/bin/env python3
"""
🎬 Standalone Lossless Video Joiner for GitHub Actions & CLI
Downloads videos/folders from public Google Drive links and merges them losslessly.
Self-contained script: Zero local subfolder dependencies.
"""

import os
import sys
import re
import html
import shutil
import subprocess
import tempfile
import threading
import time
import argparse
import urllib.parse
import urllib.request
import json
import random
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any, Union, Callable

# Optional dependencies
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


# =====================================================================
# 1. FILE FILTERING & NATURAL SORTING
# =====================================================================

SUPPORTED_EXTENSIONS = {
    ".mp4", ".ts", ".mkv", ".mov", ".avi", ".webm",
    ".flv", ".wmv", ".m4v", ".mts", ".m2ts", ".vob", ".3gp"
}


def natural_sort_key(s: Union[str, Path]):
    """Alphanumeric natural order key (e.g. video_2 before video_10)."""
    text = str(s.name if isinstance(s, Path) else s)
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", text)]


def is_html_or_empty_file(file_path: Path) -> bool:
    """Check if file is empty or an HTML document (e.g. Google Drive quota / login error)."""
    try:
        if not file_path.is_file() or file_path.stat().st_size == 0:
            return True
        with open(file_path, "rb") as f:
            head = f.read(1024).strip().lower()
            if head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<head" in head or b"<body" in head or b"google drive" in head:
                return True
    except Exception:
        pass
    return False


def filter_video_files(paths: List[Path]) -> List[Path]:
    """Filter paths by supported video file extensions and valid content."""
    valid = []
    for p in paths:
        if not p.is_file() or p.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if is_html_or_empty_file(p):
            print(f"⚠️ Skipping non-video/corrupted file '{p.name}' (HTML error page or 0 bytes).")
            continue
        valid.append(p)
    return valid


def sort_files(files: List[Path], sort_mode: str = "natural", reverse: bool = False) -> List[Path]:
    """Sort files according to specified mode."""
    if sort_mode == "natural":
        sorted_list = sorted(files, key=natural_sort_key)
    elif sort_mode == "alphabetical":
        sorted_list = sorted(files, key=lambda f: f.name.lower())
    elif sort_mode == "date":
        sorted_list = sorted(files, key=lambda f: f.stat().st_mtime)
    elif sort_mode == "size":
        sorted_list = sorted(files, key=lambda f: f.stat().st_size)
    else:
        sorted_list = list(files)
        
    if reverse:
        sorted_list.reverse()
    return sorted_list


# =====================================================================
# 2. FFMPEG UTILITIES & BINARY DETECTION
# =====================================================================

def parse_time_to_seconds(timestr: str) -> float:
    """Parse HH:MM:SS.micro or SS into float seconds."""
    timestr = timestr.strip()
    if not timestr or timestr == "N/A":
        return 0.0
    try:
        parts = timestr.split(":")
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        return float(timestr)
    except ValueError:
        return 0.0


def find_binary(name: str) -> Optional[Path]:
    """Find binary in system PATH or common paths."""
    exe_name = f"{name}.exe" if sys.platform == "win32" else name
    
    # 1. System PATH
    which_path = shutil.which(name) or shutil.which(exe_name)
    if which_path:
        return Path(which_path).resolve()
        
    # 2. Local folder / bin
    for folder in [Path.cwd(), Path.cwd() / "bin", Path(__file__).parent, Path(__file__).parent / "bin"]:
        cand = folder / exe_name
        if cand.is_file():
            return cand.resolve()
            
    # 3. Windows standard locations
    if sys.platform == "win32":
        for folder in [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links",
            Path("C:/ProgramData/chocolatey/bin"),
            Path("C:/ffmpeg/bin"),
            Path("C:/Program Files/ffmpeg/bin"),
        ]:
            cand = folder / exe_name
            if cand.is_file():
                return cand.resolve()
    return None


def ensure_ffmpeg(auto_prompt: bool = True) -> Tuple[Path, Path]:
    """Ensure ffmpeg and ffprobe binaries are accessible."""
    ffmpeg = find_binary("ffmpeg")
    ffprobe = find_binary("ffprobe")
    
    if ffmpeg and not ffprobe:
        sibling = ffmpeg.parent / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
        if sibling.is_file():
            ffprobe = sibling.resolve()
            
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe
        
    raise RuntimeError("FFmpeg and ffprobe must be installed on your system PATH.")


# =====================================================================
# 3. STREAM PROBING & COMPATIBILITY ENGINE
# =====================================================================

@dataclass
class VideoStreamInfo:
    codec: str = ""
    codec_tag: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    bitrate: int = 0
    pixel_format: str = ""
    aspect_ratio: str = ""


@dataclass
class AudioStreamInfo:
    codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    bitrate: int = 0


@dataclass
class MediaFileInfo:
    path: Path
    duration: float = 0.0
    size_bytes: int = 0
    video: Optional[VideoStreamInfo] = None
    audio: Optional[AudioStreamInfo] = None
    container_format: str = ""
    error: Optional[str] = None


@dataclass
class CompatibilityAnalysis:
    is_lossless_ready: bool = False
    reasons_against_copy: List[str] = field(default_factory=list)
    target_width: int = 0
    target_height: int = 0
    target_fps: float = 0.0
    target_audio_sample_rate: int = 48000
    total_duration: float = 0.0
    total_size_bytes: int = 0


def probe_with_ffmpeg(file_path: Path, ffmpeg_exe: Path) -> MediaFileInfo:
    """Fallback probe using `ffmpeg -hide_banner -i` output when ffprobe fails or is absent."""
    info = MediaFileInfo(path=file_path)
    if not file_path.is_file():
        info.error = "File does not exist"
        return info
    if file_path.stat().st_size == 0:
        info.error = "File is empty (0 bytes)"
        return info
    info.size_bytes = file_path.stat().st_size

def extract_ffmpeg_error(stderr_text: str = "", stdout_text: str = "") -> str:
    """Extract the most meaningful error line from FFmpeg/FFprobe output, ignoring startup banners."""
    combined = f"{stderr_text or ''}\n{stdout_text or ''}".strip()
    if not combined:
        return "Unknown FFmpeg error"

    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    if not lines:
        return "Unknown FFmpeg error"

    # Filter out banner lines (ffmpeg/ffprobe versions, config, libav/libsw/libpostproc lines)
    filtered = [
        l for l in lines
        if not (
            l.lower().startswith("ffmpeg version") or
            l.lower().startswith("ffprobe version") or
            l.lower().startswith("built with") or
            l.lower().startswith("configuration:") or
            re.match(r"^lib(av|sw|postproc)\w*\s+\d+", l.strip()) or
            l.startswith("Simple multimedia streams analyzer") or
            l.startswith("usage: ffprobe") or
            l.startswith("You have to specify") or
            l.startswith("Use -h to get full help")
        )
    ]
    if not filtered:
        return lines[-1]

    # Priority: Find lines containing explicit error keywords
    for l in reversed(filtered):
        l_lower = l.lower()
        if any(kw in l_lower for kw in ("error", "invalid", "could not", "failed", "unsupported", "exceeded", "cannot", "no such file")):
            return l
    return filtered[-1]


def probe_with_ffmpeg(file_path: Path, ffmpeg_exe: Path) -> MediaFileInfo:
    """Fallback probe using ffmpeg stderr output when ffprobe fails."""
    info = MediaFileInfo(path=file_path)
    if not file_path.is_file():
        info.error = "File does not exist"
        return info
    if file_path.stat().st_size == 0:
        info.error = "File is empty (0 bytes)"
        return info
    info.size_bytes = file_path.stat().st_size

    is_ts = is_ts_file(file_path)
    base_cmd = [
        str(ffmpeg_exe),
        "-hide_banner",
        "-probesize", "500M",
        "-analyzeduration", "500M",
    ]
    if is_ts:
        base_cmd.extend([
            "-fflags", "+genpts+discardcorrupt+igndts",
            "-err_detect", "ignore_err",
            "-scan_all_pmts", "1",
            "-resync_size", "100M",
        ])

    cmd = base_cmd + ["-i", str(file_path)]
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    output = ""
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            startupinfo=startupinfo,
            timeout=60
        )
        output = res.stderr or ""
    except Exception as e:
        info.error = f"Fallback probe failed: {e}"
        return info

    # If initial attempt didn't find stream and it's TS, retry with explicit -f mpegts and larger buffers
    if is_ts and "Stream #" not in output:
        try:
            retry_cmd = [
                str(ffmpeg_exe),
                "-hide_banner",
                "-f", "mpegts",
                "-probesize", "1000M",
                "-analyzeduration", "1000M",
                "-fflags", "+genpts+discardcorrupt+igndts",
                "-err_detect", "ignore_err",
                "-scan_all_pmts", "1",
                "-resync_size", "200M",
                "-i", str(file_path)
            ]
            res2 = subprocess.run(
                retry_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                startupinfo=startupinfo,
                timeout=60
            )
            if res2.stderr and "Stream #" in res2.stderr:
                output = res2.stderr
        except Exception:
            pass

    # Parse Duration: 00:01:23.45
    dur_match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", output)
    if dur_match:
        h, m, s = dur_match.groups()
        info.duration = float(h) * 3600 + float(m) * 60 + float(s)

    # Parse Video Stream: Stream #0:0[0x100]: Video: h264 (...), yuv420p, 1920x1080 ..., 30 fps
    v_match = re.search(
        r"Stream #\d+:\d+(?:\[[^\]]*\])?(?:\([^\)]*\))?.*?: Video:\s*([a-zA-Z0-9_-]+).*?,\s*([a-zA-Z0-9_-]+),\s*(\d+)x(\d+).*?,\s*([\d.]+)\s*(?:fps|tbr)",
        output
    )
    if v_match:
        v = VideoStreamInfo()
        v.codec = v_match.group(1).lower()
        v.pixel_format = v_match.group(2)
        v.width = int(v_match.group(3))
        v.height = int(v_match.group(4))
        v.fps = float(v_match.group(5))
        info.video = v
    else:
        v_simple = re.search(r"Stream #\d+:\d+(?:\[[^\]]*\])?(?:\([^\)]*\))?.*?: Video:\s*([a-zA-Z0-9_-]+).*?,\s*(\d+)x(\d+)", output)
        if v_simple:
            v = VideoStreamInfo()
            v.codec = v_simple.group(1).lower()
            v.width = int(v_simple.group(2))
            v.height = int(v_simple.group(3))
            fps_match = re.search(r"([\d.]+)\s*(?:fps|tbr)", output)
            if fps_match:
                try:
                    v.fps = float(fps_match.group(1))
                except ValueError:
                    v.fps = 0.0
            info.video = v
        else:
            v_any = re.search(r"Video:\s*([a-zA-Z0-9_-]+)", output)
            if v_any:
                v = VideoStreamInfo()
                v.codec = v_any.group(1).lower()
                info.video = v

    # Parse Audio Stream: Stream #0:1: Audio: aac (...), 48000 Hz, stereo
    a_match = re.search(
        r"Stream #\d+:\d+(?:\[[^\]]*\])?(?:\([^\)]*\))?.*?: Audio:\s*([a-zA-Z0-9_-]+).*?,\s*(\d+)\s*Hz,\s*([a-zA-Z0-9_-]+)",
        output
    )
    if a_match:
        a = AudioStreamInfo()
        a.codec = a_match.group(1).lower()
        a.sample_rate = int(a_match.group(2))
        ch_layout = a_match.group(3).lower()
        a.channels = 2 if "stereo" in ch_layout else (1 if "mono" in ch_layout else 6)
        info.audio = a
    else:
        a_simple = re.search(r"Stream #\d+:\d+(?:\[[^\]]*\])?(?:\([^\)]*\))?.*?: Audio:\s*([a-zA-Z0-9_-]+)", output)
        if a_simple:
            a = AudioStreamInfo()
            a.codec = a_simple.group(1).lower()
            info.audio = a

    if not info.video and not info.audio:
        err_hint = extract_ffmpeg_error(output)
        info.error = f"No readable video or audio stream found in file ({err_hint})"
    else:
        info.error = None

    return info


def probe_file(file_path: Path, ffprobe_exe: Optional[Path], ffmpeg_exe: Optional[Path] = None) -> MediaFileInfo:
    """Inspect video file metadata using ffprobe with automatic fallback to ffmpeg."""
    info = MediaFileInfo(path=file_path)
    if not file_path.is_file():
        info.error = "File does not exist"
        return info

    if file_path.stat().st_size == 0:
        info.error = "File is empty (0 bytes)"
        return info

    if is_html_or_empty_file(file_path):
        info.error = "File is an HTML error page or text document (check Google Drive sharing permissions/quota)"
        return info

    info.size_bytes = file_path.stat().st_size

    if not ffprobe_exe or not ffprobe_exe.is_file():
        if ffmpeg_exe and ffmpeg_exe.is_file():
            return probe_with_ffmpeg(file_path, ffmpeg_exe)
        info.error = "ffprobe binary not found"
        return info

    is_ts = is_ts_file(file_path)
    probe_opts = [
        "-hide_banner",
        "-v", "error",
        "-probesize", "500M",
        "-analyzeduration", "500M",
    ]
    if is_ts:
        probe_opts.extend([
            "-fflags", "+genpts+discardcorrupt+igndts",
            "-err_detect", "ignore_err",
            "-scan_all_pmts", "1",
            "-resync_size", "100M",
        ])

    cmd = [
        str(ffprobe_exe),
        *probe_opts,
        "-show_entries", "format=duration,format_name:stream=codec_type,codec_name,codec_tag_string,width,height,r_frame_rate,avg_frame_rate,bit_rate,pix_fmt,sample_rate,channels",
        "-of", "json",
        str(file_path)
    ]

    try:
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", startupinfo=startupinfo, timeout=60)
        if res.returncode != 0 or not res.stdout.strip():
            # Fall back to ffmpeg probe
            if ffmpeg_exe and ffmpeg_exe.is_file():
                fb = probe_with_ffmpeg(file_path, ffmpeg_exe)
                if fb and not fb.error and (fb.video or fb.audio):
                    return fb
            err_detail = extract_ffmpeg_error(res.stderr, res.stdout)
            if not err_detail or err_detail == "Unknown FFmpeg error":
                err_detail = "probesize/analyzeduration exceeded or corrupted stream"
            info.error = f"ffprobe error: {err_detail}"
            return info

        import json
        data = json.loads(res.stdout)
        fmt = data.get("format", {})
        try:
            info.duration = float(fmt.get("duration", 0.0))
        except (ValueError, TypeError):
            info.duration = 0.0
        info.container_format = fmt.get("format_name", "")

        for st in data.get("streams", []):
            ctype = st.get("codec_type")
            if ctype == "video" and not info.video:
                v = VideoStreamInfo()
                v.codec = st.get("codec_name", "")
                v.codec_tag = st.get("codec_tag_string", "")
                v.width = int(st.get("width", 0))
                v.height = int(st.get("height", 0))
                v.pixel_format = st.get("pix_fmt", "")

                # Parse FPS
                r_fps = st.get("r_frame_rate", "") or st.get("avg_frame_rate", "")
                if r_fps and "/" in r_fps:
                    num, den = r_fps.split("/")
                    if float(den) > 0:
                        v.fps = round(float(num) / float(den), 2)
                elif r_fps:
                    try:
                        v.fps = round(float(r_fps), 2)
                    except ValueError:
                        v.fps = 0.0
                info.video = v

            elif ctype == "audio" and not info.audio:
                a = AudioStreamInfo()
                a.codec = st.get("codec_name", "")
                a.sample_rate = int(st.get("sample_rate", 0))
                a.channels = int(st.get("channels", 0))
                info.audio = a

        # Fallback to ffmpeg probe if streams were not detected
        if not info.video and not info.audio and ffmpeg_exe and ffmpeg_exe.is_file():
            fb = probe_with_ffmpeg(file_path, ffmpeg_exe)
            if fb and not fb.error and (fb.video or fb.audio):
                return fb

    except Exception as e:
        if ffmpeg_exe and ffmpeg_exe.is_file():
            fb = probe_with_ffmpeg(file_path, ffmpeg_exe)
            if fb and not fb.error and (fb.video or fb.audio):
                return fb
        info.error = str(e)

    return info


def analyze_compatibility(files: List[MediaFileInfo]) -> CompatibilityAnalysis:
    """Analyze whether videos can be joined losslessly without transcoding."""
    analysis = CompatibilityAnalysis()
    valid_files = [f for f in files if not f.error and f.video]
    if not valid_files:
        analysis.reasons_against_copy.append("No valid video streams found.")
        return analysis

    analysis.total_duration = sum(f.duration for f in valid_files)
    analysis.total_size_bytes = sum(f.size_bytes for f in valid_files)

    first_v = valid_files[0].video
    max_w = max((f.video.width for f in valid_files if f.video), default=first_v.width)
    max_h = max((f.video.height for f in valid_files if f.video), default=first_v.height)
    max_fps = max((f.video.fps for f in valid_files if f.video and f.video.fps > 0), default=first_v.fps or 30.0)

    analysis.target_width = max_w if max_w > 0 else first_v.width
    analysis.target_height = max_h if max_h > 0 else first_v.height
    analysis.target_fps = max_fps if max_fps > 0 else 30.0

    has_audio_any = any(f.audio for f in valid_files)
    first_a = next((f.audio for f in valid_files if f.audio), None)
    if first_a:
        analysis.target_audio_sample_rate = first_a.sample_rate or 48000

    can_copy = True
    for idx, f in enumerate(valid_files[1:], start=2):
        v = f.video
        if v.codec.lower() != first_v.codec.lower():
            analysis.reasons_against_copy.append(f"File #{idx} video codec '{v.codec}' != File #1 '{first_v.codec}'")
            can_copy = False
        if v.width != first_v.width or v.height != first_v.height:
            analysis.reasons_against_copy.append(f"File #{idx} resolution {v.width}x{v.height} != File #1 {first_v.width}x{first_v.height}")
            can_copy = False
        if v.pixel_format and first_v.pixel_format and v.pixel_format != first_v.pixel_format:
            analysis.reasons_against_copy.append(f"File #{idx} pixel format '{v.pixel_format}' != File #1 '{first_v.pixel_format}'")
            can_copy = False
        if has_audio_any:
            if not f.audio:
                analysis.reasons_against_copy.append(f"File #{idx} is missing an audio track while others have audio.")
                can_copy = False
            elif first_a and f.audio.codec.lower() != first_a.codec.lower():
                analysis.reasons_against_copy.append(f"File #{idx} audio codec '{f.audio.codec}' != File #1 '{first_a.codec}'")
                can_copy = False

    analysis.is_lossless_ready = can_copy
    return analysis


# =====================================================================
# 4. DOWNLOADER ENGINE (GOOGLE DRIVE FILES & FOLDERS)
# =====================================================================

def extract_gdrive_id(url: str) -> Optional[str]:
    """Extract file/folder ID from Google Drive URLs."""
    url = url.strip()
    m = re.search(r"(?:/file/d/|/d/|/folders/)([a-zA-Z0-9_-]{25,})", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]{25,})", url)
    if m:
        return m.group(1)
    if re.match(r"^[a-zA-Z0-9_-]{25,}$", url):
        return url
    return None


def is_gdrive_folder(url: str) -> bool:
    """Check if URL points to a Google Drive folder."""
    return bool(re.search(r"(?:/drive/(?:u/\d+/)?folders/|/folders/)", url.strip()))


def extract_urls_from_text(text: str) -> List[str]:
    """Parse multiple URLs separated by newlines, commas, or whitespace."""
    if not text:
        return []
    urls: List[str] = []
    normalized = text.replace(",", "\n").replace(";", "\n")
    for raw_line in normalized.splitlines():
        line = raw_line.strip().strip('"').strip("'").strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        for token in line.split():
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
    """Read URLs from text file."""
    if not file_path.is_file():
        raise FileNotFoundError(f"Links file not found: {file_path}")
    return extract_urls_from_text(file_path.read_text(encoding="utf-8", errors="replace"))


def detect_video_extension(path: Path) -> str:
    """Inspect magic bytes to detect video container format."""
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
                    # Verify multi-packet sync for TS (188B), M2TS (192B), or ATSC (204B)
                    if (len(header) >= 376 and header[188] == 0x47 and header[376] == 0x47) or \
                       (len(header) >= 384 and header[192] == 0x47) or \
                       (len(header) >= 408 and header[204] == 0x47):
                        return ".ts"
    except Exception:
        pass
    return ".mp4"


def ensure_video_extension(file_path: Path, default_ext: str = ".mp4") -> Path:
    """Ensure downloaded file has a recognized video extension."""
    if file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
        return file_path
    detected_ext = detect_video_extension(file_path) or default_ext
    target_path = file_path.with_name(f"{file_path.name}{detected_ext}")
    counter = 1
    while target_path.exists() and target_path != file_path:
        target_path = file_path.with_name(f"{file_path.stem}_{counter}{detected_ext}")
        counter += 1
    try:
        file_path.rename(target_path)
        return target_path
    except Exception:
        return file_path


def format_duration(seconds: float) -> str:
    """Format seconds into HH:MM:SS."""
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs > 0 else f"{mins:02d}:{secs:02d}"


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
        # Include aac_adtstoasc bitstream filter for safe ADTS -> MP4 container conversion
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
    # Fixes incompatible audio codecs (MP2, AC3, LATM) without re-encoding video
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


def download_with_gdown(url_or_id: str, dest_dir: Path, index: int = 1) -> Path:
    """Download single Google Drive file via gdown."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    file_id = extract_gdrive_id(url_or_id)
    url = f"https://drive.google.com/uc?id={file_id}" if file_id else url_or_id
    dest_param = str(dest_dir.resolve()) + os.sep
    
    res = None
    err_notes = []
    try:
        res = gdown.download(url=url, output=dest_param, quiet=False)
    except Exception as e:
        err_notes.append(str(e))
        
    if (not res or not Path(res).is_file()) and file_id:
        try:
            res = gdown.download(id=file_id, output=dest_param, quiet=False)
        except Exception as e:
            err_notes.append(str(e))
            
    if not res or not Path(res).is_file():
        fallback = dest_dir / f"video_{index:02d}.mp4"
        try:
            res = gdown.download(url=url, output=str(fallback), quiet=False)
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
        raise RuntimeError(f"gdown could not retrieve file: {url_or_id} ({full_err})")
    return ensure_video_extension(Path(res).resolve())


def download_gdrive_folder(url: str, dest_dir: Path) -> List[Path]:
    """Download all video files from a public Google Drive folder."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    folder_id = extract_gdrive_id(url)
    folder_dest = dest_dir / f"folder_{folder_id or 'shared'}"
    folder_dest.mkdir(parents=True, exist_ok=True)
    
    print(f"\n📁 Batch downloading Google Drive shared folder: {url}...")
    try:
        gdown.download_folder(url=url, output=str(folder_dest.resolve()), quiet=False)
    except Exception as e:
        if folder_id:
            try:
                gdown.download_folder(id=folder_id, output=str(folder_dest.resolve()), quiet=False)
            except Exception:
                pass
                
    found_videos = []
    for p in folder_dest.rglob("*"):
        if p.is_file():
            p_fixed = ensure_video_extension(p)
            if p_fixed.suffix.lower() in SUPPORTED_EXTENSIONS:
                if is_ts_file(p_fixed):
                    p_fixed = convert_ts_to_mp4(p_fixed)
                if p_fixed not in found_videos:
                    found_videos.append(p_fixed)
    print(f"✓ Found {len(found_videos)} video files in folder.")
    return found_videos


def download_with_requests(url_or_id: str, dest_dir: Path, index: int = 1) -> Path:
    """Download Google Drive file using requests with token handling and permission validation."""
    if not requests:
        raise ImportError("requests is required for downloading.")
    file_id = extract_gdrive_id(url_or_id)
    download_url = f"https://drive.google.com/uc?id={file_id}&export=download" if file_id else url_or_id
    session = requests.Session()
    session.trust_env = False  # Direct connection without proxy/VPN for Google Drive
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VideoJoiner/1.0"}
    
    resp = session.get(download_url, headers=headers, stream=True, allow_redirects=True)

    # Check for authentication redirect (Restricted / Private file)
    if "accounts.google.com" in resp.url or "/signin" in resp.url or "ServiceLogin" in resp.url:
        raise PermissionError(
            f"Google Drive access restricted for: {url_or_id}\n"
            f"File is PRIVATE or requires Google Sign-in.\n"
            f"👉 Fix: In Google Drive, right-click file -> Share -> Change 'General access' to 'Anyone with the link' (Viewer)."
        )

    content_type = resp.headers.get("content-type", "").lower()
    if "text/html" in content_type:
        html_text = next(resp.iter_content(65536), b"").decode("utf-8", errors="replace")

        # 1. Check for sign-in / restricted access
        if "accounts.google.com" in resp.url or "accounts.google.com" in html_text or "ServiceLogin" in html_text:
            raise PermissionError(
                f"Google Drive access restricted for: {url_or_id}\n"
                f"File is PRIVATE or requires Google Sign-in.\n"
                f"👉 Fix: In Google Drive, right-click file -> Share -> Change 'General access' to 'Anyone with the link' (Viewer)."
            )

        # 2. Check for quota exceeded
        if "quota" in html_text.lower() or "too many users" in html_text.lower():
            raise RuntimeError(f"Google Drive download quota exceeded for: {url_or_id}")

        # 3. Check for Google Drive Virus Scan Warning form (for files > 100MB)
        # Google provides a <form id="download-form" action="https://drive.usercontent.google.com/download" method="get">
        # with hidden inputs: id, export, confirm, uuid
        form_inputs = {}
        for m in re.finditer(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html_text):
            form_inputs[m.group(1)] = m.group(2)

        action_match = re.search(r'<form[^>]+action="([^"]+)"', html_text)
        action_url = action_match.group(1) if action_match else "https://drive.usercontent.google.com/download"

        if form_inputs and "confirm" in form_inputs:
            resp = session.get(action_url, params=form_inputs, headers=headers, stream=True, allow_redirects=True)
        else:
            # Fallback legacy token search
            m_token = re.search(r'confirm=([0-9A-Za-z_-]+)', html_text)
            if m_token and file_id:
                token = m_token.group(1)
                confirm_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm={token}"
                resp = session.get(confirm_url, headers=headers, stream=True, allow_redirects=True)
            else:
                raise RuntimeError(
                    f"Google Drive returned an HTML page instead of video data for: {url_or_id}\n"
                    f"Please verify the file sharing permission is set to 'Anyone with the link'."
                )
        
    cd = resp.headers.get("content-disposition", "")
    filename = None
    if cd:
        m = re.search(r'filename\*=UTF-8\'\'([^;]+)', cd, re.IGNORECASE)
        if m:
            filename = urllib.parse.unquote(m.group(1))
        else:
            m2 = re.search(r'filename="?([^";]+)"?', cd)
            if m2:
                filename = m2.group(1).strip()
    if not filename:
        parsed = urllib.parse.urlparse(url_or_id)
        path_name = os.path.basename(parsed.path)
        if path_name and "." in path_name:
            filename = path_name
        else:
            filename = f"drive_video_{index:02d}.mp4"
        
    dest_path = dest_dir / filename
    try:
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=4 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
    except Exception:
        if dest_path.exists():
            dest_path.unlink(missing_ok=True)
        raise

    # Ensure file is not empty and not HTML
    if not dest_path.is_file() or dest_path.stat().st_size == 0:
        if dest_path.exists():
            dest_path.unlink(missing_ok=True)
        raise RuntimeError(f"Download produced an empty file (0 bytes): {url_or_id}")

    with open(dest_path, "rb") as f:
        magic = f.read(512)
    if magic.strip().startswith(b"<!DOCTYPE") or magic.strip().startswith(b"<html") or b"<head>" in magic.lower():
        dest_path.unlink(missing_ok=True)
        raise PermissionError(
            f"Google Drive returned an HTML page instead of video data for: {url_or_id}\n"
            f"The file is PRIVATE or requires Google account sign-in.\n"
            f"👉 Fix: Set file sharing to 'Anyone with the link' (Viewer) in Google Drive."
        )

    return ensure_video_extension(dest_path)


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
    manifest_path = dest_dir / DOWNLOAD_MANIFEST_FILENAME
    try:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def find_cached_video(url_or_id: str, dest_dir: Path, index: int) -> Optional[Path]:
    """Check if file for url_or_id is already downloaded and valid."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_download_manifest(dest_dir)
    file_id = extract_gdrive_id(url_or_id)
    key = file_id if file_id else url_or_id

    # 1. Check in manifest
    if key in manifest:
        cached_file = Path(manifest[key].get("path", ""))
        if cached_file.is_file() and cached_file.stat().st_size > 1024 and not is_html_or_empty_file(cached_file):
            return cached_file

    # 2. Check by file_id anywhere in filename within dest_dir
    if file_id:
        for f in dest_dir.iterdir():
            if f.is_file() and file_id in f.name and f.suffix.lower() in SUPPORTED_EXTENSIONS:
                if f.stat().st_size > 1024 and not is_html_or_empty_file(f):
                    manifest[key] = {"path": str(f.resolve()), "name": f.name, "size": f.stat().st_size}
                    save_download_manifest(dest_dir, manifest)
                    return f

    # 3. Check by standard index file pattern
    for pattern in [f"video_{index:02d}.mp4", f"drive_video_{index:02d}.mp4", f"video_{index}.mp4"]:
        cand = dest_dir / pattern
        if cand.is_file() and cand.stat().st_size > 1024 and not is_html_or_empty_file(cand):
            manifest[key] = {"path": str(cand.resolve()), "name": cand.name, "size": cand.stat().st_size}
            save_download_manifest(dest_dir, manifest)
            return cand

    return None


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
    index: int = 1
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
                print(f"⚡ Download finished with aria2c ({format_size(temp_path.stat().st_size)}).")
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
                    chunk_size = 4 * 1024 * 1024  # 4MB buffer for maximum I/O throughput
                    with open(temp_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=chunk_size):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                if total_size:
                                    pct = (downloaded / total_size) * 100
                                    print(f"\r  [{index:02d}] {downloaded / (1024*1024):.1f}MB / {total_size / (1024*1024):.1f}MB ({pct:.1f}%)", end="", flush=True)
                    print()
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


def download_webpage_video(page_url: str, dest_dir: Path, index: int = 1) -> Path:
    """
    Scrapes video page, resolves best stream URL, and downloads with progress and referer.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
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

    # Cache check
    if dest_path.is_file() and dest_path.stat().st_size > 1024:
        try:
            head_resp = requests.head(stream_url, headers={"Referer": page_url, "User-Agent": WEB_HEADERS["User-Agent"]}, timeout=10)
            remote_sz = int(head_resp.headers.get("content-length", 0))
            if remote_sz and dest_path.stat().st_size == remote_sz:
                print(f"⏩ [Cache Hit] '{dest_path.name}' ({format_size(remote_sz)}) already downloaded.")
                return dest_path
        except Exception:
            pass

    print(f"📥 Downloading: {dest_path.name} (Quality: {quality.upper() if quality else 'Auto'})")
    download_stream_file(stream_url, dest_path, referer=page_url, index=index)
    return dest_path



def download_video(url_or_id: str, dest_dir: Path, index: int = 1) -> Path:
    """Download a video using gdown with requests fallback and convert .ts to .mp4, with caching."""
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 1. Webpage URL (e.g., 4kporno.xxx or HTML5 video page)
    if is_webpage_url(url_or_id):
        res_path = download_webpage_video(url_or_id, dest_dir=dest_dir, index=index)
        if res_path and res_path.is_file():
            if is_ts_file(res_path):
                res_path = convert_ts_to_mp4(res_path)
            manifest = load_download_manifest(dest_dir)
            manifest[url_or_id] = {"path": str(res_path.resolve()), "name": res_path.name, "size": res_path.stat().st_size}
            save_download_manifest(dest_dir, manifest)
        return res_path

    file_id = extract_gdrive_id(url_or_id)

    # Check download cache first
    cached = find_cached_video(url_or_id, dest_dir, index)
    if cached:
        print(f"⏩ [Download Cache Hit] '{cached.name}' ({format_size(cached.stat().st_size)}) already downloaded. Skipping.")
        if is_ts_file(cached):
            return convert_ts_to_mp4(cached)
        return cached

    last_err: Optional[Exception] = None
    res_path = None
    if gdown and file_id:
        try:
            res_path = download_with_gdown(url_or_id, dest_dir, index=index)
        except PermissionError:
            raise
        except Exception as e:
            last_err = e
            print(f"⚠️ gdown attempt note: {e}, falling back to requests session...")
    if not res_path or not res_path.is_file():
        try:
            res_path = download_with_requests(url_or_id, dest_dir, index=index)
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
    max_runtime_minutes: Optional[int] = None,
    job_start_time: Optional[float] = None
) -> Tuple[List[Path], bool]:
    """Download all videos from URLs (files or folders) and convert any .ts to .mp4, with time budget checkpointing."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    failed_downloads = []
    total = len(urls)
    for i, u in enumerate(urls, start=1):
        # Check safety time budget before starting next download
        if max_runtime_minutes and job_start_time:
            elapsed_mins = (time.time() - job_start_time) / 60.0
            if elapsed_mins >= max_runtime_minutes:
                pct = ((i - 1) / total) * 100.0 if total else 0.0
                print(f"\n\n⚠️ ========================================================")
                print(f"⚠️ TIME LIMIT BUDGET REACHED DURING DOWNLOAD ({elapsed_mins:.1f}m >= {max_runtime_minutes}m)!")
                print(f"💾 Checkpoint safely preserved in cache: {len(downloaded)}/{total} files downloaded ({pct:.1f}%).")
                print(f"🔄 Setting resumed_needed=true for GitHub Actions auto-continuation.")
                print(f"========================================================\n")
                final_list = [convert_ts_to_mp4(p) if is_ts_file(p) else p for p in downloaded]
                return final_list, True

        if is_gdrive_folder(u):
            try:
                folder_vids = download_gdrive_folder(u, dest_dir)
                downloaded.extend(folder_vids)
            except Exception as e:
                failed_downloads.append((u, str(e)))
        else:
            print(f"📥 Downloading [{i}/{total}]: {u}")
            try:
                p = download_video(u, dest_dir, index=len(downloaded) + 1)
                if p and p.is_file() and p not in downloaded:
                    downloaded.append(p)
            except Exception as e:
                print(f"❌ Error downloading [{i}/{total}]: {e}")
                failed_downloads.append((u, str(e)))

    if failed_downloads:
        print("\n" + "=" * 62)
        print("❌ DOWNLOAD ERRORS DETECTED:")
        for u, err in failed_downloads:
            print(f"  • {u}\n    {err}")
        print("=" * 62 + "\n")
        raise RuntimeError(f"{len(failed_downloads)} download(s) failed. See error details above.")

    final_list = []
    for p in downloaded:
        if is_ts_file(p):
            final_list.append(convert_ts_to_mp4(p))
        else:
            final_list.append(p)

    return final_list, False


# =====================================================================
# 5. VIDEO JOINER EXECUTION ENGINE (LOSSLESS & TRANSCODE)
# =====================================================================

@dataclass
class JoinProgress:
    percent: float = 0.0
    current_time_sec: float = 0.0
    total_duration_sec: float = 0.0
    speed: str = "1.0x"
    fps: float = 0.0
    eta_sec: float = 0.0


def escape_ffmpeg_concat_path(path: Path) -> str:
    """Escape path for ffmpeg concat demuxer text file."""
    posix_path = path.resolve().as_posix()
    escaped = posix_path.replace("'", "'\\''")
    return f"file '{escaped}'"


_current_segment_file: Optional[Path] = None


def _handle_signal_cancellation(signum, frame):
    """Gracefully handle SIGINT / SIGTERM (e.g. GitHub runner timeout cancellation)."""
    global _current_segment_file
    print(f"\n\n⚠️ ========================================================")
    print(f"⚠️ Process received termination signal ({signum})!")
    print(f"💾 Preserving completed checkpoint segments and signalling auto-continuation...")
    print(f"========================================================\n")
    if _current_segment_file and _current_segment_file.exists():
        try:
            _current_segment_file.unlink(missing_ok=True)
        except Exception:
            pass
    set_github_action_output("completed", "false")
    set_github_action_output("resumed_needed", "true")
    sys.exit(0)


try:
    signal.signal(signal.SIGINT, _handle_signal_cancellation)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal_cancellation)
except Exception:
    pass


def run_ffmpeg_with_progress(
    cmd: List[str],
    total_duration: float,
    progress_callback: Optional[Callable[[JoinProgress], None]] = None,
    deadline_time: Optional[float] = None
) -> Tuple[bool, str]:
    """Execute ffmpeg subprocess with live progress parsing and optional execution deadline."""
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    final_cmd = list(cmd) + ["-progress", "pipe:1", "-nostats"]
    start_time = time.time()
    stderr_lines = []

    try:
        proc = subprocess.Popen(
            final_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            startupinfo=startupinfo
        )
    except Exception as e:
        return False, f"Failed to start FFmpeg: {e}"

    def read_stderr():
        for line in proc.stderr:
            clean = line.strip()
            if clean:
                stderr_lines.append(clean)

    err_thread = threading.Thread(target=read_stderr, daemon=True)
    err_thread.start()

    out_time_sec = 0.0
    speed_str = "1.0x"
    fps_val = 0.0
    timed_out = False

    for line in proc.stdout:
        # Check if execution deadline has been reached
        if deadline_time and time.time() >= deadline_time:
            timed_out = True
            try:
                proc.terminate()
                proc.wait(timeout=3.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            break

        line = line.strip()
        if not line:
            continue
        if line.startswith("out_time_us="):
            val = line.split("=", 1)[1].strip()
            try:
                out_time_sec = float(val) / 1_000_000.0
            except ValueError:
                pass
        elif line.startswith("out_time="):
            parsed = parse_time_to_seconds(line.split("=", 1)[1].strip())
            if parsed > 0:
                out_time_sec = parsed
        elif line.startswith("speed="):
            speed_str = line.split("=", 1)[1].strip()
        elif line.startswith("fps="):
            try:
                fps_val = float(line.split("=", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("progress="):
            pct = min(100.0, max(0.0, (out_time_sec / total_duration) * 100.0)) if total_duration > 0 else 0.0
            elapsed = max(0.001, time.time() - start_time)
            eta = max(0.0, (elapsed / (pct / 100.0)) - elapsed) if pct > 0 else 0.0
            if progress_callback:
                p = JoinProgress(percent=pct, current_time_sec=out_time_sec, total_duration_sec=total_duration, speed=speed_str, fps=fps_val, eta_sec=eta)
                progress_callback(p)

    if timed_out:
        err_thread.join(timeout=1.0)
        return False, "TIME_BUDGET_REACHED"

    proc.wait()
    err_thread.join(timeout=2.0)
    try:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
    except Exception:
        pass

    success = (proc.returncode == 0)
    err_msg = "\n".join(stderr_lines[-30:]) if not success else ""
    return success, err_msg


class VideoJoiner:
    """Concatenation engine supporting stream copy and fallback transcoding."""
    def __init__(self, ffmpeg_exe: Path, ffprobe_exe: Path):
        self.ffmpeg_exe = ffmpeg_exe
        self.ffprobe_exe = ffprobe_exe

    def join_lossless_demux(self, files: List[MediaFileInfo], output_path: Path, progress_callback=None) -> Tuple[bool, str]:
        """FFmpeg Concat Demuxer: 100% mathematical zero-loss copy (-c copy)."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        total_duration = sum(f.duration for f in files)
        out_ext = output_path.suffix.lower()

        has_aac = any(f.audio and "aac" in f.audio.codec.lower() for f in files)
        has_ts = any(f.path.suffix.lower() == ".ts" for f in files)
        bsf_args = ["-bsf:a", "aac_adtstoasc"] if (has_aac and (out_ext in (".mp4", ".m4v", ".mov") or has_ts)) else []

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as concat_file:
            concat_list_path = Path(concat_file.name)
            concat_file.write("ffconcat version 1.0\n")
            for f in files:
                concat_file.write(f"{escape_ffmpeg_concat_path(f.path)}\n")

        try:
            cmd = [
                str(self.ffmpeg_exe), "-y",
                "-fflags", "+genpts+discardcorrupt",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list_path),
                "-c", "copy"
            ]
            if out_ext == ".mkv":
                cmd.extend(["-map", "0"])
            else:
                cmd.extend(["-map", "0:v?", "-map", "0:a?"])
            cmd.extend(bsf_args)
            cmd.extend(["-avoid_negative_ts", "make_zero", "-max_muxing_queue_size", "4096"])
            if out_ext in (".mp4", ".m4v", ".mov"):
                cmd.extend(["-movflags", "+faststart"])
            cmd.append(str(output_path))
            return run_ffmpeg_with_progress(cmd, total_duration, progress_callback)
        finally:
            try:
                if concat_list_path.is_file():
                    concat_list_path.unlink()
            except Exception:
                pass

    def join_lossless_remux(self, files: List[MediaFileInfo], output_path: Path, progress_callback=None) -> Tuple[bool, str]:
        """Intermediate remux into transport streams then concat copy."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        total_duration = sum(f.duration for f in files)
        out_ext = output_path.suffix.lower()
        temp_dir = Path(tempfile.mkdtemp(prefix="joiner_remux_"))
        temp_ts_files: List[Path] = []

        try:
            for i, f in enumerate(files):
                if f.path.suffix.lower() == ".ts":
                    temp_ts_files.append(f.path)
                    continue
                temp_ts = temp_dir / f"chunk_{i:04d}.ts"
                temp_ts_files.append(temp_ts)
                v_codec = (f.video.codec if f.video else "").lower()
                bsf_v = ["-bsf:v", "h264_mp4toannexb"] if "264" in v_codec else (["-bsf:v", "hevc_mp4toannexb"] if "265" in v_codec or "hevc" in v_codec else [])
                
                cmd = [
                    str(self.ffmpeg_exe), "-y",
                    "-fflags", "+genpts+discardcorrupt+igndts",
                    "-err_detect", "ignore_err",
                    "-i", str(f.path),
                    "-c", "copy",
                    "-map", "0:v?",
                    "-map", "0:a?"
                ] + bsf_v + [str(temp_ts)]
                startupinfo = None
                if sys.platform == "win32":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startupinfo)

            concat_url = "concat:" + "|".join(str(p.resolve()) for p in temp_ts_files)
            cmd = [str(self.ffmpeg_exe), "-y", "-i", concat_url, "-c", "copy", "-fflags", "+genpts", "-avoid_negative_ts", "make_zero"]
            if out_ext in (".mp4", ".m4v", ".mov"):
                cmd.extend(["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"])
            cmd.append(str(output_path))
            return run_ffmpeg_with_progress(cmd, total_duration, progress_callback)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def join_lossless_smart(self, files: List[MediaFileInfo], output_path: Path, progress_callback=None) -> Tuple[bool, str]:
        """Smart lossless joiner: chooses best direct stream copy strategy."""
        exts = {f.path.suffix.lower() for f in files}
        if len(exts) > 1 or ".ts" in exts:
            return self.join_lossless_remux(files, output_path, progress_callback)
        success, err = self.join_lossless_demux(files, output_path, progress_callback)
        if not success:
            return self.join_lossless_remux(files, output_path, progress_callback)
        return success, err

    def join_visually_lossless_transcode_resumable(
        self,
        files: List[MediaFileInfo],
        analysis: CompatibilityAnalysis,
        output_path: Path,
        crf: int = 17,
        preset: str = "veryfast",
        progress_callback=None,
        cache_dir: Optional[Path] = None,
        max_runtime_minutes: Optional[int] = None,
        job_start_time: Optional[float] = None
    ) -> Tuple[bool, str, bool]:
        """
        Segment-by-segment resumable normalizer with checkpointing & safety time budget guard.
        Returns: (success: bool, error_message: str, resume_needed: bool)
        """
        global _current_segment_file
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if not cache_dir:
            cache_dir = Path(".video_cache") / output_path.stem
        cache_dir = cache_dir.resolve()
        cache_dir.mkdir(parents=True, exist_ok=True)

        n = len(files)
        target_w = analysis.target_width or 1920
        target_h = analysis.target_height or 1080
        target_fps = analysis.target_fps if analysis.target_fps > 0 else 30.0
        target_sr = analysis.target_audio_sample_rate or 48000
        out_ext = output_path.suffix.lower()

        deadline_time = (job_start_time + (max_runtime_minutes * 60.0)) if (max_runtime_minutes and job_start_time) else None

        ts_segments: List[Path] = []
        resumed_count = 0
        total_duration = analysis.total_duration

        print(f"\n🧩 Segment-by-Segment Resumable Normalization Engine")
        print(f"   Target Spec:   {target_w}x{target_h} @ {target_fps:.2f} fps, {target_sr} Hz Audio")
        print(f"   Transcode:     libx264, preset={preset}, crf={crf}")
        print(f"   Cache Folder:  {cache_dir}")
        print(f"   Total Clips:   {n}")
        if max_runtime_minutes:
            print(f"   Safety Budget: {max_runtime_minutes} minutes max runtime")

        for i, f in enumerate(files):
            seg_path = cache_dir / f"seg_{i:04d}.ts"
            ts_segments.append(seg_path)

            # Check safety time budget before starting next clip
            if deadline_time and time.time() >= deadline_time:
                pct = (i / n) * 100.0
                elapsed_mins = (time.time() - job_start_time) / 60.0 if job_start_time else 0.0
                print(f"\n\n⚠️ ========================================================")
                print(f"⚠️ TIME LIMIT BUDGET REACHED ({elapsed_mins:.1f}m >= {max_runtime_minutes}m)!")
                print(f"💾 Checkpoint safely preserved in cache: {i}/{n} clips processed ({pct:.1f}%).")
                print(f"🔄 Setting resumed_needed=true for GitHub Actions auto-continuation.")
                print(f"========================================================\n")
                return False, f"TIME_BUDGET_REACHED ({i}/{n} clips done)", True

            # Verify if this segment is already normalized and valid
            is_valid_segment = False
            if seg_path.is_file() and seg_path.stat().st_size > 1024:
                seg_probe = probe_file(seg_path, self.ffprobe_exe, self.ffmpeg_exe)
                if seg_probe and not seg_probe.error and seg_probe.video:
                    if f.duration > 0:
                        # Must match source clip duration within 2 seconds
                        if abs(seg_probe.duration - f.duration) <= 2.0:
                            is_valid_segment = True
                    else:
                        if seg_probe.duration > 1.0:
                            is_valid_segment = True

            if is_valid_segment:
                resumed_count += 1
                cur_pct = ((i + 1) / n) * 100.0
                sys.stdout.write(f"\r  [{'█'*int(30*(cur_pct/100.0)):<30}] {cur_pct:5.1f}% | ⏩ [Resume {i+1}/{n}] '{f.path.name}' already normalized. Skipping.\n")
                sys.stdout.flush()
                continue

            if seg_path.exists():
                seg_path.unlink(missing_ok=True)

            _current_segment_file = seg_path
            cur_pct = (i / n) * 100.0
            clip_dur_str = format_duration(f.duration) if f.duration > 0 else "unknown"
            print(f"\n🎬 Normalizing Clip [{i+1}/{n}]: {f.path.name} ({clip_dur_str})...")

            # Preserve 100% original resolution without re-scaling if clip already matches target dimensions
            v_info = f.video
            if v_info and v_info.width == target_w and v_info.height == target_h and abs((v_info.fps or 0) - target_fps) < 0.05:
                clip_vf = "setsar=1"
            elif v_info and v_info.width == target_w and v_info.height == target_h:
                clip_vf = f"setsar=1,fps={target_fps}"
            else:
                clip_vf = (
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                    f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={target_fps}"
                )

            # Resilient audio filtering: downmix/remap gracefully without crashing on corrupt packets
            # that claim sudden channel jumps (e.g. 32 channels from corrupted AAC PCE headers).
            if f.audio and f.audio.channels == 1:
                clip_af = "aresample=async=1:first_pts=0,pan=stereo|c0=c0|c1=c0"
            elif f.audio and f.audio.channels == 6:
                clip_af = "aresample=async=1:first_pts=0,pan=stereo|c0=c0+0.707*c2+0.707*c4|c1=c1+0.707*c2+0.707*c5"
            else:
                clip_af = "aresample=async=1:first_pts=0,pan=stereo|c0=c0|c1=c1"

            resilient_input_opts = [
                "-fflags", "+genpts+discardcorrupt+igndts",
                "-err_detect", "ignore_err"
            ]

            if f.audio:
                cmd = [
                    str(self.ffmpeg_exe), "-y",
                ] + resilient_input_opts + [
                    "-i", str(f.path),
                    "-vf", clip_vf,
                    "-af", clip_af,
                    "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-ar", str(target_sr), "-ac", "2",
                    "-avoid_negative_ts", "make_zero",
                    "-max_muxing_queue_size", "4096",
                    "-bsf:v", "h264_mp4toannexb",
                    str(seg_path)
                ]
            else:
                cmd = [
                    str(self.ffmpeg_exe), "-y",
                ] + resilient_input_opts + [
                    "-i", str(f.path),
                    "-f", "lavfi", "-i", f"anullsrc=r={target_sr}:cl=stereo",
                    "-vf", clip_vf,
                    "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-shortest",
                    "-avoid_negative_ts", "make_zero",
                    "-max_muxing_queue_size", "4096",
                    "-bsf:v", "h264_mp4toannexb",
                    str(seg_path)
                ]

            def clip_progress(p: JoinProgress):
                clip_pct = p.percent
                overall_p = (i / n) * 100.0 + (clip_pct / n)
                sys.stdout.write(f"\r  [{'█'*int(30*(overall_p/100.0)):<30}] {overall_p:5.1f}% | Clip {i+1}/{n}: {clip_pct:5.1f}% | Speed: {p.speed}")
                sys.stdout.flush()

            success, err = run_ffmpeg_with_progress(cmd, f.duration, clip_progress, deadline_time=deadline_time)
            print()

            if "TIME_BUDGET_REACHED" in err:
                if seg_path.exists():
                    seg_path.unlink(missing_ok=True)
                _current_segment_file = None
                pct = (i / n) * 100.0
                elapsed_mins = (time.time() - job_start_time) / 60.0 if job_start_time else 0.0
                print(f"\n\n⚠️ ========================================================")
                print(f"⚠️ TIME LIMIT BUDGET REACHED DURING CLIP [{i+1}/{n}] '{f.path.name}' ({elapsed_mins:.1f}m >= {max_runtime_minutes}m)!")
                print(f"💾 Checkpoint safely preserved in cache: {i}/{n} clips processed ({pct:.1f}%).")
                print(f"🔄 Setting resumed_needed=true for GitHub Actions auto-continuation.")
                print(f"========================================================\n")
                return False, f"TIME_BUDGET_REACHED ({i}/{n} clips done)", True

            # Multi-stage fault-tolerant fallback if primary attempt failed (e.g. fatal audio bitstream corruption)
            if not success or not seg_path.is_file() or seg_path.stat().st_size < 1024:
                if seg_path.exists():
                    seg_path.unlink(missing_ok=True)

                print(f"  ⚠️ Warning: Primary normalization failed for clip [{i+1}/{n}] '{f.path.name}'.")
                
                # Check if failure is due to corrupt audio or filter network
                audio_err_keywords = ["aac", "swr", "rematrix", "filter", "audio", "sample rate", "channel", "aresample", "corrupt", "decode"]
                is_audio_suspect = any(k in err.lower() for k in audio_err_keywords) or (f.audio is not None)

                if is_audio_suspect:
                    print(f"  🔄 Attempting Recovery: Normalizing video with clean synchronized audio replacement...")
                    fallback_cmd = [
                        str(self.ffmpeg_exe), "-y",
                        "-fflags", "+genpts+discardcorrupt+igndts",
                        "-err_detect", "ignore_err",
                        "-i", str(f.path),
                        "-f", "lavfi", "-i", f"anullsrc=r={target_sr}:cl=stereo",
                        "-map", "0:v:0",
                        "-map", "1:a:0",
                        "-vf", clip_vf,
                        "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k", "-shortest",
                        "-avoid_negative_ts", "make_zero",
                        "-max_muxing_queue_size", "4096",
                        "-bsf:v", "h264_mp4toannexb",
                        str(seg_path)
                    ]
                    success, err = run_ffmpeg_with_progress(fallback_cmd, f.duration, clip_progress, deadline_time=deadline_time)
                    print()
                    if "TIME_BUDGET_REACHED" in err:
                        if seg_path.exists():
                            seg_path.unlink(missing_ok=True)
                        _current_segment_file = None
                        pct = (i / n) * 100.0
                        elapsed_mins = (time.time() - job_start_time) / 60.0 if job_start_time else 0.0
                        print(f"\n\n⚠️ ========================================================")
                        print(f"⚠️ TIME LIMIT BUDGET REACHED DURING CLIP [{i+1}/{n}] '{f.path.name}' ({elapsed_mins:.1f}m >= {max_runtime_minutes}m)!")
                        print(f"💾 Checkpoint safely preserved in cache: {i}/{n} clips processed ({pct:.1f}%).")
                        print(f"🔄 Setting resumed_needed=true for GitHub Actions auto-continuation.")
                        print(f"========================================================\n")
                        return False, f"TIME_BUDGET_REACHED ({i}/{n} clips done)", True

                    if success and seg_path.is_file() and seg_path.stat().st_size > 1024:
                        print(f"  ✅ Recovered clip [{i+1}/{n}] '{f.path.name}' successfully using video preservation fallback!")

            _current_segment_file = None
            if not success or not seg_path.is_file():
                if seg_path.exists():
                    seg_path.unlink(missing_ok=True)
                return False, f"Error normalizing clip [{i+1}/{n}] {f.path.name}: {err}", False

        _current_segment_file = None

        # All segments normalized! Losslessly concatenate them
        concat_list_file = cache_dir / "concat_segments.txt"
        with open(concat_list_file, "w", encoding="utf-8") as lf:
            for s in ts_segments:
                escaped = s.resolve().as_posix().replace("'", "'\\''")
                lf.write(f"file '{escaped}'\n")

        print(f"\n⚡ All {n} segments normalized! (Resumed {resumed_count}, Transcoded {n - resumed_count})")
        print(f"🚀 Losslessly concatenating segments into '{output_path.name}'...")

        concat_cmd = [
            str(self.ffmpeg_exe), "-y",
            "-fflags", "+genpts+discardcorrupt",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list_file),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero"
        ]
        if out_ext in (".mp4", ".m4v", ".mov"):
            concat_cmd.extend(["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"])
        concat_cmd.append(str(output_path))

        success, err = run_ffmpeg_with_progress(concat_cmd, total_duration, progress_callback)
        try:
            if concat_list_file.exists():
                concat_list_file.unlink()
        except Exception:
            pass

        if not success or not output_path.is_file():
            return False, f"Failed lossless concat of segments: {err}", False

        return True, "", False

    def join_visually_lossless_transcode(self, files: List[MediaFileInfo], analysis: CompatibilityAnalysis, output_path: Path, crf: int = 17, preset: str = "veryfast", progress_callback=None, cache_dir: Optional[Path] = None, max_runtime_minutes: Optional[int] = None, job_start_time: Optional[float] = None) -> Tuple[bool, str]:
        """Harmonize mismatched resolutions/codecs using resumable segment normalizer."""
        success, err, _ = self.join_visually_lossless_transcode_resumable(
            files, analysis, output_path, crf=crf, preset=preset,
            progress_callback=progress_callback, cache_dir=cache_dir,
            max_runtime_minutes=max_runtime_minutes, job_start_time=job_start_time
        )
        return success, err


# =====================================================================
# 6. OUTPUT FORMATTING & CI SUMMARY
# =====================================================================

BANNER = r"""
  ╔═══════════════════════════════════════════════════════════╗
  ║          🎬 ADVANCED LOSSLESS VIDEO JOINER                ║
  ║  Intact Quality Concatenation for MP4, TS, MKV, MOV, etc. ║
  ║       With Google Drive URL Downloader Integration        ║
  ╚═══════════════════════════════════════════════════════════╝
"""


def set_github_action_output(key: str, value: str):
    """Write output key=value to GITHUB_OUTPUT file for GitHub Actions."""
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        try:
            with open(gh_out, "a", encoding="utf-8") as f:
                f.write(f"{key}={value}\n")
        except Exception:
            pass


def write_github_step_summary(files: List[MediaFileInfo], analysis: CompatibilityAnalysis, output_path: Path, mode_used: str, success: bool, error_msg: str = ""):
    """Write metrics to GitHub Step Summary if running in GitHub Actions."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("# 🎬 Video Joiner Execution Summary\n\n")
            if success and output_path.is_file():
                f.write("### 🎉 Success: Video Merged Successfully!\n\n")
                f.write(f"- **Output File:** `{output_path.name}`\n")
                f.write(f"- **File Size:** `{format_size(output_path.stat().st_size)}`\n")
                f.write(f"- **Total Duration:** `{format_duration(analysis.total_duration)}`\n")
                f.write(f"- **Processing Mode:** `{mode_used}`\n\n")
                if "Lossless" in mode_used:
                    f.write("> [!NOTE]\n> **Quality Guarantee: 100% Lossless Direct Stream Copy** (`-c copy`).\n> Zero re-encoding occurred.\n\n")
                else:
                    f.write("> [!IMPORTANT]\n> **Visually Lossless Transcode** was applied due to varying stream properties.\n\n")
            elif "TIME_BUDGET_REACHED" in error_msg:
                f.write("### ⏳ Checkpoint Preserved: Time Budget Reached\n\n")
                f.write("> [!TIP]\n> Reached safety runtime budget before the 6-hour GitHub Actions timeout.\n")
                f.write("> Intermediate normalized clips have been preserved in cache.\n")
                f.write("> The workflow will automatically continue the remaining clips in the next run!\n\n")
            else:
                f.write("### ❌ Error: Video Joining Failed\n\n")
                if error_msg:
                    f.write(f"```text\n{error_msg}\n```\n\n")
            f.write("### 📊 Input Video Clips\n\n| # | File Name | Resolution | Video Codec | FPS | Audio | Duration | Size |\n|---|---|---|---|---|---|---|---|\n")
            for idx, item in enumerate(files, start=1):
                v, a = item.video, item.audio
                res = f"{v.width}x{v.height}" if (v and v.width) else "N/A"
                v_codec = v.codec if v else "None"
                fps = f"{v.fps:.2f}" if (v and v.fps) else "N/A"
                a_codec = f"{a.codec} {a.channels}ch" if a else "No Audio"
                f.write(f"| {idx} | `{item.path.name}` | {res} | {v_codec} | {fps} | {a_codec} | {format_duration(item.duration)} | {format_size(item.size_bytes)} |\n")
    except Exception:
        pass


def format_timestamp_hms(seconds: float) -> str:
    """Formats seconds into HH:MM:SS or MM:SS for timestamps."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def clean_chapter_title(filename_or_title: str) -> str:
    """Cleans a video filename or raw title into a readable chapter title."""
    stem = Path(filename_or_title).stem
    # Remove index prefixes like '01_', '01 - ', '1. '
    stem = re.sub(r"^\d+[\s._-]+", "", stem)
    return stem.strip() or filename_or_title


def generate_timestamps_file(probed_files: List[MediaFileInfo], output_path: Path) -> Path:
    """
    Generates YouTube and media player chapter timestamps file (.txt) alongside the merged output video.
    Returns the Path to the generated timestamps file.
    """
    ts_file = output_path.parent / f"{output_path.stem}_timestamps.txt"
    root_ts_file = output_path.parent / "merged_video_times_stamp.txt"

    total_duration = sum(f.duration for f in probed_files)
    
    lines = [
        "# ⏱️ Merged Video Timestamps & Chapter List",
        f"# Output Video: {output_path.name}",
        f"# Total Videos: {len(probed_files)}",
        f"# Total Duration: {format_duration(total_duration)} ({format_timestamp_hms(total_duration)})\n",
        "=" * 80,
        "🎬 YOUTUBE & MEDIA PLAYER CHAPTER TIMESTAMPS",
        "=" * 80,
    ]

    current_sec = 0.0
    chapter_rows = []
    for idx, item in enumerate(probed_files, 1):
        clean_title = clean_chapter_title(item.path.name)
        start_ts = format_timestamp_hms(current_sec)
        # Ensure first timestamp in chapter block begins at 00:00:00 or 00:00 for YouTube compliance
        if idx == 1:
            start_ts = "00:00:00" if total_duration >= 3600 else "00:00"
        lines.append(f"{start_ts} - {clean_title}")
        
        end_sec = current_sec + item.duration
        chapter_rows.append({
            "index": idx,
            "title": clean_title,
            "filename": item.path.name,
            "start": start_ts,
            "end": format_timestamp_hms(end_sec),
            "duration": format_duration(item.duration),
            "size": format_size(item.size_bytes)
        })
        current_sec = end_sec

    lines.append("\n" + "=" * 80)
    lines.append("📋 DETAILED TIMESTAMPS BREAKDOWN (START - END - DURATION)")
    lines.append("=" * 80)
    for row in chapter_rows:
        lines.append(f"{row['index']}. [{row['start']} - {row['end']}] ({row['duration']})")
        lines.append(f"   Title: {row['title']}")
        lines.append(f"   File:  {row['filename']} ({row['size']})\n")

    content = "\n".join(lines) + "\n"
    try:
        with open(ts_file, "w", encoding="utf-8") as f:
            f.write(content)
        with open(root_ts_file, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"📝 Timestamps file generated: {ts_file.resolve()}")
    except Exception as e:
        print(f"⚠️ Warning: Could not write timestamps file: {e}")

    return ts_file


def upload_merged_video_to_youtube(
    video_path: Path,
    timestamps_file: Optional[Path] = None,
    title: Optional[str] = None,
    privacy: str = "private",
    category: str = "22",
    tags: Optional[List[str]] = None
) -> Optional[str]:
    """
    Invokes scripts/upload.py to upload the merged video with timestamps chapters to YouTube.
    """
    print(f"\n📺 Auto-uploading merged video to YouTube ({privacy.upper()})...")
    upload_script = Path(__file__).parent / "scripts" / "upload.py"
    if not upload_script.is_file():
        upload_script = Path("scripts/upload.py")
    
    if not upload_script.is_file():
        print(f"⚠️ scripts/upload.py not found at {upload_script.resolve()}. Skipping YouTube auto-upload.")
        return None

    cmd = [
        sys.executable,
        str(upload_script.resolve()),
        str(video_path.resolve()),
        "--privacy", privacy,
        "--category", category,
    ]
    if title:
        cmd.extend(["--title", title])
    if timestamps_file and timestamps_file.is_file():
        cmd.extend(["--timestamps-file", str(timestamps_file.resolve())])
    if tags:
        cmd.extend(["--tags", ",".join(tags)])

    try:
        res = subprocess.run(cmd, check=False)
        if res.returncode == 0:
            print("✅ YouTube upload completed successfully!")
            return "success"
        else:
            print(f"⚠️ YouTube upload process returned non-zero exit code: {res.returncode}")
    except Exception as e:
        print(f"❌ YouTube upload failed with exception: {e}")
    return None


def execute_join(
    files: List[Path],
    output_path: Path,
    mode: str = "auto",
    crf: int = 17,
    preset: str = "veryfast",
    sort_mode: str = "natural",
    reverse_sort: bool = False,
    overwrite: bool = False,
    cache_dir: Optional[Path] = None,
    max_runtime_minutes: Optional[int] = None,
    job_start_time: Optional[float] = None,
    upload_youtube: bool = False,
    youtube_privacy: str = "private",
    youtube_title: Optional[str] = None,
    youtube_category: str = "22",
    youtube_tags: Optional[List[str]] = None,
) -> Tuple[bool, bool]:
    """Execute video joining workflow. Returns: (success: bool, resume_needed: bool)"""
    print(BANNER)
    is_ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))
    ffmpeg_exe, ffprobe_exe = ensure_ffmpeg(auto_prompt=not is_ci)

    # Convert any input .ts videos to .mp4 before probing and merging
    processed_files: List[Path] = []
    for p in files:
        if is_ts_file(p):
            processed_files.append(convert_ts_to_mp4(p, ffmpeg_exe=ffmpeg_exe, ffprobe_exe=ffprobe_exe, delete_original=True))
        else:
            processed_files.append(p)

    valid_files = filter_video_files(processed_files)
    if not valid_files:
        print("❌ Error: No supported video files found to join.")
        return False, False
    if len(valid_files) < 2:
        print(f"❌ Error: At least 2 video files are required to join. Found {len(valid_files)}.")
        return False, False

    sorted_paths = sort_files(valid_files, sort_mode=sort_mode, reverse=reverse_sort)
    print(f"🔍 Probing {len(sorted_paths)} media files...")
    probed_files = [probe_file(p, ffprobe_exe, ffmpeg_exe) for p in sorted_paths]

    errors = [f for f in probed_files if f.error]
    if errors:
        for err in errors:
            print(f"  ❌ Error probing {err.path.name}: {err.error}")
        valid_probed = [f for f in probed_files if not f.error and (f.video or f.audio)]
        if len(valid_probed) >= 2:
            print(f"⚠️ Warning: Skipping {len(errors)} unreadable or corrupted file(s) that could not be decoded.")
            print(f"   Proceeding to join the remaining {len(valid_probed)} valid videos...")
            probed_files = valid_probed
        else:
            return False, False

    analysis = analyze_compatibility(probed_files)
    output_path = output_path.resolve()

    if output_path.is_file() and not overwrite and not is_ci:
        ans = input(f"Output file '{output_path.name}' already exists. Overwrite? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            return False, False

    joiner = VideoJoiner(ffmpeg_exe, ffprobe_exe)
    use_lossless = (mode == "copy") or (mode == "auto" and analysis.is_lossless_ready)
    mode_reported = "Lossless Stream Copy (-c copy)" if use_lossless else f"Resumable Transcode (CRF {crf}, preset {preset})"

    def progress_callback(p: JoinProgress):
        cur_t, tot_t = format_duration(p.current_time_sec), format_duration(p.total_duration_sec)
        sys.stdout.write(f"\r  [{'█'*int(30*(p.percent/100.0)):<30}] {p.percent:5.1f}% ({cur_t} / {tot_t}) | Speed: {p.speed}")
        sys.stdout.flush()

    print(f"\n🚀 Joining into '{output_path.name}' ({mode_reported})...")
    success = False
    err_msg = ""
    resume_needed = False

    if use_lossless:
        success, err_msg = joiner.join_lossless_smart(probed_files, output_path, progress_callback)
        if not success and mode == "auto":
            print("\n🔄 Falling back to Resumable Visually Lossless Transcode...")
            mode_reported = f"Fallback Resumable Transcode (CRF {crf}, preset {preset})"
            success, err_msg, resume_needed = joiner.join_visually_lossless_transcode_resumable(
                probed_files, analysis, output_path, crf=crf, preset=preset,
                progress_callback=progress_callback, cache_dir=cache_dir,
                max_runtime_minutes=max_runtime_minutes, job_start_time=job_start_time
            )
    else:
        success, err_msg, resume_needed = joiner.join_visually_lossless_transcode_resumable(
            probed_files, analysis, output_path, crf=crf, preset=preset,
            progress_callback=progress_callback, cache_dir=cache_dir,
            max_runtime_minutes=max_runtime_minutes, job_start_time=job_start_time
        )

    print()
    write_github_step_summary(probed_files, analysis, output_path, mode_reported, success, err_msg)

    if success and output_path.is_file():
        print(f"\n🎉 SUCCESS! Merged video saved: {output_path} ({format_size(output_path.stat().st_size)})")
        
        # 1. Automatically generate timestamps/chapters file
        ts_file = generate_timestamps_file(probed_files, output_path)
        set_github_action_output("completed", "true")
        set_github_action_output("resumed_needed", "false")
        set_github_action_output("timestamps_file", str(ts_file.resolve()))

        # 2. Auto upload to YouTube if enabled
        if upload_youtube:
            upload_title = youtube_title or output_path.stem
            upload_merged_video_to_youtube(
                video_path=output_path,
                timestamps_file=ts_file,
                title=upload_title,
                privacy=youtube_privacy,
                category=youtube_category,
                tags=youtube_tags,
            )

        return True, False

    if resume_needed:
        set_github_action_output("completed", "false")
        set_github_action_output("resumed_needed", "true")
        return False, True

    set_github_action_output("completed", "false")
    set_github_action_output("resumed_needed", "false")
    print(f"\n❌ JOINING FAILED: {err_msg}")
    return False, False


# =====================================================================
# 7. MAIN CLI & CI ENTRYPOINT
# =====================================================================

def main():
    parser = argparse.ArgumentParser(description="🎬 Advanced Lossless Video Joiner (Google Drive & Local)")
    parser.add_argument("-i", "--input", nargs="+", help="Input video file paths.")
    parser.add_argument("-d", "--dir", help="Directory containing video clips.")
    parser.add_argument("-l", "--list", help="Text file with video paths.")
    parser.add_argument("-g", "--gdrive", help="Text file containing Google Drive links.")
    parser.add_argument("--urls", nargs="+", help="Google Drive URLs to download and merge.")
    parser.add_argument("--download-dir", default="downloads", help="Download directory.")
    parser.add_argument("--clean-downloads", action="store_true", help="Remove downloaded clips after merge.")
    parser.add_argument("-o", "--output", default="merged_video.mp4", help="Output filename.")
    parser.add_argument("--batch-range", default=None, help="Range or list of batches to process (e.g. '1 to 3', '1-3', '2-4', '1, 3', or 'all').")
    parser.add_argument("--batch-index", type=int, default=None, help="1-based index of specific batch to process from links file (e.g. 1 for first batch).")
    parser.add_argument("--batch-delay", type=int, default=300, help="Cooldown delay in seconds between batch items (default: 300 / 5 minutes).")
    parser.add_argument("-m", "--mode", choices=["auto", "copy", "transcode"], default="auto", help="Join mode.")
    parser.add_argument("--crf", type=int, default=17, help="Transcode CRF.")
    parser.add_argument("--preset", default="veryfast", choices=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"], help="x264 transcode speed preset (default: veryfast).")
    parser.add_argument("--cache-dir", default=None, help="Segment cache directory for resumable processing.")
    parser.add_argument("--max-runtime", type=int, default=330, help="Maximum execution runtime in minutes before saving checkpoint (default: 330).")
    parser.add_argument("--sort", choices=["natural", "alphabetical", "date", "size", "none"], default="natural", help="Sort order.")
    parser.add_argument("--reverse", action="store_true", help="Reverse sort.")
    parser.add_argument("-y", "--yes", action="store_true", help="Overwrite without asking.")
    
    # YouTube Auto-Upload Integration Options
    parser.add_argument("--upload-youtube", action="store_true", default=False,
                        help="Automatically upload the merged video with timestamps to YouTube after merge.")
    parser.add_argument("--youtube-privacy", default="private", choices=["private", "unlisted", "public"],
                        help="YouTube upload privacy status (default: private).")
    parser.add_argument("--youtube-title", default=None,
                        help="Custom title override for YouTube upload (default: output video name).")
    parser.add_argument("--youtube-category", default="22",
                        help="YouTube category ID (default: 22 / People & Blogs).")
    parser.add_argument("--youtube-tags", default=None,
                        help="Comma-separated tags for YouTube video.")

    args = parser.parse_args()
    is_ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))
    if is_ci:
        args.yes = True

    # Resolve environment overrides for YouTube upload
    upload_youtube = args.upload_youtube or (os.environ.get("UPLOAD_TO_YOUTUBE", "").strip().lower() in ("1", "true", "yes"))
    youtube_privacy = os.environ.get("YOUTUBE_PRIVACY") or args.youtube_privacy
    youtube_title = os.environ.get("YOUTUBE_TITLE") or args.youtube_title
    youtube_category = os.environ.get("YOUTUBE_CATEGORY") or args.youtube_category
    youtube_tags_raw = os.environ.get("YOUTUBE_TAGS") or args.youtube_tags
    youtube_tags = [t.strip() for t in youtube_tags_raw.split(",") if t.strip()] if youtube_tags_raw else None

    download_dir = Path(args.download_dir).resolve()
    job_start_time = time.time()

    def countdown_sleep(seconds: int, next_task_name: str = ""):
        """Countdown timer with dynamic in-place terminal updates."""
        if seconds <= 0:
            return
        target_info = f" for '{next_task_name}'" if next_task_name else ""
        print(f"\n⏳ Waiting {seconds}s ({seconds // 60}m {seconds % 60}s){target_info} before starting next batch...")
        remaining = seconds
        while remaining > 0:
            mins, secs = divmod(remaining, 60)
            sys.stdout.write(f"\r  🕒 Next task starts in: {mins:02d}:{secs:02d} (Ctrl+C to abort) ")
            sys.stdout.flush()
            sleep_step = min(1, remaining)
            time.sleep(sleep_step)
            remaining -= 1
        sys.stdout.write("\r  🚀 Timer finished. Resuming now!                                \n\n")
        sys.stdout.flush()

    # Check for multi-group batch processing in links file or arguments
    batch_groups: List[BatchGroup] = []
    if args.gdrive:
        p = Path(args.gdrive)
        if p.is_file():
            batch_groups = parse_batch_groups(p)
        else:
            batch_groups = parse_batch_groups(args.gdrive)
    elif args.urls:
        combined = "\n".join(args.urls)
        p = Path(combined.strip())
        if p.is_file():
            batch_groups = parse_batch_groups(p)
        else:
            batch_groups = parse_batch_groups(combined)
    elif os.environ.get("INPUT_URLS") or os.environ.get("GDRIVE_URLS") or os.environ.get("VIDEO_URLS"):
        raw_env = (os.environ.get("INPUT_URLS") or os.environ.get("GDRIVE_URLS") or os.environ.get("VIDEO_URLS") or "").strip()
        p = Path(raw_env)
        if p.is_file():
            batch_groups = parse_batch_groups(p)
        else:
            batch_groups = parse_batch_groups(raw_env)

    # Filter batches if user specified a range or specific index
    batch_range_input = args.batch_range or os.environ.get("BATCH_RANGE")
    if batch_range_input and batch_groups:
        selected_indices = parse_range_selection(batch_range_input, len(batch_groups))
        batch_groups = [batch_groups[i - 1] for i in selected_indices if 1 <= i <= len(batch_groups)]
        if not batch_groups:
            print(f"❌ Error: --batch-range '{batch_range_input}' matched 0 batches.")
            sys.exit(1)
    elif args.batch_index is not None and batch_groups:
        target_idx = args.batch_index - 1
        if 0 <= target_idx < len(batch_groups):
            batch_groups = [batch_groups[target_idx]]
        else:
            print(f"❌ Error: --batch-index {args.batch_index} out of range (1..{len(batch_groups)})")
            sys.exit(1)

    if len(batch_groups) > 1:
        print(f"\n========================================================")
        print(f"📦 BATCH QUEUE DETECTED: Found {len(batch_groups)} distinct video groups to process!")
        for idx, g in enumerate(batch_groups, 1):
            print(f"   [{idx}/{len(batch_groups)}] '{g.raw_label}' -> {g.name}.mp4 ({len(g.urls)} URLs)")
        print(f"   Cooldown between groups: {args.batch_delay} seconds ({args.batch_delay // 60} minutes)")
        print(f"========================================================\n")

        overall_success = True
        for idx, g in enumerate(batch_groups, 1):
            target_output = Path(f"{g.name}.mp4")
            print(f"\n🎬 ========================================================")
            print(f"🎬 PROCESSING BATCH [{idx}/{len(batch_groups)}]: '{g.raw_label}'")
            print(f"🎯 Target output: {target_output.resolve()}")
            print(f"🔗 Links count:   {len(g.urls)}")
            print(f"========================================================\n")

            input_paths = []
            try:
                input_paths, dl_resume_needed = download_all_videos(
                    g.urls,
                    dest_dir=download_dir,
                    max_runtime_minutes=args.max_runtime,
                    job_start_time=job_start_time
                )
                if dl_resume_needed:
                    set_github_action_output("completed", "false")
                    set_github_action_output("resumed_needed", "true")
                    print(f"\n💾 Download checkpoint preserved for Batch [{idx}/{len(batch_groups)}]: '{g.raw_label}'. Auto-continuation needed.")
                    sys.exit(0)
            except Exception as dl_err:
                print(f"❌ Batch [{idx}/{len(batch_groups)}] download failed: {dl_err}")
                overall_success = False
                continue

            if not input_paths:
                print(f"❌ Batch [{idx}/{len(batch_groups)}] produced no downloaded files.")
                overall_success = False
                continue

            batch_cache = (Path(args.cache_dir) / g.name) if args.cache_dir else (Path(".video_cache") / g.name)
            batch_yt_title = f"{g.name}" if not youtube_title else f"{youtube_title} - {g.name}"
            success, resume_needed = execute_join(
                files=input_paths,
                output_path=target_output,
                mode=args.mode,
                crf=args.crf,
                preset=args.preset,
                sort_mode=args.sort,
                reverse_sort=args.reverse,
                overwrite=args.yes,
                cache_dir=batch_cache,
                max_runtime_minutes=args.max_runtime,
                job_start_time=job_start_time,
                upload_youtube=upload_youtube,
                youtube_privacy=youtube_privacy,
                youtube_title=batch_yt_title,
                youtube_category=youtube_category,
                youtube_tags=youtube_tags,
            )
            if resume_needed:
                print(f"\n💾 Checkpoint preserved for Batch [{idx}/{len(batch_groups)}]: '{g.raw_label}'. Auto-continuation needed.")
                sys.exit(0)
            if not success:
                overall_success = False

            if success and args.clean_downloads:
                print(f"🧹 Cleaning up {len(input_paths)} downloaded source clips...")
                for f in input_paths:
                    try:
                        f.unlink(missing_ok=True)
                    except Exception:
                        pass

            # If more groups remain, wait configured batch_delay
            if idx < len(batch_groups):
                next_group = batch_groups[idx]
                countdown_sleep(args.batch_delay, next_task_name=next_group.raw_label)

        print(f"\n🎉 All {len(batch_groups)} batches finished!")
        sys.exit(0 if overall_success else 1)

    # Single job execution flow
    input_paths: List[Path] = []
    is_downloaded = False
    single_output_name = args.output
    dl_resume_needed = False

    if batch_groups and len(batch_groups) == 1:
        single_g = batch_groups[0]
        if single_g.name != "merged_video" and (not args.output or args.output == "merged_video.mp4"):
            single_output_name = f"{single_g.name}.mp4"
        input_paths, dl_resume_needed = download_all_videos(
            single_g.urls,
            dest_dir=download_dir,
            max_runtime_minutes=args.max_runtime,
            job_start_time=job_start_time
        )
        is_downloaded = True
    elif args.gdrive:
        urls = parse_links_file(Path(args.gdrive))
        input_paths, dl_resume_needed = download_all_videos(
            urls,
            dest_dir=download_dir,
            max_runtime_minutes=args.max_runtime,
            job_start_time=job_start_time
        )
        is_downloaded = True
    elif args.urls:
        parsed_urls = []
        for item in args.urls:
            parsed_urls.extend(extract_urls_from_text(item))
        input_paths, dl_resume_needed = download_all_videos(
            parsed_urls,
            dest_dir=download_dir,
            max_runtime_minutes=args.max_runtime,
            job_start_time=job_start_time
        )
        is_downloaded = True
    elif os.environ.get("GDRIVE_URLS") or os.environ.get("VIDEO_URLS"):
        env_val = os.environ.get("GDRIVE_URLS") or os.environ.get("VIDEO_URLS") or ""
        parsed_urls = extract_urls_from_text(env_val)
        input_paths, dl_resume_needed = download_all_videos(
            parsed_urls,
            dest_dir=download_dir,
            max_runtime_minutes=args.max_runtime,
            job_start_time=job_start_time
        )
        is_downloaded = True
    elif args.input:
        for item in args.input:
            p = Path(item)
            if p.is_file():
                input_paths.append(p)
    elif args.dir:
        input_paths = [p for p in Path(args.dir).iterdir() if p.is_file()]

    if dl_resume_needed:
        set_github_action_output("completed", "false")
        set_github_action_output("resumed_needed", "true")
        print(f"\n💾 Download checkpoint preserved for '{single_output_name}'. Auto-continuation needed.")
        sys.exit(0)

    if not input_paths:
        print("❌ Error: No input video files or Google Drive URLs provided.")
        sys.exit(1)

    output_path = Path(single_output_name)
    single_cache = Path(args.cache_dir) if args.cache_dir else (Path(".video_cache") / output_path.stem)
    success, resume_needed = execute_join(
        files=input_paths,
        output_path=output_path,
        mode=args.mode,
        crf=args.crf,
        preset=args.preset,
        sort_mode=args.sort,
        reverse_sort=args.reverse,
        overwrite=args.yes,
        cache_dir=single_cache,
        max_runtime_minutes=args.max_runtime,
        job_start_time=job_start_time,
        upload_youtube=upload_youtube,
        youtube_privacy=youtube_privacy,
        youtube_title=youtube_title or output_path.stem,
        youtube_category=youtube_category,
        youtube_tags=youtube_tags,
    )

    if resume_needed:
        print(f"\n💾 Checkpoint preserved for '{output_path.name}'. Auto-continuation needed.")
        sys.exit(0)

    if success and is_downloaded and args.clean_downloads:
        for f in input_paths:
            try:
                f.unlink()
            except Exception:
                pass

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
