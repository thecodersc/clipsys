"""YouTube Shorts publisher via Data API v3 resumable upload."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from clipsys._env import settings
from clipsys._log import get_logger
from clipsys.publish.base import Publisher, PublishResult
from clipsys.publish.quota import (
    YT_UPDATE_UNITS,
    YT_UPLOAD_UNITS,
    QuotaExceededError,
    charge_units,
    check_quota,
)

log = get_logger(__name__)

TOKENS_DIR = Path("data/tokens")
# youtube.upload covers videos.insert; force-ssl is needed for videos.update (private -> public).
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"
PUBLIC_RETRY_SECONDS = 10


def authorize(token_ref: str) -> Path:
    """Run the browser OAuth flow (Desktop-app client) and save the token for token_ref."""
    from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import]

    if not (settings.youtube_client_id and settings.youtube_client_secret):
        raise RuntimeError("Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env first.")
    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": settings.youtube_client_id,
                "client_secret": settings.youtube_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": TOKEN_URI,
            }
        },
        SCOPES,
    )
    # prompt=consent forces a refresh_token even on re-auth.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    token_path = TOKENS_DIR / f"{token_ref}.json"
    token_path.write_text(json.dumps({"token": creds.token, "refresh_token": creds.refresh_token}))
    return token_path


def _get_credentials(token_ref: str):
    from google.oauth2.credentials import Credentials  # type: ignore[import]

    token_path = TOKENS_DIR / f"{token_ref}.json"
    if not token_path.exists():
        raise FileNotFoundError(f"Token file not found: {token_path}")
    data = json.loads(token_path.read_text())
    return Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=TOKEN_URI,
        client_id=settings.youtube_client_id,
        client_secret=settings.youtube_client_secret,
        scopes=SCOPES,
    )


class YouTubePublisher(Publisher):
    def __init__(self, token_ref: str, dry_run: bool = True, quota_budget: int = 10_000) -> None:
        super().__init__(dry_run)
        self.token_ref = token_ref
        self.quota_budget = quota_budget

    def publish(
        self,
        variant_id: str,
        file_path: str,
        title: str,
        description: str,
        tags: list[str],
        account_id: str,
        privacy_status: str = "private",
        **kwargs: Any,
    ) -> PublishResult:
        # Always upload private; privacy_status="public" adds one videos.update afterwards.
        go_public = privacy_status == "public"
        units = YT_UPLOAD_UNITS + (YT_UPDATE_UNITS if go_public else 0)
        if not check_quota(account_id, units, self.quota_budget):
            raise QuotaExceededError(f"Quota would be exceeded for {account_id}")

        if self.dry_run:
            log.info(
                "youtube_dry_run",
                extra={
                    "variant_id": variant_id[:8],
                    "account_id": account_id,
                    "title": title[:40],
                },
            )
            charge_units(account_id, units)
            return PublishResult(
                platform_post_id=f"dry_run_{variant_id[:8]}",
                dry_run=True,
                metadata={"title": title, "privacy": privacy_status},
            )

        # Live path
        from googleapiclient.discovery import build  # type: ignore[import]
        from googleapiclient.http import MediaFileUpload  # type: ignore[import]

        creds = _get_credentials(self.token_ref)
        yt = build("youtube", "v3", credentials=creds)

        body = {
            "snippet": {
                "title": title[:100],
                # Hashtags only work as hashtags in the description; API tags take no "#".
                "description": (description + "\n\n" + " ".join(tags)).strip()[:5000],
                "tags": [t.lstrip("#") for t in tags[:10]],
                "categoryId": "20",  # Gaming
            },
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
        }

        media = MediaFileUpload(file_path, mimetype="video/mp4", resumable=True, chunksize=10 * 1024 * 1024)
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        while response is None:
            _, response = req.next_chunk()

        post_id = response["id"]
        charge_units(account_id, YT_UPLOAD_UNITS)

        if go_public:
            # A failed flip must not raise: the upload exists, and raising would re-upload it next run.
            # YouTube can answer 403 for a few seconds right after an upload, so retry briefly.
            for attempt in range(1, 5):
                try:
                    yt.videos().update(
                        part="status",
                        body={"id": post_id, "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}},
                    ).execute()
                    charge_units(account_id, YT_UPDATE_UNITS)
                    break
                except Exception as exc:
                    if attempt < 4:
                        time.sleep(PUBLIC_RETRY_SECONDS)
                        continue
                    privacy_status = "private"
                    log.error(
                        "youtube_make_public_failed",
                        extra={"account_id": account_id, "post_id": post_id, "err": str(exc)[:500]},
                    )

        log.info(
            "youtube_published",
            extra={"variant_id": variant_id[:8], "account_id": account_id, "post_id": post_id},
        )
        return PublishResult(
            platform_post_id=post_id,
            dry_run=False,
            metadata={"title": title, "privacy": privacy_status},
        )
