from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.chat import Chat, ChatType
from db.models.forum_topic import ForumTopic
from repo.chat import ChatRepo


class ForumTopicRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._chats = ChatRepo(session)

    async def get_all(self) -> list[ForumTopic]:
        result = await self._session.scalars(select(ForumTopic))
        return list(result.all())

    async def get_by_chat_id_and_thread_id(self, chat_id: int, telegram_thread_id: int) -> ForumTopic | None:
        topic = await self._session.scalar(
            select(ForumTopic).where(
                ForumTopic.chat_id == chat_id,
                ForumTopic.telegram_thread_id == telegram_thread_id,
            )
        )
        return topic

    async def get_by_telegram_chat_id_and_thread_id(
        self,
        telegram_chat_id: int,
        telegram_thread_id: int,
    ) -> ForumTopic | None:
        topic = await self._session.scalar(
            select(ForumTopic)
            .join(Chat)
            .where(
                Chat.telegram_chat_id == telegram_chat_id,
                ForumTopic.telegram_thread_id == telegram_thread_id,
            )
        )
        return topic

    async def ensure_by_telegram_chat_id_and_thread_id(
        self,
        telegram_chat_id: int,
        telegram_thread_id: int,
        chat_type: ChatType,
    ) -> ForumTopic:
        if isinstance(telegram_thread_id, bool) or not isinstance(telegram_thread_id, int) or telegram_thread_id <= 0:
            raise ValueError("telegram_thread_id 必须是正整数。")

        chat = await self._chats.ensure_by_telegram_chat_id(telegram_chat_id, chat_type)
        topic = await self.get_by_chat_id_and_thread_id(chat.id, telegram_thread_id)
        if topic is not None:
            return topic

        topic = ForumTopic(chat_id=chat.id, telegram_thread_id=telegram_thread_id)
        self._session.add(topic)
        await self._session.flush()
        return topic
