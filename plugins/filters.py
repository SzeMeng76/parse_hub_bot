from typing import Any

from pyrogram import filters
from pyrogram.types import InlineQuery, Message, User

from db.session import get_session
from services import ParseService, SettingsService, TelegramSettingsTarget


def platform_filter(use_user_config: bool = False) -> filters.Filter:
    """
    平台过滤器
    Args:
        use_user_config: 使用用户配置

    Returns:

    """

    async def func(flt: Any, __: Any, update: Message | InlineQuery) -> bool:
        t: str | None = None
        match update:
            case Message():
                t = update.caption or update.text
            case InlineQuery():
                t = update.query

        try:
            platform = ParseService().parser.get_platform(t)
        except Exception:
            return False

        if not platform:
            return False

        if update.from_user:
            if flt.use_user_config is False:
                return True

            async with get_session() as session:
                user_config = await SettingsService(session).get_config(
                    TelegramSettingsTarget.user(update.from_user.id)
                )
                if platform.id in user_config.disabled_platforms:
                    return False
                return True
        else:
            return True

    return filters.create(func, use_user_config=use_user_config)


async def _via_me(_: Any, __: Any, update: Message) -> bool:
    return bool(update.via_bot and update.via_bot.is_self)


via_me_filter = filters.create(_via_me)


async def _forwarded_from_bot(_: Any, __: Any, update: Message) -> bool:
    sender_user: User | None = getattr(update.forward_origin, "sender_user", None)
    if sender_user and sender_user.is_bot:
        return True
    return False


forwarded_from_bot_filter = filters.create(_forwarded_from_bot)
