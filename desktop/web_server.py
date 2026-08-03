"""Local browser interface for the downloader.

Run this module on the computer that owns the downloader database. The web UI
is available to the computer and other devices on the same Wi-Fi network.
"""

import json
import mimetypes
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

PROJECT_DIR = Path(__file__).resolve().parent.parent
DESKTOP_DIR = PROJECT_DIR / "desktop"
WEBSITE_DIR = PROJECT_DIR / "website"
if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))

from config import Config  # noqa: E402
from database import Database  # noqa: E402
from downloader import FormatType, YouTubeDownloader  # noqa: E402
from cloud_supabase import SupabaseConfig, SupabaseProvider  # noqa: E402


class LocalWebApp:
    def __init__(self):
        self.config = Config()
        self.db = Database()
        self.downloader = YouTubeDownloader(
            self.config,
            self.db,
            on_complete=lambda _item: None,
        )
        self._extract_lock = threading.Lock()

    def tracks(self):
        rows = self.db.get_all_tracks(limit=500)
        return [
            {
                "id": row["id"],
                "title": row.get("title", "Untitled"),
                "artist": row.get("artist", "Unknown"),
                "album": row.get("album", ""),
                "duration": row.get("duration", 0),
                "file_size": row.get("file_size", 0),
                "format": row.get("format", "audio"),
                "downloaded_at": row.get("downloaded_at"),
                "thumbnail_url": row.get("thumbnail_url"),
                "file_available": bool(row.get("file_path") and Path(row["file_path"]).exists()),
            }
            for row in rows
        ]

    def add_download(self, url, format_name):
        if not url or not url.startswith(("http://", "https://")):
            raise ValueError("Enter a valid YouTube URL.")
        fmt = {
            "audio": FormatType.AUDIO,
            "video": FormatType.VIDEO,
            "audio+video": FormatType.AUDIO_VIDEO,
        }.get(format_name, FormatType.AUDIO)

        def worker():
            with self._extract_lock:
                self.downloader.add_url(url, fmt)
                if not self.downloader.is_running():
                    self.downloader.start()

        threading.Thread(target=worker, daemon=True).start()

    def sync_cloud(self, session):
        """Upload local library metadata/files for the signed-in Supabase user."""
        if not session or not session.get("access_token") or not session.get("user", {}).get("id"):
            raise ValueError("A successful Supabase sign-in is required.")
        provider = SupabaseProvider(self.config)
        provider._supabase_config = SupabaseConfig(
            url="https://yhnekdvxafgdbmbituql.supabase.co",
            anon_key=os.environ.get("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_p8XiGHHNiakXc4IgWY33xw_y1HrUdM2"),
            access_token=session["access_token"],
            refresh_token=session.get("refresh_token", ""),
            user_id=session["user"]["id"],
            email=session["user"].get("email", ""),
        )
        uploaded = 0
        failed = 0
        for row in self.db.get_all_tracks(limit=500):
            path = row.get("file_path")
            if not path or not Path(path).is_file():
                failed += 1
                continue
            remote_name = Path(path).name
            data = dict(row)
            data["file_path"] = f"{session['user']['id']}/{remote_name}"
            uploaded_file = provider.upload_file(path, remote_name)
            if provider.sync_track(data):
                uploaded += 1
                if not uploaded_file:
                    failed += 1
            else:
                failed += 1
        return {"uploaded": uploaded, "failed": failed}

    def download_file(self, track_id):
        track = self.db.get_track(int(track_id))
        if not track or not track.get("file_path"):
            return None
        path = Path(track["file_path"]).resolve()
        root = Path(self.config.download_dir).resolve()
        if root not in path.parents and path != root:
            return None
        return path if path.is_file() else None

    def delete_track(self, track_id):
        track = self.db.get_track(int(track_id))
        if not track:
            return False
        path = Path(track.get("file_path", "")).resolve() if track.get("file_path") else None
        root = Path(self.config.download_dir).resolve()
        if path and path.exists() and (root in path.parents or path == root):
            path.unlink()
        return self.db.delete_track(int(track_id))


class Handler(BaseHTTPRequestHandler):
    app = None

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/tracks":
            self.send_json({"tracks": self.app.tracks()})
            return
        if parsed.path == "/api/status":
            active = self.app.downloader.get_active()
            completed = self.app.downloader.get_completed()[-10:]
            self.send_json({
                "queued": self.app.downloader.queue_size,
                "active": self.app.downloader.active_count,
                "running": self.app.downloader.is_running(),
                "active_items": [{"title": item.title or item.url, "progress": item.progress, "status": item.status, "speed": item.speed, "eta": item.eta} for item in active],
                "completed": [{"title": item.title or item.url, "status": item.status, "error": item.error} for item in completed],
            })
            return
        if parsed.path.startswith("/api/download/"):
            file_path = self.app.download_file(parsed.path.rsplit("/", 1)[-1])
            if not file_path:
                self.send_json({"error": "File not found"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{file_path.name}"')
            self.send_header("Content-Length", str(file_path.stat().st_size))
            self.end_headers()
            with file_path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    self.wfile.write(chunk)
            return
        if parsed.path.startswith("/api/stream/"):
            file_path = self.app.download_file(parsed.path.rsplit("/", 1)[-1])
            if not file_path:
                self.send_json({"error": "File not found"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "audio/mpeg")
            self.send_header("Content-Length", str(file_path.stat().st_size))
            self.end_headers()
            with file_path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    self.wfile.write(chunk)
            return
        self.serve_static(parsed.path)

    def serve_static(self, path):
        relative = unquote(path.lstrip("/")) or "index.html"
        candidate = (WEBSITE_DIR / relative).resolve()
        if WEBSITE_DIR not in candidate.parents and candidate != WEBSITE_DIR:
            self.send_error(403)
            return
        if not candidate.is_file():
            candidate = WEBSITE_DIR / "index.html"
        data = candidate.read_bytes()
        self.send_response(200)
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type == "text/html":
            content_type = "text/html; charset=utf-8"
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path == "/api/cloud-sync":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                self.send_json(self.app.sync_cloud(payload.get("session")))
            except (ValueError, json.JSONDecodeError, KeyError) as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if self.path == "/api/delete-bulk":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                ids = payload.get("ids", [])
                deleted = sum(1 for track_id in ids if self.app.delete_track(track_id))
                self.send_json({"deleted": deleted})
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if self.path.startswith("/api/delete/"):
            try:
                deleted = self.app.delete_track(self.path.rsplit("/", 1)[-1])
                self.send_json({"deleted": deleted}, 200 if deleted else 404)
            except (TypeError, ValueError):
                self.send_json({"error": "Invalid track id"}, 400)
            return
        if self.path != "/api/download":
            self.send_json({"error": "Not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.app.add_download(payload.get("url", "").strip(), payload.get("format", "audio"))
            self.send_json({"queued": True})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)


def serve(host="0.0.0.0", port=4180):
    app = LocalWebApp()
    Handler.app = app
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Local website: http://127.0.0.1:{port}/")
    print(f"Phone access:  http://<this-computer-ip>:{port}/")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.downloader.stop()
        app.db.close()
        server.server_close()


if __name__ == "__main__":
    serve()
