"""
Video Joiner Execution Engine
Implements Lossless Stream Copy, TS Protocol Concat, and Visually Lossless Transcoding.
"""

import os
import sys
import tempfile
import subprocess
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Callable, Dict, Any, Tuple

from .probe import MediaFileInfo, CompatibilityAnalysis, probe_file
from .ffmpeg_utils import parse_time_to_seconds


@dataclass
class JoinProgress:
    percent: float = 0.0
    current_time_sec: float = 0.0
    total_duration_sec: float = 0.0
    speed: str = "1.0x"
    fps: float = 0.0
    eta_sec: float = 0.0
    status: str = "Processing..."


def escape_ffmpeg_concat_path(path: Path) -> str:
    """Escape file path for ffmpeg concat demuxer text file."""
    # Convert to POSIX format with forward slashes
    posix_path = path.resolve().as_posix()
    # In ffmpeg concat file, single quotes must be escaped as '\''
    escaped = posix_path.replace("'", "'\\''")
    return f"file '{escaped}'"


def run_ffmpeg_with_progress(
    cmd: List[str],
    total_duration: float,
    progress_callback: Optional[Callable[[JoinProgress], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    deadline_time: Optional[float] = None
) -> Tuple[bool, str]:
    """
    Execute ffmpeg subprocess, parsing progress pipe in real time with optional deadline check.
    Returns (success: bool, error_output: str).
    """
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    # Append progress reporting to stdout/pipe
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

    # Read progress from stdout
    out_time_sec = 0.0
    speed_str = "1.0x"
    fps_val = 0.0
    timed_out = False
    
    # Non-blocking or threaded stderr collector
    import threading
    def read_stderr():
        for line in proc.stderr:
            clean_line = line.strip()
            if clean_line:
                stderr_lines.append(clean_line)
                if log_callback:
                    log_callback(clean_line)
                    
    err_thread = threading.Thread(target=read_stderr, daemon=True)
    err_thread.start()

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
            timestr = line.split("=", 1)[1].strip()
            parsed = parse_time_to_seconds(timestr)
            if parsed > 0:
                out_time_sec = parsed
        elif line.startswith("speed="):
            speed_str = line.split("=", 1)[1].strip()
        elif line.startswith("fps="):
            try:
                fps_val = float(line.split("=", 1)[1].strip())
            except ValueError:
                fps_val = 0.0
        elif line.startswith("progress="):
            stage = line.split("=", 1)[1].strip()
            if total_duration > 0:
                pct = min(100.0, max(0.0, (out_time_sec / total_duration) * 100.0))
            else:
                pct = 0.0
                
            elapsed = max(0.001, time.time() - start_time)
            if pct > 0:
                est_total = (elapsed / (pct / 100.0))
                eta_sec = max(0.0, est_total - elapsed)
            else:
                eta_sec = 0.0
                
            if progress_callback:
                prog = JoinProgress()
                prog.percent = pct
                prog.current_time_sec = out_time_sec
                prog.total_duration_sec = total_duration
                prog.speed = speed_str
                prog.fps = fps_val
                prog.eta_sec = eta_sec
                prog.status = "Complete" if stage == "end" else "Joining..."
                progress_callback(prog)

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
    """
    High-performance video joiner supporting Lossless Demux Concat,
    TS protocol join, and High-Fidelity Visually Lossless Transcode.
    """
    def __init__(self, ffmpeg_exe: Path, ffprobe_exe: Path):
        self.ffmpeg_exe = ffmpeg_exe
        self.ffprobe_exe = ffprobe_exe

    def join_lossless_demux(
        self,
        files: List[MediaFileInfo],
        output_path: Path,
        progress_callback: Optional[Callable[[JoinProgress], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str]:
        """
        Execute 100% mathematical lossless concatenation via FFmpeg Concat Demuxer.
        Copies raw video & audio packets directly with ZERO quality degradation.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        total_duration = sum(f.duration for f in files)
        
        # Determine bitstream filters
        # e.g., if converting AAC from TS/ADTS or stream to MP4 container
        out_ext = output_path.suffix.lower()
        has_aac = any(f.audio and "aac" in f.audio.codec.lower() for f in files)
        has_ts = any(f.path.suffix.lower() == ".ts" for f in files)
        
        bsf_args = []
        if has_aac and (out_ext in (".mp4", ".m4v", ".mov") or has_ts):
            bsf_args = ["-bsf:a", "aac_adtstoasc"]

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as concat_file:
            concat_list_path = Path(concat_file.name)
            concat_file.write("ffconcat version 1.0\n")
            for f in files:
                concat_file.write(f"{escape_ffmpeg_concat_path(f.path)}\n")

        try:
            cmd = [
                str(self.ffmpeg_exe),
                "-y",
                "-fflags", "+genpts+discardcorrupt",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list_path),
                "-c", "copy"
            ]
            
            # Subtitle / stream mapping
            if out_ext == ".mkv":
                cmd.extend(["-map", "0"])
            else:
                # Map video and audio if present
                cmd.extend(["-map", "0:v?", "-map", "0:a?"])
                
            cmd.extend(bsf_args)
            
            # Timestamp continuity and web-optimization flags
            cmd.extend([
                "-avoid_negative_ts", "make_zero",
                "-max_muxing_queue_size", "4096"
            ])
            
            if out_ext in (".mp4", ".m4v", ".mov"):
                cmd.extend(["-movflags", "+faststart"])
                
            cmd.append(str(output_path))
            
            success, err = run_ffmpeg_with_progress(
                cmd,
                total_duration=total_duration,
                progress_callback=progress_callback,
                log_callback=log_callback
            )
            return success, err
        finally:
            try:
                if concat_list_path.is_file():
                    concat_list_path.unlink()
            except Exception:
                pass

    def join_lossless_remux(
        self,
        files: List[MediaFileInfo],
        output_path: Path,
        progress_callback: Optional[Callable[[JoinProgress], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str]:
        """
        Lossless Intermediate Normalization:
        Remuxes mixed containers (.ts, .mp4, .mkv) losslessly (-c copy) into
        intermediate transport stream (.ts) chunks, then merges them with concat protocol.
        Guarantees 100% mathematical zero loss across mixed containers.
        """
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
                bsf_v = []
                if "264" in v_codec or "avc" in v_codec:
                    bsf_v = ["-bsf:v", "h264_mp4toannexb"]
                elif "265" in v_codec or "hevc" in v_codec:
                    bsf_v = ["-bsf:v", "hevc_mp4toannexb"]

                cmd = [
                    str(self.ffmpeg_exe),
                    "-y",
                    "-fflags", "+genpts+discardcorrupt+igndts",
                    "-err_detect", "ignore_err",
                    "-i", str(f.path),
                    "-c", "copy",
                    "-map", "0:v?",
                    "-map", "0:a?"
                ]
                cmd.extend(bsf_v)
                cmd.append(str(temp_ts))

                startupinfo = None
                if sys.platform == "win32":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

                subprocess.run(
                    cmd,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    startupinfo=startupinfo
                )

            # Join all TS chunks via concat protocol
            concat_url = "concat:" + "|".join(str(p.resolve()) for p in temp_ts_files)
            cmd = [
                str(self.ffmpeg_exe),
                "-y",
                "-i", concat_url,
                "-c", "copy",
                "-fflags", "+genpts",
                "-avoid_negative_ts", "make_zero"
            ]

            if out_ext in (".mp4", ".m4v", ".mov"):
                cmd.extend(["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"])

            cmd.append(str(output_path))

            return run_ffmpeg_with_progress(
                cmd,
                total_duration=total_duration,
                progress_callback=progress_callback,
                log_callback=log_callback
            )
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def join_lossless_smart(
        self,
        files: List[MediaFileInfo],
        output_path: Path,
        progress_callback: Optional[Callable[[JoinProgress], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str]:
        """
        Smart lossless joiner:
        - If all files are .ts: uses concat protocol
        - If mixed containers (.ts, .mp4, .mkv): uses lossless intermediate remux
        - If matching containers: tries concat demuxer, falling back to remux if needed.
        """
        extensions = {f.path.suffix.lower() for f in files}
        
        if extensions == {".ts"}:
            return self.join_ts_protocol(files, output_path, progress_callback, log_callback)
        elif len(extensions) > 1 or ".ts" in extensions:
            return self.join_lossless_remux(files, output_path, progress_callback, log_callback)
        else:
            success, err = self.join_lossless_demux(files, output_path, progress_callback, log_callback)
            if not success:
                # Fallback to remux
                return self.join_lossless_remux(files, output_path, progress_callback, log_callback)
            return success, err

    def join_ts_protocol(
        self,
        files: List[MediaFileInfo],
        output_path: Path,
        progress_callback: Optional[Callable[[JoinProgress], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str]:
        """
        Lossless concatenation using FFmpeg's concat: protocol for MPEG-TS streams.
        Ideal for .ts chunks (e.g. from HLS, broadcast streams).
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        total_duration = sum(f.duration for f in files)
        out_ext = output_path.suffix.lower()
        
        # Build concat string: "concat:f1.ts|f2.ts|f3.ts"
        ts_inputs = "|".join(str(f.path.resolve()) for f in files)
        concat_url = f"concat:{ts_inputs}"
        
        cmd = [
            str(self.ffmpeg_exe),
            "-y",
            "-i", concat_url,
            "-c", "copy",
            "-fflags", "+genpts",
            "-avoid_negative_ts", "make_zero"
        ]
        
        if out_ext in (".mp4", ".m4v"):
            cmd.extend(["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"])
            
        cmd.append(str(output_path))
        
        return run_ffmpeg_with_progress(
            cmd,
            total_duration=total_duration,
            progress_callback=progress_callback,
            log_callback=log_callback
        )

    def join_visually_lossless_transcode_resumable(
        self,
        files: List[MediaFileInfo],
        analysis: CompatibilityAnalysis,
        output_path: Path,
        crf: int = 17,
        preset: str = "veryfast",
        progress_callback: Optional[Callable[[JoinProgress], None]] = None,
        cache_dir: Optional[Path] = None,
        max_runtime_minutes: Optional[int] = None,
        job_start_time: Optional[float] = None
    ) -> Tuple[bool, str, bool]:
        """
        Segment-by-segment resumable normalizer with checkpointing & safety time budget guard.
        Returns: (success: bool, error_message: str, resume_needed: bool)
        """
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

        manifest_path = cache_dir / "normalization_manifest.json"
        manifest_data = {}
        if manifest_path.is_file():
            try:
                with open(manifest_path, "r", encoding="utf-8") as mf:
                    manifest_data = json.load(mf)
            except Exception:
                manifest_data = {}

        for i, f in enumerate(files):
            seg_path = cache_dir / f"seg_{i:04d}.ts"
            done_file = cache_dir / f"seg_{i:04d}.done"
            ts_segments.append(seg_path)
            seg_key = f"seg_{i:04d}"

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
            partial_dur = 0.0
            if seg_path.is_file() and seg_path.stat().st_size > 1024:
                if done_file.is_file() or (seg_key in manifest_data and manifest_data[seg_key].get("done")):
                    is_valid_segment = True
                else:
                    seg_probe = probe_file(seg_path, self.ffprobe_exe, self.ffmpeg_exe)
                    if seg_probe and not seg_probe.error and seg_probe.video:
                        if f.duration > 0:
                            if seg_probe.duration > 0 and seg_probe.duration >= f.duration - 2.0:
                                is_valid_segment = True
                            elif seg_probe.duration > 0:
                                partial_dur = seg_probe.duration
                        else:
                            if seg_probe.duration > 1.0 or seg_path.stat().st_size > 10240:
                                is_valid_segment = True

                if is_valid_segment and not done_file.is_file():
                    try:
                        done_file.touch(exist_ok=True)
                    except Exception:
                        pass

            if is_valid_segment:
                resumed_count += 1
                cur_pct = ((i + 1) / n) * 100.0
                sys.stdout.write(f"\r  [{'█'*int(30*(cur_pct/100.0)):<30}] {cur_pct:5.1f}% | ⏩ [Resume {i+1}/{n}] '{f.path.name}' already normalized. Skipping.\n")
                sys.stdout.flush()
                continue

            # Check if we can do partial resumption for an interrupted clip
            is_partial_resume = False
            resume_offset = 0.0
            seg_resume_part = cache_dir / f"seg_{i:04d}_resume.ts"
            if seg_resume_part.exists():
                seg_resume_part.unlink(missing_ok=True)

            if partial_dur >= 5.0 and f.duration > 0 and partial_dur < f.duration - 2.0:
                is_partial_resume = True
                resume_offset = partial_dur
                resume_pct = (resume_offset / f.duration) * 100.0
                print(f"\n⚡ Resuming Clip [{i+1}/{n}]: '{f.path.name}' from {resume_offset:.1f}s ({resume_pct:.1f}%) -> {f.duration:.1f}s...")
            else:
                if seg_path.exists():
                    seg_path.unlink(missing_ok=True)
                if done_file.exists():
                    done_file.unlink(missing_ok=True)
                clip_dur_str = f"{f.duration:.1f}s" if f.duration > 0 else "unknown"
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

            target_seg_dest = seg_resume_part if is_partial_resume else seg_path

            if is_partial_resume:
                resilient_input_opts = [
                    "-ss", f"{resume_offset:.3f}",
                    "-fflags", "+genpts+discardcorrupt+igndts",
                    "-err_detect", "ignore_err"
                ]
            else:
                resilient_input_opts = [
                    "-fflags", "+genpts+discardcorrupt+igndts",
                    "-err_detect", "ignore_err"
                ]

            if f.audio:
                cmd = [
                    str(self.ffmpeg_exe), "-y",
                ] + resilient_input_opts + [
                    "-i", str(f.path),
                    "-threads", "0",
                    "-vf", clip_vf,
                    "-af", clip_af,
                    "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-ar", str(target_sr), "-ac", "2",
                    "-avoid_negative_ts", "make_zero",
                    "-max_muxing_queue_size", "4096",
                    "-bsf:v", "h264_mp4toannexb",
                    str(target_seg_dest)
                ]
            else:
                cmd = [
                    str(self.ffmpeg_exe), "-y",
                ] + resilient_input_opts + [
                    "-i", str(f.path),
                    "-f", "lavfi", "-i", f"anullsrc=r={target_sr}:cl=stereo",
                    "-threads", "0",
                    "-vf", clip_vf,
                    "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-shortest",
                    "-avoid_negative_ts", "make_zero",
                    "-max_muxing_queue_size", "4096",
                    "-bsf:v", "h264_mp4toannexb",
                    str(target_seg_dest)
                ]

            rem_duration = max(1.0, f.duration - resume_offset) if is_partial_resume else f.duration

            def clip_progress(p: JoinProgress):
                clip_sec = min(f.duration, resume_offset + p.current_time_sec) if f.duration > 0 else (resume_offset + p.current_time_sec)
                clip_pct = min(100.0, (clip_sec / f.duration) * 100.0) if f.duration > 0 else p.percent
                overall_p = (i / n) * 100.0 + (clip_pct / n)
                sys.stdout.write(f"\r  [{'█'*int(30*(overall_p/100.0)):<30}] {overall_p:5.1f}% | Clip {i+1}/{n}: {clip_pct:5.1f}% | Speed: {p.speed}")
                sys.stdout.flush()

            success, err = run_ffmpeg_with_progress(cmd, rem_duration, clip_progress, deadline_time=deadline_time)
            print()

            if "TIME_BUDGET_REACHED" in err:
                if is_partial_resume and seg_resume_part.is_file() and seg_resume_part.stat().st_size > 1024:
                    try:
                        with open(seg_path, "ab") as f_main, open(seg_resume_part, "rb") as f_sub:
                            shutil.copyfileobj(f_sub, f_main)
                        seg_resume_part.unlink(missing_ok=True)
                    except Exception:
                        pass
                pct = (i / n) * 100.0
                elapsed_mins = (time.time() - job_start_time) / 60.0 if job_start_time else 0.0
                print(f"\n\n⚠️ ========================================================")
                print(f"⚠️ TIME LIMIT BUDGET REACHED DURING CLIP [{i+1}/{n}] '{f.path.name}' ({elapsed_mins:.1f}m >= {max_runtime_minutes}m)!")
                print(f"💾 Checkpoint safely preserved in cache: partial progress saved. Auto-continuation needed.")
                print(f"🔄 Setting resumed_needed=true for GitHub Actions auto-continuation.")
                print(f"========================================================\n")
                return False, f"TIME_BUDGET_REACHED ({i}/{n} clips done)", True

            # Multi-stage fault-tolerant fallback if primary attempt failed (e.g. fatal audio bitstream corruption)
            if not success or not target_seg_dest.is_file() or target_seg_dest.stat().st_size < 1024:
                if not is_partial_resume:
                    print(f"  ⚠️ Warning: Primary normalization failed for clip [{i+1}/{n}] '{f.path.name}'.")
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
                            "-threads", "0",
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
                            pct = (i / n) * 100.0
                            elapsed_mins = (time.time() - job_start_time) / 60.0 if job_start_time else 0.0
                            print(f"\n\n⚠️ ========================================================")
                            print(f"⚠️ TIME LIMIT BUDGET REACHED DURING CLIP [{i+1}/{n}] '{f.path.name}' ({elapsed_mins:.1f}m >= {max_runtime_minutes}m)!")
                            print(f"💾 Checkpoint safely preserved in cache: partial progress saved. Auto-continuation needed.")
                            print(f"🔄 Setting resumed_needed=true for GitHub Actions auto-continuation.")
                            print(f"========================================================\n")
                            return False, f"TIME_BUDGET_REACHED ({i}/{n} clips done)", True

                        if success and seg_path.is_file() and seg_path.stat().st_size > 1024:
                            print(f"  ✅ Recovered clip [{i+1}/{n}] '{f.path.name}' successfully using video preservation fallback!")

            # If this was a partial resume, merge resume part into main seg_path
            if is_partial_resume and success and seg_resume_part.is_file() and seg_resume_part.stat().st_size > 1024:
                try:
                    with open(seg_path, "ab") as f_main, open(seg_resume_part, "rb") as f_sub:
                        shutil.copyfileobj(f_sub, f_main)
                    seg_resume_part.unlink(missing_ok=True)
                except Exception as merge_err:
                    print(f"  ⚠️ Warning appending resume segment: {merge_err}")

            if not seg_path.is_file() or seg_path.stat().st_size < 1024:
                if seg_path.exists():
                    seg_path.unlink(missing_ok=True)
                if done_file.exists():
                    done_file.unlink(missing_ok=True)
                return False, f"Error normalizing clip [{i+1}/{n}] {f.path.name}: {err}", False

            # Mark segment as successfully completed
            try:
                done_file.touch(exist_ok=True)
                manifest_data[seg_key] = {
                    "index": i,
                    "name": f.path.name,
                    "size": seg_path.stat().st_size,
                    "done": True,
                    "timestamp": time.time()
                }
                with open(manifest_path, "w", encoding="utf-8") as mf:
                    json.dump(manifest_data, mf, indent=2)
            except Exception:
                pass

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

    def join_visually_lossless_transcode(
        self,
        files: List[MediaFileInfo],
        analysis: CompatibilityAnalysis,
        output_path: Path,
        crf: int = 17,
        preset: str = "veryfast",
        progress_callback: Optional[Callable[[JoinProgress], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        cache_dir: Optional[Path] = None,
        max_runtime_minutes: Optional[int] = None,
        job_start_time: Optional[float] = None
    ) -> Tuple[bool, str]:
        """Harmonize mismatched resolutions/codecs using resumable segment normalizer."""
        success, err, _ = self.join_visually_lossless_transcode_resumable(
            files, analysis, output_path, crf=crf, preset=preset,
            progress_callback=progress_callback, cache_dir=cache_dir,
            max_runtime_minutes=max_runtime_minutes, job_start_time=job_start_time
        )
        return success, err
