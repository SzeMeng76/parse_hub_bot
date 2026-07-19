from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.settings import Scope, Settings
from repo.settings.migrate import migrate
from repo.settings.schema import Config, SettingsTarget, validate_patch_scope


class SettingsRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def config_from_raw(raw: dict[str, Any] | None) -> Config:
        config = Config.model_validate(raw) if raw is not None else None
        return migrate(config)

    @staticmethod
    def _coerce_target(target: SettingsTarget | int) -> SettingsTarget:
        if isinstance(target, SettingsTarget):
            return target
        if isinstance(target, bool) or not isinstance(target, int):
            raise TypeError("target 必须是 SettingsTarget 或用户内部 ID。")
        return SettingsTarget.user(target)

    @staticmethod
    def _values_for(target: SettingsTarget) -> dict[str, int | None | Scope]:
        return {
            "scope": target.scope,
            "user_id": target.user_id,
            "chat_id": target.chat_id,
            "forum_topic_id": target.forum_topic_id,
        }

    @staticmethod
    def _config_data(config: Config) -> dict[str, Any]:
        return config.model_dump(mode="json")

    async def get(self, target: SettingsTarget | int) -> Settings | None:
        target = self._coerce_target(target)
        settings = await self._session.scalar(select(Settings).filter_by(**self._values_for(target)))
        return settings

    async def get_or_create(self, target: SettingsTarget | int) -> Settings:
        target = self._coerce_target(target)
        settings = await self.get(target)
        if settings is not None:
            return settings

        settings = Settings(
            **self._values_for(target),
            config=self._config_data(Config()),
        )
        self._session.add(settings)
        await self._session.flush()
        return settings

    async def get_config(self, target: SettingsTarget | int) -> Config:
        target = self._coerce_target(target)
        settings = await self.get_or_create(target)
        config = self.config_from_raw(settings.config)
        config_data = self._config_data(config)

        if settings.config != config_data:
            await self._save_config(target, config)

        return config

    async def _save_config(self, target: SettingsTarget, config: Config) -> Settings:
        settings = await self.get_or_create(target)
        settings.config = self._config_data(config)
        await self._session.flush()
        return settings

    async def save_config(self, target: SettingsTarget | int, config: Config) -> Settings:
        target = self._coerce_target(target)
        config = self.config_from_raw(self._config_data(config))
        current = await self.get_config(target)
        changed_fields = {
            name for name, value in config.model_dump().items() if current.model_dump().get(name) != value
        }
        validate_patch_scope(target.scope, changed_fields)
        return await self._save_config(target, config)

    async def patch_config(self, target: SettingsTarget | int, **kwargs: Any) -> Config:
        target = self._coerce_target(target)
        validate_patch_scope(target.scope, set(kwargs))
        current = await self.get_config(target)
        config = Config.model_validate(current.model_dump() | kwargs)
        await self._save_config(target, config)
        return config

    async def get_by_user_ids(self, user_ids: list[int]) -> list[Settings]:
        if not user_ids:
            return []
        result = await self._session.scalars(
            select(Settings).where(
                Settings.scope == Scope.USER,
                Settings.user_id.in_(user_ids),
            )
        )
        return list(result)

    async def save_raw(self, target: SettingsTarget | int, data: dict[str, Any]) -> Settings:
        target = self._coerce_target(target)
        config = self.config_from_raw(data)
        return await self.save_config(target, config)
