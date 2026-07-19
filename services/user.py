from sqlalchemy.ext.asyncio import AsyncSession

from db.models.user import User
from repo.user import UserRepo


class UserService:
    def __init__(self, session: AsyncSession, telegram_user_id: int) -> None:
        self.telegram_user_id = telegram_user_id
        self.users = UserRepo(session)

    async def ensure_user(self) -> User:
        return await self.users.ensure_by_telegram_user_id(self.telegram_user_id)

    async def get_user(self) -> User:
        return await self.ensure_user()

    async def get_lang(self) -> str:
        return (await self.ensure_user()).language_code

    async def set_language(self, language_code: str) -> User:
        user = await self.ensure_user()
        user.language_code = language_code
        return user
