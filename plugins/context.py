from pyrogram.enums import ChatType
from pyrogram.types import InlineQuery, Message

from services import (
    AnySettingsTarget,
    ChannelSettingsTarget,
    ForumTopicMemberSettingsTarget,
    GroupMemberSettingsTarget,
    UserSettingsTarget,
)


def get_config_target(update: Message | InlineQuery) -> AnySettingsTarget:
    if isinstance(update, InlineQuery):
        return UserSettingsTarget(telegram_user_id=update.from_user.id)

    if update.chat and update.chat.id is not None and update.chat.type == ChatType.CHANNEL:
        return ChannelSettingsTarget(telegram_chat_id=update.chat.id)

    if not update.from_user:
        raise ValueError("缺少配置目标用户")

    thread_id = update.message_thread_id
    if update.chat and update.chat.id is not None and thread_id:
        return ForumTopicMemberSettingsTarget(
            telegram_chat_id=update.chat.id,
            telegram_thread_id=thread_id,
            telegram_user_id=update.from_user.id,
        )

    if update.chat and update.chat.id is not None and update.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return GroupMemberSettingsTarget(telegram_chat_id=update.chat.id, telegram_user_id=update.from_user.id)

    return UserSettingsTarget(telegram_user_id=update.from_user.id)
