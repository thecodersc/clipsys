"""YouTube Shorts publisher via Data API v3 resumable upload."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clipsys._env import settings
from clipsys._log import get_logger
from clipsys.publish.base import Publisher, PublishResult
from clipsys.publish.quota import YT_UPLOAD_UNITS, QuotaExceededError, charge_units, check_quota

log = get_logger(__name__)

TOKENS_DIR = Path("data/tokens")


def _get_credentials(token_ref: str):
    from google.oauth2.credentials import Credentials  # type: ignore[import]

    token_path = TOKENS_DIR / f"{token_ref}.json"
    if not token_path.exists():
        raise FileNotFoundError(f"Token file not found: {token_path}")
    data = json.loads(token_path.read_text())
    return Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.youtube_client_id,
        client_secret=settings.youtube_client_secret,
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
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
        if not check_quota(account_id, YT_UPLOAD_UNITS, self.quota_budget):
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
            charge_units(account_id, YT_UPLOAD_UNITS)
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
                "description": description[:5000],
                "tags": tags[:10],
                "categoryId": "20",  # Gaming
            },
            "status": {"privacyStatus": privacy_status, "selfDeclaredMadeForKids": False},
        }

        media = MediaFileUpload(file_path, mimetype="video/mp4", resumable=True, chunksize=10 * 1024 * 1024)
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        while response is None:
            _, response = req.next_chunk()

        post_id = response["id"]
        charge_units(account_id, YT_UPLOAD_UNITS)

        log.info(
            "youtube_published",
            extra={"variant_id": variant_id[:8], "account_id": account_id, "post_id": post_id},
        )
        return PublishResult(
            platform_post_id=post_id,
            dry_run=False,
            metadata={"title": title, "privacy": privacy_status},
        )
