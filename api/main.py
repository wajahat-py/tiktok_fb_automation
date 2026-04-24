import json
import os
import shutil
import subprocess
import threading
import requests
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator
import uvicorn

app = FastAPI(title="TikTok → Facebook Prep API")

FB_API_BASE = "https://graph.facebook.com/v19.0"
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
STATE_FILE = DOWNLOAD_DIR / ".queued_ids.json"

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# State helpers — track every TikTok ID ever queued for upload
# ---------------------------------------------------------------------------

def _load_queued_ids() -> set[str]:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except Exception:
        return set()


def _save_queued_ids(ids: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(ids)))


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class PrepareRequest(BaseModel):
    tiktok_profile_url: str
    facebook_page_id: str
    facebook_api_access_token: str

    @field_validator("tiktok_profile_url")
    @classmethod
    def must_be_tiktok(cls, v: str) -> str:
        if "tiktok.com" not in v:
            raise ValueError("tiktok_profile_url must be a TikTok URL")
        return v.rstrip("/")

    @field_validator("facebook_page_id")
    @classmethod
    def must_be_numeric(cls, v: str) -> str:
        if not v.strip().isdigit():
            raise ValueError("facebook_page_id must be a numeric string")
        return v.strip()


class VideoItem(BaseModel):
    tiktok_id: str
    video_url: str
    caption: str
    local_path: str
    facebook_page_id: str
    facebook_api_access_token: str


class PrepareResponse(BaseModel):
    videos_to_upload: list[VideoItem]
    total_remaining: int
    facebook_page_id: str
    facebook_api_access_token: str


# ---------------------------------------------------------------------------
# TikTok helpers
# ---------------------------------------------------------------------------

def fetch_tiktok_videos(profile_url: str) -> list[dict]:
    result = subprocess.run(
        [
            "yt-dlp",
            "--flat-playlist",
            "--print", "%(id)s\t%(title)s",
            profile_url,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=502, detail=f"yt-dlp error: {result.stderr.strip()}")

    videos = []
    for line in result.stdout.splitlines():
        parts = line.strip().split("\t", 1)
        if parts and parts[0]:
            videos.append({
                "id": parts[0],
                "title": parts[1] if len(parts) > 1 else "",
            })
    return videos


def download_video(vid_id: str, username: str) -> Path | None:
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    video_url = f"https://www.tiktok.com/@{username}/video/{vid_id}"
    out_template = str(DOWNLOAD_DIR / f"{vid_id}.%(ext)s")

    result = subprocess.run(
        [
            "yt-dlp",
            "--output", out_template,
            "--format", "best[ext=mp4][vcodec!=none]/best[vcodec!=none]",
            "--no-warnings",
            video_url,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    matches = list(DOWNLOAD_DIR.glob(f"{vid_id}.*"))
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Prepare endpoint
# ---------------------------------------------------------------------------

@app.post("/prepare-videos", response_model=PrepareResponse)
def prepare_videos(req: PrepareRequest):
    tiktok_videos = fetch_tiktok_videos(req.tiktok_profile_url)
    if not tiktok_videos:
        raise HTTPException(status_code=404, detail="No videos found on TikTok profile")

    with _lock:
        queued_ids = _load_queued_ids()

    # TikTok returns newest-first — reverse for oldest-first, then exclude already queued
    oldest_first = [v for v in reversed(tiktok_videos) if v["id"] not in queued_ids]
    total_remaining = len(oldest_first)

    username = req.tiktok_profile_url.split("@")[-1].rstrip("/")
    videos_to_upload: list[VideoItem] = []

    for video in oldest_first:
        if len(videos_to_upload) == 2:
            break
        local_path = download_video(video["id"], username)
        if local_path:
            videos_to_upload.append(
                VideoItem(
                    tiktok_id=video["id"],
                    video_url=f"https://www.tiktok.com/@{username}/video/{video['id']}",
                    caption=video["title"],
                    local_path=str(local_path.resolve()),
                    facebook_page_id=req.facebook_page_id,
                    facebook_api_access_token=req.facebook_api_access_token,
                )
            )

    # Permanently record these IDs so they are never queued again
    if videos_to_upload:
        with _lock:
            queued_ids = _load_queued_ids()
            queued_ids.update(v.tiktok_id for v in videos_to_upload)
            _save_queued_ids(queued_ids)

    return PrepareResponse(
        videos_to_upload=videos_to_upload,
        total_remaining=total_remaining,
        facebook_page_id=req.facebook_page_id,
        facebook_api_access_token=req.facebook_api_access_token,
    )


# ---------------------------------------------------------------------------
# Delete all downloaded video files (state is preserved)
# ---------------------------------------------------------------------------

@app.delete("/downloads")
def delete_downloads():
    with _lock:
        if not DOWNLOAD_DIR.exists():
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            return {"success": True, "deleted": 0, "message": "Nothing to delete"}

        entries = [e for e in DOWNLOAD_DIR.iterdir() if e != STATE_FILE]
        errors = []
        for entry in entries:
            try:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink(missing_ok=True)
            except Exception as e:
                errors.append({"name": entry.name, "error": str(e)})

        if errors:
            return {"success": False, "deleted": len(entries) - len(errors), "errors": errors}

    return {"success": True, "deleted": len(entries), "message": f"Deleted {len(entries)} file(s)"}


# ---------------------------------------------------------------------------
# Reset upload state (clears queued IDs — use to start fresh)
# ---------------------------------------------------------------------------

@app.delete("/state")
def reset_state():
    with _lock:
        count = len(_load_queued_ids())
        if STATE_FILE.exists():
            STATE_FILE.unlink()
    return {"success": True, "cleared": count, "message": f"Cleared {count} queued ID(s)"}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
