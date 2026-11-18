"""Unit tests for caption schema validation, banned-phrase rejection, and JSON extraction."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from clipsys.caption import CaptionSchema, _extract_json, _strip_think_tags


class TestCaptionSchema:
    def test_valid(self):
        cap = CaptionSchema(
            title="GTA 6 Wild Chase",
            hook="Watch this insane getaway",
            description="Full chase sequence from GTA 6.",
            hashtags=["#GTA6", "#gaming"],
        )
        assert cap.title == "GTA 6 Wild Chase"

    def test_hashtag_auto_prefix(self):
        cap = CaptionSchema(
            title="T", hook="H", description="D",
            hashtags=["GTA6", "#gaming"],
        )
        assert cap.hashtags == ["#GTA6", "#gaming"]

    def test_title_too_long(self):
        with pytest.raises(ValidationError):
            CaptionSchema(
                title="X" * 101,
                hook="H",
                description="D",
                hashtags=["#GTA6"],
            )

    def test_missing_required_field(self):
        with pytest.raises(ValidationError):
            CaptionSchema(title="T", hook="H", hashtags=["#GTA6"])  # type: ignore[call-arg]

    def test_empty_hashtags_allowed(self):
        cap = CaptionSchema(title="T", hook="H", description="D", hashtags=[])
        assert cap.hashtags == []


class TestStripThinkTags:
    """qwen3 emits <think>...</think> blocks; ensure they are stripped."""

    def test_strips_think_block(self):
        raw = "<think>Some long reasoning here.\nMulti-line.</think>\n{\"title\": \"T\"}"
        result = _strip_think_tags(raw)
        assert "<think>" not in result
        assert '{"title": "T"}' in result

    def test_no_think_block_unchanged(self):
        raw = '{"title": "T", "hook": "H"}'
        assert _strip_think_tags(raw) == raw

    def test_multiple_think_blocks(self):
        raw = "<think>a</think>hello<think>b</think>world"
        result = _strip_think_tags(raw)
        assert result == "helloworld"


class TestExtractJson:
    def _valid_obj(self) -> dict:
        return {"title": "T", "hook": "H", "description": "D", "hashtags": ["#GTA6"]}

    def test_bare_json(self):
        text = json.dumps(self._valid_obj())
        assert _extract_json(text) == self._valid_obj()

    def test_json_with_prose_before(self):
        text = "Sure! Here is the JSON:\n" + json.dumps(self._valid_obj())
        assert _extract_json(text) == self._valid_obj()

    def test_json_code_fence(self):
        text = "```json\n" + json.dumps(self._valid_obj()) + "\n```"
        assert _extract_json(text) == self._valid_obj()

    def test_json_bare_code_fence(self):
        text = "```\n" + json.dumps(self._valid_obj()) + "\n```"
        assert _extract_json(text) == self._valid_obj()

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="No JSON"):
            _extract_json("Here is nothing useful.")

    def test_think_then_json(self):
        think_stripped = _strip_think_tags(
            "<think>Reasoning...</think>\n" + json.dumps(self._valid_obj())
        )
        result = _extract_json(think_stripped)
        assert result == self._valid_obj()


class TestBannedPhrases:
    def _has_banned(self, text: str, banned: list[str]) -> list[str]:
        combined = text.lower()
        return [bp for bp in banned if bp in combined]

    def test_detects_banned(self):
        banned = ["you won't believe", "shocking"]
        hits = self._has_banned("you won't believe this clip", banned)
        assert "you won't believe" in hits

    def test_no_hits(self):
        banned = ["you won't believe", "shocking"]
        hits = self._has_banned("epic getaway sequence", banned)
        assert hits == []

    def test_case_insensitive(self):
        banned = ["shocking"]
        hits = self._has_banned("SHOCKING moment", banned)
        assert "shocking" in hits
