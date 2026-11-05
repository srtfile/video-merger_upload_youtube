#!/usr/bin/env python3
"""
🔄 Auto-Continuation Dispatcher for GitHub Actions
Automatically triggers the next workflow run when a batch does not finish
within the runtime safety limit or is interrupted, continuing from the exact
checkpoint saved in cache.
"""

import os
import sys
import json
import subprocess
import urllib.request
import urllib.error


def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    ref = os.environ.get("GITHUB_REF_NAME", "main").strip()
    video_urls = os.environ.get("INPUT_VIDEO_URLS", "").strip()
    batch_index_raw = os.environ.get("BATCH_INDEX", "1").strip()
    total_batches_raw = os.environ.get("TOTAL_BATCHES", "1").strip()
    output_filename = os.environ.get("OUTPUT_FILENAME", "merged_video.mp4").strip()
    join_mode = os.environ.get("JOIN_MODE", "auto").strip()
    preset = os.environ.get("PRESET", "ultrafast").strip()
    crf = os.environ.get("CRF", "20").strip()
    sort_order = os.environ.get("SORT_ORDER", "natural").strip()
    max_runtime = os.environ.get("MAX_RUNTIME", "320").strip()
    upload_release = os.environ.get("UPLOAD_RELEASE", "false").strip().lower()
    upload_youtube = os.environ.get("UPLOAD_YOUTUBE", "false").strip().lower()
    youtube_privacy = os.environ.get("YOUTUBE_PRIVACY", "private").strip()
    youtube_title = os.environ.get("YOUTUBE_TITLE", "").strip()
    batch_delay = os.environ.get("BATCH_DELAY", "300").strip()

    try:
        batch_index = int(batch_index_raw)
    except ValueError:
        batch_index = 1

    try:
        total_batches = int(total_batches_raw)
    except ValueError:
        total_batches = batch_index

    # Calculate resumption range (e.g. if batch 2 of 4 timed out, resume "2-4")
    if total_batches > batch_index:
        resume_range = f"{batch_index}-{total_batches}"
    else:
        resume_range = str(batch_index)

    print("========================================================")
    print("🔄 AUTO-CONTINUATION TRIGGER")
    print(f"📦 Target Repository: {repo}")
    print(f"🌿 Ref / Branch:      {ref}")
    print(f"🎯 Resuming Batches:  {resume_range} (current batch: {batch_index}, total: {total_batches})")
    print(f"⏱️ Safety Max Runtime: {max_runtime} minutes")
    print(f"⏳ Cooldown Delay:    {batch_delay} seconds (5 minutes)")
    print("========================================================")

    # Determine token: check multiple common PAT secret names
    pat_candidates = [
        ("GH_PAT", os.environ.get("GH_PAT", "")),
        ("PAT", os.environ.get("PAT", "")),
        ("PERSONAL_ACCESS_TOKEN", os.environ.get("PERSONAL_ACCESS_TOKEN", "")),
        ("GITHUB_PAT", os.environ.get("GITHUB_PAT", "")),
        ("WORKFLOW_TOKEN", os.environ.get("WORKFLOW_TOKEN", "")),
        ("GH_TOKEN", os.environ.get("GH_TOKEN", "")),
        ("ACTIONS_PAT", os.environ.get("ACTIONS_PAT", "")),
    ]

    selected_pat_name = None
    token = None
    for name, val in pat_candidates:
        if val and val.strip():
            selected_pat_name = name
            token = val.strip()
            break

    using_default_token = False
    if not token:
        # Fall back to GITHUB_TOKEN
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        using_default_token = True

    if not token:
        print("❌ Error: No authentication token found (neither GH_PAT nor GITHUB_TOKEN).")
        sys.exit(1)

    payload_inputs = {
        "video_urls": video_urls,
        "batch_range": resume_range,
        "output_filename": output_filename,
        "join_mode": join_mode,
        "preset": preset,
        "crf": crf,
        "sort_order": sort_order,
        "max_runtime": max_runtime,
        "upload_release": upload_release,
        "upload_youtube": upload_youtube,
        "youtube_privacy": youtube_privacy,
        "youtube_title": youtube_title,
        "batch_delay": batch_delay,
        "is_continuation": "true",
    }

    # First attempt: Direct GitHub REST API dispatch
    api_url = f"https://api.github.com/repos/{repo}/actions/workflows/main.yml/dispatches"
    req_body = json.dumps({
        "ref": ref,
        "inputs": payload_inputs
    }).encode("utf-8")

    req = urllib.request.Request(api_url, data=req_body, method="POST")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "GitHub-Actions-Continuation")

    success = False
    error_msg = ""

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status in (200, 201, 204):
                success = True
                print("\n✅ Successfully dispatched continuation workflow via GitHub REST API!")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:
            pass
        error_msg = f"HTTP {e.code}: {e.reason} - {body}"
    except Exception as e:
        error_msg = str(e)

    # Second attempt: Try gh CLI if REST API failed or for redundancy
    if not success:
        print(f"⚠️ REST API dispatch returned: {error_msg}")
        print("🔄 Attempting dispatch via GitHub CLI (gh)...")

        gh_env = os.environ.copy()
        gh_env["GH_TOKEN"] = token
        cmd = [
            "gh", "workflow", "run", "main.yml",
            "-R", repo,
            "--ref", ref,
            "-f", f"video_urls={video_urls}",
            "-f", f"batch_range={resume_range}",
            "-f", f"output_filename={output_filename}",
            "-f", f"join_mode={join_mode}",
            "-f", f"preset={preset}",
            "-f", f"crf={crf}",
            "-f", f"sort_order={sort_order}",
            "-f", f"max_runtime={max_runtime}",
            "-f", f"upload_release={upload_release}",
            "-f", f"upload_youtube={upload_youtube}",
            "-f", f"youtube_privacy={youtube_privacy}",
            "-f", f"youtube_title={youtube_title}",
            "-f", f"batch_delay={batch_delay}",
            "-f", "is_continuation=true",
        ]
        try:
            res = subprocess.run(cmd, env=gh_env, capture_output=True, text=True, timeout=30)
            if res.returncode == 0:
                success = True
                print("✅ Successfully dispatched continuation workflow via GitHub CLI!")
            else:
                error_msg = f"{error_msg} | gh CLI: {res.stderr.strip()}"
        except Exception as e:
            error_msg = f"{error_msg} | gh CLI exception: {e}"

    if success:
        print("\n========================================================")
        print("🚀 NEXT RUN TRIGGERED SUCCESSFULLY IN GITHUB ACTIONS!")
        print(f"   Batch(es) to process: {resume_range}")
        print(f"   Continuation mode:   ACTIVE (is_continuation=true)")
        print(f"   Token used:          {selected_pat_name or 'Token'}")
        print("⏳ The new workflow run has been created and will wait")
        print("   5 minutes (300 seconds) cooldown before resuming work.")
        print("========================================================\n")
        sys.exit(0)
    else:
        print("\n" + "=" * 70)
        print("❌ COULD NOT AUTO-TRIGGER CONTINUATION RUN AUTOMATICALLY")
        print(f"Details: {error_msg}")
        print("=" * 70)
        if using_default_token or "Resource not accessible by integration" in error_msg or "403" in error_msg:
            print("\n🔑 REASON: GitHub requires a Personal Access Token (PAT) with 'workflow' scope.")
            print("   GitHub's built-in GITHUB_TOKEN is prevented by security policy from")
            print("   creating new workflow runs to prevent recursive loops.")
            print("\n📋 HOW TO FIX (Takes 1 minute):")
            print("1. Open: https://github.com/settings/tokens/new")
            print("2. Set Note: 'Video Merger Auto Continuation'")
            print("3. Check the 'workflow' scope checkbox (also grants 'repo').")
            print("4. Click 'Generate token' and copy it.")
            print(f"5. In this repo ({repo}): Go to Settings -> Secrets and variables -> Actions")
            print("6. Click 'New repository secret', Name: 'GH_PAT', and paste your token.")
            print("7. Re-run your workflow - all future continuation runs will trigger automatically!\n")
        print("=" * 70 + "\n")
        sys.exit(1)


if __name__ == "__main__":
    main()

