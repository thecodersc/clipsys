"""Live smoke-test against real Ollama. Skipped if Ollama is unreachable.

Run explicitly:
    pytest tests/test_caption_live.py -v
"""

from __future__ import annotations

import pytest

from clipsys.caption import CaptionSchema, generate_caption


def _ollama_available() -> bool:
    try:
        import httpx

        from clipsys._env import settings
        resp = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=3)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        return settings.ollama_model in models
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _ollama_available(),
    reason="Ollama not reachable or model not pulled",
)

_SAMPLE_TRANSCRIPT = (
    "The player steals a car and races through downtown. "
    "Police give chase with sirens blaring. "
    "The driver drifts around a corner at full speed, narrowly avoiding a crash. "
    "An explosion erupts behind the vehicle. "
    "The escape is clean — crowd goes wild."
)

_PERSONA = "personas/acct_01.md"


class TestLiveCaptions:
    def test_valid_json_output(self):
        cap = generate_caption(
            variant_id="live_test_0001",
            transcript_excerpt=_SAMPLE_TRANSCRIPT,
            persona_path=_PERSONA,
        )
        assert isinstance(cap, CaptionSchema)
        assert len(cap.title) > 0
        assert len(cap.title) <= 100
        assert len(cap.hook) > 0
        assert isinstance(cap.hashtags, list)
        assert len(cap.hashtags) >= 1
        assert all(h.startswith("#") for h in cap.hashtags)

    def test_no_banned_phrases(self):
        from clipsys.caption import _load_persona
        persona = _load_persona(_PERSONA)
        banned = persona["banned_phrases"]
        cap = generate_caption(
            variant_id="live_test_0002",
            transcript_excerpt=_SAMPLE_TRANSCRIPT,
            persona_path=_PERSONA,
        )
        combined = f"{cap.title} {cap.hook} {cap.description}".lower()
        hits = [bp for bp in banned if bp in combined]
        assert hits == [], f"Banned phrases in output: {hits}"

    def test_retries_on_bad_model_output(self, monkeypatch):
        """Force first call to return garbage; verify retry succeeds."""
        call_count = 0
        original = __import__("clipsys.caption", fromlist=["_call_ollama"])._call_ollama

        def _patched(prompt: str, model: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "not valid json at all"
            return original(prompt, model)

        monkeypatch.setattr("clipsys.caption._call_ollama", _patched)
        cap = generate_caption(
            variant_id="live_test_0003",
            transcript_excerpt=_SAMPLE_TRANSCRIPT,
            persona_path=_PERSONA,
        )
        assert call_count >= 2
        assert isinstance(cap, CaptionSchema)
