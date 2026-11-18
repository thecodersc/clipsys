"""FastAPI review UI — single server-rendered HTML page. No build step."""

from __future__ import annotations

import html
import json
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from clipsys.db.db import get_conn

app = FastAPI(title="clipsys Review")

VARIANTS_DIR = Path("data/variants")

# Serve variant videos as static files
if VARIANTS_DIR.exists():
    app.mount("/variants", StaticFiles(directory=str(VARIANTS_DIR)), name="variants")

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>clipsys Review</title>
<style>
  body {{ background:#111; color:#eee; font-family:monospace; padding:2rem; }}
  .card {{ background:#1a1a1a; border:1px solid #333; border-radius:8px;
           padding:1.5rem; margin-bottom:2rem; max-width:700px; }}
  video {{ width:100%; max-height:400px; background:#000; border-radius:4px; }}
  label {{ display:block; margin-top:.8rem; color:#aaa; font-size:.85rem; }}
  input, textarea {{ width:100%; padding:.5rem; background:#222; color:#fff;
                     border:1px solid #444; border-radius:4px; box-sizing:border-box; }}
  textarea {{ height:80px; }}
  .btns {{ margin-top:1rem; display:flex; gap:.5rem; }}
  button {{ padding:.5rem 1.2rem; border:none; border-radius:4px; cursor:pointer; font-size:.9rem; }}
  .approve {{ background:#2d8a2d; color:#fff; }}
  .reject  {{ background:#8a2d2d; color:#fff; }}
  .save    {{ background:#2d5a8a; color:#fff; }}
  .badge {{ display:inline-block; padding:.15rem .5rem; border-radius:3px;
            font-size:.75rem; margin-left:.5rem; }}
  .pending   {{ background:#555; }}
  .rendered  {{ background:#336; }}
  .captioned {{ background:#363; }}
  .approved  {{ background:#2d8a2d; }}
  .rejected  {{ background:#8a2d2d; }}
  .published {{ background:#8a6a2d; }}
  h1 {{ color:#ccff00; letter-spacing:.05em; }}
  .empty {{ color:#666; font-size:1.1rem; }}
</style>
</head>
<body>
<h1>clipsys Review</h1>
{content}
</body>
</html>"""

_CARD_TEMPLATE = """
<div class="card">
  <div>
    <strong>{variant_id_short}</strong>
    <span class="badge {status}">{status}</span>
    &nbsp; account: <code>{account_id}</code>
  </div>
  <video controls src="/variants/{filename}" preload="metadata"></video>
  <form method="post" action="/variant/{variant_id}/save">
    <label>Title</label>
    <input name="title" value="{title}">
    <label>Hook</label>
    <input name="hook" value="{hook}">
    <label>Description</label>
    <textarea name="description">{description}</textarea>
    <label>Hashtags (comma-separated)</label>
    <input name="hashtags" value="{hashtags_str}">
    <div class="btns">
      <button class="save" type="submit">Save</button>
      <button class="approve" formaction="/variant/{variant_id}/approve" type="submit">Approve</button>
      <button class="reject"  formaction="/variant/{variant_id}/reject"  type="submit">Reject</button>
    </div>
  </form>
</div>
"""


def _get_variants(statuses: list[str] | None = None) -> list[dict]:
    statuses = statuses or ["rendered", "captioned", "approved", "rejected"]
    placeholders = ",".join("?" * len(statuses))
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT v.id, v.account_id, v.file_path, v.caption_json, v.status
                FROM variants v
                WHERE v.status IN ({placeholders})
                ORDER BY v.updated_at DESC
                LIMIT 50""",
            statuses,
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    variants = _get_variants()
    if not variants:
        content = '<p class="empty">No variants to review yet. Run <code>clipsys render</code> first.</p>'
    else:
        cards = []
        for v in variants:
            cap = json.loads(v["caption_json"]) if v["caption_json"] else {}
            filename = Path(v["file_path"]).name if v["file_path"] else ""
            card = _CARD_TEMPLATE.format(
                variant_id=v["id"],
                variant_id_short=v["id"][:8],
                status=v["status"],
                account_id=v["account_id"],
                filename=filename,
                # escape: a quote in a title would otherwise cut the form field short
                title=html.escape(cap.get("title", "")),
                hook=html.escape(cap.get("hook", "")),
                description=html.escape(cap.get("description", "")),
                hashtags_str=html.escape(", ".join(cap.get("hashtags", []))),
            )
            cards.append(card)
        content = "".join(cards)
    return HTMLResponse(_HTML_TEMPLATE.format(content=content))


def _save_caption(variant_id: str, title: str, hook: str, description: str, hashtags: str) -> None:
    tags = [h.strip() for h in hashtags.split(",") if h.strip()]
    cap = {"title": title, "hook": hook, "description": description, "hashtags": tags}
    with get_conn() as conn:
        conn.execute(
            """UPDATE variants SET caption_json=?,
               updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
               WHERE id=?""",
            (json.dumps(cap), variant_id),
        )


# Form("") rather than Form(...): an empty field (e.g. no hook) must not be a 422.
@app.post("/variant/{variant_id}/save")
async def save_caption(
    variant_id: str,
    title: str = Form(""),
    hook: str = Form(""),
    description: str = Form(""),
    hashtags: str = Form(""),
) -> RedirectResponse:
    _save_caption(variant_id, title, hook, description, hashtags)
    return RedirectResponse("/", status_code=303)


@app.post("/variant/{variant_id}/approve")
async def approve(
    variant_id: str,
    title: str = Form(""),
    hook: str = Form(""),
    description: str = Form(""),
    hashtags: str = Form(""),
) -> RedirectResponse:
    # Approve submits the same form, so keep any edits made on the page.
    if title:
        _save_caption(variant_id, title, hook, description, hashtags)
    _set_status(variant_id, "approved")
    return RedirectResponse("/", status_code=303)


@app.post("/variant/{variant_id}/reject")
async def reject(variant_id: str) -> RedirectResponse:
    _set_status(variant_id, "rejected")
    return RedirectResponse("/", status_code=303)


def _set_status(variant_id: str, status: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE variants SET status=?, updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
            (status, variant_id),
        )
