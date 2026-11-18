"""Live path: upload private, flip to public, charge 1,650 units. No network."""

from unittest.mock import MagicMock

from clipsys.publish import youtube as yt_mod
from clipsys.publish.quota import units_used_today


def _fake_yt(monkeypatch, update_fails=False):
    yt = MagicMock()
    yt.videos().insert().next_chunk.return_value = (None, {"id": "vid123"})
    if update_fails:
        yt.videos().update().execute.side_effect = RuntimeError("forbidden")
    monkeypatch.setattr(yt_mod, "_get_credentials", lambda ref: None)
    monkeypatch.setattr(yt_mod, "PUBLIC_RETRY_SECONDS", 0)
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: yt)
    monkeypatch.setattr("googleapiclient.http.MediaFileUpload", MagicMock())
    return yt


def _publish():
    pub = yt_mod.YouTubePublisher(token_ref="tok", dry_run=False)
    return pub.publish("v" * 8, "x.mp4", "t", "d", [], "acct_01", privacy_status="public")


def test_uploads_private_then_public(monkeypatch):
    yt = _fake_yt(monkeypatch)
    result = _publish()
    assert yt.videos().insert.call_args.kwargs["body"]["status"]["privacyStatus"] == "private"
    assert yt.videos().update.call_args.kwargs["body"]["status"]["privacyStatus"] == "public"
    assert result.metadata["privacy"] == "public"
    assert units_used_today("acct_01") == 1650


def test_failed_flip_keeps_upload_and_reports_private(monkeypatch):
    _fake_yt(monkeypatch, update_fails=True)
    result = _publish()
    assert result.platform_post_id == "vid123"
    assert result.metadata["privacy"] == "private"
    assert units_used_today("acct_01") == 1600


def test_hashtags_go_in_description_and_tags_lose_hash(monkeypatch):
    yt = _fake_yt(monkeypatch)
    pub = yt_mod.YouTubePublisher(token_ref="tok", dry_run=False)
    pub.publish("v" * 8, "x.mp4", "t", "desc", ["#GTA6", "#GTAVI"], "acct_01")
    snippet = yt.videos().insert.call_args.kwargs["body"]["snippet"]
    assert snippet["description"] == "desc\n\n#GTA6 #GTAVI"
    assert snippet["tags"] == ["GTA6", "GTAVI"]


def test_flip_is_retried_after_a_transient_403(monkeypatch):
    yt = _fake_yt(monkeypatch)
    yt.videos().update().execute.side_effect = [RuntimeError("forbidden"), {}]
    assert _publish().metadata["privacy"] == "public"
    assert units_used_today("acct_01") == 1650
