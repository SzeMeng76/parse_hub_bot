from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.user import User


class UserRepo:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_all(self) -> list[User]:
        result = await self._session.scalars(select(User))
        return list(result.all())

    async def get_by_telegram_user_id(self, telegram_user_id: int) -> User | None:
        user = await self._session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        return user

    async def ensure_by_telegram_user_id(
        self,
        telegram_user_id: int,
    ) -> User:
        user = await self.get_by_telegram_user_id(telegram_user_id)
        if user is not None:
            return user

        user = User(telegram_user_id=telegram_user_id)
        self._session.add(user)
        await self._session.flush()
        return user
