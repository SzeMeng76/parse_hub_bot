from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.chat import Chat, ChatType


class ChatRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[Chat]:
        result = await self._session.scalars(select(Chat))
        return list(result.all())

    async def get_by_telegram_chat_id(self, telegram_chat_id: int) -> Chat | None:
        chat = await self._session.scalar(select(Chat).where(Chat.telegram_chat_id == telegram_chat_id))
        return chat

    async def ensure_by_telegram_chat_id(self, telegram_chat_id: int, chat_type: ChatType) -> Chat:
        if isinstance(telegram_chat_id, bool) or not isinstance(telegram_chat_id, int) or telegram_chat_id == 0:
            raise ValueError("telegram_chat_id 必须是非零整数。")
        if not isinstance(chat_type, ChatType):
            raise TypeError("chat_type 必须是 ChatType 枚举值。")

        chat = await self.get_by_telegram_chat_id(telegram_chat_id)
        if chat is not None:
            return chat

        chat = Chat(telegram_chat_id=telegram_chat_id, type=chat_type)
        self._session.add(chat)
        await self._session.flush()
        return chat
