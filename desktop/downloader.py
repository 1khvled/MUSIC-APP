"""
YouTube downloader powered by yt-dlp.
Handles single videos, playlists, channels. Supports audio-only and video downloads.
"""

import os
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from queue import Queue
from typing import Callable, Optional

import yt_dlp

from config import Config
from database import Database


class FormatType(Enum):
    AUDIO = "audio"
    VIDEO = "video"
    AUDIO_VIDEO = "audio_video"


@dataclass
class DownloadItem:
    """Represents a single item in the download queue."""
    url: str
    title: str = ""
    format_type: FormatType = FormatType.AUDIO
    playlist_title: str = ""
    playlist_index: int = 0
    progress: float = 0.0
    status: str = "queued"  # queued, downloading, processing, completed, failed
    speed: str = ""
    eta: str = ""
    file_path: str = ""
    error: str = ""
    youtube_id: str = ""
    thumbnail: str = ""
    duration: int = 0
    artist: str = ""
    db_id: Optional[int] = None


@dataclass
class PlaylistInfo:
    url: str
    title: str
    is_playlist: bool = False
    entries: list[dict] = field(default_factory=list)


class ProgressCallback:
    """Hook for yt-dlp progress reporting — thread-safe with item snapshot."""

    def __init__(self, item: DownloadItem, on_progress: Callable, cancel_event: threading.Event):
        self.item = item
        self.on_progress = on_progress
        self.cancel_event = cancel_event
        self._lock = threading.Lock()

    def __call__(self, d):
        if self.cancel_event.is_set():
            self.item.status = "cancelled"
            raise yt_dlp.utils.DownloadError("Download cancelled by user")
        with self._lock:
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
                downloaded = d.get("downloaded_bytes", 0)
                self.item.progress = (downloaded / total * 100) if total else 0
                self.item.speed = self._format_speed(d.get("speed", 0))
                self.item.eta = self._format_eta(d.get("eta", 0))
                self.item.status = "downloading"

            elif d["status"] == "finished":
                self.item.progress = 100
                self.item.status = "processing"

            elif d["status"] == "error":
                self.item.status = "failed"
                self.item.error = str(d.get("error", "Unknown error"))

        # Callback always runs on main thread via after() in app
        self.on_progress(self.item)

    @staticmethod
    def _format_speed(bps: Optional[float]) -> str:
        if not bps:
            return ""
        if bps > 1_000_000:
            return f"{bps / 1_000_000:.1f} MB/s"
        elif bps > 1_000:
            return f"{bps / 1_000:.0f} KB/s"
        return f"{bps:.0f} B/s"

    @staticmethod
    def _format_eta(seconds: Optional[float]) -> str:
        if not seconds or seconds < 0:
            return ""
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}h{m:02d}m"
        if m:
            return f"{m}m{s:02d}s"
        return f"{s}s"


class YouTubeDownloader:
    """Manages download queue, yt-dlp instances, and post-processing."""

    def __init__(self, config: Config, db: Database,
                 on_progress: Optional[Callable] = None,
                 on_complete: Optional[Callable] = None):
        self.config = config
        self.db = db
        self.on_progress = on_progress or (lambda x: None)
        self.on_complete = on_complete or (lambda x: None)
        self._queue: Queue[DownloadItem] = Queue()
        self._active: list[DownloadItem] = []
        self._completed: list[DownloadItem] = []
        self._lock = threading.Lock()
        self._running = False
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        self._cancel_event = threading.Event()

    @staticmethod
    def extract_playlist(url: str) -> Optional[PlaylistInfo]:
        """Parse a YouTube URL and return playlist info (or single-video wrapper)."""
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "skip_download": True,
            "ignoreerrors": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info is None:
                    return None

                if info.get("_type") == "playlist" or "entries" in info:
                    entries = info.get("entries") or []
                    return PlaylistInfo(
                        url=url,
                        title=info.get("title", "Untitled Playlist"),
                        is_playlist=True,
                        entries=[
                            {
                                "url": (f"https://youtube.com/watch?v={e.get('id')}"
                                        if e.get("id") and not e.get("url")
                                        else e.get("url") or e.get("webpage_url", "")),
                                "title": e.get("title", "Unknown"),
                                "id": e.get("id", ""),
                                "duration": e.get("duration", 0),
                                "thumbnail": e.get("thumbnail", ""),
                                "channel": e.get("channel", e.get("uploader", "Unknown")),
                            }
                            for e in entries if e is not None and (e.get("id") or e.get("url"))
                        ],
                    )
                # Single video
                video_id = info.get("id", "")
                return PlaylistInfo(
                    url=url,
                    title="",
                    is_playlist=False,
                    entries=[{
                        "url": f"https://youtube.com/watch?v={video_id}",
                        "title": info.get("title", "Unknown"),
                        "id": video_id,
                        "duration": info.get("duration", 0),
                        "thumbnail": info.get("thumbnail", ""),
                        "channel": info.get("channel", info.get("uploader", "Unknown")),
                    }],
                )
        except yt_dlp.utils.DownloadError as e:
            print(f"yt-dlp extract error for {url}: {e}")
            return PlaylistInfo(url=url, title="Error", is_playlist=False, entries=[])
        except Exception as e:
            print(f"Extract error for {url}: {e}")
            return None

    def add_url(self, url: str, fmt: FormatType = FormatType.AUDIO):
        """Parse a URL and queue all its entries."""
        playlist = self.extract_playlist(url)
        if not playlist:
            item = DownloadItem(url=url, format_type=fmt)
            item.status = "failed"
            item.error = "Could not parse URL — is it a valid YouTube link?"
            self._completed.append(item)
            self.on_complete(item)
            return

        if not playlist.entries:
            item = DownloadItem(url=url, format_type=fmt)
            item.status = "failed"
            item.error = f"No videos found in: {playlist.title}"
            self._completed.append(item)
            self.on_complete(item)
            return

        for entry in playlist.entries:
            item = DownloadItem(
                url=entry["url"],
                title=entry["title"],
                format_type=fmt,
                playlist_title=playlist.title if playlist.is_playlist else "",
                youtube_id=entry.get("id", ""),
                thumbnail=entry.get("thumbnail", ""),
                duration=entry.get("duration", 0),
                artist=entry.get("channel", ""),
            )
            # Register in DB
            db_id = self.db.add_download(entry["url"], entry["title"])
            item.db_id = db_id
            self._queue.put(item)

    def add_items(self, items: list[DownloadItem]):
        """Add pre-built items (e.g. from a loaded playlist)."""
        for item in items:
            self._queue.put(item)

    def start(self):
        """Start the download worker threads."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._cancel_event.clear()
        max_workers = min(self.config.max_concurrent, 5)

        for _ in range(max_workers):
            t = threading.Thread(target=self._worker_loop, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        """Signal all workers to stop."""
        self._stop_event.set()
        self._running = False

    def cancel_all(self):
        """Stop workers and remove every item still waiting in the queue."""
        self._cancel_event.set()
        self.stop()
        removed = []
        with self._queue.mutex:
            while self._queue.queue:
                removed.append(self._queue.queue.popleft())
        with self._lock:
            active = list(self._active)
        for item in active + removed:
            item.status = "cancelled"
            item.error = "Cancelled by user"
        for item in removed:
            with self._lock:
                self._completed.append(item)
        return len(removed)

    def _worker_loop(self):
        """Worker thread: pulls items from queue and downloads them."""
        try:
            while not self._stop_event.is_set():
                try:
                    item = self._queue.get(timeout=1)
                except Exception:
                    continue

                with self._lock:
                    self._active.append(item)

                try:
                    self._download_item(item)
                finally:
                    with self._lock:
                        if item in self._active:
                            self._active.remove(item)
                        self._completed.append(item)
                    self._queue.task_done()
                    self.on_complete(item)
        finally:
            # Each worker gets its own SQLite connection via thread-local
            # storage. Close it when the worker exits so repeated start/stop
            # cycles do not leak file handles on Windows.
            self.db.close()

    def _get_ydl_opts(self, item: DownloadItem) -> dict:
        """Build yt-dlp options based on item format type."""
        download_dir = Path(self.config.download_dir)
        fmt = item.format_type

        # Sanitize playlist title for folder name
        playlist_dir = ""
        if item.playlist_title:
            safe = re.sub(r'[\\/*?:"<>|]', "_", item.playlist_title)[:100]
            playlist_dir = str(download_dir / "Playlists" / safe)
        else:
            playlist_dir = str(download_dir / fmt.value)

        base_opts: dict = {
            "outtmpl": os.path.join(playlist_dir, "%(title).150s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
        }
        # Only set proxy if user explicitly configured one
        if self.config.proxy:
            base_opts["proxy"] = self.config.proxy

        if fmt == FormatType.AUDIO:
            quality = self.config.audio_quality
            base_opts.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": quality,
                }],
                "postprocessor_args": ["-id3v2_version", "3"],
                "embedthumbnail": True,
                "addmetadata": True,
            })
        elif fmt == FormatType.VIDEO:
            quality = self.config.video_quality
            quality_map = {"360": "360", "480": "480", "720": "720",
                           "1080": "1080", "2160": "2160"}
            q = quality_map.get(quality, "1080")
            base_opts.update({
                "format": f"bestvideo[height<={q}]+bestaudio/best[height<={q}]",
                "merge_output_format": "mp4",
            })
        else:  # AUDIO_VIDEO
            base_opts.update({
                "format": "bestvideo+bestaudio/best",
                "merge_output_format": "mp4",
            })

        return base_opts

    def _download_item(self, item: DownloadItem):
        """Execute the actual download for one item."""
        opts = self._get_ydl_opts(item)

        # Wire up progress callback
        cb = ProgressCallback(item, self.on_progress, self._cancel_event)
        opts["progress_hooks"] = [cb]

        try:
            if item.db_id is not None:
                self.db.update_download_status(item.db_id, "downloading")

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(item.url, download=True)

                if info:
                    item.title = info.get("title", item.title)
                    item.youtube_id = info.get("id", item.youtube_id)
                    item.duration = info.get("duration", item.duration)
                    item.artist = info.get("channel", info.get("uploader", item.artist))
                    item.thumbnail = info.get("thumbnail", item.thumbnail)

                    # Get actual file path from yt-dlp's output
                    found_path = ""
                    req = info.get("requested_downloads")
                    if req and len(req) > 0:
                        fp = req[0].get("filepath", "")
                        if fp and os.path.exists(fp):
                            found_path = fp
                    # Fallback: search by glob if yt-dlp path didn't work
                    if not found_path:
                        ext = "mp3" if item.format_type == FormatType.AUDIO else "mp4"
                        dl_dir = Path(self.config.download_dir)
                        if item.playlist_title:
                            dl_dir = dl_dir / "Playlists" / re.sub(r'[\\/*?:"<>|]', "_", item.playlist_title)[:100]
                        else:
                            dl_dir = dl_dir / item.format_type.value
                        safe_title = re.sub(r'[\\/*?:"<>|]', "_", item.title)[:150]
                        for f in dl_dir.glob(f"*{safe_title}*.{ext}"):
                            found_path = str(f); break
                        if not found_path:
                            for f in dl_dir.glob(f"*.{ext}"):
                                if safe_title.lower() in f.stem.lower():
                                    found_path = str(f); break
                    item.file_path = found_path

                    # Save to database
                    file_size = 0
                    if found_path:
                        file_size = os.path.getsize(found_path)
                        if item.format_type == FormatType.AUDIO and self.config.save_metadata:
                            self._embed_metadata(item)

                    track_id = self.db.add_track(
                        youtube_id=item.youtube_id,
                        title=item.title,
                        file_path=item.file_path,
                        artist=item.artist,
                        album=item.playlist_title,
                        duration=item.duration,
                        file_size=file_size,
                        fmt=item.format_type.value,
                        thumbnail_url=item.thumbnail,
                    )
                    item.status = "completed"
                    if item.db_id is not None:
                        self.db.update_download_status(item.db_id, "completed",
                                                       track_id=track_id)

        except yt_dlp.utils.DownloadError as e:
            item.status = "cancelled" if self._cancel_event.is_set() else "failed"
            item.error = "Cancelled by user" if self._cancel_event.is_set() else f"YouTube error: {e}"
            if item.status == "failed":
                self._update_db_error(item, str(e))
            print(f"Download error for {item.url}: {e}")

        except Exception as e:
            item.status = "failed"
            item.error = str(e)
            self._update_db_error(item, str(e))
            print(f"Download error for {item.url}: {e}")

    def _update_db_error(self, item: DownloadItem, error: str):
        """Update DB with error status safely (handles None db_id)."""
        if item.db_id is not None:
            try:
                self.db.update_download_status(item.db_id, "failed", error=error)
            except Exception:
                pass  # Best-effort DB update

    def _embed_metadata(self, item: DownloadItem):
        """Add artist/album tags to the downloaded file."""
        if not item.file_path or not os.path.exists(item.file_path):
            return
        try:
            ext = Path(item.file_path).suffix.lower()
            if ext not in (".mp3", ".m4a", ".opus", ".flac"):
                return

            from mutagen import File as MutagenFile
            audio = MutagenFile(item.file_path, easy=True)
            if audio is None:
                return

            audio["title"] = item.title
            audio["artist"] = item.artist or "Unknown"
            if item.playlist_title:
                audio["album"] = item.playlist_title
            audio.save()

            # Embed thumbnail for MP3 only
            if ext == ".mp3" and item.thumbnail and item.thumbnail.startswith("http"):
                try:
                    import requests
                    from mutagen.id3 import ID3, APIC
                    resp = requests.get(item.thumbnail, timeout=10)
                    if resp.status_code == 200:
                        id3 = ID3(item.file_path)
                        id3.add(APIC(
                            encoding=3, mime="image/jpeg",
                            type=3, desc="Cover", data=resp.content,
                        ))
                        id3.save()
                except Exception:
                    pass

        except Exception as e:
            print(f"Metadata embed warning: {e}")

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def get_active(self) -> list[DownloadItem]:
        with self._lock:
            return list(self._active)

    def get_queued(self) -> list[DownloadItem]:
        with self._queue.mutex:
            return list(self._queue.queue)

    def get_completed(self) -> list[DownloadItem]:
        with self._lock:
            return list(self._completed)

    def clear_completed(self):
        with self._lock:
            self._completed.clear()

    def is_running(self) -> bool:
        return self._running

    def wait_for_completion(self):
        """Block until the queue is empty."""
        self._queue.join()
