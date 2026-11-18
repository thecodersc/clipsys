"""Facebook Reels publisher — dry-run stub until access is confirmed."""

from __future__ import annotations

from typing import Any

from clipsys._log import get_logger
from clipsys.publish.base import Publisher, PublishResult

log = get_logger(__name__)


class FacebookPublisher(Publisher):
    def publish(
        self,
        variant_id: str,
        file_path: str,
        title: str,
        description: str,
        tags: list[str],
        account_id: str,
        **kwargs: Any,
    ) -> PublishResult:
        if not self.dry_run:
            raise NotImplementedError(
                "Facebook live publishing not yet configured. "
                "Confirm Meta Graph API access first."
            )
        log.info(
            "facebook_dry_run",
            extra={
                "variant_id": variant_id[:8],
                "account_id": account_id,
                "file": file_path,
                "title": title[:40],
            },
        )
        return PublishResult(
            platform_post_id=f"fb_dry_{variant_id[:8]}",
            dry_run=True,
            metadata={"title": title},
        )
