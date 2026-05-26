#!/usr/bin/env python3
"""
Review videos in a folder with thumbnail contact sheets.

Requirements:
  - ffmpeg and ffprobe available on your PATH

Shortcuts:
  k / Right / Space  keep current video and move to the next
  d / Delete         delete current video, then move to the next
  s                  skip current video and leave it undecided
  o                  open the current video in your default player
  f                  reveal the current video in Finder
  u                  undo the last keep/delete/skip
  Escape             close the enlarged thumbnail preview
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse


VIDEO_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".asf",
    ".avi",
    ".flv",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".mxf",
    ".ogv",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
}


@dataclass(frozen=True)
class Settings:
    directory: Path
    recursive: bool
    samples: int
    columns: int
    thumb_width: int
    jpeg_quality: int
    accurate_seek: bool
    delete_permanently: bool
    trash_dir: Path
    sort_mode: str
    preload_count: int
    session_file: Optional[Path]
    resume_with_left: Optional[int]
    ffmpeg_threads: int


@dataclass(frozen=True)
class HistoryItem:
    action: str
    video: Path
    index: int
    destination: Optional[Path] = None


def find_videos(directory: Path, recursive: bool, trash_dir: Path, sort_mode: str) -> list[Path]:
    videos: list[Path] = []
    trash_dir_text = os.path.normpath(str(trash_dir))

    if recursive:
        for root, dirnames, filenames in os.walk(directory):
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if os.path.normpath(os.path.join(root, dirname)) != trash_dir_text
            ]
            for filename in filenames:
                if Path(filename).suffix.lower() in VIDEO_EXTENSIONS:
                    videos.append(Path(root) / filename)
    else:
        with os.scandir(directory) as entries:
            for entry in entries:
                if Path(entry.name).suffix.lower() in VIDEO_EXTENSIONS and entry.is_file():
                    videos.append(Path(entry.path))

    return sort_videos(videos, sort_mode)


def sort_videos(videos: list[Path], sort_mode: str) -> list[Path]:
    if sort_mode == "date":
        return sorted(videos, key=lambda path: path.stat().st_mtime)
    if sort_mode == "size":
        return sorted(videos, key=lambda path: path.stat().st_size)
    if sort_mode == "random":
        shuffled = list(videos)
        random.shuffle(shuffled)
        return shuffled
    return sorted(videos)


def probe_duration(video: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    try:
        return max(0.0, float(result.stdout.strip()))
    except ValueError as exc:
        raise RuntimeError("ffprobe did not return a usable duration.") from exc


def probe_video_metadata(video: Path, stat: Optional[os.stat_result] = None) -> dict[str, object]:
    stat = stat or video.stat()
    fields = basic_metadata_fields(stat)

    try:
        command = [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(video),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
    except Exception:
        return {"fields": fields}

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]

    duration = first_value(video_stream.get("duration"), fmt.get("duration"))
    if duration:
        fields.insert(0, {"label": "Length", "value": format_duration(float(duration))})

    width = video_stream.get("width")
    height = video_stream.get("height")
    if width and height:
        fields.append({"label": "Resolution", "value": f"{width} x {height}"})

    codec = video_stream.get("codec_name")
    profile = video_stream.get("profile")
    if codec:
        value = str(codec).upper()
        if profile:
            value = f"{value} ({profile})"
        fields.append({"label": "Video Codec", "value": value})

    pix_fmt = video_stream.get("pix_fmt")
    if pix_fmt:
        fields.append({"label": "Pixel Format", "value": str(pix_fmt)})

    frame_rate = format_rate(first_value(video_stream.get("avg_frame_rate"), video_stream.get("r_frame_rate")))
    if frame_rate:
        fields.append({"label": "Frame Rate", "value": frame_rate})

    bitrate = first_value(video_stream.get("bit_rate"), fmt.get("bit_rate"))
    if bitrate:
        fields.append({"label": "Bitrate", "value": format_bitrate(float(bitrate))})

    if audio_streams:
        audio_codecs = sorted({str(stream.get("codec_name", "")).upper() for stream in audio_streams if stream.get("codec_name")})
        if audio_codecs:
            fields.append({"label": "Audio", "value": ", ".join(audio_codecs)})

    creation_time = (fmt.get("tags") or {}).get("creation_time")
    if creation_time:
        fields.append({"label": "Embedded Date", "value": format_embedded_date(str(creation_time))})

    return {"fields": fields}


def basic_video_metadata(video: Path, stat: os.stat_result) -> dict[str, object]:
    return {"fields": basic_metadata_fields(stat)}


def basic_metadata_fields(stat: os.stat_result) -> list[dict[str, str]]:
    return [
        {"label": "Size", "value": format_bytes(stat.st_size)},
        {"label": "Created", "value": format_file_date(stat)},
        {"label": "Modified", "value": format_timestamp(stat.st_mtime)},
    ]


def video_key(video: Path, stat: os.stat_result) -> str:
    return f"{safe_temp_name(video)}-{stat.st_size}-{int(stat.st_mtime)}"


def first_value(*values: object) -> Optional[object]:
    for value in values:
        if value not in (None, "", "N/A", "0/0"):
            return value
    return None


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def format_bitrate(bits_per_second: float) -> str:
    if bits_per_second >= 1_000_000:
        return f"{bits_per_second / 1_000_000:.1f} Mbps"
    if bits_per_second >= 1_000:
        return f"{bits_per_second / 1_000:.0f} kbps"
    return f"{bits_per_second:.0f} bps"


def format_rate(rate: Optional[object]) -> Optional[str]:
    if not rate:
        return None
    text = str(rate)
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            value = float(numerator) / float(denominator)
        else:
            value = float(text)
    except (ValueError, ZeroDivisionError):
        return None
    if value <= 0:
        return None
    return f"{value:.2f}".rstrip("0").rstrip(".") + " fps"


def format_file_date(stat: os.stat_result) -> str:
    created = getattr(stat, "st_birthtime", None)
    if created:
        return format_timestamp(created)
    return format_timestamp(stat.st_mtime)


def format_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %I:%M %p")


def format_embedded_date(value: str) -> str:
    try:
        cleaned = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(cleaned)
        return parsed.astimezone().strftime("%Y-%m-%d %I:%M %p")
    except ValueError:
        return value


def sample_times(duration: float, count: int) -> list[float]:
    if duration <= 0:
        return [0.0]

    count = max(1, count)
    if count == 1:
        return [min(duration * 0.5, max(duration - 0.1, 0.0))]

    start = min(duration * 0.05, 5.0)
    end = max(start, duration - min(duration * 0.05, 5.0))
    step = (end - start) / (count - 1)
    return [min(duration, start + step * i) for i in range(count)]


def safe_temp_name(video: Path) -> str:
    return "".join(char if char.isalnum() else "-" for char in video.stem)[:80]


def unique_destination(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    for number in range(1, 10_000):
        candidate = path.with_name(f"{stem}-{number}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find a unique destination for {path}")


def trash_destination(video: Path, settings: Settings) -> Path:
    try:
        relative = video.relative_to(settings.directory)
    except ValueError:
        relative = Path(video.name)
    return unique_destination(settings.trash_dir / relative)


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def parse_args() -> Settings:
    parser = argparse.ArgumentParser(
        description="Review videos with thumbnail contact sheets and keep/delete shortcuts."
    )
    parser.add_argument("directory", type=Path, help="Directory containing videos to review.")
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Only review videos directly inside DIRECTORY, not subdirectories.",
    )
    parser.add_argument(
        "-n",
        "--samples",
        type=positive_int,
        default=12,
        help="Number of thumbnails per video. Default: 12.",
    )
    parser.add_argument(
        "-c",
        "--columns",
        type=positive_int,
        default=4,
        help="Number of thumbnail columns. Default: 4.",
    )
    parser.add_argument(
        "--thumb-width",
        type=positive_int,
        default=260,
        help="Thumbnail width in pixels. Default: 260.",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=positive_int,
        default=3,
        help="Preview JPEG quality, 2 is best and 31 is worst/smallest. Default: 3.",
    )
    parser.add_argument(
        "--accurate-seek",
        action="store_true",
        help="Use more accurate, slower thumbnail seeking instead of fast keyframe previews.",
    )
    parser.add_argument(
        "--permanent",
        action="store_true",
        help="Permanently delete files instead of moving them into the review trash folder.",
    )
    parser.add_argument(
        "--trash-dir",
        type=Path,
        default=None,
        help="Folder for deleted videos. Default: DIRECTORY/Video Review Trash.",
    )
    parser.add_argument(
        "--sort",
        choices=("name", "date", "size", "random"),
        default="name",
        help="Review order. Default: name.",
    )
    parser.add_argument(
        "--preload",
        type=positive_int,
        default=2,
        help="Number of upcoming videos to preload. Default: 2.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Start fresh instead of resuming the prior review session.",
    )
    parser.add_argument(
        "--resume-with-left",
        type=positive_int,
        default=None,
        help="Begin at the point where this many videos are left to review.",
    )
    parser.add_argument(
        "--start-left",
        type=positive_int,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--ffmpeg-threads",
        type=positive_int,
        default=1,
        help="CPU threads per thumbnail extraction. Default: 1.",
    )

    args = parser.parse_args()
    directory = args.directory.expanduser().resolve()
    if not directory.is_dir():
        raise SystemExit(f"Directory does not exist: {directory}")

    require_command("ffmpeg")
    require_command("ffprobe")

    trash_dir = args.trash_dir.expanduser().resolve() if args.trash_dir else directory / "Video Review Trash"
    session_file = directory / ".video_review_session.json"
    return Settings(
        directory=directory,
        recursive=not args.no_recursive,
        samples=args.samples,
        columns=args.columns,
        thumb_width=args.thumb_width,
        jpeg_quality=min(args.jpeg_quality, 31),
        accurate_seek=args.accurate_seek,
        delete_permanently=args.permanent,
        trash_dir=trash_dir,
        sort_mode=args.sort,
        preload_count=args.preload,
        session_file=session_file if not args.no_resume else None,
        resume_with_left=args.resume_with_left if args.resume_with_left is not None else args.start_left,
        ffmpeg_threads=args.ffmpeg_threads,
    )


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"{name} is required. Install ffmpeg, then try again.")


class WebReviewState:
    def __init__(self, videos: list[Path], settings: Settings, temp_dir: Path) -> None:
        self.videos = videos
        self.settings = settings
        self.temp_dir = temp_dir
        self.index = 0
        self.initial_total = len(videos)
        self.metadata_cache: dict[tuple[str, int, int], dict[str, object]] = {}
        self.duration_cache: dict[tuple[str, int, int], float] = {}
        self.duration_lock = threading.Lock()
        self.history: list[HistoryItem] = []
        self.kept = 0
        self.deleted = 0
        self.skipped = 0
        self.lock = threading.RLock()
        if self.settings.resume_with_left is not None:
            self.index = max(0, min(len(self.videos), len(self.videos) - self.settings.resume_with_left))
        else:
            self.load_session()

    def current(self) -> dict[str, object]:
        with self.lock:
            if self.index >= len(self.videos):
                return {
                    "done": True,
                    "index": self.index,
                    "total": self.initial_total,
                    "videosLeft": 0,
                    "kept": self.kept,
                    "deleted": self.deleted,
                    "skipped": self.skipped,
                    "canUndo": bool(self.history),
                    "preloadCount": self.settings.preload_count,
                    "permanentDelete": self.settings.delete_permanently,
                    "samples": self.settings.samples,
                    "columns": self.settings.columns,
                }

            video = self.videos[self.index]
            stat = video.stat()
            videos_left = len(self.videos) - self.index
            return {
                "done": False,
                "index": self.index,
                "position": self.index + 1,
                "total": self.initial_total,
                "videosLeft": videos_left,
                "kept": self.kept,
                "deleted": self.deleted,
                "skipped": self.skipped,
                "canUndo": bool(self.history),
                "preloadCount": self.settings.preload_count,
                "permanentDelete": self.settings.delete_permanently,
                "name": video.name,
                "path": str(video),
                "videoKey": video_key(video, stat),
                "metadata": basic_video_metadata(video, stat),
                "upcoming": self.upcoming_videos(),
                "samples": self.settings.samples,
                "columns": self.settings.columns,
            }

    def action(self, action: str) -> dict[str, object]:
        with self.lock:
            if action == "undo":
                self.undo_last()
                self.save_session()
                return self.current()

            if self.index >= len(self.videos):
                return {
                    "done": True,
                    "index": self.index,
                    "total": self.initial_total,
                    "videosLeft": 0,
                    "kept": self.kept,
                    "deleted": self.deleted,
                    "skipped": self.skipped,
                    "canUndo": bool(self.history),
                    "preloadCount": self.settings.preload_count,
                    "permanentDelete": self.settings.delete_permanently,
                    "samples": self.settings.samples,
                    "columns": self.settings.columns,
                }

            if action == "keep":
                self.history.append(HistoryItem(action="keep", video=self.videos[self.index], index=self.index))
                self.kept += 1
                self.index += 1
            elif action == "skip":
                video = self.videos[self.index]
                self.videos.append(self.videos.pop(self.index))
                self.history.append(HistoryItem(action="skip", video=video, index=self.index))
                self.skipped += 1
            elif action == "delete":
                video = self.videos[self.index]
                if self.settings.delete_permanently:
                    video.unlink()
                    self.history.append(HistoryItem(action="delete_permanent", video=video, index=self.index))
                else:
                    destination = trash_destination(video, self.settings)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(video), str(destination))
                    self.history.append(HistoryItem(action="delete", video=video, index=self.index, destination=destination))
                self.videos.pop(self.index)
                self.deleted += 1
            elif action == "open":
                open_video(self.videos[self.index])
            elif action == "reveal":
                reveal_video(self.videos[self.index])
            self.save_session()

        return self.current()

    def undo_last(self) -> None:
        if not self.history:
            return

        item = self.history.pop()
        if item.action == "keep":
            self.index = item.index
            self.kept = max(0, self.kept - 1)
        elif item.action == "skip":
            if self.videos and self.videos[-1] == item.video:
                self.videos.insert(item.index, self.videos.pop())
            else:
                self.videos.insert(item.index, item.video)
                self.videos = [path for i, path in enumerate(self.videos) if path != item.video or i == item.index]
            self.index = item.index
            self.skipped = max(0, self.skipped - 1)
        elif item.action == "delete" and item.destination:
            restored = item.video
            if item.destination.exists():
                item.video.parent.mkdir(parents=True, exist_ok=True)
                restored = unique_destination(item.video)
                shutil.move(str(item.destination), str(restored))
            self.videos.insert(min(item.index, len(self.videos)), restored)
            self.index = item.index
            self.deleted = max(0, self.deleted - 1)
        elif item.action == "delete_permanent":
            self.index = min(item.index, len(self.videos))
            self.deleted = max(0, self.deleted - 1)

    def metadata_for(self, video: Path) -> dict[str, object]:
        stat = video.stat()
        cache_key = (str(video), stat.st_size, int(stat.st_mtime))
        cached = self.metadata_cache.get(cache_key)
        if cached is not None:
            return cached

        metadata = probe_video_metadata(video, stat)
        self.metadata_cache[cache_key] = metadata
        return metadata

    def metadata_for_index(self, video_index: int) -> dict[str, object]:
        with self.lock:
            if video_index >= len(self.videos):
                raise FileNotFoundError("No video at that index.")
            video = self.videos[video_index]
        return self.metadata_for(video)

    def upcoming_videos(self) -> list[dict[str, object]]:
        upcoming: list[dict[str, object]] = []
        end = min(len(self.videos), self.index + self.settings.preload_count + 1)
        for video_index in range(self.index + 1, end):
            video = self.videos[video_index]
            stat = video.stat()
            upcoming.append({
                "index": video_index,
                "key": video_key(video, stat),
                "name": video.name,
                "path": str(video),
                "metadata": basic_video_metadata(video, stat),
            })
        return upcoming

    def duration_for(self, video: Path, stat: os.stat_result) -> float:
        cache_key = (str(video), stat.st_size, int(stat.st_mtime))
        with self.duration_lock:
            cached = self.duration_cache.get(cache_key)
            if cached is not None:
                return cached

            duration = probe_duration(video)
            self.duration_cache[cache_key] = duration
            return duration

    def thumb_path(self, sample: int, video_index: Optional[int] = None) -> Path:
        with self.lock:
            target_index = self.index if video_index is None else video_index
            if target_index >= len(self.videos):
                raise FileNotFoundError("No current video.")
            video = self.videos[target_index]

        stat = video.stat()
        cache_name = video_key(video, stat)
        out_dir = self.temp_dir / cache_name
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / f"thumb-{sample:02d}.jpg"
        if output.exists():
            return output

        duration = self.duration_for(video, stat)
        times = sample_times(duration, self.settings.samples)
        seconds = times[min(sample, len(times) - 1)]
        extract_jpeg_frame(
            video,
            output,
            seconds,
            self.settings.thumb_width,
            self.settings.jpeg_quality,
            self.settings.accurate_seek,
            self.settings.ffmpeg_threads,
        )
        return output

    def load_session(self) -> None:
        if not self.settings.session_file or not self.settings.session_file.exists():
            return
        try:
            data = json.loads(self.settings.session_file.read_text())
            remaining = [Path(path) for path in data.get("remaining", [])]
            existing = {str(path): path for path in self.videos}
            restored = [existing[str(path)] for path in remaining if str(path) in existing and path.exists()]
            if restored:
                self.videos = restored
                self.index = 0
                self.initial_total = int(data.get("initialTotal", self.initial_total))
                self.kept = int(data.get("kept", 0))
                self.deleted = int(data.get("deleted", 0))
                self.skipped = int(data.get("skipped", 0))
        except Exception:
            return

    def save_session(self) -> None:
        if not self.settings.session_file:
            return
        data = {
            "directory": str(self.settings.directory),
            "initialTotal": self.initial_total,
            "remaining": [str(path) for path in self.videos[self.index:]],
            "kept": self.kept,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "updated": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            self.settings.session_file.write_text(json.dumps(data, indent=2))
        except Exception:
            return


def extract_jpeg_frame(
    video: Path,
    output: Path,
    seconds: float,
    width: int,
    jpeg_quality: int,
    accurate_seek: bool,
    threads: int,
) -> None:
    try:
        run_ffmpeg_frame_extract(video, output, seconds, width, jpeg_quality, accurate_seek, threads)
    except subprocess.CalledProcessError as exc:
        if accurate_seek:
            raise RuntimeError(clean_ffmpeg_error(exc)) from exc
        run_ffmpeg_frame_extract(video, output, seconds, width, jpeg_quality, True, threads)


def run_ffmpeg_frame_extract(
    video: Path,
    output: Path,
    seconds: float,
    width: int,
    jpeg_quality: int,
    accurate_seek: bool,
    threads: int,
) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        str(threads),
    ]
    if not accurate_seek:
        command.extend(["-noaccurate_seek", "-skip_frame", "nokey"])
    command.extend([
        "-ss",
        f"{seconds:.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-1",
        "-q:v",
        str(jpeg_quality),
        "-y",
        str(output),
    ])
    subprocess.run(command, capture_output=True, text=True, check=True)


def clean_ffmpeg_error(exc: subprocess.CalledProcessError) -> str:
    stderr = (exc.stderr or "").strip()
    if stderr:
        stderr = stderr.encode("ascii", "replace").decode("ascii")
        return f"ffmpeg failed: {stderr}"
    return f"ffmpeg failed with exit code {exc.returncode}"


def open_video(video: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(video)])
    elif os.name == "nt":
        os.startfile(video)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(video)])


def reveal_video(video: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(video)])
    elif os.name == "nt":
        subprocess.Popen(["explorer", "/select,", str(video)])
    else:
        subprocess.Popen(["xdg-open", str(video.parent)])


def make_handler(state: WebReviewState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_html(review_page())
            elif parsed.path == "/state":
                self._send_json(state.current())
            elif parsed.path == "/metadata":
                query = parse_qs(parsed.query)
                video_index = int(query.get("video", [str(state.index)])[0])
                try:
                    self._send_json(state.metadata_for_index(video_index))
                except Exception:
                    self._send_json({"fields": []})
            elif parsed.path == "/thumb":
                query = parse_qs(parsed.query)
                sample = int(query.get("sample", ["0"])[0])
                video_index_text = query.get("video", [None])[0]
                video_index = int(video_index_text) if video_index_text is not None else None
                try:
                    path = state.thumb_path(sample, video_index)
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Cache-Control", "private, max-age=3600")
                    self.end_headers()
                    self.wfile.write(path.read_bytes())
                except Exception:
                    self._send_svg_error()
            elif parsed.path == "/action":
                query = parse_qs(parsed.query)
                action = query.get("name", [""])[0]
                if action not in {"keep", "skip", "delete", "open", "reveal", "undo"}:
                    self.send_error(HTTPStatus.BAD_REQUEST, "Unknown action.")
                    return
                self._send_json(state.action(action))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _send_html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_json(self, data: dict[str, object]) -> None:
            encoded = json.dumps(data).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_svg_error(self) -> None:
            body = """<svg xmlns="http://www.w3.org/2000/svg" width="260" height="150">
<rect width="100%" height="100%" fill="#1f1f1f"/>
<text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle"
      fill="#f4f4f4" font-family="Arial, sans-serif" font-size="14">
Thumbnail unavailable
</text>
</svg>"""
            encoded = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return Handler


def review_page() -> str:
    return Path(__file__).with_name("review.html").read_text(encoding="utf-8")


def run_web_reviewer(settings: Settings, videos: list[Path]) -> None:
    with tempfile.TemporaryDirectory(prefix="video-review-web-") as temp:
        state = WebReviewState(videos, settings, Path(temp))
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
        host, port = server.server_address
        url = f"http://{host}:{port}/"
        print(f"Opening video reviewer: {url}")
        print("Leave this Terminal window open while reviewing. Press Ctrl+C here when done.")
        webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def main() -> None:
    settings = parse_args()
    print(f"Scanning for videos in {settings.directory}...")
    videos = find_videos(settings.directory, settings.recursive, settings.trash_dir, settings.sort_mode)
    print(f"Found {len(videos)} video(s).")
    run_web_reviewer(settings, videos)

if __name__ == "__main__":
    main()
