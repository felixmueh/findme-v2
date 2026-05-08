from pathlib import Path

from PIL import Image


def write_image(path: Path, *, size: tuple[int, int] = (32, 24), alpha: int = 255) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "RGB" if path.suffix.lower() in {".jpg", ".jpeg"} else "RGBA"
    color = (30, 120, 90) if mode == "RGB" else (30, 120, 90, alpha)
    image = Image.new(mode, size, color)
    image.save(path)
