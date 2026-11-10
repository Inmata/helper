"""群聊内置表情包目录。"""
from pathlib import Path
from types import MappingProxyType

_DIRECTORY = Path(__file__).with_name("stickers")
STICKERS = MappingProxyType({
    "吃瓜": _DIRECTORY / "popcorn.png",
    "笑死": _DIRECTORY / "laugh.png",
    "懂了": _DIRECTORY / "got-it.png",
})
