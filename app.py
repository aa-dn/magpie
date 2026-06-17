#!/usr/bin/env python3
"""
Image Intelligence — web front-end for the reverse image search tool.
Set SERPAPI_KEY as an environment variable, then run:
    uvicorn app:app --reload
"""

import asyncio
import os
import shutil
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from reverse_image_search import (
    export_csv,
    export_excel,
    export_html,
    search_all_engines,
)

SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
TEMP_DIR = Path(tempfile.gettempdir()) / "ris_cache"
TEMP_DIR.mkdir(exist_ok=True)

_pool = ThreadPoolExecutor(max_workers=4)
app = FastAPI(title="Image Intelligence")


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/debug/env")
async def debug_env():
    key = os.environ.get("SERPAPI_KEY", "")
    return {
        "SERPAPI_KEY_set": bool(key),
        "SERPAPI_KEY_length": len(key),
        "all_env_keys": sorted(os.environ.keys()),
    }


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
            search_url = str(request.base_url) + f"uploads/{search_id}/{filename}"
            source_label = file.filename
        elif image_url and image_url.strip():
            search_url = image_url.strip()
            source_label = image_url.strip()
        else:
            raise HTTPException(400, "Provide an image URL or upload a file")

        results = await loop.run_in_executor(
            _pool, search_all_engines, search_url, SERPAPI_KEY
        )

        if results:
            prefix = str(work_dir / "results")

            def _exports():
                export_csv(results, f"{prefix}.csv")
                export_excel(results, f"{prefix}.xlsx")
                export_html(results, f"{prefix}.html", source_label)

            await loop.run_in_executor(_pool, _exports)

        return {"search_id": search_id, "count": len(results), "results": results}

    except HTTPException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(500, str(e))


@app.get("/api/download/{search_id}/{fmt}")
async def download_file(search_id: str, fmt: str):
    if fmt not in {"csv", "xlsx", "html"}:
        raise HTTPException(400, "Invalid format")
    if "/" in search_id or "\\" in search_id or ".." in search_id:
        raise HTTPException(400, "Invalid ID")

    fpath = TEMP_DIR / search_id / f"results.{fmt}"
    if not fpath.exists():
        raise HTTPException(404, "Results not found — please run a new search")

    media = {
        "csv":  "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "html": "text/html",
    }
    return FileResponse(fpath, media_type=media[fmt], filename=f"results.{fmt}")


# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Image Intelligence · ISD</title>
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

    /* ── Main ── */
    main { max-width: 860px; margin: 0 auto; padding: 3rem 1.5rem 5rem; }

    /* ── Hero ── */
    .hero { text-align: center; margin-bottom: 2.5rem; }
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

    /* ── Card ── */
    .card {
      background: #fff; border-radius: var(--radius);
      border: 1px solid rgba(0,0,0,.06); box-shadow: var(--shadow);
      padding: 2rem; margin-bottom: 1.5rem;
    }

    /* ── Tabs ── */
    .tabs {
      display: inline-flex; gap: .25rem;
      background: var(--gray-100); border-radius: .625rem;
      padding: .25rem; margin-bottom: 1.5rem;
    }
    .tab-btn {
      padding: .375rem .875rem; border-radius: .4375rem;
      font-size: .875rem; font-weight: 500;
      border: none; cursor: pointer;
      color: var(--gray-500); background: transparent;
      transition: all .15s;
    }
    .tab-btn.active {
      background: #fff; color: var(--gray-900);
      box-shadow: 0 1px 4px rgba(0,0,0,.1);
    }

    /* ── Inputs ── */
    label { display: block; font-size: .875rem; font-weight: 500; color: var(--gray-700); margin-bottom: .5rem; }

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
      border: 2px dashed var(--gray-200); border-radius: .75rem;
      padding: 2.5rem 1.5rem; text-align: center; cursor: pointer;
      transition: border-color .15s, background .15s;
    }
    .drop-zone:hover, .drop-zone.drag-over {
      border-color: var(--brand-light); background: var(--brand-50);
    }
    .dz-icon { width: 2.5rem; height: 2.5rem; margin: 0 auto .75rem; color: var(--gray-300); }
    .drop-zone p { font-size: .9375rem; color: var(--gray-600); margin-bottom: .25rem; }
    .drop-zone p strong { color: var(--brand); }
    .drop-zone .hint { font-size: .8125rem; color: var(--gray-400); }

    /* ── File preview ── */
    .file-preview {
      display: none; align-items: center; gap: .75rem;
      margin-top: 1rem; padding: .75rem 1rem;
      background: var(--brand-50); border: 1px solid var(--brand-100); border-radius: .625rem;
    }
    .file-preview.show { display: flex; }
    .file-preview img { width: 3rem; height: 3rem; object-fit: cover; border-radius: .375rem; }
    .file-name { font-size: .875rem; font-weight: 500; color: var(--gray-700); flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .clear-btn {
      width: 1.5rem; height: 1.5rem; border: none; background: none;
      cursor: pointer; color: var(--gray-400); border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      padding: 0; transition: background .15s, color .15s;
    }
    .clear-btn:hover { background: var(--gray-200); color: var(--gray-600); }

    /* ── Search button ── */
    .search-btn {
      width: 100%; padding: .875rem 1.5rem; margin-top: 1.5rem;
      background: linear-gradient(135deg, #7c3aed, #a855f7);
      color: #fff; font-family: inherit; font-size: .9375rem; font-weight: 600;
      border: none; border-radius: .75rem; cursor: pointer;
      transition: transform .15s, box-shadow .15s, opacity .15s;
      box-shadow: 0 2px 10px rgba(124,58,237,.35);
      display: flex; align-items: center; justify-content: center; gap: .5rem;
    }
    .search-btn:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 4px 18px rgba(124,58,237,.45); }
    .search-btn:active:not(:disabled) { transform: translateY(0); }
    .search-btn:disabled { opacity: .6; cursor: not-allowed; transform: none !important; }

    /* ── Loading ── */
    .loading { display: none; text-align: center; padding: 3rem 1.5rem; }
    .loading.show { display: block; }
    .spinner {
      width: 2.5rem; height: 2.5rem; margin: 0 auto 1.25rem;
      border: 3px solid var(--brand-100); border-top-color: var(--brand);
      border-radius: 50%; animation: spin .7s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .loading p { color: var(--gray-600); font-size: .9375rem; font-weight: 500; }
    .loading .hint { font-size: .8125rem; color: var(--gray-400); margin-top: .5rem; }

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

    /* ── Responsive ── */
    @media (max-width: 600px) {
      main { padding: 2rem 1rem 4rem; }
      .hero h2 { font-size: 1.5rem; }
      th.col-thumb, td:nth-child(2) { display: none; }
    }
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
  </div>
</header>

<main>

  <!-- Hero -->
  <div class="hero">
    <h2>Find where <span class="grad">any image</span><br>appears online</h2>
    <p>Paste a URL or upload a file to search across the web, then export a full visual report in CSV, Excel, or HTML.</p>
  </div>

  <!-- Search card -->
  <div class="card">
    <div class="tabs">
      <button class="tab-btn active" id="tab-url"  onclick="switchTab('url')">Image URL</button>
      <button class="tab-btn"        id="tab-file" onclick="switchTab('file')">Upload File</button>
    </div>

    <!-- URL panel -->
    <div id="panel-url">
      <label for="url-input">Paste an image URL</label>
      <input type="url" id="url-input" placeholder="https://example.com/photo.jpg" autocomplete="off">
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

    <button class="search-btn" id="search-btn" onclick="doSearch()">
      <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
        <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
      </svg>
      Search
    </button>
  </div>

  <!-- Loading -->
  <div class="loading" id="loading">
    <div class="spinner"></div>
    <p>Searching the web&hellip;</p>
    <p class="hint">Fetching thumbnails for your Excel report &mdash; this may take up to a minute.</p>
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
  </div>

</main>

<script>
  let searchId = null;
  let activeTab = 'url';
  let chosenFile = null;

  // ── Tab switching ──────────────────────────────────────────────────────────
  function switchTab(tab) {
    activeTab = tab;
    show('panel-url',  tab === 'url');
    show('panel-file', tab === 'file');
    document.getElementById('tab-url').classList.toggle('active',  tab === 'url');
    document.getElementById('tab-file').classList.toggle('active', tab === 'file');
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

  // ── Search ─────────────────────────────────────────────────────────────────
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
      if (activeTab === 'url') fd.append('image_url', document.getElementById('url-input').value.trim());
      else fd.append('file', chosenFile);

      const resp = await fetch('/api/search', { method: 'POST', body: fd });
      const data = await resp.json();

      if (!resp.ok) { showError(data.detail || 'An unexpected error occurred.'); return; }

      searchId = data.search_id;
      renderResults(data.results, data.count);
    } catch {
      showError('Network error — please check your connection and try again.');
    } finally {
      setLoading(false);
    }
  }

  // ── Render results ─────────────────────────────────────────────────────────
  function renderResults(results, count) {
    document.getElementById('result-num').textContent = count;
    const tbody = document.getElementById('results-tbody');
    tbody.innerHTML = '';

    if (!results.length) {
      tbody.innerHTML = `<tr><td colspan="4"><div class="empty">
        <svg width="40" height="40" fill="none" viewBox="0 0 24 24" stroke="#d1d5db" stroke-width="1.5">
          <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
        </svg>
        <p>No results found for this image.</p>
      </div></td></tr>`;
    } else {
      results.forEach((r, i) => {
        const thumbCell = r.thumbnail
          ? `<img class="thumb-img" src="${h(r.thumbnail)}" alt="" loading="lazy"
               onerror="this.outerHTML='<div class=thumb-empty><svg width=20 height=20 fill=none viewBox=\\'0 0 24 24\\' stroke=currentColor stroke-width=1.5><path stroke-linecap=round stroke-linejoin=round d=\\'M2.25 15.75l5.159-5.159a2.25 2.25 0 013.182 0l5.159 5.159m-1.5-1.5l1.409-1.409a2.25 2.25 0 013.182 0l2.909 2.909M21 18.75H3.75A1.5 1.5 0 012.25 17.25V6.75A1.5 1.5 0 013.75 5.25h16.5A1.5 1.5 0 0121.75 6.75v10.5A1.5 1.5 0 0120.25 18.75z\\'/></svg></div>'">`
          : `<div class="thumb-empty"><svg width="20" height="20" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M2.25 15.75l5.159-5.159a2.25 2.25 0 013.182 0l5.159 5.159m-1.5-1.5l1.409-1.409a2.25 2.25 0 013.182 0l2.909 2.909M21 18.75H3.75A1.5 1.5 0 012.25 17.25V6.75A1.5 1.5 0 013.75 5.25h16.5A1.5 1.5 0 0121.75 6.75v10.5A1.5 1.5 0 0120.25 18.75z"/></svg></div>`;

        const titleCell = r.url
          ? `<a class="title-link" href="${h(r.url)}" target="_blank" rel="noopener">${h(r.title || r.url)}</a>`
          : `<span style="color:var(--gray-400)">${h(r.title || '—')}</span>`;

        const engineClass = r.engine && r.engine.includes('·') ? 'multi' : r.engine === 'Yandex' ? 'yandex' : r.engine === 'Bing' ? 'bing' : 'google';
        const engineBadge = r.engine ? `<span class="engine-badge ${engineClass}">${h(r.engine)}</span>` : '—';

        tbody.innerHTML += `<tr>
          <td><span class="row-num">${i + 1}</span></td>
          <td>${thumbCell}</td>
          <td>${titleCell}</td>
          <td><span class="source-text" title="${h(r.source)}">${h(r.source || '—')}</span></td>
          <td>${engineBadge}</td>
        </tr>`;
      });
    }

    document.getElementById('results').classList.add('show');
  }

  // ── Downloads ──────────────────────────────────────────────────────────────
  function dl(fmt) {
    if (searchId) window.location.href = `/api/download/${searchId}/${fmt}`;
  }

  // ── Helpers ────────────────────────────────────────────────────────────────
  function setLoading(on) {
    document.getElementById('loading').classList.toggle('show', on);
    const btn = document.getElementById('search-btn');
    btn.disabled = on;
    btn.innerHTML = on
      ? `<span style="display:inline-block;width:15px;height:15px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite"></span>&nbsp;Searching…`
      : `<svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg> Search`;
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

  // Enter key on URL input
  document.getElementById('url-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') doSearch();
  });
</script>
</body>
</html>"""
