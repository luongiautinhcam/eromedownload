# Erome Album Downloader (v1)

A desktop downloader with a modern PySide6 (Qt) UI that fetches all files (videos and images) from an Erome album with concurrent downloads, real-time per-file progress, and a full history table.

## Features
- Table-based UI with two views:
  - Active Downloads: live queue, per-file progress bar, speed, ETA, thread.
  - History: completed/failed items with time, size, duration, message.
- Concurrency via QThreadPool (user-configurable threads; default 3).
- Album subfolder auto-naming by album ID.
- Optional metadata (album_info.json).
- Robust HTTP session with headers and retries.
- Skips existing files safely.

## Requirements
- Python 3.10+
- Windows/Linux/macOS

Install dependencies:
```bash
pip install -r requirements.txt
```

## Run
```bash
python main.py
```

## Usage
- Enter the Erome album URL in "Source".
- Choose a "Save folder" (downloads is created by default). The app creates a subfolder named after the album ID.
- Set the number of threads (1–16; default 3).
- Optionally enable "Download album metadata (JSON)".
- Click "Start". Use "Pause" to pause/resume. "Clear history" resets the history table.

## Tables
- Active Downloads columns:
  1) #, 2) Status, 3) File name, 4) Type, 5) Total size, 6) Downloaded,
  7) Progress (bar), 8) Speed, 9) ETA, 10) Thread.
- History columns:
  - Time, Result, File name, Type, Size, Duration, Message.

## How downloads are stored
- Files are saved to: downloads/<ALBUM_ID>/
- The repository keeps downloads/ but ignores its subfolders via .gitignore (downloads/.gitkeep is tracked).

## Notes
- Progress is exact when Content-Length is provided. If unknown, progress stays at 0% but bytes and speed continue to update.
- The parser matches Erome CDN hosts like vXX.erome.com and sXX.erome.com and skips thumbnails.
- Some albums lazy-load media via JavaScript; if something doesn’t show up in HTML, it won’t be discovered without additional endpoints.

## Troubleshooting
- HTTP 403 Forbidden:
  - Reduce threads (1–2), wait 10–30 minutes, or try a different network.
- PySide6 not found:
  - Ensure `pip install -r requirements.txt` completed successfully.
- Empty results:
  - Verify the album URL is correct and accessible in a browser; some content may be paginated or loaded dynamically.

## Versioning & Tags
- This repository version is published as `v1`.
- To tag locally and push:
```bash
git tag v1
git push origin v1
```

## License
MIT License

## Changelog
- v1 (2025-12-23)
  - Initial PySide6 UI with Active/History tables, per-file progress, QThreadPool workers, album subfolders, metadata, retries.








## License
MIT License

## Changelog
- v1 (2025-12-23)
  - Initial PySide6 UI with Active/History tables, per-file progress, QThreadPool workers, album subfolders, metadata, retries.
