"""群聊内置表情包目录。"""
from pathlib import Path

_DIRECTORY = Path(__file__).with_name("stickers")


def sticker_catalog():
    return {path.stem: path for path in sorted(_DIRECTORY.iterdir())
            if path.suffix.lower() in (".png", ".jpg", ".jpeg") and path.is_file()}
