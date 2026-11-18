"""Approve must work from the page's form and keep edits; reject must not 422."""

import json

from fastapi.testclient import TestClient

from clipsys.db.db import get_conn
from clipsys.review_app import app


def _seed():
    with get_conn() as conn:
        conn.execute("INSERT INTO sources (id, file_path, program_id, creator, duration_s, received_at) VALUES ('s','f','p','c',60,'2026-01-01')")
        conn.execute("INSERT INTO clips (id, source_id, file_path, t_start, t_end, duration_s, score) VALUES ('c','s','f',0,15,15,1)")
        conn.execute(
            "INSERT INTO variants (id, clip_id, account_id, status, caption_json) VALUES ('v','c','acct_01','captioned',?)",
            (json.dumps({"title": 'Say "hi"', "hashtags": []}),),
        )


def _variant():
    with get_conn() as conn:
        return conn.execute("SELECT status, caption_json FROM variants WHERE id='v'").fetchone()


def test_approve_saves_edits_and_reject_works():
    _seed()
    client = TestClient(app, follow_redirects=False)
    assert "Say &quot;hi&quot;" in client.get("/").text
    r = client.post("/variant/v/approve", data={"title": "New", "hook": "", "description": "d", "hashtags": "#a, #b"})
    assert r.status_code == 303
    row = _variant()
    assert row["status"] == "approved"
    assert json.loads(row["caption_json"]) == {"title": "New", "hook": "", "description": "d", "hashtags": ["#a", "#b"]}
    assert client.post("/variant/v/reject").status_code == 303
    assert _variant()["status"] == "rejected"
