#!/usr/bin/env python3
"""
Image Intelligence — web front-end for the reverse image search tool.
Set SERPAPI_KEY as an environment variable, then run:
    uvicorn app:app --reload
"""

import asyncio
import functools
import json
import os
import shutil
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from reverse_image_search import (
    export_csv,
    export_excel,
    export_html,
    search_all_engines,
)
from database import init_db, record_upload, record_selections, get_stats, delete_upload

# ══════════════════════════════════════════════════════════════════════════════
# BRANDING — edit these values to customise the tool's appearance
# ══════════════════════════════════════════════════════════════════════════════

# Text
BRAND_NAME     = '<span class="grad">magpie</span>'
BRAND_SUBTITLE = '<span class="grad">Mag</span>ic <span class="grad">P</span>icture <span class="grad">I</span>ntelligence <span class="grad">E</span>xporter'
HERO_HEADING   = 'Upload or link an image and this little <span class="grad">magpie</span> will go collect similar photos from Google for you.</span><br>'
HERO_SUBTEXT   = 'Use him to find other instances of a picture across the web. Download results per photo, or all results combined. <br> (Take results with a grain of salt; he\'s only a bird.) '

# Logo — leave blank to keep the default search icon,
#         or paste a hosted image URL (e.g. from Imgur, your CDN, etc.)
LOGO_URL = ""

# Hero image — paste a hosted image URL to show a large image beside the hero text.
#              Leave blank to keep the default centered text-only layout.
HERO_IMAGE_URL = "https://i.ibb.co/v6J9vxqV/magpie.png"

# Colours — paste hex codes from coolors.co
COLOR_PRIMARY   = "#276db4"   # buttons, links, badges
COLOR_SECONDARY = "#169566"   # hover states and lighter accents
COLOR_GRADIENT  = "#169566"   # gradient endpoint (pairs with PRIMARY)

# Font — from fonts.google.com: pick a font → Get embed code → paste the two values below
FONT_NAME = "Tirra"
FONT_URL  = "https://fonts.googleapis.com/css2?family=Tirra:wght@400;500;600;700;800;900&display=swap"

# ══════════════════════════════════════════════════════════════════════════════

SERPAPI_KEY   = os.environ.get("SERPAPI_KEY", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
TEMP_DIR = Path(tempfile.gettempdir()) / "ris_cache"
TEMP_DIR.mkdir(exist_ok=True)

try:
    init_db()
except Exception as _db_err:
    print(f"WARNING: DB init failed ({_db_err}) — stats will be unavailable")

_pool = ThreadPoolExecutor(max_workers=4)
app = FastAPI(title="Image Intelligence")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/debug/env")
async def debug_env():
    key = os.environ.get("SERPAPI_KEY", "")
    return {
        "SERPAPI_KEY_set": bool(key),
        "SERPAPI_KEY_length": len(key),
        "all_env_keys": sorted(os.environ.keys()),
    }


@app.get("/api/credits")
async def get_credits():
    if not SERPAPI_KEY:
        raise HTTPException(500, "SERPAPI_KEY not set")
    import requests as _requests
    try:
        resp = _requests.get("https://serpapi.com/account", params={"api_key": SERPAPI_KEY}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return {"total_searches_left": data.get("total_searches_left", 0)}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/debug/raw-search")
async def debug_raw_search(url: str, engine: str = "all"):
    """Return raw SerpAPI responses — use to diagnose empty results.
    Example: /debug/raw-search?url=https://example.com/image.jpg&engine=bing
    """
    from reverse_image_search import _call_engine, _ENGINE_CONFIGS
    if not SERPAPI_KEY:
        return {"error": "SERPAPI_KEY not set"}
    configs = {k: v for k, v in _ENGINE_CONFIGS.items() if engine == "all" or k == engine}
    out = {}
    for key, (base_params, url_param, label) in configs.items():
        params = {**base_params, url_param: url, "api_key": SERPAPI_KEY}
        data = _call_engine(params)
        out[label] = {
            "top_level_keys": list(data.keys()),
            "error": data.get("error"),
            "result_counts": {k: len(v) for k, v in data.items() if isinstance(v, list)},
            "first_result": next((v[0] for v in data.values() if isinstance(v, list) and v), None),
        }
    return out


@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML


@app.get("/uploads/{search_id}/{filename}")
async def serve_upload(search_id: str, filename: str):
    if "/" in search_id or ".." in search_id or "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid path")
    fpath = TEMP_DIR / search_id / filename
    if not fpath.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(fpath)


@app.post("/api/search")
async def search(
    request: Request,
    image_url: str = Form(default=None),
    file: UploadFile = File(default=None),
    engines: str = Form(default="all"),
):
    if not SERPAPI_KEY:
        raise HTTPException(500, "Server not configured — SERPAPI_KEY missing")

    loop = asyncio.get_event_loop()
    search_id = str(uuid.uuid4())
    work_dir = TEMP_DIR / search_id
    work_dir.mkdir()

    try:
        if file and file.filename:
            content = await file.read()
            suffix = Path(file.filename).suffix or ".jpg"
            filename = f"input{suffix}"
            img_path = work_dir / filename
            img_path.write_bytes(content)
            scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
            host   = request.headers.get("host", request.url.netloc)
            search_url = f"{scheme}://{host}/uploads/{search_id}/{filename}"
            source_label = file.filename
        elif image_url and image_url.strip():
            search_url = image_url.strip()
            source_label = image_url.strip()
        else:
            raise HTTPException(400, "Provide an image URL or upload a file")

        only = engines if engines in {"all", "google", "yandex", "bing"} else "all"
        results, engine_errors = await loop.run_in_executor(
            _pool, functools.partial(search_all_engines, search_url, SERPAPI_KEY, only)
        )

        if results:
            prefix = str(work_dir / "results")
            (work_dir / "results.json").write_text(json.dumps({"results": results, "source_label": source_label}))

            def _exports():
                export_csv(results, f"{prefix}.csv")
                export_html(results, f"{prefix}.html", source_label)

            await loop.run_in_executor(_pool, _exports)

        source_type = "file" if (file and file.filename) else "url"
        try:
            record_upload(search_id, source_label, source_type, only, len(results))
        except Exception:
            pass

        return {"search_id": search_id, "count": len(results), "results": results, "engine_errors": engine_errors, "search_url": search_url}

    except HTTPException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(500, str(e))


@app.post("/api/search-more")
async def search_more(
    search_url: str = Form(...),
    start: int = Form(default=0),
):
    if not SERPAPI_KEY:
        raise HTTPException(500, "SERPAPI_KEY missing")
    loop = asyncio.get_event_loop()
    results, engine_errors = await loop.run_in_executor(
        _pool, functools.partial(search_all_engines, search_url, SERPAPI_KEY, "google", start)
    )
    return {"count": len(results), "results": results, "engine_errors": engine_errors}


@app.post("/api/export-selection")
async def export_selection(request: Request):
    payload = await request.json()
    results = payload.get("results", [])
    fmt = payload.get("fmt", "csv")
    if fmt not in {"csv", "xlsx", "html"}:
        raise HTTPException(400, "Invalid format")
    if not results:
        raise HTTPException(400, "No results provided")

    tmp_dir = Path(tempfile.mkdtemp())
    out_path = tmp_dir / f"selected.{fmt}"
    loop = asyncio.get_event_loop()

    if fmt == "csv":
        await loop.run_in_executor(_pool, export_csv, results, str(out_path))
    elif fmt == "xlsx":
        await loop.run_in_executor(_pool, export_excel, results, str(out_path))
    else:
        await loop.run_in_executor(_pool, export_html, results, str(out_path), "Selected results")

    media = {
        "csv":  "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "html": "text/html",
    }
    return FileResponse(
        str(out_path),
        media_type=media[fmt],
        filename=f"selected.{fmt}",
        background=BackgroundTask(shutil.rmtree, str(tmp_dir), True),
    )


@app.get("/api/download/{search_id}/{fmt}")
async def download_file(search_id: str, fmt: str):
    if fmt not in {"csv", "xlsx", "html"}:
        raise HTTPException(400, "Invalid format")
    if "/" in search_id or "\\" in search_id or ".." in search_id:
        raise HTTPException(400, "Invalid ID")

    work_dir = TEMP_DIR / search_id
    fpath = work_dir / f"results.{fmt}"

    if not fpath.exists() and fmt == "xlsx":
        json_path = work_dir / "results.json"
        if not json_path.exists():
            raise HTTPException(404, "Results not found — please run a new search")
        raw = json.loads(json_path.read_text())
        results      = raw.get("results", raw) if isinstance(raw, dict) else raw
        source_label = raw.get("source_label", "") if isinstance(raw, dict) else ""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(_pool, export_excel, results, str(fpath))

    if not fpath.exists():
        raise HTTPException(404, "Results not found — please run a new search")

    media = {
        "csv":  "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "html": "text/html",
    }
    return FileResponse(fpath, media_type=media[fmt], filename=f"results.{fmt}")


@app.post("/api/bulk-search")
async def bulk_search(
    request: Request,
    files: List[UploadFile] = File(default=[]),
    urls: str = Form(default=""),
    engines: str = Form(default="all"),
):
    if not SERPAPI_KEY:
        raise HTTPException(500, "Server not configured — SERPAPI_KEY missing")

    targets = []

    for file in files[:5]:
        if file.filename:
            search_id = str(uuid.uuid4())
            work_dir = TEMP_DIR / search_id
            work_dir.mkdir()
            content = await file.read()
            suffix = Path(file.filename).suffix or ".jpg"
            filename = f"input{suffix}"
            (work_dir / filename).write_bytes(content)
            scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
            host   = request.headers.get("host", request.url.netloc)
            public_url = f"{scheme}://{host}/uploads/{search_id}/{filename}"
            targets.append((search_id, work_dir, public_url, file.filename))

    for url in [u.strip() for u in urls.splitlines() if u.strip()][:max(0, 5 - len(targets))]:
        search_id = str(uuid.uuid4())
        work_dir = TEMP_DIR / search_id
        work_dir.mkdir()
        targets.append((search_id, work_dir, url, url))

    if not targets:
        raise HTTPException(400, "Provide at least one image or URL")

    only = engines if engines in {"all", "google", "yandex", "bing"} else "all"
    loop = asyncio.get_event_loop()

    async def _search_one(search_id, work_dir, search_url, source_label):
        try:
            results, engine_errors = await loop.run_in_executor(
                _pool, functools.partial(search_all_engines, search_url, SERPAPI_KEY, only)
            )
            for r in results:
                r["source_image"] = source_label
            if results:
                prefix = str(work_dir / "results")
                def _exports():
                    export_csv(results, f"{prefix}.csv")
                    export_html(results, f"{prefix}.html", source_label)
                await loop.run_in_executor(_pool, _exports)
            (work_dir / "results.json").write_text(json.dumps(results))
            src_type = "url" if source_label.startswith("http") else "file"
            try:
                record_upload(search_id, source_label, src_type, only, len(results))
            except Exception:
                pass
            return {"search_id": search_id, "source_label": source_label, "count": len(results), "results": results, "engine_errors": engine_errors}
        except Exception as e:
            return {"search_id": search_id, "source_label": source_label, "count": 0, "results": [], "error": str(e)}

    searches = await asyncio.gather(*[_search_one(*t) for t in targets])
    return {"searches": list(searches)}


@app.get("/api/download/combined/{fmt}")
async def download_combined(fmt: str, ids: str):
    if fmt not in {"csv", "xlsx", "html"}:
        raise HTTPException(400, "Invalid format")

    all_results = []
    for sid in ids.split(","):
        sid = sid.strip()
        if not sid or "/" in sid or ".." in sid:
            continue
        json_path = TEMP_DIR / sid / "results.json"
        if json_path.exists():
            raw = json.loads(json_path.read_text())
            # Single-search files are saved as {"results": [...], "source_label": "..."}
            # Bulk-search files are saved as plain lists
            if isinstance(raw, dict):
                all_results.extend(raw.get("results", []))
            else:
                all_results.extend(raw)

    if not all_results:
        raise HTTPException(404, "No results found — please run a new search")

    combined_dir = TEMP_DIR / "combined"
    combined_dir.mkdir(exist_ok=True)
    prefix = str(combined_dir / ids.replace(",", "_")[:60])

    media = {
        "csv":  "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "html": "text/html",
    }
    if fmt == "csv":
        export_csv(all_results, f"{prefix}.csv")
    elif fmt == "xlsx":
        export_excel(all_results, f"{prefix}.xlsx")
    else:
        export_html(all_results, f"{prefix}.html", "Combined bulk search")

    return FileResponse(f"{prefix}.{fmt}", media_type=media[fmt], filename=f"combined_results.{fmt}")


@app.post("/api/record-selections")
async def api_record_selections(request: Request):
    payload = await request.json()
    upload_id = payload.get("upload_id", "")
    results   = payload.get("results", [])
    action    = payload.get("action", "export")
    if upload_id and results:
        try:
            record_selections(upload_id, results, action)
        except Exception:
            pass
    return {"ok": True}


@app.post("/admin/verify-password")
async def admin_verify_password(request: Request):
    payload = await request.json()
    if not ADMIN_PASSWORD or payload.get("password", "") != ADMIN_PASSWORD:
        raise HTTPException(403, "Invalid password")
    return {"ok": True}


@app.post("/admin/delete-upload")
async def admin_delete_upload(request: Request):
    payload = await request.json()
    if not ADMIN_PASSWORD or payload.get("password", "") != ADMIN_PASSWORD:
        raise HTTPException(403, "Invalid password")
    upload_id = payload.get("upload_id", "").strip()
    if not upload_id:
        raise HTTPException(400, "upload_id required")
    try:
        delete_upload(upload_id)
    except Exception as e:
        raise HTTPException(500, f"[RIS-501] Delete failed: {e}")
    return {"ok": True}


@app.get("/stats", response_class=HTMLResponse)
async def stats_page():
    try:
        data = get_stats()
    except Exception as e:
        return HTMLResponse(
            f'<html><body style="font-family:sans-serif;padding:2rem">'
            f'<h1>Stats unavailable</h1><p>Database error: {e}</p>'
            f'<p><a href="/">← back to app</a></p></body></html>',
            status_code=503,
        )

    total_results_found = sum(r["total_results"] or 0 for r in data["rows"])
    total_pct = (
        round(data["total_selected"] / total_results_found * 100, 1)
        if total_results_found else 0
    )

    rows_html = ""
    for r in data["rows"]:
        label    = r["source_label"] or "—"
        short    = label if len(label) <= 55 else label[:52] + "…"
        ca       = r["created_at"]
        ts       = (ca.strftime("%Y-%m-%d %H:%M") + " UTC") if ca else "—"
        found    = r["total_results"] or 0
        selected = r["selected_count"] or 0
        pct      = round(selected / found * 100, 1) if found else 0
        pct_html = f'<span class="pct-bar"><span class="pct-fill" style="width:{min(pct,100)}%"></span></span>{pct}%'
        is_url   = (r.get("source_type") == "url") and label.startswith("http")
        thumb    = (
            f'<img src="{label}" alt="" '
            f'style="width:48px;height:36px;object-fit:cover;border-radius:.375rem;display:block;background:#e5e7eb;" '
            f'onerror="this.replaceWith(Object.assign(document.createElement(\'div\'),{{className:\'file-placeholder\',title:\'{label}\'}}))">'
            if is_url else
            f'<div class="file-placeholder" title="{label}"></div>'
        )
        uid = r["id"]
        rows_html += f"""<tr>
            <td class="thumb-col">{thumb}</td>
            <td title="{label}">{short}</td>
            <td>{r["engines_used"] or "—"}</td>
            <td>{ts}</td>
            <td class="num">{found}</td>
            <td class="num hl">{selected}</td>
            <td class="num pct-cell">{pct_html}</td>
            <td class="del-col"><button class="del-btn" style="display:none" onclick="deleteRow(this,'{uid}')" title="Delete this entry">&#x1F5D1;</button></td>
        </tr>\n"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Magpie — Stats</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #f9fafb; color: #111827; padding: 2rem 1.5rem; }}
  h1 {{ font-size: 1.4rem; font-weight: 700; margin-bottom: 1.5rem; }}
  h1 a {{ color: #6d28d9; text-decoration: none; font-size: .85rem; font-weight: 500;
           margin-left: .75rem; }}
  .cards {{ display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 2rem; }}
  .card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: .75rem;
            padding: 1.25rem 1.75rem; min-width: 180px; box-shadow: 0 1px 3px rgba(0,0,0,.05); }}
  .card .label {{ font-size: .78rem; font-weight: 600; text-transform: uppercase;
                  letter-spacing: .05em; color: #6b7280; margin-bottom: .25rem; }}
  .card .value {{ font-size: 2.2rem; font-weight: 700; color: #6d28d9; line-height: 1; }}
  .card .sub {{ font-size: .75rem; color: #9ca3af; margin-top: .2rem; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
           border: 1px solid #e5e7eb; border-radius: .75rem; overflow: hidden;
           box-shadow: 0 1px 3px rgba(0,0,0,.05); font-size: .875rem; }}
  thead tr {{ background: #f3f4f6; }}
  th {{ text-align: left; padding: .6rem 1rem; font-size: .72rem; font-weight: 600;
        text-transform: uppercase; letter-spacing: .05em; color: #6b7280; }}
  td {{ padding: .7rem 1rem; border-top: 1px solid #f3f4f6; color: #374151;
        max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  tr:hover td {{ background: #fafafa; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .hl  {{ font-weight: 700; color: #6d28d9; }}
  .pct-cell {{ min-width: 110px; }}
  .pct-bar {{ display: inline-block; width: 48px; height: 7px; background: #e5e7eb;
              border-radius: 999px; margin-right: 6px; vertical-align: middle;
              position: relative; overflow: hidden; }}
  .pct-fill {{ position: absolute; left: 0; top: 0; height: 100%;
               background: linear-gradient(90deg, #7c3aed, #a855f7);
               border-radius: 999px; }}
  .thumb-col {{ width: 56px; padding: .4rem .75rem; }}
  .file-placeholder {{ width: 48px; height: 36px; border-radius: .375rem;
                       background: #e5e7eb; display: flex; align-items: center;
                       justify-content: center; font-size: .6rem; color: #9ca3af;
                       font-weight: 600; letter-spacing: .03em; }}
  .file-placeholder::after {{ content: 'FILE'; }}
  .overflow-wrap {{ overflow-x: auto; }}
  .admin-bar {{ display:flex; align-items:center; gap:.75rem; margin-bottom:1.5rem; flex-wrap:wrap; }}
  .admin-toggle {{ font-size:.8rem; padding:.35rem .85rem; border:1px solid #e5e7eb; border-radius:.5rem; background:#fff; cursor:pointer; font-weight:500; color:#374151; }}
  .admin-toggle:hover {{ background:#f3f4f6; }}
  .admin-form {{ display:none; align-items:center; gap:.5rem; }}
  .admin-form input {{ font-size:.8rem; padding:.35rem .6rem; border:1px solid #d1d5db; border-radius:.5rem; outline:none; }}
  .admin-form button {{ font-size:.8rem; padding:.35rem .75rem; background:#6d28d9; color:#fff; border:none; border-radius:.5rem; cursor:pointer; }}
  .admin-status {{ font-size:.75rem; color:#dc2626; font-weight:600; }}
  .del-col {{ width:44px; text-align:center; padding:.4rem .5rem !important; }}
  .del-btn {{ background:none; border:none; cursor:pointer; font-size:.95rem; padding:.25rem .4rem; border-radius:.375rem; color:#d1d5db; transition:color .15s,background .15s; }}
  .del-btn:hover {{ color:#dc2626; background:#fef2f2; }}
</style>
</head>
<body>
<h1>Magpie — Usage Stats <a href="/">← back to app</a></h1>
<div class="admin-bar">
  <button class="admin-toggle" id="admin-toggle" onclick="toggleAdmin()">&#x1F513; Admin mode</button>
  <div class="admin-form" id="admin-form">
    <input type="password" id="admin-pw" placeholder="Password" onkeydown="if(event.key==='Enter')unlockAdmin()">
    <button onclick="unlockAdmin()">Unlock</button>
  </div>
  <span class="admin-status" id="admin-status"></span>
</div>
<div class="cards">
  <div class="card"><div class="label">Images uploaded</div><div class="value">{data["total_uploads"]}</div></div>
  <div class="card"><div class="label">Results selected</div><div class="value">{data["total_selected"]}</div><div class="sub">of {total_results_found} total found</div></div>
  <div class="card"><div class="label">Selection rate</div><div class="value">{total_pct}%</div><div class="sub">results kept on average</div></div>
</div>
<div class="overflow-wrap">
<table>
  <thead><tr>
    <th></th><th>Source image</th><th>Engines</th><th>Uploaded</th>
    <th class="num">Results found</th><th class="num">Selected</th><th class="num">% selected</th><th class="del-col"></th>
  </tr></thead>
  <tbody>{rows_html or '<tr><td colspan="8" style="text-align:center;color:#9ca3af;padding:2rem">No searches yet.</td></tr>'}</tbody>
</table>
</div>
</body>
</html>"""


# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Magpie</title>
  <link rel="icon" type="image/png" href="/static/favicon.png">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --brand:       #7c3aed;
      --brand-light: #8b5cf6;
      --brand-50:    #f5f3ff;
      --brand-100:   #ede9fe;
      --gray-50:     #f9fafb;
      --gray-100:    #f3f4f6;
      --gray-200:    #e5e7eb;
      --gray-300:    #d1d5db;
      --gray-400:    #9ca3af;
      --gray-500:    #6b7280;
      --gray-600:    #4b5563;
      --gray-700:    #374151;
      --gray-800:    #1f2937;
      --gray-900:    #111827;
      --radius:      1rem;
      --shadow:      0 1px 3px rgba(0,0,0,.06), 0 4px 16px rgba(0,0,0,.04);
    }

    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      background: linear-gradient(140deg, #fdfcff 0%, #f5f3ff 50%, #fdf4ff 100%);
      min-height: 100vh;
      color: var(--gray-900);
      line-height: 1.6;
    }

    /* ── Header ── */
    header {
      position: sticky; top: 0; z-index: 100;
      background: rgba(255,255,255,.85);
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      border-bottom: 1px solid rgba(0,0,0,.06);
    }
    .header-inner {
      max-width: 860px; margin: 0 auto;
      padding: .9rem 1.5rem;
      display: flex; align-items: center; gap: .75rem;
    }
    .logo {
      width: 2.25rem; height: 2.25rem; border-radius: .6rem; flex-shrink: 0;
      background: linear-gradient(135deg, #7c3aed, #a855f7);
      display: flex; align-items: center; justify-content: center;
    }
    .logo svg { color: #fff; }
    .brand-name { font-size: .9375rem; font-weight: 600; line-height: 1.2; }
    .brand-sub  { font-size: .75rem; color: var(--gray-400); }
    .brand-sub .grad { background: linear-gradient(135deg, #7c3aed 0%, #a855f7 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }

    /* ── Main ── */
    main { max-width: 860px; margin: 0 auto; padding: 3rem 1.5rem 5rem; }

    /* ── Hero ── */
    .hero { text-align: center; margin-bottom: 2.5rem; }
    .hero.hero--with-image { display: flex; justify-content: center; align-items: center; gap: 1.5rem; }
    .hero-img { display: none; flex: 0 0 auto; }
    .hero--with-image .hero-img { display: block; }
    .hero-img img {
      width: 165px; height: auto; border-radius: 1rem;
      display: block; opacity: 1;
    }
    .hero h2 {
      font-size: 2rem; font-weight: 700; letter-spacing: -.025em;
      color: var(--gray-900); line-height: 1.2; margin-bottom: .75rem;
    }
    .hero h2 .grad {
      background: linear-gradient(135deg, #7c3aed 0%, #a855f7 100%);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
      background-clip: text;
    }
    .hero p { font-size: .9375rem; color: var(--gray-500); max-width: 34rem; margin: 0 auto; }
    @media (max-width: 660px) {
      .hero--with-image { display: flex; flex-direction: column; }
      .hero-img img { width: 100%; }
    }

    /* ── Card ── */
    .card {
      background: linear-gradient(135deg, #7c3aed, #a855f7); border-radius: var(--radius);
      border: 1px solid rgba(255,255,255,.15); box-shadow: var(--shadow);
      padding: 2rem; margin-bottom: 1.5rem;
    }

    /* ── Tabs ── */
    .tabs {
      display: inline-flex; gap: .25rem;
      background: rgba(255,255,255,.15); border-radius: .625rem;
      padding: .25rem; margin-bottom: 1.5rem;
    }
    .tab-btn {
      padding: .375rem .875rem; border-radius: .4375rem;
      font-size: .875rem; font-weight: 500;
      border: none; cursor: pointer;
      color: rgba(255,255,255,.7); background: transparent;
      transition: all .15s;
    }
    .tab-btn.active {
      background: rgba(255,255,255,.25); color: #fff;
      box-shadow: 0 1px 4px rgba(0,0,0,.15);
    }

    /* ── Inputs ── */
    label { display: block; font-size: .875rem; font-weight: 500; color: var(--gray-700); margin-bottom: .5rem; }
    .card label { color: rgba(255,255,255,.9); }

    input[type="url"] {
      width: 100%; padding: .75rem 1rem;
      border: 1.5px solid var(--gray-200); border-radius: .75rem;
      font-size: .9375rem; font-family: inherit;
      color: var(--gray-900); background: #fff; outline: none;
      transition: border-color .15s, box-shadow .15s;
    }
    input[type="url"]:focus {
      border-color: var(--brand-light);
      box-shadow: 0 0 0 3px rgba(139,92,246,.12);
    }

    /* ── Drop zone ── */
    .drop-zone {
      border: 2px dashed rgba(255,255,255,.4); border-radius: .75rem;
      padding: 2.5rem 1.5rem; text-align: center; cursor: pointer;
      transition: border-color .15s, background .15s;
    }
    .drop-zone:hover, .drop-zone.drag-over {
      border-color: rgba(255,255,255,.8); background: rgba(255,255,255,.1);
    }
    .dz-icon { width: 2.5rem; height: 2.5rem; margin: 0 auto .75rem; color: rgba(255,255,255,.6); }
    .drop-zone p { font-size: .9375rem; color: rgba(255,255,255,.9); margin-bottom: .25rem; }
    .drop-zone p strong { color: #fff; }
    .drop-zone .hint { font-size: .8125rem; color: rgba(255,255,255,.6); }

    /* ── File preview ── */
    .file-preview {
      display: none; align-items: center; gap: .75rem;
      margin-top: 1rem; padding: .75rem 1rem;
      background: rgba(255,255,255,.15); border: 1px solid rgba(255,255,255,.25); border-radius: .625rem;
    }
    .file-preview.show { display: flex; }
    .file-preview img { width: 3rem; height: 3rem; object-fit: cover; border-radius: .375rem; }
    .file-name { font-size: .875rem; font-weight: 500; color: #fff; flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .clear-btn {
      width: 1.5rem; height: 1.5rem; border: none; background: none;
      cursor: pointer; color: rgba(255,255,255,.7); border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      padding: 0; transition: background .15s, color .15s;
    }
    .clear-btn:hover { background: rgba(255,255,255,.2); color: #fff; }

    /* ── Search button ── */
    .search-btn {
      width: 100%; padding: .875rem 1.5rem; margin-top: 1.5rem;
      background: #fff;
      color: #7c3aed; font-family: inherit; font-size: .9375rem; font-weight: 600;
      border: none; border-radius: .75rem; cursor: pointer;
      transition: transform .15s, box-shadow .15s, opacity .15s;
      box-shadow: 0 2px 10px rgba(0,0,0,.2);
      display: flex; align-items: center; justify-content: center; gap: .5rem;
    }
    .search-btn:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 4px 18px rgba(0,0,0,.3); }
    .search-btn:active:not(:disabled) { transform: translateY(0); }
    .search-btn:disabled { opacity: .6; cursor: not-allowed; transform: none !important; }

    /* ── Loading ── */
    .loading { display: none; text-align: center; padding: 3rem 1.5rem; }
    .loading.show { display: block; }
    .bird-loader { width: 90px; height: auto; margin: 0 auto 1rem; display: block; }
    .loading p { color: var(--gray-600); font-size: .9375rem; font-weight: 500; }
    .loading .hint { font-size: .8125rem; color: var(--gray-400); margin-top: .5rem; }
    .loading .fact { font-size: .78rem; color: var(--gray-400); margin-top: .75rem; max-width: 420px; margin-left: auto; margin-right: auto; font-style: italic; line-height: 1.5; }

    /* ── Error ── */
    .error-box {
      display: none; background: #fff5f5; border: 1px solid #fecaca;
      border-radius: var(--radius); padding: 1.25rem 1.5rem;
    }
    .error-box.show { display: block; animation: fadeUp .25s ease; }
    .error-box h3 { color: #b91c1c; font-size: .9375rem; font-weight: 600; margin-bottom: .375rem; }
    .error-box p { color: #7f1d1d; font-size: .875rem; }

    /* ── Results ── */
    .results { display: none; }
    .results.show { display: block; animation: fadeUp .3s ease; }
    @keyframes fadeUp {
      from { opacity: 0; transform: translateY(8px); }
      to   { opacity: 1; transform: translateY(0); }
    }

    .results-header {
      display: flex; align-items: center; justify-content: space-between;
      flex-wrap: wrap; gap: .75rem; margin-bottom: 1rem;
    }
    .results-count { font-size: 1rem; font-weight: 600; color: var(--gray-900); }
    .results-count .num {
      font-size: 1.75rem; font-weight: 700; line-height: 1;
      background: linear-gradient(135deg, #7c3aed, #a855f7);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
      background-clip: text; margin-right: .25rem;
    }

    .dl-btns { display: flex; gap: .5rem; flex-wrap: wrap; }
    .dl-btn {
      display: inline-flex; align-items: center; gap: .375rem;
      padding: .4375rem .875rem;
      border: 1.5px solid var(--gray-200); border-radius: .5rem;
      font-size: .8125rem; font-weight: 500; font-family: inherit;
      color: var(--gray-700); background: #fff; cursor: pointer;
      transition: border-color .15s, color .15s, background .15s;
    }
    .dl-btn:hover { border-color: var(--brand-light); color: var(--brand); background: var(--brand-50); }
    .new-search-btn {
      display: inline-flex; align-items: center; gap: .375rem;
      padding: .4375rem .875rem;
      border: 1.5px solid var(--gray-200); border-radius: .5rem;
      font-size: .8125rem; font-weight: 500; font-family: inherit;
      color: var(--gray-500); background: #fff; cursor: pointer;
      transition: border-color .15s, color .15s, background .15s;
    }
    .new-search-btn:hover { border-color: var(--gray-400); color: var(--gray-700); background: var(--gray-50); }
    .search-footer { margin-top: 1rem; display: flex; flex-direction: column; gap: .6rem; }
    .count-picker { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
    .count-picker-label { font-size: .78rem; color: rgba(255,255,255,.65); }
    .count-pill input { display: none; }
    .count-pill span {
      display: inline-block; padding: 4px 11px; border-radius: 999px; cursor: pointer;
      font-size: .78rem; font-weight: 500; border: 1.5px solid rgba(255,255,255,.3);
      color: rgba(255,255,255,.65); background: rgba(255,255,255,.07);
      transition: background .15s, border-color .15s, color .15s; user-select: none;
    }
    .count-pill span em { font-style: normal; opacity: .7; font-size: .72rem; }
    .count-pill input:checked + span { background: rgba(255,255,255,.2); border-color: rgba(255,255,255,.75); color: #fff; }
    .count-pill input:checked + span em { opacity: 1; }
    .credit-note { font-size: .72rem; color: rgba(255,255,255,.5); text-align: center; }
    #results-tbody tr.filtered-out { display: none; }
    .filter-toggle {
      font-size: .78rem; padding: 3px 10px; border-radius: 6px;
      border: 1px solid var(--gray-200); background: #fff; cursor: pointer;
      color: var(--gray-600); display: inline-flex; align-items: center; gap: 5px;
      transition: border-color .15s, background .15s, color .15s;
    }
    .filter-toggle:hover { border-color: var(--gray-300); background: var(--gray-50); }
    .filter-toggle.active { border-color: var(--brand); background: var(--brand-50); color: var(--brand); font-weight: 600; }
    .eng-filter-bar { display: none; align-items: center; gap: 6px; padding: 8px 16px; border-bottom: 1px solid var(--gray-100); background: #fff; flex-wrap: wrap; }
    .eng-filter-bar > span { font-size: .75rem; color: var(--gray-500); font-weight: 600; letter-spacing: .02em; margin-right: 2px; }
    #credit-widget {
      position: fixed; bottom: 16px; left: 16px; z-index: 200;
      display: flex; align-items: center; gap: 6px;
      background: rgba(15,15,20,.72); backdrop-filter: blur(10px);
      color: rgba(255,255,255,.85); font-size: .72rem; font-weight: 500;
      padding: 5px 11px; border-radius: 999px;
      border: 1px solid rgba(255,255,255,.12);
      cursor: pointer; transition: background .15s;
      user-select: none;
    }
    #credit-widget:hover { background: rgba(30,30,40,.85); }
    #credit-widget .cw-num { font-weight: 700; color: #fff; }
    #credit-widget .cw-dot {
      width: 6px; height: 6px; border-radius: 50%;
      background: #4ade80; flex-shrink: 0;
    }
    #credit-widget.cw-low .cw-dot { background: #f97316; }
    #credit-widget.cw-empty .cw-dot { background: #ef4444; }
    .load-more-wrap { text-align: center; padding: 1.25rem 1rem; display: none; }
    .load-more-btn {
      position: relative; display: inline-flex; align-items: center; gap: .4rem;
      padding: .5rem 1.25rem; border-radius: .6rem; cursor: pointer; font-family: inherit;
      font-size: .85rem; font-weight: 500; border: 1.5px solid var(--gray-200);
      background: #fff; color: var(--gray-700); transition: border-color .15s, color .15s, background .15s;
    }
    .load-more-btn:hover:not(:disabled) { border-color: var(--brand-light); color: var(--brand); background: var(--brand-50); }
    .load-more-btn:disabled { opacity: .55; cursor: not-allowed; }
    .load-more-btn .credit-badge { font-size: .7rem; padding: 1px 6px; border-radius: 999px; background: var(--gray-100); color: var(--gray-500); }
    .load-more-btn .tooltip {
      position: absolute; bottom: calc(100% + 8px); left: 50%; transform: translateX(-50%);
      background: #1f2937; color: #fff; font-size: .72rem; padding: 5px 9px; border-radius: 5px;
      white-space: nowrap; pointer-events: none; opacity: 0; transition: opacity .15s; z-index: 10;
    }
    .load-more-btn .tooltip::after {
      content: ''; position: absolute; top: 100%; left: 50%; transform: translateX(-50%);
      border: 5px solid transparent; border-top-color: #1f2937;
    }
    .load-more-btn:hover .tooltip { opacity: 1; }
    .engine-error-bar { display: none; align-items: flex-start; gap: 8px; padding: 8px 16px; background: #fffbeb; border-bottom: 1px solid #fde68a; font-size: .8rem; color: #92400e; line-height: 1.4; }
    .engine-picker { display: flex; align-items: center; gap: 6px; margin-top: 1rem; flex-wrap: wrap; }
    .engine-picker label { font-size: .78rem; color: rgba(255,255,255,.65); margin-right: 4px; }
    .engine-pill input { display: none; }
    .engine-pill span {
      display: inline-block; padding: 4px 12px; border-radius: 999px; cursor: pointer;
      font-size: .78rem; font-weight: 500; border: 1.5px solid rgba(255,255,255,.35);
      color: rgba(255,255,255,.7); background: rgba(255,255,255,.08);
      transition: background .15s, border-color .15s, color .15s;
      user-select: none;
    }
    .engine-pill input:checked + span { background: rgba(255,255,255,.22); border-color: rgba(255,255,255,.8); color: #fff; }

    /* ── Table ── */
    .table-wrap {
      background: #fff; border-radius: var(--radius);
      border: 1px solid rgba(0,0,0,.06); box-shadow: var(--shadow);
      overflow: hidden;
    }
    table { width: 100%; border-collapse: collapse; }
    thead tr { background: var(--gray-50); border-bottom: 1px solid var(--gray-100); }
    th {
      text-align: left; padding: .75rem 1rem;
      font-size: .6875rem; font-weight: 600;
      text-transform: uppercase; letter-spacing: .06em;
      color: var(--gray-400);
    }
    th.col-n     { width: 3rem; }
    th.col-thumb { width: 7.5rem; }
    th.col-src   { width: 9rem; }
    th.col-eng   { width: 7rem; }

    .engine-badge {
      display: inline-block; padding: .2rem .55rem;
      border-radius: 999px; font-size: .7rem; font-weight: 600;
      white-space: nowrap;
    }
    .engine-badge.google { background: #e8f0fe; color: #1a56c4; }
    .engine-badge.yandex { background: #fde8e8; color: #c41a1a; }
    .engine-badge.bing   { background: #e8faf0; color: #1a7a46; }
    .engine-badge.multi  { background: #f3e8ff; color: #7c3aed; }
    td {
      padding: .875rem 1rem; font-size: .875rem; color: var(--gray-700);
      vertical-align: middle; border-bottom: 1px solid var(--gray-50);
    }
    tbody tr:last-child td { border-bottom: none; }
    tbody tr:hover td { background: var(--gray-50); }

    .row-num { font-size: .75rem; font-weight: 500; color: var(--gray-400); }

    .thumb-img {
      width: 5.5rem; height: 3.75rem; object-fit: cover;
      border-radius: .375rem; display: block; background: var(--gray-100);
    }
    .thumb-empty {
      width: 5.5rem; height: 3.75rem; border-radius: .375rem;
      background: var(--gray-100); display: flex; align-items: center; justify-content: center;
    }
    .thumb-empty svg { color: var(--gray-300); }

    .title-link {
      color: var(--gray-800); text-decoration: none; font-weight: 500;
      display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
      overflow: hidden; line-height: 1.45;
    }
    .title-link:hover { color: var(--brand); }

    .source-text {
      font-size: .8125rem; color: var(--gray-500);
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      max-width: 9rem; display: block;
    }

    /* ── Empty results ── */
    .empty {
      padding: 3rem 1.5rem; text-align: center; color: var(--gray-400);
    }
    .empty svg { margin: 0 auto 1rem; display: block; }
    .empty p { font-size: .9375rem; }

    /* ── Bulk file list ── */
    .bulk-file-list { display: flex; flex-direction: column; gap: .5rem; margin-top: 1rem; }
    .bulk-file-item {
      display: flex; align-items: center; gap: .75rem;
      padding: .5rem .75rem;
      background: var(--brand-50); border: 1px solid var(--brand-100); border-radius: .625rem;
    }
    .bulk-file-item img { width: 2.5rem; height: 2.5rem; object-fit: cover; border-radius: .25rem; flex-shrink: 0; }
    .bulk-file-name { flex: 1; font-size: .875rem; font-weight: 500; color: var(--gray-700); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .bulk-remove {
      width: 1.5rem; height: 1.5rem; border: none; background: none; cursor: pointer;
      color: var(--gray-400); border-radius: 50%; display: flex; align-items: center; justify-content: center;
      padding: 0; flex-shrink: 0; transition: background .15s, color .15s;
    }
    .bulk-remove:hover { background: var(--gray-200); color: var(--gray-600); }

    /* ── Bulk results ── */
    .bulk-results { display: none; }
    .bulk-results.show { display: block; animation: fadeUp .3s ease; }
    .bulk-summary-bar {
      display: flex; align-items: center; justify-content: space-between;
      flex-wrap: wrap; gap: .75rem; margin-bottom: 1.25rem;
    }
    .bulk-summary-text { font-size: 1rem; font-weight: 600; color: var(--gray-900); }
    .bulk-summary-text .num {
      font-size: 1.75rem; font-weight: 700; line-height: 1;
      background: linear-gradient(135deg, #7c3aed, #a855f7);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
      background-clip: text; margin-right: .25rem;
    }

    .image-section { margin-bottom: 1rem; border: 1px solid rgba(0,0,0,.06); border-radius: var(--radius); overflow: hidden; background: #fff; box-shadow: var(--shadow); }
    .section-header {
      display: flex; align-items: center; gap: .75rem;
      padding: .875rem 1.25rem; cursor: pointer;
      background: var(--gray-50); user-select: none;
      transition: background .15s;
    }
    .section-header:hover { background: var(--gray-100); }
    .section-toggle { font-size: .75rem; color: var(--gray-400); transition: transform .2s; flex-shrink: 0; }
    .section-toggle.open { transform: rotate(90deg); }
    .section-filename { font-size: .9375rem; font-weight: 600; color: var(--gray-800); flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .section-count { font-size: .8125rem; font-weight: 500; color: var(--gray-500); flex-shrink: 0; }
    .section-body { display: none; }
    .section-body.open { display: block; }
    .section-dl { display: flex; gap: .5rem; flex-wrap: wrap; padding: .875rem 1.25rem; border-top: 1px solid var(--gray-100); }

    /* ── Responsive ── */
    @media (max-width: 600px) {
      main { padding: 2rem 1rem 4rem; }
      .hero h2 { font-size: 1.5rem; }
      th.col-thumb, td:nth-child(2) { display: none; }
    }
    #url-input::placeholder { color: rgba(255,255,255,.45); }
  .col-cb { width: 36px; text-align: center; }
  .result-cb { width: 16px; height: 16px; cursor: pointer; accent-color: var(--brand); }
  .sel-bar { display: flex; align-items: center; gap: 10px; padding: 8px 16px; background: var(--gray-50); border-bottom: 1px solid var(--gray-100); flex-wrap: wrap; position: sticky; top: 4rem; z-index: 50; box-shadow: 0 2px 6px rgba(0,0,0,.06); }
  .sel-bar .sel-count { font-size: .82rem; color: var(--gray-500); margin-right: auto; }
  .sel-bar .sel-btn { font-size: .78rem; padding: 3px 10px; border-radius: 6px; border: 1px solid var(--gray-200); background: #fff; cursor: pointer; color: var(--gray-600); }
  .sel-bar .sel-btn:hover { background: var(--gray-50); border-color: var(--gray-300); }
  .sel-bar .sel-export { font-size: .78rem; padding: 3px 10px; border-radius: 6px; border: 1px solid var(--brand); background: #fff; cursor: pointer; color: var(--brand); font-weight: 500; }
  .sel-bar .sel-export:hover { background: var(--brand); color: #fff; }
  .section-sel-bar { display: flex; align-items: center; gap: 8px; padding: 6px 12px; flex-wrap: wrap; border-top: 1px solid var(--gray-100); background: var(--gray-50); }
  .section-sel-bar .sel-count { font-size: .78rem; color: var(--gray-500); margin-right: auto; }
  .section-sel-bar .sel-btn { font-size: .75rem; padding: 2px 8px; border-radius: 5px; border: 1px solid var(--gray-200); background: #fff; cursor: pointer; color: var(--gray-600); }
  .section-sel-bar .sel-export { font-size: .75rem; padding: 2px 8px; border-radius: 5px; border: 1px solid var(--brand); background: #fff; cursor: pointer; color: var(--brand); font-weight: 500; }
  .section-sel-bar .sel-export:hover { background: var(--brand); color: #fff; }
  #open-tabs-btn {
    position: fixed; bottom: 16px; right: 16px; z-index: 200;
    display: none; align-items: center; gap: 7px;
    background: linear-gradient(135deg, var(--brand) 0%, var(--brand-light) 100%); color: #fff;
    font-size: .78rem; font-weight: 600; font-family: inherit;
    padding: 9px 16px; border-radius: 999px; border: none; cursor: pointer;
    box-shadow: 0 2px 14px rgba(124,58,237,.4);
    transition: filter .15s, transform .15s, box-shadow .15s;
  }
  #open-tabs-btn:hover { filter: brightness(1.12); transform: translateY(-1px); box-shadow: 0 4px 20px rgba(124,58,237,.55); }
  #open-tabs-btn svg { flex-shrink: 0; }

  /* ── Birdhouse ── */
  .bh-header-badge {
    display: none; align-items: center; gap: 5px; margin-left: auto;
    padding: 5px 13px; border-radius: 999px; cursor: pointer; font-family: inherit;
    background: linear-gradient(135deg, #7c3aed, #a855f7); color: #fff;
    font-size: .75rem; font-weight: 600; border: none;
    box-shadow: 0 1px 6px rgba(124,58,237,.3); transition: filter .15s;
  }
  .bh-header-badge.visible { display: flex; }
  .bh-header-badge:hover { filter: brightness(1.1); }
  .save-bh-btn {
    font-size: .78rem; padding: 3px 10px; border-radius: 6px;
    background: linear-gradient(135deg, #7c3aed, #a855f7); color: #fff;
    border: none; cursor: pointer; font-weight: 500; font-family: inherit;
    display: inline-flex; align-items: center; gap: 4px; transition: filter .15s;
  }
  .save-bh-btn:hover { filter: brightness(1.1); }
  .birdhouse-section { margin-top: 1.5rem; animation: fadeUp .3s ease; }
  .birdhouse-hdr {
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: .75rem; margin-bottom: 1rem;
  }
  .birdhouse-title { font-size: 1rem; font-weight: 600; color: var(--gray-900); display: flex; align-items: center; gap: .5rem; }
  .birdhouse-title .num {
    font-size: 1.75rem; font-weight: 700; line-height: 1;
    background: linear-gradient(135deg, #7c3aed, #a855f7);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }
  .batch-card { background: #fff; border: 1px solid var(--gray-100); border-radius: .75rem; margin-bottom: .625rem; overflow: hidden; box-shadow: var(--shadow); }
  .batch-card-hdr {
    display: flex; align-items: center; gap: .75rem; padding: .75rem 1rem;
    background: var(--gray-50); cursor: pointer; user-select: none; transition: background .15s;
  }
  .batch-card-hdr:hover { background: var(--gray-100); }
  .batch-src-img { width: 2.5rem; height: 2.5rem; object-fit: cover; border-radius: .375rem; flex-shrink: 0; background: var(--gray-200); }
  .batch-src-lbl { font-size: .8125rem; font-weight: 500; color: var(--gray-700); flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .batch-meta { font-size: .75rem; color: var(--gray-400); flex-shrink: 0; white-space: nowrap; }
  .batch-toggle { font-size: .7rem; color: var(--gray-400); transition: transform .2s; flex-shrink: 0; }
  .batch-toggle.open { transform: rotate(90deg); }
  .batch-remove-btn {
    width: 1.5rem; height: 1.5rem; border: none; background: none; cursor: pointer;
    color: var(--gray-400); border-radius: 50%; display: flex; align-items: center;
    justify-content: center; padding: 0; flex-shrink: 0; transition: background .15s, color .15s;
  }
  .batch-remove-btn:hover { background: var(--gray-200); color: var(--gray-700); }
  .batch-body { display: none; }
  .batch-body.open { display: block; }
  .batch-result-row { display: flex; align-items: center; gap: .75rem; padding: .5rem 1rem; border-top: 1px solid var(--gray-50); font-size: .8125rem; }
  .batch-result-row:hover { background: var(--gray-50); }
  .batch-result-num { color: var(--gray-400); width: 1.5rem; flex-shrink: 0; text-align: right; font-size: .75rem; }
  .batch-result-title { flex: 1; min-width: 0; }
  .batch-result-title a { color: var(--gray-800); text-decoration: none; font-weight: 500; display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical; overflow: hidden; }
  .batch-result-title a:hover { color: var(--brand); }
  .bh-toast {
    position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%) translateY(8px);
    z-index: 300; background: #111827; color: #fff; font-size: .78rem; font-weight: 500;
    padding: 8px 16px; border-radius: 8px; white-space: nowrap;
    opacity: 0; transition: opacity .2s, transform .2s; pointer-events: none;
  }
  .bh-toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
  </style>
</head>
<body>

<header>
  <div class="header-inner">
    <div class="logo">
      <svg width="15" height="15" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
        <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
      </svg>
    </div>
    <div>
      <div class="brand-name">Image Intelligence</div>
      <div class="brand-sub">ISD · Reverse Image Search</div>
    </div>
    <a class="bh-header-badge" href="/stats" style="text-decoration:none">
      <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z"/>
      </svg>
      Stats
    </a>
    <button class="bh-header-badge" id="bh-header-badge" onclick="document.getElementById(\'birdhouse-section\').scrollIntoView({behavior:\'smooth\'})">
      <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M2.25 12l8.954-8.955c.44-.439 1.152-.439 1.591 0L21.75 12M4.5 9.75v10.125c0 .621.504 1.125 1.125 1.125H9.75v-4.875c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125V21h4.125c.621 0 1.125-.504 1.125-1.125V9.75M8.25 21h8.25"/>
      </svg>
      Birdhouse &middot; <span id="bh-header-count">0</span>
    </button>
  </div>
</header>

<main>

  <!-- Hero -->
  <div class="hero">
    <div class="hero-img"><img src="HERO_IMAGE_PLACEHOLDER" alt=""></div>
    <div class="hero-text">
      <h2>Find where <span class="grad">any image</span><br>appears online</h2>
      <p>Use him to find other instances of a picture across the web. Download results per photo, or all results combined. <br> (Take results with a grain of salt; he\'s only a bird.)
</p>
    </div>
  </div>

  <!-- Search card -->
  <div class="card">
    <div class="tabs">
      <button class="tab-btn active" id="tab-url"  onclick="switchTab('url')">Image URL</button>
      <button class="tab-btn"        id="tab-file" onclick="switchTab('file')">Upload File</button>
      <button class="tab-btn"        id="tab-bulk" onclick="switchTab('bulk')">Bulk Upload</button>
    </div>

    <!-- URL panel -->
    <div id="panel-url">
      <label for="url-input">Paste image URLs — one per line</label>
      <textarea id="url-input" rows="4" placeholder="https://example.com/photo1.jpg&#10;https://example.com/photo2.jpg" autocomplete="off" style="width:100%;resize:vertical;font-family:inherit;font-size:.9rem;padding:.6rem .75rem;border:1.5px solid rgba(255,255,255,.4);border-radius:.6rem;box-sizing:border-box;background:rgba(255,255,255,.15);color:#fff;outline:none;"></textarea>
    </div>

    <!-- File panel -->
    <div id="panel-file" style="display:none">
      <div class="drop-zone" id="drop-zone" onclick="document.getElementById('file-input').click()">
        <svg class="dz-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round"
                d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5"/>
        </svg>
        <p>Drop an image here, or <strong>browse</strong></p>
        <p class="hint">JPG, PNG, GIF, WebP, BMP</p>
      </div>
      <input type="file" id="file-input" accept="image/*" style="display:none">

      <div class="file-preview" id="file-preview">
        <img id="preview-img" src="" alt="">
        <span class="file-name" id="preview-name"></span>
        <button class="clear-btn" onclick="clearFile()" title="Remove">
          <svg width="13" height="13" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
            <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/>
          </svg>
        </button>
      </div>
    </div>

    <!-- Bulk panel -->
    <div id="panel-bulk" style="display:none">
      <label>Drop up to 5 images, or <strong style="color:var(--brand);cursor:pointer" onclick="document.getElementById('bulk-input').click()">browse</strong></label>
      <div class="drop-zone" id="bulk-drop-zone" onclick="document.getElementById('bulk-input').click()" style="margin-top:.5rem">
        <svg class="dz-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5"/>
        </svg>
        <p>Drop images here, or <strong>browse</strong></p>
        <p class="hint">Up to 5 images · JPG, PNG, GIF, WebP</p>
      </div>
      <input type="file" id="bulk-input" accept="image/*" multiple style="display:none">
      <div class="bulk-file-list" id="bulk-file-list"></div>
    </div>

    <div class="search-footer">
      <div class="engine-picker">
        <label>Engines:</label>
        <label class="engine-pill"><input type="radio" name="engine-choice" value="all" checked onchange="updateCreditNote()"><span>All three</span></label>
        <label class="engine-pill"><input type="radio" name="engine-choice" value="google" onchange="updateCreditNote()"><span>Google</span></label>
        <label class="engine-pill"><input type="radio" name="engine-choice" value="yandex" onchange="updateCreditNote()"><span>Yandex</span></label>
        <label class="engine-pill"><input type="radio" name="engine-choice" value="bing" onchange="updateCreditNote()"><span>Bing</span></label>
      </div>
      <div class="count-picker">
        <span class="count-picker-label">Results per search:</span>
        <label class="count-pill"><input type="radio" name="result-pages" value="1" checked onchange="updateCreditNote()"><span>59 · <em id="pill-credits-1">3 credits</em></span></label>
        <label class="count-pill"><input type="radio" name="result-pages" value="2" onchange="updateCreditNote()"><span>~120 · <em id="pill-credits-2">6 credits</em></span></label>
        <label class="count-pill"><input type="radio" name="result-pages" value="3" onchange="updateCreditNote()"><span>~180 · <em id="pill-credits-3">9 credits</em></span></label>
      </div>
      <button class="search-btn" id="search-btn" onclick="doSearch()">
        <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
          <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
        </svg>
        Search
      </button>
      <p class="credit-note" id="credit-note">Uses 3 SerpAPI credits per search (1 per engine)</p>
    </div>
  </div>

  <!-- Loading -->
  <div class="loading" id="loading">
    <img class="bird-loader" src="/static/magpie flying gif.gif" alt="Searching…">
    <p>Searching the web&hellip;</p>
    <div class="fact" id="loading-fact"></div>
  </div>

  <!-- Error -->
  <div class="error-box" id="error-box">
    <h3>Something went wrong</h3>
    <p id="error-msg"></p>
  </div>

  <!-- Results -->
  <div class="results" id="results">
    <div class="results-header">
      <div class="results-count">
        <span class="num" id="result-num">0</span> results found
      </div>
      <div class="dl-btns">
        <button class="new-search-btn" onclick="clearResults()">
          <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
          New search
        </button>
        <button class="dl-btn" onclick="dl('csv')">
          <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/>
          </svg>
          CSV
        </button>
        <button class="dl-btn" onclick="dl('xlsx')">
          <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/>
          </svg>
          Excel
        </button>
        <button class="dl-btn" onclick="dl('html')">
          <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
            <path stroke-linecap="round" stroke-linejoin="round"
                  d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/>
          </svg>
          HTML Report
        </button>
      </div>
    </div>

    <div class="engine-error-bar" id="engine-error-bar"></div>
    <div class="eng-filter-bar" id="eng-filter-bar">
      <span>Filter by engine:</span>
      <button class="filter-toggle active" data-engine="all" onclick="setEngineFilter('all')">All</button>
      <button class="filter-toggle" data-engine="Google Lens" onclick="setEngineFilter('Google Lens')">Google Lens</button>
      <button class="filter-toggle" data-engine="Bing" onclick="setEngineFilter('Bing')">Bing</button>
      <button class="filter-toggle" data-engine="Yandex" onclick="setEngineFilter('Yandex')">Yandex</button>
    </div>
    <div class="sel-bar" id="main-sel-bar">
      <button class="sel-btn" onclick="toggleAllMain(true)">Select all</button>
      <button class="sel-btn" onclick="toggleAllMain(false)">Select none</button>
      <span class="sel-count" id="main-sel-count">0 selected</span>
      <button class="filter-toggle" id="social-filter-btn" onclick="toggleSocialFilter()">
        <svg width="11" height="11" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M12 3c2.755 0 5.455.232 8.083.678.533.09.917.556.917 1.096v1.044a2.25 2.25 0 01-.659 1.591l-5.432 5.432a2.25 2.25 0 00-.659 1.591v2.927a2.25 2.25 0 01-1.244 2.013L9.75 21v-6.568a2.25 2.25 0 00-.659-1.591L3.659 7.409A2.25 2.25 0 013 5.818V4.774c0-.54.384-1.006.917-1.096A48.32 48.32 0 0112 3z"/></svg>
        Social media only
      </button>
      <button class="sel-export" onclick="exportSelected('main','csv')">↓ CSV</button>
      <button class="sel-export" onclick="exportSelected('main','xlsx')">↓ Excel</button>
      <button class="sel-export" onclick="exportSelected('main','html')">↓ HTML</button>
      <button class="save-bh-btn" onclick="saveToBirdhouse('main')">
        <svg width="11" height="11" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M12 4.5v15m7.5-7.5h-15"/></svg>
        Save to Birdhouse
      </button>
    </div>
    <div class="table-wrap">
      <table>
        <colgroup>
          <col class="col-n">
          <col class="col-thumb">
          <col>
          <col class="col-src">
          <col class="col-eng">
        </colgroup>
        <thead>
          <tr>
            <th class="col-cb"><input type="checkbox" id="sel-all-main" class="result-cb" onchange="toggleAllMain(this.checked)" title="Select all"></th>
            <th class="col-n">#</th>
            <th class="col-thumb">Preview</th>
            <th>Title</th>
            <th class="col-src">Source</th>
            <th class="col-eng">Engine</th>
          </tr>
        </thead>
        <tbody id="results-tbody"></tbody>
      </table>
    </div>
    <div class="load-more-wrap" id="load-more-wrap">
      <button class="load-more-btn" id="load-more-btn" onclick="loadMore()">
        <span class="tooltip">Each page costs 1 SerpAPI credit</span>
        Load more results
        <span class="credit-badge">+1 credit</span>
      </button>
    </div>
  </div>

  <!-- Bulk results -->
  <div class="bulk-results" id="bulk-results">
    <div class="bulk-summary-bar">
      <div class="bulk-summary-text">
        <span class="num" id="bulk-image-count">0</span> images &nbsp;·&nbsp;
        <span class="num" id="bulk-total-count">0</span> total results
      </div>
      <div class="dl-btns">
        <button class="new-search-btn" onclick="clearResults()">
          <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
          New search
        </button>
        <button class="dl-btn" onclick="dlAllCombined('csv')">
          <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/></svg>
          All · CSV
        </button>
        <button class="dl-btn" onclick="dlAllCombined('xlsx')">
          <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/></svg>
          All · Excel
        </button>
        <button class="dl-btn" onclick="dlAllCombined('html')">
          <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/></svg>
          All · HTML
        </button>
      </div>
    </div>
    <div id="bulk-sections"></div>
  </div>

  <!-- Birdhouse -->
  <div class="birdhouse-section" id="birdhouse-section" style="display:none">
    <div class="birdhouse-hdr">
      <div class="birdhouse-title">
        <img src="/static/birdhouseicon.png" style="height:2rem;width:auto;flex-shrink:0" alt="">
        <span class="num" id="bh-total">0</span>&nbsp;in the Birdhouse
      </div>
      <div class="dl-btns">
        <button class="dl-btn" onclick="exportBirdhouse(\'csv\')">
          <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/></svg>
          CSV
        </button>
        <button class="dl-btn" onclick="exportBirdhouse(\'xlsx\')">
          <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/></svg>
          Excel
        </button>
        <button class="dl-btn" onclick="exportBirdhouse(\'html\')">
          <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/></svg>
          HTML Report
        </button>
        <button class="new-search-btn" onclick="clearBirdhouse()">
          <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
          Clear
        </button>
      </div>
    </div>
    <div id="bh-batches"></div>
  </div>

</main>

<div id="bh-toast" class="bh-toast"></div>

<button id="open-tabs-btn" onclick="openCheckedTabs()" title="Open all checked results in new tabs">
  <svg width="13" height="13" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
    <path stroke-linecap="round" stroke-linejoin="round" d="M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25"/>
  </svg>
  Open <span id="otb-count">0</span> in tabs
</button>

<div id="credit-widget" onclick="refreshCredits()" title="SerpAPI credits remaining — click to refresh">
  <span class="cw-dot"></span>
  <span class="cw-num" id="credit-count">—</span>
  <span>credits</span>
</div>

<script>
  const _MAGPIE_FACTS = [
    `Magpies are from the family Corvidae, shared by ravens, crows, jackdaws, rooks, and others.`,
    `The "mag" in magpie is believed to be derived from the French name Margot or Margaret, used in the late 14th century for women who chattered idly.`,
    `Magpies are the national bird of Bangladesh.`,
    `In Ireland, it's bad luck to see a single magpie; especially if you pass it without saying "Hello Mr. Magpie"`,
    `It's a common myth that magpies favour shiny objects over dull ones; they will collect anything!`,
    `The magpie is the official bird of Edmonton, Canada.`,
    `Australian magpies aren't corvids like most other magpies around the world.`,
    `The Latin name for the Eurasian magpie is pica pica.`,
    `Magpies in Asia are often seen in bright greens, reds and blues, or yellow.`,
    `Magpies have shown the ability to make and use tools, imitate human speech, grieve, play games, and work in teams.`
  ];

  let searchId = null;
  let activeTab = 'url';
  let chosenFile = null;
  let _singleResults = [];
  let _bulkSections = [];
  let _bulkSourceLabels = [];
  let _birdhouse = (() => {
    try { return JSON.parse(localStorage.getItem('magpie_birdhouse') || '[]'); } catch { return []; }
  })();
  let _searchUrl = null;
  let _searchStart = 0;
  const _PAGE_SIZE = 59;
  let _maxPages = 1;
  let _socialFilter = false;
  let _engineFilter = 'all';
  const _SOCIAL_DOMAINS = [
    'instagram','facebook','twitter','x.com','tiktok','reddit','youtube',
    'linkedin','pinterest','tumblr','snapchat','vk.com','weibo','flickr',
    'imgur','telegram','twitch','whatsapp','discord','threads','bluesky','mastodon'
  ];

  // ── Tab switching ──────────────────────────────────────────────────────────
  function switchTab(tab) {
    activeTab = tab;
    show('panel-url',  tab === 'url');
    show('panel-file', tab === 'file');
    show('panel-bulk', tab === 'bulk');
    document.getElementById('tab-url').classList.toggle('active',  tab === 'url');
    document.getElementById('tab-file').classList.toggle('active', tab === 'file');
    document.getElementById('tab-bulk').classList.toggle('active', tab === 'bulk');
    const btn = document.getElementById('search-btn');
    btn.style.display = tab === 'bulk' ? 'none' : '';
  }

  // ── Drag and drop ──────────────────────────────────────────────────────────
  const dz   = document.getElementById('drop-zone');
  const fi   = document.getElementById('file-input');

  dz.addEventListener('dragover',  e => { e.preventDefault(); dz.classList.add('drag-over'); });
  dz.addEventListener('dragleave', ()  => dz.classList.remove('drag-over'));
  dz.addEventListener('drop', e => {
    e.preventDefault(); dz.classList.remove('drag-over');
    const f = e.dataTransfer.files[0];
    if (f && f.type.startsWith('image/')) setFile(f);
  });
  fi.addEventListener('change', () => { if (fi.files[0]) setFile(fi.files[0]); });

  function setFile(f) {
    chosenFile = f;
    document.getElementById('preview-name').textContent = f.name;
    const reader = new FileReader();
    reader.onload = e => { document.getElementById('preview-img').src = e.target.result; };
    reader.readAsDataURL(f);
    document.getElementById('file-preview').classList.add('show');
  }

  function clearFile() {
    chosenFile = null; fi.value = '';
    document.getElementById('file-preview').classList.remove('show');
    document.getElementById('preview-img').src = '';
  }

  // ── Bulk upload ────────────────────────────────────────────────────────────
  let bulkFiles = [];
  let bulkSearchIds = [];

  const bulkDz = document.getElementById('bulk-drop-zone');
  const bulkFi = document.getElementById('bulk-input');

  bulkDz.addEventListener('dragover',  e => { e.preventDefault(); bulkDz.classList.add('drag-over'); });
  bulkDz.addEventListener('dragleave', ()  => bulkDz.classList.remove('drag-over'));
  bulkDz.addEventListener('drop', e => {
    e.preventDefault(); bulkDz.classList.remove('drag-over');
    Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('image/')).forEach(addBulkFile);
  });
  bulkFi.addEventListener('change', () => { Array.from(bulkFi.files).forEach(addBulkFile); bulkFi.value = ''; });

  function addBulkFile(f) {
    if (bulkFiles.length >= 5) { alert('Maximum 5 images.'); return; }
    if (bulkFiles.find(x => x.name === f.name && x.size === f.size)) return;
    bulkFiles.push(f);
    renderBulkFileList();
  }

  function removeBulkFile(idx) {
    bulkFiles.splice(idx, 1);
    renderBulkFileList();
  }

  function renderBulkFileList() {
    const list = document.getElementById('bulk-file-list');
    list.innerHTML = '';
    bulkFiles.forEach((f, i) => {
      const item = document.createElement('div');
      item.className = 'bulk-file-item';
      const reader = new FileReader();
      reader.onload = e => { item.querySelector('img').src = e.target.result; };
      reader.readAsDataURL(f);
      item.innerHTML = `<img src="" alt=""><span class="bulk-file-name">${h(f.name)}</span>
        <button class="bulk-remove" onclick="removeBulkFile(${i})" title="Remove">
          <svg width="13" height="13" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
        </button>`;
      list.appendChild(item);
    });
    if (bulkFiles.length > 0 && !document.getElementById('bulk-search-btn')) {
      const btn = document.createElement('button');
      btn.id = 'bulk-search-btn';
      btn.className = 'search-btn';
      btn.style.marginTop = '1.5rem';
      btn.innerHTML = `<svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg> Search ${bulkFiles.length} Image${bulkFiles.length > 1 ? 's' : ''}`;
      btn.onclick = doBulkSearch;
      list.after(btn);
    } else if (document.getElementById('bulk-search-btn')) {
      const btn = document.getElementById('bulk-search-btn');
      if (bulkFiles.length === 0) { btn.remove(); }
      else btn.innerHTML = `<svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg> Search ${bulkFiles.length} Image${bulkFiles.length > 1 ? 's' : ''}`;
    }
  }

  async function doBulkSearch() {
    if (!bulkFiles.length) { alert('Please add at least one image.'); return; }
    setLoading(true);
    cls('bulk-results'); cls('results'); cls('error-box');
    try {
      const fd = new FormData();
      bulkFiles.forEach(f => fd.append('files', f));
      fd.append('engines', getEngineChoice());
      const resp = await fetch('/api/bulk-search', { method: 'POST', body: fd });
      const data = await resp.json();
      if (!resp.ok) { showError(data.detail || 'An unexpected error occurred.'); return; }
      renderBulkResults(data.searches);
    } catch {
      showError('Network error — please check your connection and try again.');
    } finally {
      setLoading(false);
      refreshCredits();
    }
  }

  function renderBulkResults(searches) {
    bulkSearchIds = searches.map(s => s.search_id);
    const totalResults = searches.reduce((sum, s) => sum + s.count, 0);
    document.getElementById('bulk-image-count').textContent = searches.length;
    document.getElementById('bulk-total-count').textContent = totalResults;

    const container = document.getElementById('bulk-sections');
    container.innerHTML = '';
    searches.forEach((s, idx) => {
      const section = document.createElement('div');
      section.className = 'image-section';
      const label = s.source_label || `Image ${idx + 1}`;
      const shortLabel = label.length > 60 ? label.slice(0, 57) + '…' : label;

      const sIdx = idx;
      _bulkSections[sIdx] = s.results || [];
      _bulkSourceLabels[sIdx] = s.source_label || `Image ${sIdx + 1}`;

      section.innerHTML = `
        <div class="section-header" onclick="toggleSection(this)">
          <span class="section-toggle${idx === 0 ? ' open' : ''}">▶</span>
          <span class="section-filename" title="${h(label)}">${h(shortLabel)}</span>
          <span class="section-count">${s.count} result${s.count !== 1 ? 's' : ''}</span>
        </div>
        <div class="section-body${idx === 0 ? ' open' : ''}">
          <div class="section-dl">
            <button class="dl-btn" onclick="dlBulk('${s.search_id}','csv')">↓ CSV</button>
            <button class="dl-btn" onclick="dlBulk('${s.search_id}','xlsx')">↓ Excel</button>
            <button class="dl-btn" onclick="dlBulk('${s.search_id}','html')">↓ HTML</button>
          </div>
          <div class="section-sel-bar">
            <button class="sel-btn" onclick="toggleAllBulk(${sIdx},true)">Select all</button>
            <button class="sel-btn" onclick="toggleAllBulk(${sIdx},false)">Select none</button>
            <span class="sel-count" id="bulk-sel-count-${sIdx}"></span>
            <button class="sel-export" onclick="exportSelected('bulk-${sIdx}','csv')">↓ CSV</button>
            <button class="sel-export" onclick="exportSelected('bulk-${sIdx}','xlsx')">↓ Excel</button>
            <button class="sel-export" onclick="exportSelected('bulk-${sIdx}','html')">↓ HTML</button>
            <button class="save-bh-btn" onclick="saveToBirdhouse('bulk-${sIdx}')">
              <svg width="11" height="11" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M12 4.5v15m7.5-7.5h-15"/></svg>
              Save to Birdhouse
            </button>
          </div>
          <div class="table-wrap" style="border-radius:0;border:none;border-top:1px solid var(--gray-100)">
            <table>
              <thead><tr>
                <th class="col-cb"><input type="checkbox" class="result-cb" onchange="toggleAllBulk(${sIdx},this.checked)" title="Select all"></th>
                <th class="col-n">#</th><th class="col-thumb">Preview</th><th>Title</th><th class="col-src">Source</th><th class="col-eng">Engine</th>
              </tr></thead>
              <tbody id="bulk-tbody-${sIdx}"></tbody>
            </table>
          </div>
        </div>`;
      container.appendChild(section);

      const tbody = document.getElementById(`bulk-tbody-${sIdx}`);
      if (s.error) {
        tbody.innerHTML = `<tr><td colspan="6"><div class="empty" style="padding:2rem"><p style="color:#dc2626">[RIS-401] Search failed: ${h(s.error)}</p></div></td></tr>`;
      } else if (!s.results || !s.results.length) {
        tbody.innerHTML = `<tr><td colspan="6"><div class="empty" style="padding:2rem"><p>No results found.</p></div></td></tr>`;
      } else {
        s.results.forEach((r, i) => {
          const thumbCell = r.thumbnail
            ? `<img class="thumb-img" src="${h(r.thumbnail)}" alt="" loading="lazy" onerror="this.style.display='none'">`
            : `<div class="thumb-empty"></div>`;
          const titleCell = r.url
            ? `<a class="title-link" href="${h(r.url)}" target="_blank" rel="noopener">${h(r.title || r.url)}</a>`
            : `<span style="color:var(--gray-400)">${h(r.title || '—')}</span>`;
          const engineClass = r.engine && r.engine.includes('·') ? 'multi' : r.engine === 'Yandex' ? 'yandex' : r.engine === 'Bing' ? 'bing' : 'google';
          const badge = r.engine ? `<span class="engine-badge ${engineClass}">${h(r.engine)}</span>` : '—';
          const row = document.createElement('tr');
          row.dataset.idx = i;
          row.innerHTML = `
            <td class="col-cb"><input type="checkbox" class="result-cb" onchange="updateBulkCount(${sIdx})"></td>
            <td><span class="row-num">${i + 1}</span></td>
            <td>${thumbCell}</td>
            <td>${titleCell}</td>
            <td><span class="source-text">${h(r.source || '—')}</span></td>
            <td>${badge}</td>`;
          tbody.appendChild(row);
        });
      }
      updateBulkCount(sIdx);
    });

    document.getElementById('bulk-results').classList.add('show');
  }

  function toggleSection(header) {
    const toggle = header.querySelector('.section-toggle');
    const body = header.nextElementSibling;
    const open = body.classList.toggle('open');
    toggle.classList.toggle('open', open);
  }

  function dlBulk(searchId, fmt) {
    window.location.href = `/api/download/${searchId}/${fmt}`;
  }

  function dlAllCombined(fmt) {
    if (!bulkSearchIds.length) return;
    window.location.href = `/api/download/combined/${fmt}?ids=${bulkSearchIds.join(',')}`;
  }

  // ── Search ─────────────────────────────────────────────────────────────────
  function getEngineChoice() {
    const el = document.querySelector('input[name="engine-choice"]:checked');
    return el ? el.value : 'all';
  }

  async function doSearch() {
    if (activeTab === 'url' && !document.getElementById('url-input').value.trim()) {
      alert('Please enter an image URL.'); return;
    }
    if (activeTab === 'file' && !chosenFile) {
      alert('Please select an image file.'); return;
    }

    setLoading(true);
    cls('results'); cls('error-box');

    try {
      const fd = new FormData();
      fd.append('engines', getEngineChoice());
      let resp, data;
      if (activeTab === 'url') {
        const urls = document.getElementById('url-input').value.trim().split(/\\r?\\n/).map(u => u.trim()).filter(Boolean);
        if (urls.length === 1) {
          fd.append('image_url', urls[0]);
          resp = await fetch('/api/search', { method: 'POST', body: fd });
          data = await resp.json();
          if (!resp.ok) { showError(data.detail || 'An unexpected error occurred.'); return; }
          searchId = data.search_id; _searchUrl = data.search_url; _searchStart = 0;
          renderResults(data.results, data.count, data.engine_errors || {});
          if (data.count >= _PAGE_SIZE && getMaxPages() > 1) await autoFetchPages(_searchUrl, getMaxPages());
        } else {
          fd.append('urls', urls.join('\\n'));
          resp = await fetch('/api/bulk-search', { method: 'POST', body: fd });
          data = await resp.json();
          if (!resp.ok) { showError(data.detail || 'An unexpected error occurred.'); return; }
          renderBulkResults(data.searches || data.results);
        }
      } else {
        fd.append('file', chosenFile);
        resp = await fetch('/api/search', { method: 'POST', body: fd });
        data = await resp.json();
        if (!resp.ok) { showError(data.detail || 'An unexpected error occurred.'); return; }
        searchId = data.search_id; _searchUrl = data.search_url; _searchStart = 0;
        renderResults(data.results, data.count, data.engine_errors || {});
        if (data.count >= _PAGE_SIZE && getMaxPages() > 1) await autoFetchPages(_searchUrl, getMaxPages());
      }
    } catch {
      showError('Network error — please check your connection and try again.');
    } finally {
      setLoading(false);
      refreshCredits();
    }
  }

  // ── Render results ─────────────────────────────────────────────────────────
  function renderEngineErrors(errors, containerId) {
    const el = document.getElementById(containerId);
    if (!el) return;
    const failed = Object.keys(errors || {});
    if (!failed.length) { el.style.display = 'none'; el.innerHTML = ''; return; }
    el.style.display = '';
    el.innerHTML = `<svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" style="flex-shrink:0;color:#d97706"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126z"/><path stroke-linecap="round" stroke-linejoin="round" d="M12 15.75h.007v.008H12v-.008z"/></svg>
    <span>Could not reach: <strong>${failed.join(', ')}</strong>. Results shown are from the engines that responded. This is usually a SerpAPI plan or credit issue.</span>`;
  }

  function makeResultRow(r, idx) {
    const thumbCell = r.thumbnail
      ? `<img class="thumb-img" src="${h(r.thumbnail)}" alt="" loading="lazy"
             onerror="this.outerHTML='<div class=thumb-empty><svg width=20 height=20 fill=none viewBox=\\'0 0 24 24\\' stroke=currentColor stroke-width=1.5><path stroke-linecap=round stroke-linejoin=round d=\\'M2.25 15.75l5.159-5.159a2.25 2.25 0 013.182 0l5.159 5.159m-1.5-1.5l1.409-1.409a2.25 2.25 0 013.182 0l2.909 2.909M21 18.75H3.75A1.5 1.5 0 012.25 17.25V6.75A1.5 1.5 0 013.75 5.25h16.5A1.5 1.5 0 0121.75 6.75v10.5A1.5 1.5 0 0120.25 18.75z\\'/></svg></div>'">`
      : `<div class="thumb-empty"><svg width="20" height="20" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M2.25 15.75l5.159-5.159a2.25 2.25 0 013.182 0l5.159 5.159m-1.5-1.5l1.409-1.409a2.25 2.25 0 013.182 0l2.909 2.909M21 18.75H3.75A1.5 1.5 0 012.25 17.25V6.75A1.5 1.5 0 013.75 5.25h16.5A1.5 1.5 0 0121.75 6.75v10.5A1.5 1.5 0 0120.25 18.75z"/></svg></div>`;
    const titleCell = r.url
      ? `<a class="title-link" href="${h(r.url)}" target="_blank" rel="noopener">${h(r.title || r.url)}</a>`
      : `<span style="color:var(--gray-400)">${h(r.title || '—')}</span>`;
    const engineClass = r.engine && r.engine.includes('·') ? 'multi' : r.engine === 'Yandex' ? 'yandex' : r.engine === 'Bing' ? 'bing' : 'google';
    const engineBadge = r.engine ? `<span class="engine-badge ${engineClass}">${h(r.engine)}</span>` : '—';
    const row = document.createElement('tr');
    row.dataset.idx = idx;
    row.innerHTML = `
      <td class="col-cb"><input type="checkbox" class="result-cb" onchange="updateMainCount()"></td>
      <td><span class="row-num">${idx + 1}</span></td>
      <td>${thumbCell}</td>
      <td>${titleCell}</td>
      <td><span class="source-text" title="${h(r.source)}">${h(r.source || '—')}</span></td>
      <td>${engineBadge}</td>`;
    return row;
  }

  function checkLoadMore(pageCount) {
    const wrap = document.getElementById('load-more-wrap');
    if (wrap) wrap.style.display = pageCount >= _PAGE_SIZE ? 'block' : 'none';
  }

  function renderResults(results, count, engineErrors) {
    _singleResults = results;
    document.getElementById('result-num').textContent = count;
    renderEngineErrors(engineErrors, 'engine-error-bar');
    const tbody = document.getElementById('results-tbody');
    tbody.innerHTML = '';

    if (!results.length) {
      tbody.innerHTML = `<tr><td colspan="6"><div class="empty">
        <svg width="40" height="40" fill="none" viewBox="0 0 24 24" stroke="#d1d5db" stroke-width="1.5">
          <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
        </svg>
        <p>No results found for this image.</p>
      </div></td></tr>`;
    } else {
      results.forEach((r, i) => tbody.appendChild(makeResultRow(r, i)));
    }

    applyFilter();
    checkLoadMore(count);
    document.getElementById('results').classList.add('show');
    document.getElementById('eng-filter-bar').style.display = 'flex';
  }

  async function autoFetchPages(searchUrl, totalPages) {
    for (let page = 1; page < totalPages; page++) {
      try {
        const fd = new FormData();
        fd.append('search_url', searchUrl);
        fd.append('start', page * _PAGE_SIZE);
        const resp = await fetch('/api/search-more', {method: 'POST', body: fd});
        const data = await resp.json();
        if (!resp.ok || !data.results.length) break;
        _searchStart = page * _PAGE_SIZE;
        const seen = new Set(_singleResults.map(r => r.url).filter(Boolean));
        const fresh = data.results.filter(r => !r.url || !seen.has(r.url));
        if (!fresh.length) break;
        const startIdx = _singleResults.length;
        _singleResults = _singleResults.concat(fresh);
        document.getElementById('result-num').textContent = _singleResults.length;
        const tbody = document.getElementById('results-tbody');
        fresh.forEach((r, i) => tbody.appendChild(makeResultRow(r, startIdx + i)));
        applyFilter();
        checkLoadMore(data.count);
      } catch (e) {
        showError('Could not load additional pages — ' + (e.message || 'network error') + '. Partial results shown above.');
        break;
      }
    }
    refreshCredits();
  }

  async function loadMore() {
    if (!_searchUrl) return;
    const btn = document.getElementById('load-more-btn');
    btn.disabled = true;
    btn.querySelector('.credit-badge').textContent = 'Loading…';
    try {
      const fd = new FormData();
      fd.append('search_url', _searchUrl);
      fd.append('start', _searchStart + _PAGE_SIZE);
      const resp = await fetch('/api/search-more', {method: 'POST', body: fd});
      const data = await resp.json();
      if (!resp.ok) { showError(data.detail || 'Failed to load more results.'); return; }
      _searchStart += _PAGE_SIZE;
      const seen = new Set(_singleResults.map(r => r.url).filter(Boolean));
      const fresh = data.results.filter(r => !r.url || !seen.has(r.url));
      const startIdx = _singleResults.length;
      _singleResults = _singleResults.concat(fresh);
      document.getElementById('result-num').textContent = _singleResults.length;
      const tbody = document.getElementById('results-tbody');
      fresh.forEach((r, i) => tbody.appendChild(makeResultRow(r, startIdx + i)));
      applyFilter();
      checkLoadMore(data.count);
    } catch {
      showError('Network error — could not load more results.');
    } finally {
      btn.disabled = false;
      btn.querySelector('.credit-badge').textContent = '+1 credit';
      refreshCredits();
    }
  }

  // ── Selection helpers ──────────────────────────────────────────────────────
  function toggleAllMain(checked) {
    document.querySelectorAll('#results-tbody tr[data-idx]:not(.filtered-out) .result-cb').forEach(cb => cb.checked = checked);
    const hdr = document.getElementById('sel-all-main');
    if (hdr) hdr.checked = checked;
    updateMainCount();
  }

  function updateMainCount() {
    const all = [...document.querySelectorAll('#results-tbody tr[data-idx]:not(.filtered-out) .result-cb')];
    const on  = all.filter(cb => cb.checked);
    document.getElementById('main-sel-count').textContent = `${on.length} of ${all.length} selected`;
    const hdr = document.getElementById('sel-all-main');
    if (hdr) hdr.indeterminate = on.length > 0 && on.length < all.length;
    updateOpenTabsBtn();
  }

  function toggleAllBulk(sIdx, checked) {
    document.querySelectorAll(`#bulk-tbody-${sIdx} .result-cb`).forEach(cb => cb.checked = checked);
    updateBulkCount(sIdx);
  }

  function updateBulkCount(sIdx) {
    const all = document.querySelectorAll(`#bulk-tbody-${sIdx} .result-cb`);
    const on  = document.querySelectorAll(`#bulk-tbody-${sIdx} .result-cb:checked`);
    const el  = document.getElementById(`bulk-sel-count-${sIdx}`);
    if (el) el.textContent = `${on.length} of ${all.length} selected`;
    updateOpenTabsBtn();
  }

  async function exportSelected(scope, fmt) {
    let results = [];
    let uploadId = null;
    if (scope === 'main') {
      uploadId = searchId;
      document.querySelectorAll('#results-tbody tr[data-idx]:not(.filtered-out)').forEach(row => {
        if (row.querySelector('.result-cb')?.checked)
          results.push(_singleResults[parseInt(row.dataset.idx)]);
      });
    } else {
      const sIdx = parseInt(scope.replace('bulk-', ''));
      uploadId = bulkSearchIds[sIdx] || null;
      document.querySelectorAll(`#bulk-tbody-${sIdx} tr[data-idx]`).forEach(row => {
        if (row.querySelector('.result-cb')?.checked)
          results.push(_bulkSections[sIdx][parseInt(row.dataset.idx)]);
      });
    }
    if (!results.length) { alert('Please tick at least one result first.'); return; }
    if (uploadId) fetch('/api/record-selections', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({upload_id: uploadId, results, action: 'export'})
    }).catch(() => {});
    try {
      const resp = await fetch('/api/export-selection', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({results, fmt})
      });
      if (!resp.ok) { const d = await resp.json(); showError(d.detail || 'Export failed.'); return; }
      const blob = await resp.blob();
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement('a');
      a.href = url; a.download = `selected.${fmt}`;
      document.body.appendChild(a); a.click();
      document.body.removeChild(a); URL.revokeObjectURL(url);
    } catch { showError('Export failed — please try again.'); }
  }

  // ── Open in tabs ────────────────────────────────────────────────────────────
  function updateOpenTabsBtn() {
    let count = 0;
    document.querySelectorAll('#results-tbody tr[data-idx]:not(.filtered-out) .result-cb:checked').forEach(() => count++);
    document.querySelectorAll('[id^="bulk-tbody-"] tr[data-idx] .result-cb:checked').forEach(() => count++);
    const btn = document.getElementById('open-tabs-btn');
    if (!btn) return;
    document.getElementById('otb-count').textContent = count;
    btn.style.display = count > 0 ? 'flex' : 'none';
  }

  function openCheckedTabs() {
    const urls = [];
    document.querySelectorAll('#results-tbody tr[data-idx]:not(.filtered-out)').forEach(row => {
      if (row.querySelector('.result-cb')?.checked) {
        const r = _singleResults[parseInt(row.dataset.idx)];
        if (r?.url) urls.push(r.url);
      }
    });
    document.querySelectorAll('[id^="bulk-tbody-"]').forEach(tbody => {
      const sIdx = parseInt(tbody.id.replace('bulk-tbody-', ''));
      tbody.querySelectorAll('tr[data-idx]').forEach(row => {
        if (row.querySelector('.result-cb')?.checked) {
          const r = _bulkSections[sIdx]?.[parseInt(row.dataset.idx)];
          if (r?.url) urls.push(r.url);
        }
      });
    });
    urls.forEach(url => window.open(url, '_blank', 'noopener'));
  }

  // ── Downloads ──────────────────────────────────────────────────────────────
  function dl(fmt) {
    if (searchId) window.location.href = `/api/download/${searchId}/${fmt}`;
  }

  // ── Helpers ────────────────────────────────────────────────────────────────
  function setLoading(on) {
    const isBulk = activeTab === 'bulk';
    document.getElementById('loading').classList.toggle('show', on);
    if (on) {
      document.getElementById('loading').scrollIntoView({behavior: 'smooth', block: 'center'});
      document.getElementById('loading-fact').textContent = _MAGPIE_FACTS[Math.floor(Math.random() * _MAGPIE_FACTS.length)];
    }
    document.querySelector('.loading p').textContent = isBulk ? `Searching ${bulkFiles.length} images across 3 engines…` : 'Searching the web…';
    const btn = document.getElementById(isBulk ? 'bulk-search-btn' : 'search-btn');
    if (!btn) return;
    btn.disabled = on;
    btn.innerHTML = on
      ? `<span style="display:inline-block;width:15px;height:15px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite"></span>&nbsp;Searching…`
      : `<svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg> ${isBulk ? `Search ${bulkFiles.length} Image${bulkFiles.length > 1 ? 's' : ''}` : 'Search'}`;
  }

  function showError(msg) {
    document.getElementById('error-msg').textContent = msg;
    document.getElementById('error-box').classList.add('show');
  }

  function show(id, on) { document.getElementById(id).style.display = on ? '' : 'none'; }
  function cls(id)      { document.getElementById(id).classList.remove('show'); }

  function h(s) {
    if (!s) return '';
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  // ── Result count / credit note ─────────────────────────────────────────────
  function getMaxPages() {
    const el = document.querySelector('input[name="result-pages"]:checked');
    return el ? parseInt(el.value) : 1;
  }

  function updateCreditNote() {
    _maxPages = getMaxPages();
    const engine = getEngineChoice();
    const engineCount = engine === 'all' ? 3 : 1;
    const credits = _maxPages * engineCount;
    const el = document.getElementById('credit-note');
    if (el) el.textContent = `Uses ${credits} SerpAPI credit${credits !== 1 ? 's' : ''} per search`;
    [1, 2, 3].forEach(pages => {
      const pill = document.getElementById(`pill-credits-${pages}`);
      if (!pill) return;
      const n = pages * engineCount;
      pill.textContent = `${n} credit${n !== 1 ? 's' : ''}`;
    });
  }
  updateCreditNote();

  // ── Social media filter ────────────────────────────────────────────────────
  function isSocial(r) {
    const text = ((r.source || '') + ' ' + (r.url || '')).toLowerCase();
    return _SOCIAL_DOMAINS.some(d => text.includes(d));
  }

  function applyFilter() {
    document.querySelectorAll('#results-tbody tr[data-idx]').forEach(row => {
      const r = _singleResults[parseInt(row.dataset.idx)];
      const socialOk = !_socialFilter || (!!r && isSocial(r));
      const engineOk = _engineFilter === 'all' || (!!r && r.engine && r.engine.includes(_engineFilter));
      row.classList.toggle('filtered-out', !socialOk || !engineOk);
    });
    updateMainCount();
  }

  function toggleSocialFilter() {
    _socialFilter = !_socialFilter;
    document.getElementById('social-filter-btn').classList.toggle('active', _socialFilter);
    applyFilter();
  }

  function setEngineFilter(engine) {
    _engineFilter = engine;
    document.querySelectorAll('#eng-filter-bar .filter-toggle').forEach(b =>
      b.classList.toggle('active', b.dataset.engine === engine));
    applyFilter();
  }

  // ── Credit counter ─────────────────────────────────────────────────────────
  async function refreshCredits() {
    try {
      const resp = await fetch('/api/credits');
      if (!resp.ok) return;
      const { total_searches_left: n } = await resp.json();
      document.getElementById('credit-count').textContent = n.toLocaleString();
      const w = document.getElementById('credit-widget');
      w.classList.toggle('cw-low',   n > 0 && n <= 500);
      w.classList.toggle('cw-empty', n === 0);
    } catch {}
  }
  refreshCredits();
  renderBirdhouse();

  // ── Birdhouse ──────────────────────────────────────────────────────────────
  function _bhSave() {
    try { localStorage.setItem('magpie_birdhouse', JSON.stringify(_birdhouse)); } catch {}
  }

  function saveToBirdhouse(scope) {
    let results = [], sourceImage = '', uploadId = null;
    if (scope === 'main') {
      uploadId = searchId;
      document.querySelectorAll('#results-tbody tr[data-idx]:not(.filtered-out)').forEach(row => {
        if (row.querySelector('.result-cb')?.checked)
          results.push(Object.assign({}, _singleResults[parseInt(row.dataset.idx)]));
      });
      sourceImage = _searchUrl || '';
    } else {
      const sIdx = parseInt(scope.replace('bulk-', ''));
      uploadId = bulkSearchIds[sIdx] || null;
      document.querySelectorAll(`#bulk-tbody-${sIdx} tr[data-idx]`).forEach(row => {
        if (row.querySelector('.result-cb')?.checked)
          results.push(Object.assign({}, (_bulkSections[sIdx] || [])[parseInt(row.dataset.idx)]));
      });
      sourceImage = _bulkSourceLabels[sIdx] || '';
    }
    if (!results.length) { alert('Please tick at least one result first.'); return; }
    const existingUrls = new Set(_birdhouse.flatMap(b => b.results.map(r => r.url)).filter(Boolean));
    const dupes = results.filter(r => r.url && existingUrls.has(r.url));
    const fresh = results.filter(r => !r.url || !existingUrls.has(r.url));
    if (!fresh.length) {
      showBhToast(`⚠️ All ${results.length} already in the Birdhouse — none saved`);
      return;
    }
    const now = new Date();
    const pad = n => String(n).padStart(2, '0');
    const savedAt = `${pad(now.getHours())}:${pad(now.getMinutes())} on ${pad(now.getDate())}/${pad(now.getMonth()+1)}/${now.getFullYear()}`;
    fresh.forEach(r => { r.source_image = sourceImage; r.birdhouse_saved = savedAt; });
    _birdhouse.push({ source_image: sourceImage, saved_at: savedAt, results: fresh });
    _bhSave();
    if (uploadId) fetch('/api/record-selections', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({upload_id: uploadId, results: fresh, action: 'birdhouse'})
    }).catch(() => {});
    renderBirdhouse();
    if (dupes.length) {
      showBhToast(`${fresh.length} saved — ⚠️ ${dupes.length} duplicate${dupes.length !== 1 ? 's' : ''} skipped`);
    } else {
      showBhToast(`${fresh.length} result${fresh.length !== 1 ? 's' : ''} saved to the Birdhouse`);
    }
  }

  function renderBirdhouse() {
    const total = _birdhouse.reduce((s, b) => s + b.results.length, 0);
    document.getElementById('bh-total').textContent = total;
    document.getElementById('bh-header-count').textContent = total;
    document.getElementById('bh-header-badge').classList.toggle('visible', total > 0);
    const section = document.getElementById('birdhouse-section');
    section.style.display = total > 0 ? 'block' : 'none';
    const container = document.getElementById('bh-batches');
    container.innerHTML = '';
    _birdhouse.forEach((batch, idx) => {
      const card = document.createElement('div');
      card.className = 'batch-card';
      const isUrl = /^https?:\\/\\//.test(batch.source_image);
      const imgHtml = isUrl
        ? `<img class="batch-src-img" src="${h(batch.source_image)}" onerror="this.style.display=\\'none\\'" alt="">`
        : `<div class="batch-src-img" style="display:flex;align-items:center;justify-content:center;font-size:.65rem;color:var(--gray-400);font-weight:600">FILE</div>`;
      const srcLabel = batch.source_image || 'Unknown source';
      const shortSrc = srcLabel.length > 55 ? srcLabel.slice(0, 52) + '\\u2026' : srcLabel;
      const rowsHtml = batch.results.map((r, i) =>
        `<div class="batch-result-row">
          <span class="batch-result-num">${i + 1}</span>
          <span class="batch-result-title">${r.url ? `<a href="${h(r.url)}" target="_blank" rel="noopener">${h(r.title || r.url)}</a>` : h(r.title || '\\u2014')}</span>
          <span style="font-size:.72rem;color:var(--gray-400);flex-shrink:0">${h(r.source || '')}</span>
        </div>`).join('');
      card.innerHTML = `
        <div class="batch-card-hdr" onclick="toggleBatch(this)">
          <span class="batch-toggle">&#x25b6;</span>
          ${imgHtml}
          <span class="batch-src-lbl" title="${h(srcLabel)}">${h(shortSrc)}</span>
          <span class="batch-meta">${batch.results.length} result${batch.results.length !== 1 ? 's' : ''} &middot; ${h(batch.saved_at)}</span>
          <button class="batch-remove-btn" onclick="event.stopPropagation();removeBatch(${idx})" title="Remove">
            <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
          </button>
        </div>
        <div class="batch-body">${rowsHtml}</div>`;
      container.appendChild(card);
    });
  }

  function toggleBatch(hdr) {
    const body = hdr.nextElementSibling;
    const toggle = hdr.querySelector('.batch-toggle');
    const open = body.classList.toggle('open');
    toggle.classList.toggle('open', open);
  }

  function removeBatch(idx) { _birdhouse.splice(idx, 1); _bhSave(); renderBirdhouse(); }

  function clearBirdhouse() {
    if (!_birdhouse.length) return;
    if (!confirm('Are you sure you want to clear the Birdhouse? This is his home!! :(')) return;
    _birdhouse = [];
    _bhSave();
    renderBirdhouse();
  }

  async function exportBirdhouse(fmt) {
    const allResults = _birdhouse.flatMap(b => b.results);
    if (!allResults.length) return;
    try {
      const resp = await fetch('/api/export-selection', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ results: allResults, fmt })
      });
      if (!resp.ok) { const d = await resp.json(); showError(d.detail || 'Export failed.'); return; }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `birdhouse.${fmt}`;
      document.body.appendChild(a); a.click();
      document.body.removeChild(a); URL.revokeObjectURL(url);
    } catch { showError('Export failed — please try again.'); }
  }

  function showBhToast(msg) {
    const t = document.getElementById('bh-toast');
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 2500);
  }

  // ── Clear / reset ──────────────────────────────────────────────────────────
  function clearResults() {
    // Hide results panels and error
    document.getElementById('results').classList.remove('show');
    document.getElementById('bulk-results').classList.remove('show');
    document.getElementById('error-box').classList.remove('show');

    // Clear result data
    _singleResults = [];
    _bulkSections  = [];
    _bulkSourceLabels = [];
    searchId       = null;
    bulkSearchIds  = [];
    _searchUrl     = null;
    _searchStart   = 0;
    _socialFilter  = false;
    _engineFilter  = 'all';
    _maxPages      = getMaxPages();
    const sfb = document.getElementById('social-filter-btn');
    if (sfb) sfb.classList.remove('active');
    document.querySelectorAll('#eng-filter-bar .filter-toggle').forEach(b =>
      b.classList.toggle('active', b.dataset.engine === 'all'));
    document.getElementById('eng-filter-bar').style.display = 'none';
    const lmw = document.getElementById('load-more-wrap');
    if (lmw) lmw.style.display = 'none';
    document.getElementById('results-tbody').innerHTML = '';
    document.getElementById('bulk-sections').innerHTML = '';
    updateOpenTabsBtn();

    // Clear URL textarea and single-file input
    document.getElementById('url-input').value = '';
    document.getElementById('file-input').value = '';
    document.getElementById('bulk-input').value = '';
    chosenFile = null;

    // Hide single-file preview
    const prev = document.getElementById('file-preview');
    if (prev) prev.classList.remove('show');

    // Reset bulk file state
    bulkFiles = [];
    document.getElementById('bulk-file-list').innerHTML = '';

    // Reset search button label
    const btn = document.getElementById('search-btn');
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `<svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg> Search`;
    }

    // Scroll back to form
    document.querySelector('.card').scrollIntoView({behavior: 'smooth', block: 'start'});
  }

</script>
<script>
  let _adminPw = null;

  function toggleAdmin() {{
    const f = document.getElementById('admin-form');
    const open = f.style.display !== 'flex';
    f.style.display = open ? 'flex' : 'none';
    if (open) document.getElementById('admin-pw').focus();
  }}

  async function unlockAdmin() {{
    const pw = document.getElementById('admin-pw').value.trim();
    if (!pw) return;
    const statusEl = document.getElementById('admin-status');
    statusEl.textContent = 'Verifying…';
    try {{
      const resp = await fetch('/admin/verify-password', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{password: pw}})
      }});
      if (!resp.ok) {{
        statusEl.textContent = 'Incorrect password.';
        document.getElementById('admin-pw').value = '';
        document.getElementById('admin-pw').focus();
        return;
      }}
    }} catch (e) {{
      statusEl.textContent = 'Network error — could not verify.';
      return;
    }}
    _adminPw = pw;
    document.querySelectorAll('.del-btn').forEach(b => b.style.display = '');
    document.getElementById('admin-form').style.display = 'none';
    document.getElementById('admin-toggle').innerHTML = '&#x1F512; Lock';
    document.getElementById('admin-toggle').onclick = lockAdmin;
    statusEl.textContent = 'Admin mode active — delete buttons visible';
  }}

  function lockAdmin() {{
    _adminPw = null;
    document.querySelectorAll('.del-btn').forEach(b => b.style.display = 'none');
    document.getElementById('admin-toggle').innerHTML = '&#x1F513; Admin mode';
    document.getElementById('admin-toggle').onclick = toggleAdmin;
    document.getElementById('admin-status').textContent = '';
  }}

  async function deleteRow(btn, uploadId) {{
    if (!_adminPw) return;
    if (!confirm('Delete this entry and all its saved selections? This cannot be undone.')) return;
    btn.disabled = true;
    try {{
      const resp = await fetch('/admin/delete-upload', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{password: _adminPw, upload_id: uploadId}})
      }});
      if (!resp.ok) {{
        const d = await resp.json().catch(() => ({{}}));
        if (resp.status === 403) {{
          alert('Incorrect password — admin mode locked.');
          lockAdmin();
        }} else {{
          alert('[RIS-501] Delete failed: ' + (d.detail || 'unknown error'));
          btn.disabled = false;
        }}
        return;
      }}
      btn.closest('tr').remove();
    }} catch (e) {{
      alert('Network error — could not delete: ' + e.message);
      btn.disabled = false;
    }}
  }}
</script>
</body>
</html>"""

# ── Apply branding config ─────────────────────────────────────────────────────
_logo_inner = (
    f'<img src="{LOGO_URL}" alt="{BRAND_NAME}" '
    f'style="width:100%;height:100%;object-fit:cover;border-radius:.6rem;">'
    if LOGO_URL else
    '<svg width="15" height="15" fill="none" viewBox="0 0 24 24" '
    'stroke="currentColor" stroke-width="2.5">\n        '
    '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>\n      </svg>'
)

_HTML = (
    _HTML
    # text
    .replace("Image Intelligence · ISD",    f"{BRAND_NAME} · ISD")
    .replace(">Image Intelligence<",        f">{BRAND_NAME}<")
    .replace(">ISD · Reverse Image Search<", f">{BRAND_SUBTITLE}<")
    .replace(
        'Find where <span class="grad">any image</span><br>appears online',
        HERO_HEADING,
    )
    .replace(
        "Use him to find other instances of a picture across the web. Download results per photo, or all results combined. <br> (Take results with a grain of salt; he\'s only a bird.)",
        HERO_SUBTEXT,
    )
    # logo
    .replace(
        '<svg width="15" height="15" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">\n        <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>\n      </svg>',
        _logo_inner,
    )
    # colours
    .replace("#7c3aed", COLOR_PRIMARY)
    .replace("#8b5cf6", COLOR_SECONDARY)
    .replace("#a855f7", COLOR_GRADIENT)
    # font
    .replace(
        "https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap",
        FONT_URL,
    )
    .replace("'Inter',", f"'{FONT_NAME}',")
)

# hero image — swap in URL and activate the side-by-side layout if provided
if HERO_IMAGE_URL:
    _HTML = (
        _HTML
        .replace('<div class="hero">', '<div class="hero hero--with-image">')
        .replace("HERO_IMAGE_PLACEHOLDER", HERO_IMAGE_URL)
    )
else:
    _HTML = _HTML.replace("HERO_IMAGE_PLACEHOLDER", "")
