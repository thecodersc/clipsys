"""Pydantic v2 models for YAML config files."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

_log = logging.getLogger(__name__)

# ─── programs.yaml ────────────────────────────────────────────────────────────

class Program(BaseModel):
    id: str
    name: str
    credit_line: str
    require_link_to_creator: bool = True
    allowed_platforms: list[str]
    rights_notes: str = ""
    clip_permission: bool = False
    permission_ref: str = ""


class ProgramsConfig(BaseModel):
    programs: list[Program]

    def by_id(self, pid: str) -> Program:
        for p in self.programs:
            if p.id == pid:
                return p
        raise KeyError(pid)


# ─── accounts.yaml ────────────────────────────────────────────────────────────

class RenderProfile(BaseModel):
    # crop: fill the screen, cutting the sides off (subjects can fall out of frame)
    # blur: keep the whole frame, centred over a blurred background
    layout: Literal["crop", "blur"] = "crop"
    crop_offset: Literal["left", "center", "right"] = "center"
    zoom: float = 1.05
    length_band: Literal["short", "long"] = "short"
    persona: str  # relative path under config/


class Account(BaseModel):
    id: str
    platform: Literal["youtube_shorts", "facebook_reels", "tiktok"]
    handle: str
    token_ref: str
    stage: int = Field(ge=0, le=2, default=0)
    daily_cap: int = 2
    posting_windows: list[str] = Field(default_factory=list)
    jitter_minutes: int = 20
    live: bool = False
    api_public: bool = False
    render_profile: RenderProfile


class AccountsConfig(BaseModel):
    accounts: list[Account]

    def by_id(self, aid: str) -> Account:
        for a in self.accounts:
            if a.id == aid:
                return a
        raise KeyError(aid)

    def by_platform(self, platform: str) -> list[Account]:
        return [a for a in self.accounts if a.platform == platform]


# ─── thresholds.yaml ──────────────────────────────────────────────────────────

class ScoreWeights(BaseModel):
    comment_density: float = 0.35
    loudness_peak: float = 0.25
    scene_density: float = 0.15
    keyword_hits: float = 0.25

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> ScoreWeights:
        total = self.comment_density + self.loudness_peak + self.scene_density + self.keyword_hits
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"Score weights must sum to 1.0, got {total}")
        return self


class ScoreConfig(BaseModel):
    weights: ScoreWeights = Field(default_factory=ScoreWeights)
    min_score: float = 1.0
    nms_window_seconds: float = 45.0
    keywords: list[str] = Field(default_factory=list)


class LengthBand(BaseModel):
    min_s: int
    max_s: int


class ClipsConfig(BaseModel):
    short: LengthBand = LengthBand(min_s=15, max_s=59)
    long: LengthBand = LengthBand(min_s=60, max_s=120)
    pre_roll_seconds: float = 2.5


class AllocationConfig(BaseModel):
    max_accounts_per_clip: int = 3
    max_clips_per_day: int = 50


class ReviewConfig(BaseModel):
    auto_publish_pass_rate: float = 0.9
    min_reviews_for_auto: int = 30


class ThresholdsConfig(BaseModel):
    score: ScoreConfig = Field(default_factory=ScoreConfig)
    clips: ClipsConfig = Field(default_factory=ClipsConfig)
    allocation: AllocationConfig = Field(default_factory=AllocationConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)


# ─── Loaders ──────────────────────────────────────────────────────────────────

_CONFIG_DIR = Path("config")


def load_programs(path: Path | None = None) -> ProgramsConfig:
    p = path or (_CONFIG_DIR / "programs.yaml")
    raw = ProgramsConfig(**yaml.safe_load(p.read_text()))
    permitted = []
    for prog in raw.programs:
        if not prog.clip_permission:
            _log.warning(
                "program_dropped_no_permission",
                extra={"program_id": prog.id, "reason": "clip_permission not true"},
            )
            continue
        if not prog.permission_ref:
            _log.warning(
                "program_dropped_no_permission",
                extra={"program_id": prog.id, "reason": "permission_ref is empty"},
            )
            continue
        permitted.append(prog)
    return ProgramsConfig(programs=permitted)


def load_accounts(path: Path | None = None) -> AccountsConfig:
    p = path or (_CONFIG_DIR / "accounts.yaml")
    return AccountsConfig(**yaml.safe_load(p.read_text()))


def load_thresholds(path: Path | None = None) -> ThresholdsConfig:
    p = path or (_CONFIG_DIR / "thresholds.yaml")
    return ThresholdsConfig(**yaml.safe_load(p.read_text()))
