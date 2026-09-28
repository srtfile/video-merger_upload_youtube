"""
Video Stream Prober & Compatibility Matrix
Analyzes video/audio streams and determines if lossless concatenation is safe.
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from fractions import Fraction


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

@dataclass
class VideoStreamInfo:
    codec: str = "unknown"
    profile: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    fps_str: str = ""
    pix_fmt: str = ""
    bitrate: int = 0
    duration: float = 0.0

@dataclass
class AudioStreamInfo:
    codec: str = "unknown"
    sample_rate: int = 0
    channels: int = 0
    channel_layout: str = ""
    bitrate: int = 0

@dataclass
class MediaFileInfo:
    path: Path
    format_name: str = ""
    duration: float = 0.0
    size_bytes: int = 0
    video: Optional[VideoStreamInfo] = None
    audio: Optional[AudioStreamInfo] = None
    has_subtitles: bool = False
    raw_data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

@dataclass
class CompatibilityAnalysis:
    is_lossless_ready: bool = False
    reasons_against_copy: List[str] = field(default_factory=list)
    video_codecs: List[str] = field(default_factory=list)
    resolutions: List[str] = field(default_factory=list)
    audio_codecs: List[str] = field(default_factory=list)
    total_duration: float = 0.0
    total_size_bytes: int = 0
    target_width: int = 0
    target_height: int = 0
    target_fps: float = 0.0
    target_pix_fmt: str = "yuv420p"
    target_audio_sample_rate: int = 48000


def parse_fps(fps_str: str) -> float:
    """Parse fractional fps string like '30000/1001' or '30' to float."""
    try:
        if "/" in fps_str:
            num, den = fps_str.split("/")
            den_f = float(den)
            return float(num) / den_f if den_f != 0 else 0.0
        return float(fps_str)
    except Exception:
        return 0.0


def probe_with_ffmpeg(file_path: Path, ffmpeg_exe: Path) -> MediaFileInfo:
    """Fallback probe using `ffmpeg -i` output when ffprobe is absent."""
    info = MediaFileInfo(path=file_path)
    if not file_path.is_file():
        info.error = f"File not found: {file_path}"
        return info
    info.size_bytes = file_path.stat().st_size
    
    cmd = [str(ffmpeg_exe), "-hide_banner", "-i", str(file_path)]
    startupinfo = None
    if hasattr(subprocess, "STARTUPINFO"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        
    res = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        startupinfo=startupinfo
    )
    output = res.stderr
    
    # Parse Duration: 00:01:23.45
    dur_match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", output)
    if dur_match:
        h, m, s = dur_match.groups()
        info.duration = float(h) * 3600 + float(m) * 60 + float(s)
        
    # Parse Video Stream: Stream #0:0: Video: h264 (...), yuv420p, 1920x1080 ..., 30 fps
    v_match = re.search(r"Stream #\d+:\d+.*?: Video:\s*([a-zA-Z0-9_-]+).*?,\s*([a-zA-Z0-9_-]+),\s*(\d+)x(\d+).*?,\s*([\d.]+)\s*fps", output)
    if v_match:
        v = VideoStreamInfo()
        v.codec = v_match.group(1).lower()
        v.pix_fmt = v_match.group(2)
        v.width = int(v_match.group(3))
        v.height = int(v_match.group(4))
        v.fps = float(v_match.group(5))
        info.video = v
    else:
        # Secondary simpler match
        v_simple = re.search(r"Stream #\d+:\d+.*?: Video:\s*([a-zA-Z0-9_-]+).*?,\s*(\d+)x(\d+)", output)
        if v_simple:
            v = VideoStreamInfo()
            v.codec = v_simple.group(1).lower()
            v.width = int(v_simple.group(2))
            v.height = int(v_simple.group(3))
            fps_match = re.search(r"([\d.]+)\s*(?:fps|tbr)", output)
            if fps_match:
                v.fps = float(fps_match.group(1))
            info.video = v
            
    # Parse Audio Stream: Stream #0:1: Audio: aac (...), 48000 Hz, stereo
    a_match = re.search(r"Stream #\d+:\d+.*?: Audio:\s*([a-zA-Z0-9_-]+).*?,\s*(\d+)\s*Hz,\s*([a-zA-Z0-9_-]+)", output)
    if a_match:
        a = AudioStreamInfo()
        a.codec = a_match.group(1).lower()
        a.sample_rate = int(a_match.group(2))
        ch_layout = a_match.group(3).lower()
        a.channels = 2 if "stereo" in ch_layout else (1 if "mono" in ch_layout else 6)
        info.audio = a
        
    return info


def probe_file(file_path: Path, ffprobe_exe: Optional[Path], ffmpeg_exe: Optional[Path] = None) -> MediaFileInfo:
    """Run ffprobe on a single file, falling back to ffmpeg if needed."""
    info = MediaFileInfo(path=file_path)
    if not file_path.is_file():
        info.error = f"File not found: {file_path}"
        return info

    if file_path.stat().st_size == 0:
        info.error = "File is empty (0 bytes)"
        return info

    if is_html_or_empty_file(file_path):
        info.error = "File is an HTML error page or text document (check Google Drive permissions/quota)"
        return info

    info.size_bytes = file_path.stat().st_size

    if not ffprobe_exe or not ffprobe_exe.is_file():
        if ffmpeg_exe and ffmpeg_exe.is_file():
            return probe_with_ffmpeg(file_path, ffmpeg_exe)
        info.error = "Neither ffprobe nor ffmpeg available to probe file"
        return info

    cmd = [
        str(ffprobe_exe),
        "-v", "error",
        "-probesize", "100M",
        "-analyzeduration", "100M",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(file_path)
    ]
    
    try:
        startupinfo = None
        if hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            startupinfo=startupinfo,
            timeout=45
        )
        if res.returncode != 0 or not res.stdout.strip():
            if ffmpeg_exe and ffmpeg_exe.is_file():
                fb = probe_with_ffmpeg(file_path, ffmpeg_exe)
                if fb and not fb.error and (fb.video or fb.audio):
                    return fb
            err_detail = res.stderr.strip() or "probesize/analyzeduration exceeded or corrupted stream"
            info.error = f"ffprobe error: {err_detail}"
            return info

        data = json.loads(res.stdout)
        info.raw_data = data
        
        # Parse Format
        fmt = data.get("format", {})
        info.format_name = fmt.get("format_name", "")
        try:
            info.duration = float(fmt.get("duration", 0.0))
        except (ValueError, TypeError):
            info.duration = 0.0
            
        # Parse Streams
        for stream in data.get("streams", []):
            codec_type = stream.get("codec_type")
            if codec_type == "video" and info.video is None:
                # Video stream
                v_info = VideoStreamInfo()
                v_info.codec = stream.get("codec_name", "unknown")
                v_info.profile = stream.get("profile", "")
                v_info.width = int(stream.get("width", 0))
                v_info.height = int(stream.get("height", 0))
                v_info.pix_fmt = stream.get("pix_fmt", "")
                v_info.fps_str = stream.get("r_frame_rate", "")
                v_info.fps = parse_fps(v_info.fps_str)
                try:
                    v_info.bitrate = int(stream.get("bit_rate", 0))
                except (ValueError, TypeError):
                    v_info.bitrate = 0
                try:
                    v_info.duration = float(stream.get("duration", 0.0))
                except (ValueError, TypeError):
                    v_info.duration = 0.0
                info.video = v_info
                
            elif codec_type == "audio" and info.audio is None:
                # Audio stream
                a_info = AudioStreamInfo()
                a_info.codec = stream.get("codec_name", "unknown")
                try:
                    a_info.sample_rate = int(stream.get("sample_rate", 0))
                except (ValueError, TypeError):
                    a_info.sample_rate = 0
                try:
                    a_info.channels = int(stream.get("channels", 0))
                except (ValueError, TypeError):
                    a_info.channels = 0
                a_info.channel_layout = stream.get("channel_layout", "")
                try:
                    a_info.bitrate = int(stream.get("bit_rate", 0))
                except (ValueError, TypeError):
                    a_info.bitrate = 0
                try:
                    a_info.duration = float(stream.get("duration", 0.0))
                except (ValueError, TypeError):
                    a_info.duration = 0.0
                info.audio = a_info
                
            elif codec_type == "subtitle":
                info.has_subtitles = True

        # If format duration was missing or 0 (common in MPEG-TS files), use stream or ffmpeg probe
        if info.duration <= 0.0:
            if info.video and getattr(info.video, "duration", 0.0) > 0:
                info.duration = info.video.duration
            elif info.audio and getattr(info.audio, "duration", 0.0) > 0:
                info.duration = info.audio.duration
            elif ffmpeg_exe and ffmpeg_exe.is_file():
                fb = probe_with_ffmpeg(file_path, ffmpeg_exe)
                if fb and fb.duration > 0:
                    info.duration = fb.duration

        if not info.video and not info.audio and ffmpeg_exe and ffmpeg_exe.is_file():
            fb = probe_with_ffmpeg(file_path, ffmpeg_exe)
            if fb and not fb.error and (fb.video or fb.audio):
                return fb
                
    except Exception as e:
        if ffmpeg_exe and ffmpeg_exe.is_file():
            fb = probe_with_ffmpeg(file_path, ffmpeg_exe)
            if fb and not fb.error and (fb.video or fb.audio):
                return fb
        info.error = f"Probing failed: {e}"
        
    return info


def analyze_compatibility(files: List[MediaFileInfo]) -> CompatibilityAnalysis:
    """
    Compare multiple probed media files and check if lossless stream copy is safe,
    or if differences require high-fidelity transcoding.
    """
    analysis = CompatibilityAnalysis()
    if not files:
        analysis.reasons_against_copy.append("No input files provided.")
        return analysis
        
    analysis.total_duration = sum(f.duration for f in files)
    analysis.total_size_bytes = sum(f.size_bytes for f in files)
    
    first = files[0]
    if first.error:
        analysis.reasons_against_copy.append(f"File 1 ({first.path.name}) error: {first.error}")
        return analysis
        
    first_v = first.video
    first_a = first.audio
    
    if not first_v:
        analysis.reasons_against_copy.append(f"File 1 ({first.path.name}) contains no video stream.")
        return analysis
        
    # Baseline attributes
    base_v_codec = first_v.codec
    base_width = first_v.width
    base_height = first_v.height
    base_fps = first_v.fps
    base_pix_fmt = first_v.pix_fmt
    
    base_a_codec = first_a.codec if first_a else None
    base_a_sr = first_a.sample_rate if first_a else None
    base_a_ch = first_a.channels if first_a else None
    
    max_w = base_width
    max_h = base_height
    max_fps = base_fps
    
    v_codecs = {base_v_codec}
    resolutions = {f"{base_width}x{base_height}"}
    a_codecs = {base_a_codec} if base_a_codec else set()
    
    for i, item in enumerate(files[1:], start=2):
        if item.error:
            analysis.reasons_against_copy.append(f"File {i} ({item.path.name}) error: {item.error}")
            continue
            
        v = item.video
        a = item.audio
        
        if not v:
            analysis.reasons_against_copy.append(f"File {i} ({item.path.name}) has no video stream.")
            continue
            
        v_codecs.add(v.codec)
        resolutions.add(f"{v.width}x{v.height}")
        if a:
            a_codecs.add(a.codec)
            
        if v.width > max_w:
            max_w = v.width
        if v.height > max_h:
            max_h = v.height
        if v.fps > max_fps:
            max_fps = v.fps
            
        # Compare video codec
        if v.codec != base_v_codec:
            analysis.reasons_against_copy.append(
                f"Video codec mismatch: '{first.path.name}' is {base_v_codec} but '{item.path.name}' is {v.codec}"
            )
            
        # Compare resolution
        if v.width != base_width or v.height != base_height:
            analysis.reasons_against_copy.append(
                f"Resolution mismatch: '{first.path.name}' is {base_width}x{base_height} but '{item.path.name}' is {v.width}x{v.height}"
            )
            
        # Compare pixel format
        if v.pix_fmt and base_pix_fmt and v.pix_fmt != base_pix_fmt:
            analysis.reasons_against_copy.append(
                f"Pixel format mismatch: '{first.path.name}' is {base_pix_fmt} but '{item.path.name}' is {v.pix_fmt}"
            )
            
        # Compare FPS (allow slight deviation < 0.1 fps)
        if abs(v.fps - base_fps) > 0.1 and v.fps > 0 and base_fps > 0:
            analysis.reasons_against_copy.append(
                f"Framerate mismatch: '{first.path.name}' is {base_fps} fps but '{item.path.name}' is {v.fps} fps"
            )
            
        # Compare audio presence & properties
        if (first_a is None) != (a is None):
            analysis.reasons_against_copy.append(
                f"Audio stream presence mismatch between '{first.path.name}' and '{item.path.name}'"
            )
        elif first_a and a:
            if a.codec != base_a_codec:
                analysis.reasons_against_copy.append(
                    f"Audio codec mismatch: '{first.path.name}' has {base_a_codec} but '{item.path.name}' has {a.codec}"
                )
            if a.sample_rate != base_a_sr:
                analysis.reasons_against_copy.append(
                    f"Audio sample rate mismatch: '{first.path.name}' is {base_a_sr}Hz but '{item.path.name}' is {a.sample_rate}Hz"
                )
            if a.channels != base_a_ch:
                analysis.reasons_against_copy.append(
                    f"Audio channel count mismatch: '{first.path.name}' is {base_a_ch}ch but '{item.path.name}' is {a.channels}ch"
                )

    analysis.video_codecs = sorted(list(v_codecs))
    analysis.resolutions = sorted(list(resolutions))
    analysis.audio_codecs = sorted(list(a_codecs))
    analysis.target_width = max_w if max_w > 0 else 1920
    analysis.target_height = max_h if max_h > 0 else 1080
    analysis.target_fps = max_fps if max_fps > 0 else 30.0
    analysis.target_pix_fmt = base_pix_fmt or "yuv420p"
    analysis.target_audio_sample_rate = base_a_sr if (first_a and base_a_sr) else 48000
    
    # If no blockers found, lossless copy is 100% ready!
    analysis.is_lossless_ready = (len(analysis.reasons_against_copy) == 0)
    return analysis
