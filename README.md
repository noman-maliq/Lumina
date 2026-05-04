# 📷 Lumina — Local Media Browser

A lightweight Python media server that lets you browse, preview, and download images, videos, and RAW files (including **Nikon NEF**) straight from your browser — no cloud, no accounts, no fuss.

![Python](https://img.shields.io/badge/python-3.8%2B-blue?style=flat-square)
![Flask](https://img.shields.io/badge/flask-3.x-lightgrey?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)

---

## Features

- **Image viewer** — JPEG, PNG, GIF, WebP, BMP, TIFF, SVG, AVIF
- **Video streaming** — MP4, MOV, AVI, MKV, WebM, M4V, FLV, WMV with HTTP range support (scrubbing works)
- **RAW / NEF support** — Nikon NEF, Canon CR2/CR3, Sony ARW, Olympus ORF, Fuji RAF, Adobe DNG, Panasonic RW2 — previewed as high-quality JPEG on-the-fly, original downloaded untouched
- **Lightbox** with keyboard navigation (← → arrows, Escape)
- **Video hover preview** — hover a card to see a silent clip
- **Grid / list toggle** and live filename search
- **One-click download** of original files (never the converted copy)
- **Folder navigation** with breadcrumb trail
- **Path traversal protection** — users can't escape the served directory

---

## Quick Start

### 1. Install dependencies

```bash
pip install flask rawpy numpy pillow
```

> `rawpy` and `numpy` are only required for RAW/NEF preview. If you skip them, everything else works normally and a warning banner appears in the UI.

### 2. Run

```bash
# Serve the current directory
python media_server.py

# Serve a specific folder
python media_server.py ~/Photos

# Custom host and port
python media_server.py ~/Photos --host 127.0.0.1 --port 9000
```

### 3. Open your browser

```
http://localhost:8080
```

---

## Usage

```
usage: media_server.py [-h] [--port PORT] [--host HOST] [directory]

positional arguments:
  directory        Directory to serve (default: current directory)

options:
  -h, --help       show this help message and exit
  --port PORT, -p  Port to listen on (default: 8080)
  --host HOST      Host to bind to (default: 0.0.0.0)
```

---

## Supported Formats

| Category | Extensions |
|----------|-----------|
| Images   | `.jpg` `.jpeg` `.png` `.gif` `.webp` `.bmp` `.tiff` `.svg` `.avif` |
| Videos   | `.mp4` `.mov` `.avi` `.mkv` `.webm` `.m4v` `.flv` `.wmv` `.ogv` |
| RAW      | `.nef` `.cr2` `.cr3` `.arw` `.orf` `.raf` `.dng` `.rw2` |

---

## How RAW Preview Works

RAW files are decoded in-memory on demand using [rawpy](https://github.com/letmaik/rawpy). The server:

1. Reads the RAW file with camera white balance applied
2. Post-processes to 8-bit RGB
3. Encodes to JPEG (quality 88) and streams it to the browser

The **original file is always served untouched** when you click Download — never the converted version.

---

## Project Structure

```
lumina/
├── media_server.py   # Single-file server — everything lives here
└── README.md
```

No configuration files, no database, no build step.

---

## Requirements

| Package  | Version | Purpose                        |
|----------|---------|--------------------------------|
| Flask    | ≥ 3.0   | HTTP server                    |
| rawpy    | ≥ 0.18  | RAW / NEF decoding *(optional)*|
| numpy    | ≥ 1.24  | Required by rawpy *(optional)* |
| Pillow   | ≥ 10.0  | JPEG encoding *(optional)*     |

Python 3.8 or newer required.

---

## Security Notes

- Lumina is intended for **trusted local networks** only. Do not expose it to the public internet.
- All URL paths are resolved and checked against the served root directory — path traversal attempts are rejected with HTTP 403.
- No authentication is implemented.

---

## License

Product is free to use, but not to distribute under any commercial use. ALL RIGHTS RESERVED
