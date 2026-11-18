"""Publisher interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class PublishResult:
    platform_post_id: str
    dry_run: bool
    metadata: dict[str, Any]


class Publisher(ABC):
    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run

    @abstractmethod
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
        ...
