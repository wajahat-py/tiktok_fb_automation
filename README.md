# TikTok → Facebook Automation

Automatically pulls the 2 oldest unposted TikTok videos from a profile and prepares them for upload to a Facebook page. A FastAPI service handles downloading and deduplication; n8n orchestrates the full workflow.

---

## How It Works

1. n8n triggers the FastAPI `/prepare-videos` endpoint on a schedule
2. FastAPI fetches the TikTok profile, filters out already-queued videos, downloads the 2 oldest unposted ones into a shared volume, and returns their metadata
3. n8n reads each video file from the shared volume and uploads it to Facebook via the Graph API
4. A local state file permanently records every queued video ID so the same video is never uploaded twice

---

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running
- A Facebook Page with a valid **Page Access Token** (long-lived, with `pages_manage_posts` and `pages_read_engagement` permissions)
- The numeric **Facebook Page ID**
- A public TikTok profile URL

---

## Setup

### 1. Clone the repo

```bash
git clone <your-repo-url>
cd tiktok_fb_automation
```

### 2. Start the containers

```bash
docker compose up --build -d
```

This starts:
- **FastAPI** at `http://localhost:8000`
- **n8n** at `http://localhost:5678`

Wait about 10 seconds for both services to be fully ready.

### 3. Open n8n and create an account

Go to `http://localhost:5678` and complete the first-time setup (create a local account — this stays on your machine).

### 4. Import the workflow

1. In n8n, click the **+** (New Workflow) button or go to **Workflows** in the sidebar
2. Click the **⋮** menu (top right) → **Import from File**
3. Select `workflow.json` from the project root
4. Click **Save**

### 5. Configure the workflow nodes

After importing, open the workflow and update the following nodes:

**HTTP Request node** (calls the FastAPI API):
- URL: `http://api:8000/prepare-videos`
- Method: `POST`
- Body (JSON):
  ```json
  {
    "tiktok_profile_url": "https://www.tiktok.com/@yourusername",
    "facebook_page_id": "YOUR_PAGE_ID",
    "facebook_api_access_token": "YOUR_ACCESS_TOKEN"
  }
  ```

**Read Binary File node** (reads the downloaded video):
- File Path: `{{ $json.local_path }}`

**Facebook upload node** (HTTP Request to Graph API):
- The response from `/prepare-videos` already includes `facebook_page_id` and `facebook_api_access_token` on each video item, so you can reference them directly with `{{ $json.facebook_page_id }}` and `{{ $json.facebook_api_access_token }}`

### 6. Activate the workflow

Toggle the workflow to **Active** in n8n. It will now run on the configured schedule automatically.

---

## API Endpoints

Base URL (from host): `http://localhost:8000`  
Base URL (from inside n8n): `http://api:8000`

### `POST /prepare-videos`

Fetches the TikTok profile, finds the 2 oldest unposted videos, downloads them, and returns their metadata. Videos returned are permanently recorded and will never be returned again.

**Request body:**
```json
{
  "tiktok_profile_url": "https://www.tiktok.com/@username",
  "facebook_page_id": "123456789",
  "facebook_api_access_token": "EAAx..."
}
```

**Response:**
```json
{
  "videos_to_upload": [
    {
      "tiktok_id": "7516591862803647766",
      "video_url": "https://www.tiktok.com/@username/video/7516591862803647766",
      "caption": "Video title here",
      "local_path": "/shared/downloads/7516591862803647766.mp4",
      "facebook_page_id": "123456789",
      "facebook_api_access_token": "EAAx..."
    }
  ],
  "total_remaining": 42,
  "facebook_page_id": "123456789",
  "facebook_api_access_token": "EAAx..."
}
```

### `DELETE /downloads`

Deletes all downloaded video files from the shared volume. The upload state (which videos have been queued) is **preserved** — deleted videos will not be re-queued.

**Response:**
```json
{ "success": true, "deleted": 2, "message": "Deleted 2 file(s)" }
```

### `DELETE /state`

Clears the upload history. Use this to start completely fresh — all TikTok videos will be considered unposted again.

**Response:**
```json
{ "success": true, "cleared": 10, "message": "Cleared 10 queued ID(s)" }
```

### `GET /health`

Returns `{ "status": "ok" }`. Useful for checking if the API is running.

---

## Project Structure

```
tiktok_fb_api/
├── docker-compose.yml       # Orchestrates api + n8n containers
├── workflow.json            # n8n workflow — import this on fresh setup
├── README.md
├── api/
│   ├── main.py              # FastAPI application
│   ├── requirements.txt
│   └── Dockerfile
├── shared/                  # Shared volume (bind mount)
│   └── downloads/           # Downloaded videos land here
│       └── .queued_ids.json # Auto-generated: tracks uploaded video IDs
└── n8n_data/                # n8n internal data (gitignored)
```

> `shared/` and `n8n_data/` are bind-mounted from the host — data persists across container restarts without named Docker volumes.

---

## Resetting Everything

**Delete downloaded videos only** (keeps upload history):
```bash
curl -X DELETE http://localhost:8000/downloads
```

**Reset upload history** (next run re-queues from the oldest video):
```bash
curl -X DELETE http://localhost:8000/state
```

**Full reset** (nuke everything and start over):
```bash
docker compose down
rm -rf shared/downloads/* n8n_data/*
docker compose up -d
```

---

## Troubleshooting

**n8n can't reach the API**
- Use `http://api:8000` (the service name), not `http://localhost:8000`. Inside Docker, `localhost` refers to the container itself.

**Read Binary File node: access not allowed**
- The file path must start with `/shared`. If you see a different path in `local_path`, check that `DOWNLOAD_DIR=/shared/downloads` is set in `docker-compose.yml`.

**Same videos uploading every time**
- Call `DELETE /state` to clear the queued IDs file, then trigger the workflow again.

**yt-dlp fails on TikTok videos**
- TikTok sometimes blocks scrapers. Update yt-dlp inside the container: `docker exec tiktok_fb_api pip install -U yt-dlp`
