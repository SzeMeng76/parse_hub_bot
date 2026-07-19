from dataclasses import dataclass
from typing import Self, Unpack

from sqlalchemy.ext.asyncio import AsyncSession

from db.models.chat import ChatType
from db.models.settings import Scope
from repo.chat import ChatRepo
from repo.forum_topic import ForumTopicRepo
from repo.settings import Config, ConfigPatch, SettingsRepo, SettingsTarget
from repo.user import UserRepo


@dataclass(frozen=True, slots=True, kw_only=True)
class TelegramSettingsTarget:
    scope: Scope
    telegram_user_id: int | None = None
    telegram_chat_id: int | None = None
    telegram_thread_id: int | None = None
    chat_type: ChatType | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, Scope):
            raise TypeError("scope 必须是 Scope 枚举值。")

        expected_fields = {
            Scope.USER: {"telegram_user_id"},
            Scope.GROUP: {"telegram_chat_id", "chat_type"},
            Scope.GROUP_MEMBER: {"telegram_user_id", "telegram_chat_id", "chat_type"},
            Scope.FORUM_TOPIC: {"telegram_chat_id", "telegram_thread_id", "chat_type"},
            Scope.FORUM_TOPIC_MEMBER: {
                "telegram_user_id",
                "telegram_chat_id",
                "telegram_thread_id",
                "chat_type",
            },
            Scope.CHANNEL: {"telegram_chat_id", "chat_type"},
        }[self.scope]
        values = {
            "telegram_user_id": self.telegram_user_id,
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_thread_id": self.telegram_thread_id,
            "chat_type": self.chat_type,
        }
        actual_fields = {name for name, value in values.items() if value is not None}
        if actual_fields != expected_fields:
            raise ValueError(f"{self.scope.value} scope 必须且只能指定 {sorted(expected_fields)}。")

        if self.telegram_user_id is not None:
            self._validate_positive_id("telegram_user_id", self.telegram_user_id)
        if self.telegram_chat_id is not None:
            if (
                isinstance(self.telegram_chat_id, bool)
                or not isinstance(self.telegram_chat_id, int)
                or self.telegram_chat_id == 0
            ):
                raise ValueError("telegram_chat_id 必须是非零整数。")
        if self.telegram_thread_id is not None:
            self._validate_positive_id("telegram_thread_id", self.telegram_thread_id)

        if self.chat_type is not None and not isinstance(self.chat_type, ChatType):
            raise TypeError("chat_type 必须是 ChatType 枚举值。")
        if self.scope in {Scope.GROUP, Scope.GROUP_MEMBER} and self.chat_type not in {
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        }:
            raise ValueError("group scope 仅支持 GROUP 或 SUPERGROUP chat_type。")
        if self.scope in {Scope.FORUM_TOPIC, Scope.FORUM_TOPIC_MEMBER} and self.chat_type is not ChatType.SUPERGROUP:
            raise ValueError("forum topic scope 仅支持 SUPERGROUP chat_type。")
        if self.scope is Scope.CHANNEL and self.chat_type is not ChatType.CHANNEL:
            raise ValueError("channel scope 仅支持 CHANNEL chat_type。")

    @classmethod
    def user(cls, telegram_user_id: int) -> Self:
        return cls(scope=Scope.USER, telegram_user_id=telegram_user_id)

    @classmethod
    def group(cls, telegram_chat_id: int, chat_type: ChatType) -> Self:
        return cls(scope=Scope.GROUP, telegram_chat_id=telegram_chat_id, chat_type=chat_type)

    @classmethod
    def group_member(cls, telegram_user_id: int, telegram_chat_id: int, chat_type: ChatType) -> Self:
        return cls(
            scope=Scope.GROUP_MEMBER,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            chat_type=chat_type,
        )

    @classmethod
    def forum_topic(cls, telegram_chat_id: int, telegram_thread_id: int, chat_type: ChatType) -> Self:
        return cls(
            scope=Scope.FORUM_TOPIC,
            telegram_chat_id=telegram_chat_id,
            telegram_thread_id=telegram_thread_id,
            chat_type=chat_type,
        )

    @classmethod
    def forum_topic_member(
        cls,
        telegram_user_id: int,
        telegram_chat_id: int,
        telegram_thread_id: int,
        chat_type: ChatType,
    ) -> Self:
        return cls(
            scope=Scope.FORUM_TOPIC_MEMBER,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            telegram_thread_id=telegram_thread_id,
            chat_type=chat_type,
        )

    @classmethod
    def channel(cls, telegram_chat_id: int, chat_type: ChatType) -> Self:
        return cls(scope=Scope.CHANNEL, telegram_chat_id=telegram_chat_id, chat_type=chat_type)

    @staticmethod
    def _validate_positive_id(name: str, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} 必须是正整数。")


class SettingsService:
    def __init__(self, session: AsyncSession) -> None:
        self.users = UserRepo(session)
        self.chats = ChatRepo(session)
        self.forum_topics = ForumTopicRepo(session)
        self.settings = SettingsRepo(session)

    async def get_config(self, target: TelegramSettingsTarget) -> Config:
        return await self.settings.get_config(await self._resolve(target))

    async def save_config(self, target: TelegramSettingsTarget, config: Config) -> Config:
        settings = await self.settings.save_config(await self._resolve(target), config)
        return self.settings.config_from_raw(settings.config)

    async def patch_config(self, target: TelegramSettingsTarget, **kwargs: Unpack[ConfigPatch]) -> Config:
        return await self.settings.patch_config(await self._resolve(target), **kwargs)

    async def _resolve(self, target: TelegramSettingsTarget) -> SettingsTarget:
        if target.scope is Scope.USER:
            telegram_user_id = target.telegram_user_id
            assert telegram_user_id is not None
            user = await self.users.ensure_by_telegram_user_id(telegram_user_id)
            return SettingsTarget.user(user.id)

        if target.scope in {Scope.GROUP, Scope.CHANNEL}:
            telegram_chat_id = target.telegram_chat_id
            chat_type = target.chat_type
            assert telegram_chat_id is not None and chat_type is not None
            chat = await self.chats.ensure_by_telegram_chat_id(telegram_chat_id, chat_type)
            return SettingsTarget.group(chat.id) if target.scope is Scope.GROUP else SettingsTarget.channel(chat.id)

        if target.scope is Scope.GROUP_MEMBER:
            telegram_user_id = target.telegram_user_id
            telegram_chat_id = target.telegram_chat_id
            chat_type = target.chat_type
            assert telegram_user_id is not None and telegram_chat_id is not None and chat_type is not None
            user = await self.users.ensure_by_telegram_user_id(telegram_user_id)
            chat = await self.chats.ensure_by_telegram_chat_id(telegram_chat_id, chat_type)
            return SettingsTarget.group_member(chat.id, user.id)

        telegram_chat_id = target.telegram_chat_id
        telegram_thread_id = target.telegram_thread_id
        chat_type = target.chat_type
        assert telegram_chat_id is not None and telegram_thread_id is not None and chat_type is not None
        topic = await self.forum_topics.ensure_by_telegram_chat_id_and_thread_id(
            telegram_chat_id,
            telegram_thread_id,
            chat_type,
        )
        if target.scope is Scope.FORUM_TOPIC:
            return SettingsTarget.forum_topic(topic.id)

        telegram_user_id = target.telegram_user_id
        assert telegram_user_id is not None
        user = await self.users.ensure_by_telegram_user_id(telegram_user_id)
        return SettingsTarget.forum_topic_member(topic.id, user.id)
