import random

from .._shared.stickers import STICKERS
from wxbot_framework.commands import command_text


class Plugin:
    def on_message(self, message, ctx):
        if not message.is_group:
            return
        parts = command_text(message.text).split(maxsplit=1)
        if not parts or parts[0] != "/表情":
            return
        name = parts[1] if len(parts) == 2 else random.choice(tuple(STICKERS))
        path = STICKERS.get(name)
        if path is None:
            ctx.reply(message, "有这几张：" + "、".join(STICKERS))
        elif ctx.reply_image(message, path):
            ctx.logger.info("群表情包已发送：%s", name)
        else:
            ctx.reply(message, "这张没发出去")
