#!/usr/bin/env python3
"""
Media Server — view images, videos, and NEF RAW files in your browser.
Usage:
    pip install flask rawpy pillow
    python media_server.py [directory] [--port 8080]
"""

import os
import sys
import io
import argparse
import mimetypes
from pathlib import Path
from flask import Flask, Response, abort, request, send_file, stream_with_context

# ── optional deps (graceful degradation) ─────────────────────────────────────
try:
    import rawpy
    import numpy as np
    NEF_OK = True
except ImportError:
    NEF_OK = False

try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False

# ── constants ─────────────────────────────────────────────────────────────────
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".svg", ".avif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv", ".ogv"}
RAW_EXTS   = {".nef", ".cr2", ".cr3", ".arw", ".orf", ".raf", ".dng", ".rw2"}
ALL_EXTS   = IMAGE_EXTS | VIDEO_EXTS | RAW_EXTS

CHUNK = 1 << 20  # 1 MB streaming chunk

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
SERVE_DIR: Path = Path(".")

# ── helpers ───────────────────────────────────────────────────────────────────

def safe_path(rel: str) -> Path:
    """Resolve a URL-relative path and ensure it stays inside SERVE_DIR."""
    target = (SERVE_DIR / rel.lstrip("/")).resolve()
    if not str(target).startswith(str(SERVE_DIR.resolve())):
        abort(403)
    return target


def file_kind(p: Path) -> str:
    ext = p.suffix.lower()
    if ext in IMAGE_EXTS:  return "image"
    if ext in VIDEO_EXTS:  return "video"
    if ext in RAW_EXTS:    return "raw"
    return "other"


def scan_dir(directory: Path):
    items = []
    for p in sorted(directory.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if p.name.startswith("."):
            continue
        rel = p.relative_to(SERVE_DIR).as_posix()
        if p.is_dir():
            items.append({"name": p.name, "rel": rel, "kind": "dir"})
        elif p.suffix.lower() in ALL_EXTS:
            items.append({"name": p.name, "rel": rel, "kind": file_kind(p), "size": p.stat().st_size})
    return items


def human_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/browse/")
@app.route("/browse/<path:rel>")
def browse(rel=""):
    directory = safe_path(rel)
    if directory.is_file():
        return view_file(rel)
    if not directory.is_dir():
        abort(404)

    items    = scan_dir(directory)
    up_link  = str(Path(rel).parent) if rel else None
    page     = _render_gallery(rel, items, up_link)
    return Response(page, mimetype="text/html")


@app.route("/raw/<path:rel>")
def serve_raw(rel):
    """Serve the original file (download)."""
    p = safe_path(rel)
    if not p.is_file():
        abort(404)
    return send_file(p, as_attachment=True, download_name=p.name)


@app.route("/preview/<path:rel>")
def preview(rel):
    """Serve a viewable version of a file (NEF → JPEG on-the-fly, others pass-through)."""
    p = safe_path(rel)
    if not p.is_file():
        abort(404)

    ext = p.suffix.lower()

    # ── RAW → JPEG ──────────────────────────────────────────────────────────
    if ext in RAW_EXTS:
        if not NEF_OK:
            abort(501, "rawpy not installed; cannot preview RAW files.")
        if not PIL_OK:
            abort(501, "Pillow not installed; cannot preview RAW files.")
        try:
            with rawpy.imread(str(p)) as raw:
                rgb = raw.postprocess(
                    use_camera_wb=True,
                    half_size=False,
                    no_auto_bright=False,
                    output_bps=8,
                )
            img = Image.fromarray(rgb)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=88, optimize=True)
            buf.seek(0)
            return Response(buf, mimetype="image/jpeg",
                            headers={"Content-Disposition": f'inline; filename="{p.stem}.jpg"'})
        except Exception as e:
            abort(500, f"RAW decode error: {e}")

    # ── video — range-aware streaming ────────────────────────────────────────
    if ext in VIDEO_EXTS:
        return _stream_video(p)

    # ── regular image ────────────────────────────────────────────────────────
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return send_file(p, mimetype=mime)


def _stream_video(p: Path):
    """HTTP range-request streaming for videos."""
    size  = p.stat().st_size
    range_header = request.headers.get("Range", None)
    mime  = mimetypes.guess_type(p.name)[0] or "video/mp4"

    if not range_header:
        def gen_full():
            with open(p, "rb") as f:
                while chunk := f.read(CHUNK):
                    yield chunk
        return Response(stream_with_context(gen_full()), mimetype=mime,
                        headers={"Content-Length": size, "Accept-Ranges": "bytes"})

    # parse "bytes=start-end"
    byte_range = range_header.replace("bytes=", "").split("-")
    start = int(byte_range[0])
    end   = int(byte_range[1]) if byte_range[1] else size - 1
    length = end - start + 1

    def gen_range():
        with open(p, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range":  f"bytes {start}-{end}/{size}",
        "Accept-Ranges":  "bytes",
        "Content-Length": length,
        "Content-Type":   mime,
    }
    return Response(stream_with_context(gen_range()), 206, headers=headers)

# ── HTML renderer ─────────────────────────────────────────────────────────────

def _render_gallery(rel: str, items: list, up_link) -> str:
    crumbs = _breadcrumbs(rel)
    cards  = _render_cards(items)
    raw_warning = "" if NEF_OK else (
        '<div class="warn">⚠ <strong>rawpy</strong> not installed — '
        'NEF/RAW preview disabled. Run <code>pip install rawpy numpy</code>.</div>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MEDIA / {rel or 'root'}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=DM+Sans:wght@300;500;700&display=swap');

  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  :root {{
    --bg:        #0c0c0e;
    --surface:   #141418;
    --border:    #2a2a32;
    --accent:    #e8ff47;
    --accent2:   #47b8ff;
    --text:      #e8e8f0;
    --muted:     #6a6a7a;
    --danger:    #ff6b6b;
    --radius:    6px;
    --font-mono: 'IBM Plex Mono', monospace;
    --font-body: 'DM Sans', sans-serif;
  }}

  html {{ scroll-behavior: smooth; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-body);
    min-height: 100vh;
    padding-bottom: 4rem;
  }}

  /* ── header ── */
  header {{
    position: sticky; top: 0; z-index: 100;
    background: rgba(12,12,14,.92);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border);
    padding: 0 2rem;
    display: flex; align-items: center; gap: 1.5rem;
    height: 56px;
  }}
  .logo {{
    font-family: var(--font-mono);
    font-size: .8rem; font-weight: 600;
    letter-spacing: .2em; text-transform: uppercase;
    color: var(--accent);
    white-space: nowrap;
  }}
  .logo span {{ color: var(--muted); }}
  .crumbs {{
    font-family: var(--font-mono);
    font-size: .75rem; color: var(--muted);
    display: flex; align-items: center; gap: .4rem;
    overflow: hidden;
  }}
  .crumbs a {{ color: var(--text); text-decoration: none; }}
  .crumbs a:hover {{ color: var(--accent); }}
  .crumbs .sep {{ color: var(--border); }}

  .search-wrap {{
    margin-left: auto;
    position: relative;
  }}
  #search {{
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    font-family: var(--font-mono);
    font-size: .8rem;
    padding: .4rem .8rem .4rem 2rem;
    border-radius: var(--radius);
    width: 200px;
    outline: none;
    transition: border-color .2s, width .3s;
  }}
  #search:focus {{ border-color: var(--accent); width: 280px; }}
  .search-wrap::before {{
    content: '⌕';
    position: absolute; left: .6rem; top: 50%;
    transform: translateY(-50%);
    color: var(--muted); font-size: 1rem; pointer-events: none;
  }}

  /* ── toolbar ── */
  .toolbar {{
    padding: 1.2rem 2rem .8rem;
    display: flex; align-items: center; gap: 1rem;
    flex-wrap: wrap;
  }}
  .count {{ font-family: var(--font-mono); font-size: .75rem; color: var(--muted); }}
  .view-toggle {{ margin-left: auto; display: flex; gap: .5rem; }}
  .vbtn {{
    background: none; border: 1px solid var(--border);
    color: var(--muted); border-radius: var(--radius);
    padding: .3rem .6rem; cursor: pointer; font-size: .9rem;
    transition: all .15s;
  }}
  .vbtn.active, .vbtn:hover {{ border-color: var(--accent); color: var(--accent); }}

  /* ── warn ── */
  .warn {{
    margin: .5rem 2rem 1rem;
    background: #2a1a00; border: 1px solid #6b4400;
    border-radius: var(--radius); padding: .7rem 1rem;
    font-size: .82rem; color: #ffb84d;
  }}
  .warn code {{ background: rgba(255,255,255,.07); border-radius: 3px; padding: .1em .3em; font-family: var(--font-mono); }}

  /* ── grid ── */
  #gallery {{
    padding: 0 2rem;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 12px;
    transition: grid-template-columns .3s;
  }}
  #gallery.list-view {{
    grid-template-columns: 1fr;
    gap: 4px;
  }}

  /* ── cards ── */
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    cursor: pointer;
    transition: border-color .15s, transform .15s;
    position: relative;
    display: flex; flex-direction: column;
  }}
  .card:hover {{ border-color: #3a3a50; transform: translateY(-2px); }}
  .card:hover .card-actions {{ opacity: 1; }}

  .card-thumb {{
    width: 100%; aspect-ratio: 16/10;
    object-fit: cover;
    background: #0a0a10;
    display: flex; align-items: center; justify-content: center;
    font-size: 3rem; color: var(--border);
    flex-shrink: 0;
  }}
  .card-thumb img, .card-thumb video {{
    width: 100%; height: 100%; object-fit: cover;
  }}

  .card-body {{
    padding: .55rem .7rem;
    display: flex; flex-direction: column; gap: .15rem;
  }}
  .card-name {{
    font-size: .8rem; font-weight: 500;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  .card-meta {{
    font-family: var(--font-mono);
    font-size: .68rem; color: var(--muted);
  }}

  .badge {{
    display: inline-block;
    font-family: var(--font-mono); font-size: .6rem; font-weight: 600;
    letter-spacing: .08em; text-transform: uppercase;
    padding: .1em .4em; border-radius: 3px;
    margin-right: .3em;
  }}
  .badge-image {{ background: #0f2a1a; color: #5fffaa; border: 1px solid #1a5a30; }}
  .badge-video {{ background: #0a1a2e; color: var(--accent2); border: 1px solid #1a3a6a; }}
  .badge-raw   {{ background: #2a1a0a; color: #ff9a3c; border: 1px solid #6a3a0a; }}
  .badge-dir   {{ background: #1a1a1a; color: var(--muted); border: 1px solid var(--border); }}

  .card-actions {{
    position: absolute; top: .4rem; right: .4rem;
    display: flex; gap: .3rem;
    opacity: 0; transition: opacity .15s;
  }}
  .act-btn {{
    background: rgba(0,0,0,.75); backdrop-filter: blur(4px);
    border: 1px solid rgba(255,255,255,.12);
    color: #fff; border-radius: 4px;
    padding: .28rem .5rem; font-size: .72rem; cursor: pointer;
    text-decoration: none; display: inline-flex; align-items: center; gap: .25rem;
    transition: background .15s, border-color .15s;
  }}
  .act-btn:hover {{ background: var(--accent); color: #000; border-color: var(--accent); }}

  /* list-view card overrides */
  .list-view .card {{ flex-direction: row; align-items: center; }}
  .list-view .card-thumb {{ width: 56px; aspect-ratio: 1; border-radius: 0; font-size: 1.4rem; }}
  .list-view .card-body {{ flex: 1; flex-direction: row; align-items: center; gap: 1rem; }}
  .list-view .card-name {{ flex: 1; }}
  .list-view .card-actions {{ position: static; opacity: 1; flex-shrink: 0; }}

  /* dir card */
  .dir-card {{ cursor: default; }}
  .dir-card .card-thumb {{ background: #0f0f14; }}
  .dir-card:hover {{ transform: none; border-color: #2a2a3a; }}

  /* ── lightbox ── */
  #lb {{
    display: none; position: fixed; inset: 0; z-index: 1000;
    background: rgba(0,0,0,.92); backdrop-filter: blur(6px);
    flex-direction: column; align-items: center; justify-content: center;
    gap: 1rem;
  }}
  #lb.open {{ display: flex; }}

  #lb-media {{
    max-width: min(90vw, 1400px);
    max-height: 80vh;
    border-radius: var(--radius);
    background: #000;
    display: flex; align-items: center; justify-content: center;
  }}
  #lb-media img {{ max-width: 100%; max-height: 80vh; object-fit: contain; border-radius: var(--radius); display: block; }}
  #lb-media video {{ max-width: 100%; max-height: 80vh; border-radius: var(--radius); display: block; }}
  #lb-spin {{
    width: 48px; height: 48px;
    border: 3px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin .7s linear infinite;
    display: none;
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}

  #lb-bar {{
    display: flex; align-items: center; gap: 1rem;
    font-family: var(--font-mono); font-size: .78rem; color: var(--muted);
  }}
  #lb-name {{ color: var(--text); max-width: 60vw; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  #lb-dl {{
    background: var(--accent); color: #000;
    border: none; border-radius: var(--radius);
    padding: .4rem .9rem; font-size: .8rem; font-weight: 700;
    cursor: pointer; text-decoration: none; font-family: var(--font-mono);
    letter-spacing: .05em; transition: opacity .15s;
  }}
  #lb-dl:hover {{ opacity: .85; }}
  #lb-close {{
    position: fixed; top: 1rem; right: 1rem;
    background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.1);
    color: var(--text); border-radius: 50%;
    width: 36px; height: 36px; font-size: 1.2rem;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer; z-index: 1001; transition: background .15s;
  }}
  #lb-close:hover {{ background: rgba(255,255,255,.14); }}

  #lb-nav {{
    position: fixed; top: 50%; transform: translateY(-50%);
    width: 100%; display: flex; justify-content: space-between;
    padding: 0 1rem; pointer-events: none;
  }}
  .nav-btn {{
    pointer-events: all;
    background: rgba(0,0,0,.6); border: 1px solid rgba(255,255,255,.1);
    color: var(--text); border-radius: 50%;
    width: 44px; height: 44px; font-size: 1.3rem;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer; transition: background .15s;
  }}
  .nav-btn:hover {{ background: rgba(255,255,255,.12); }}

  /* empty */
  .empty {{ text-align: center; padding: 5rem 2rem; color: var(--muted); font-family: var(--font-mono); }}
  .empty .icon {{ font-size: 3rem; margin-bottom: 1rem; }}
</style>
</head>
<body>

<header>
  <div class="logo">MEDIA<span>/SERVER</span></div>
  <nav class="crumbs">{crumbs}</nav>
  <div class="search-wrap">
    <input id="search" type="search" placeholder="filter..." autocomplete="off">
  </div>
</header>

{raw_warning}

<div class="toolbar">
  <span class="count" id="count"></span>
  <div class="view-toggle">
    <button class="vbtn active" id="btn-grid" title="Grid view">⊞</button>
    <button class="vbtn" id="btn-list" title="List view">☰</button>
  </div>
</div>

<div id="gallery">
  {cards if cards else '<div class="empty"><div class="icon">📂</div><div>No media files here.</div></div>'}
</div>

<!-- lightbox -->
<div id="lb" role="dialog" aria-modal="true">
  <button id="lb-close" aria-label="Close">✕</button>
  <div id="lb-nav">
    <button class="nav-btn" id="lb-prev" aria-label="Previous">‹</button>
    <button class="nav-btn" id="lb-next" aria-label="Next">›</button>
  </div>
  <div id="lb-media">
    <div id="lb-spin"></div>
  </div>
  <div id="lb-bar">
    <span id="lb-name"></span>
    <a id="lb-dl" download>⬇ Download</a>
  </div>
</div>

<script>
  // ── data ────────────────────────────────────────────────────────────────────
  const cards = Array.from(document.querySelectorAll('.card[data-kind]'));
  const mediaItems = cards.filter(c => c.dataset.kind !== 'dir');

  // ── count ────────────────────────────────────────────────────────────────────
  function updateCount() {{
    const visible = cards.filter(c => c.style.display !== 'none').length;
    document.getElementById('count').textContent = visible + ' item' + (visible !== 1 ? 's' : '');
  }}
  updateCount();

  // ── search ───────────────────────────────────────────────────────────────────
  document.getElementById('search').addEventListener('input', function() {{
    const q = this.value.toLowerCase();
    cards.forEach(c => {{
      const name = c.dataset.name.toLowerCase();
      c.style.display = name.includes(q) ? '' : 'none';
    }});
    updateCount();
  }});

  // ── view toggle ───────────────────────────────────────────────────────────────
  const gallery = document.getElementById('gallery');
  document.getElementById('btn-grid').addEventListener('click', () => {{
    gallery.classList.remove('list-view');
    document.getElementById('btn-grid').classList.add('active');
    document.getElementById('btn-list').classList.remove('active');
  }});
  document.getElementById('btn-list').addEventListener('click', () => {{
    gallery.classList.add('list-view');
    document.getElementById('btn-list').classList.add('active');
    document.getElementById('btn-grid').classList.remove('active');
  }});

  // ── lightbox ───────────────────────────────────────────────────────────────
  const lb      = document.getElementById('lb');
  const lbMedia = document.getElementById('lb-media');
  const lbSpin  = document.getElementById('lb-spin');
  const lbName  = document.getElementById('lb-name');
  const lbDl    = document.getElementById('lb-dl');
  let currentIdx = 0;

  function openLB(idx) {{
    const item = mediaItems[idx];
    if (!item) return;
    currentIdx = idx;
    const rel   = item.dataset.rel;
    const kind  = item.dataset.kind;
    const name  = item.dataset.name;

    lbName.textContent = name;
    lbDl.href = '/raw/' + rel;
    lbDl.download = name;

    // clear previous
    lbMedia.innerHTML = '';
    lbMedia.appendChild(lbSpin);
    lbSpin.style.display = 'block';
    lb.classList.add('open');
    document.body.style.overflow = 'hidden';

    if (kind === 'video') {{
      const vid = document.createElement('video');
      vid.src = '/preview/' + rel;
      vid.controls = true; vid.autoplay = true;
      vid.style.cssText = 'max-width:100%;max-height:80vh;';
      vid.onloadedmetadata = () => {{ lbSpin.style.display = 'none'; }};
      lbMedia.appendChild(vid);
    }} else {{
      // image or raw
      const img = document.createElement('img');
      img.src = '/preview/' + rel;
      img.alt = name;
      img.onload  = () => {{ lbSpin.style.display = 'none'; }};
      img.onerror = () => {{ lbSpin.style.display = 'none'; img.alt = '⚠ Preview unavailable'; }};
      lbMedia.appendChild(img);
    }}
  }}

  function closeLB() {{
    lb.classList.remove('open');
    lbMedia.innerHTML = '';
    document.body.style.overflow = '';
  }}

  function navLB(dir) {{
    const next = currentIdx + dir;
    if (next >= 0 && next < mediaItems.length) openLB(next);
  }}

  document.getElementById('lb-close').addEventListener('click', closeLB);
  document.getElementById('lb-prev').addEventListener('click', () => navLB(-1));
  document.getElementById('lb-next').addEventListener('click', () => navLB(1));
  lb.addEventListener('click', e => {{ if (e.target === lb) closeLB(); }});

  document.addEventListener('keydown', e => {{
    if (!lb.classList.contains('open')) return;
    if (e.key === 'Escape')       closeLB();
    if (e.key === 'ArrowLeft')    navLB(-1);
    if (e.key === 'ArrowRight')   navLB(1);
  }});

  // attach open handlers
  mediaItems.forEach((card, idx) => {{
    card.addEventListener('click', e => {{
      if (e.target.closest('.act-btn')) return; // let download/view links work
      openLB(idx);
    }});
  }});
</script>
</body>
</html>"""


def _breadcrumbs(rel: str) -> str:
    parts = Path(rel).parts if rel else []
    links = [f'<a href="/browse/">root</a>']
    for i, p in enumerate(parts):
        path = "/".join(parts[:i+1])
        links.append(f'<span class="sep">/</span><a href="/browse/{path}">{p}</a>')
    return "".join(links)


def _render_cards(items: list) -> str:
    out = []
    for it in items:
        kind = it["kind"]
        rel  = it["rel"]
        name = it["name"]

        if kind == "dir":
            out.append(f'''
<div class="card dir-card" data-kind="dir" data-name="{name}" data-rel="{rel}"
     onclick="location.href='/browse/{rel}'">
  <div class="card-thumb">📁</div>
  <div class="card-body">
    <div class="card-name">{name}</div>
    <div class="card-meta"><span class="badge badge-dir">folder</span></div>
  </div>
</div>''')
            continue

        size_str = human_size(it["size"])
        badge = {"image": "badge-image", "video": "badge-video", "raw": "badge-raw"}[kind]
        badge_label = {"image": "img", "video": "vid", "raw": "raw"}[kind]

        # thumbnail inside the card
        if kind == "image":
            thumb = f'<img src="/preview/{rel}" loading="lazy" alt="{name}">'
        elif kind == "video":
            thumb = f'<video src="/preview/{rel}" muted preload="none" onmouseenter="this.play()" onmouseleave="this.pause();this.currentTime=0"></video>'
        else:  # raw
            thumb = f'<img src="/preview/{rel}" loading="lazy" alt="{name}">'

        out.append(f'''
<div class="card" data-kind="{kind}" data-name="{name}" data-rel="{rel}">
  <div class="card-thumb">{thumb}</div>
  <div class="card-actions">
    <a class="act-btn" href="/preview/{rel}" target="_blank" title="Open">↗</a>
    <a class="act-btn" href="/raw/{rel}" download="{name}" title="Download">⬇</a>
  </div>
  <div class="card-body">
    <div class="card-name" title="{name}">{name}</div>
    <div class="card-meta"><span class="badge {badge}">{badge_label}</span>{size_str}</div>
  </div>
</div>''')

    return "\n".join(out)


def view_file(rel: str):
    """Redirect file paths to preview."""
    from flask import redirect
    return redirect(f"/preview/{rel}")

# ── entry point ───────────────────────────────────────────────────────────────

def main():
    global SERVE_DIR
    parser = argparse.ArgumentParser(description="Media browser server")
    parser.add_argument("directory", nargs="?", default=".", help="Directory to serve (default: .)")
    parser.add_argument("--port", "-p", type=int, default=8080, help="Port (default: 8080)")
    parser.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    args = parser.parse_args()

    SERVE_DIR = Path(args.directory).resolve()
    if not SERVE_DIR.is_dir():
        print(f"Error: '{SERVE_DIR}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    print(f"\n  📷  Media Server")
    print(f"  ─────────────────────────────────────")
    print(f"  Serving : {SERVE_DIR}")
    print(f"  URL     : http://localhost:{args.port}")
    print(f"  NEF/RAW : {'✓ enabled' if NEF_OK else '✗ disabled  (pip install rawpy numpy)'}")
    print(f"  Press Ctrl-C to stop.\n")

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
