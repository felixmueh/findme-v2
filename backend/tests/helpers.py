from pathlib import Path
import subprocess

from PIL import Image
from fastapi.testclient import TestClient


def write_image(path: Path, *, size: tuple[int, int] = (32, 24), alpha: int = 255) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "RGB" if path.suffix.lower() in {".jpg", ".jpeg"} else "RGBA"
    color = (30, 120, 90) if mode == "RGB" else (30, 120, 90, alpha)
    image = Image.new(mode, size, color)
    image.save(path)


def write_video(path: Path, *, size: tuple[int, int] = (640, 360)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=red:s={size[0]}x{size[1]}:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )


def write_audio(path: Path, *, duration_seconds: float = 2.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration_seconds}",
            "-c:a",
            "libmp3lame" if path.suffix.lower() == ".mp3" else "aac",
            str(path),
        ],
        check=True,
    )


def search_query(client: TestClient, params: dict[str, object] | None = None):
    return client.post("/api/search/query", json=params or {})


def directory_query(client: TestClient, params: dict[str, object] | None = None):
    body = dict(params or {})
    body["result_shape"] = "directories"
    return client.post("/api/search/query", json=body)
