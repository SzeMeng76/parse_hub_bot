from dataclasses import dataclass
from typing import Annotated, Literal, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from db.models.settings import Scope

CURRENT_SCHEMA_VERSION = 3

DefaultMode = Literal["preview", "raw", "zip"]


@dataclass(frozen=True)
class ScopePolicy:
    allowed_scopes: frozenset[Scope]


ALL_SCOPES = frozenset(Scope)


@dataclass(frozen=True, slots=True, kw_only=True)
class SettingsTarget:
    scope: Scope
    user_id: int | None = None
    chat_id: int | None = None
    forum_topic_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, Scope):
            raise TypeError("scope 必须是 Scope 枚举值。")

        expected_fields = {
            Scope.USER: {"user_id"},
            Scope.GROUP: {"chat_id"},
            Scope.GROUP_MEMBER: {"user_id", "chat_id"},
            Scope.FORUM_TOPIC: {"forum_topic_id"},
            Scope.FORUM_TOPIC_MEMBER: {"user_id", "forum_topic_id"},
            Scope.CHANNEL: {"chat_id"},
        }[self.scope]
        values = {
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "forum_topic_id": self.forum_topic_id,
        }
        actual_fields = {name for name, value in values.items() if value is not None}
        if actual_fields != expected_fields:
            raise ValueError(f"{self.scope.value} scope 必须且只能指定 {sorted(expected_fields)}。")

        for name, value in values.items():
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"{name} 必须是正整数。")

    @classmethod
    def user(cls, user_id: int) -> Self:
        return cls(scope=Scope.USER, user_id=user_id)

    @classmethod
    def group(cls, chat_id: int) -> Self:
        return cls(scope=Scope.GROUP, chat_id=chat_id)

    @classmethod
    def group_member(cls, chat_id: int, user_id: int) -> Self:
        return cls(scope=Scope.GROUP_MEMBER, chat_id=chat_id, user_id=user_id)

    @classmethod
    def forum_topic(cls, forum_topic_id: int) -> Self:
        return cls(scope=Scope.FORUM_TOPIC, forum_topic_id=forum_topic_id)

    @classmethod
    def forum_topic_member(cls, forum_topic_id: int, user_id: int) -> Self:
        return cls(scope=Scope.FORUM_TOPIC_MEMBER, forum_topic_id=forum_topic_id, user_id=user_id)

    @classmethod
    def channel(cls, chat_id: int) -> Self:
        return cls(scope=Scope.CHANNEL, chat_id=chat_id)


class Config(BaseModel):
    model_config = ConfigDict(extra="allow")  # 保留旧字段

    schema_version: Annotated[int, ScopePolicy(ALL_SCOPES), Field(ge=1, frozen=True)] = CURRENT_SCHEMA_VERSION
    default_mode: Annotated[DefaultMode, ScopePolicy(ALL_SCOPES), Field(description="默认解析模式")] = "preview"
    auto_delete_url: Annotated[bool, ScopePolicy(ALL_SCOPES), Field(description="解析完成后自动删除分享链接")] = False
    disabled_platforms: Annotated[list[str], ScopePolicy(ALL_SCOPES), Field(description="禁用的平台")] = []
    enable_inline_raw_url: Annotated[
        bool, ScopePolicy(frozenset([Scope.USER])), Field(description="启用内联模式的发送原始 URL 功能")
    ] = False
    keep_error_log: Annotated[bool, ScopePolicy(ALL_SCOPES), Field(description="保留错误日志")] = False
    hide_source: Annotated[bool, ScopePolicy(ALL_SCOPES), Field(description="隐藏底部 Source 超链接")] = False
    noprogress: Annotated[bool, ScopePolicy(ALL_SCOPES), Field(description="禁用解析进度, 直接发送结果")] = False

    def __str__(self) -> str:
        return self.model_dump_json(indent=4, ensure_ascii=True)


def validate_patch_scope(scope: Scope, fields: set[str]) -> None:
    for name in fields:
        field = Config.model_fields.get(name)
        if field is None or name == "schema_version":
            raise ValueError(f"不支持更新配置字段 {name!r}。")

        policy = next((metadata for metadata in field.metadata if isinstance(metadata, ScopePolicy)), None)
        if policy is None or scope not in policy.allowed_scopes:
            raise ValueError(f"配置字段 {name!r} 不支持 {scope.value} scope。")


class ConfigPatch(TypedDict, total=False):
    default_mode: DefaultMode
    auto_delete_url: bool
    disabled_platforms: list[str]
    enable_inline_raw_url: bool
    keep_error_log: bool
    hide_source: bool
    noprogress: bool


DEFAULT_CONFIG = Config()
