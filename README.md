# Video Review Tool

A local browser-based video review tool for quickly deciding which videos to keep, delete, or skip.

The script scans a directory, builds a contact sheet of thumbnails from each video, and lets you move through the list with keyboard shortcuts. Deleted videos are moved into a visible `Video Review Trash` folder by default, so accidental deletes are recoverable.

## Requirements

- macOS, Linux, or Windows
- Python 3.9 or newer
- `ffmpeg` and `ffprobe` available on your `PATH`

On macOS with Homebrew:

```bash
brew install ffmpeg
```

## Quick Start

```bash
python3 video_review.py "/path/to/videos"
```

Example:

```bash
python3 video_review.py "/Volumes/rootFolder/Media/youtube-dl/32_Russian"
```

The script starts a local web server, opens the review page in your browser, and prints the local URL in Terminal. Leave that Terminal window open while reviewing.

## Keyboard Shortcuts

| Key | Action |
| --- | --- |
| `k`, `Space`, `Right Arrow` | Keep current video and move on |
| `d`, `Delete`, `Backspace` | Move current video to trash and move on |
| `s` | Skip current video for later |
| `o` | Open current video in your default video player |
| `f` | Reveal current video in Finder/File Explorer |
| `u` | Undo the last keep/delete/skip |
| `Esc` | Close enlarged thumbnail preview |

Click any thumbnail to view it larger.

## What Gets Deleted?

By default, videos are **not permanently deleted**. They are moved into:

```text
Video Review Trash
```

inside the top-level folder you passed to the script.

If the deleted video came from a subfolder, the same folder structure is preserved in the trash folder.

Example:

```text
/Videos/Subfolder/movie.mp4
```

moves to:

```text
/Videos/Video Review Trash/Subfolder/movie.mp4
```

This makes restoring files easier and avoids filename collisions.

## Common Options

Review recursively, which is the default:

```bash
python3 video_review.py "/path/to/videos"
```

Only review videos directly in the top folder:

```bash
python3 video_review.py "/path/to/videos" --no-recursive
```

Change the number of thumbnails shown per video:

```bash
python3 video_review.py "/path/to/videos" --samples 16
```

Change thumbnail size:

```bash
python3 video_review.py "/path/to/videos" --thumb-width 320
```

Review in a different order:

```bash
python3 video_review.py "/path/to/videos" --sort date
python3 video_review.py "/path/to/videos" --sort size
python3 video_review.py "/path/to/videos" --sort random
```

The default sort is `name`, which means alphabetical by full path/name.

Resume an old review where you know how many videos were left:

```bash
python3 video_review.py "/path/to/videos" --resume-with-left 237
```

Disable automatic session resume:

```bash
python3 video_review.py "/path/to/videos" --no-resume
```

Change how many upcoming videos are preloaded:

```bash
python3 video_review.py "/path/to/videos" --preload 4
```

Reduce background work:

```bash
python3 video_review.py "/path/to/videos" --preload 1
```

Try more CPU threads per thumbnail extraction:

```bash
python3 video_review.py "/path/to/videos" --ffmpeg-threads 2
```

The default is `1` because the browser already asks for many thumbnails in parallel. More threads per ffmpeg process can be slower on NAS/network drives.

## Full Option List

```bash
python3 video_review.py --help
```

## Session Resume

The tool writes a small session file in the reviewed directory:

```text
.video_review_session.json
```

It stores:

- the remaining videos
- how many were kept
- how many were deleted
- how many were skipped
- the original starting total

When you restart the tool on the same directory, it resumes automatically unless you pass `--no-resume`.

## Under The Hood

### Local Browser App

The script uses Python's built-in `ThreadingHTTPServer` to start a private local web server on `127.0.0.1`. No external web service is involved. The browser UI is served from the Python script itself.

The main routes are:

| Route | Purpose |
| --- | --- |
| `/` | Serves the browser interface |
| `/state` | Returns the current video, counters, and basic metadata |
| `/metadata?video=N` | Loads detailed metadata for a video |
| `/thumb?video=N&sample=M` | Generates or serves one thumbnail |
| `/action?name=keep` | Applies keep/delete/skip/open/reveal/undo actions |

### Scanning Videos

The script walks the input directory with `os.walk()` when recursive mode is enabled. It skips the `Video Review Trash` folder so previously deleted videos do not appear again.

Supported extensions include common formats such as:

```text
.mp4, .mov, .mkv, .avi, .webm, .wmv, .m4v, .mpeg, .mpg
```

### Thumbnail Generation

Thumbnails are generated with `ffmpeg`.

For each video, the script:

1. Gets the video duration with `ffprobe`.
2. Chooses evenly spaced timestamps across the video.
3. Extracts one frame per timestamp.
4. Scales the frame to the requested thumbnail width.
5. Saves the thumbnail as a temporary JPEG.

The temporary thumbnail cache lives in the system temp folder and is cleaned up when the script exits.

### Fast Seeking

By default, the script uses faster keyframe-oriented seeking:

```text
-noaccurate_seek -skip_frame nokey
```

This is usually faster on large videos and NAS drives because ffmpeg can avoid decoding as much video. If that fast method fails for a particular thumbnail, the script retries that thumbnail with accurate seeking.

If you want slower but more exact thumbnails, use:

```bash
--accurate-seek
```

### Preloading

The browser preloads thumbnails for the next videos. The default is:

```bash
--preload 2
```

Preloading starts after the current video's thumbnails finish loading, so the next-video work does not compete with the current screen.

Thumbnail URLs include a stable per-video key based on filename, size, and modification time. This lets the browser reuse preloaded thumbnails instead of re-requesting them.

### Optimistic UI

Keep, delete, and skip are optimistic in the browser. That means the page switches to the next video immediately, before the server has fully finished saving the session or moving the file.

When the server confirms the action, the browser updates counters and metadata without rebuilding the thumbnail grid if it is already showing the same video. This avoids the flicker that can happen when the UI unnecessarily redraws an already-loaded page.

### Metadata

The first screen for a video shows lightweight file metadata immediately:

- file size
- created date
- modified date

Detailed metadata loads in the background using `ffprobe`, including:

- duration
- resolution
- video codec/profile
- pixel format
- frame rate
- bitrate
- audio codec
- embedded creation date, when available

This keeps navigation snappy while still showing useful technical information once it is ready.

### Undo

Undo works for:

- keep
- skip
- delete-to-trash

For delete-to-trash, undo moves the file back from `Video Review Trash` to its original location. If a file already exists at the original path, the restored file receives a unique name.

Permanent delete cannot restore the file because the file is actually removed.

### NAS And Network Drives

The tool is designed to behave reasonably on NAS folders:

- avoids expensive per-file path resolving during scans
- caches duration and metadata
- uses browser caching for thumbnails
- supports fast keyframe seeking
- supports configurable preload count
- supports configurable ffmpeg thread count

For NAS folders, good starting settings are usually:

```bash
python3 video_review.py "/path/to/videos" --preload 2 --ffmpeg-threads 1
```

If the NAS feels overloaded, try:

```bash
python3 video_review.py "/path/to/videos" --preload 1
```

If your CPU has plenty of room and the NAS is keeping up, experiment with:

```bash
python3 video_review.py "/path/to/videos" --ffmpeg-threads 2
```

## Troubleshooting

### `ffmpeg is required`

Install ffmpeg:

```bash
brew install ffmpeg
```

### GitHub Or Browser Did Not Open

The script prints a local URL like:

```text
http://127.0.0.1:54321/
```

Copy that URL into your browser.

### Thumbnails Are Missing

Some videos may fail on a specific frame. The tool retries with accurate seeking and then shows a placeholder if the thumbnail still cannot be extracted.

Try:

```bash
python3 video_review.py "/path/to/videos" --accurate-seek
```

### It Feels Slow

Try fewer samples:

```bash
python3 video_review.py "/path/to/videos" --samples 8
```

Try smaller thumbnails:

```bash
python3 video_review.py "/path/to/videos" --thumb-width 200
```

Try less preloading:

```bash
python3 video_review.py "/path/to/videos" --preload 1
```

## License

See [LICENSE](LICENSE).
