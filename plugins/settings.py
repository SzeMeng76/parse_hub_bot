from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from itertools import batched
from typing import Literal, Self, cast

from easy_ai18n import PostLocaleSelector, PreLocaleSelector
from parsehub.types import Platform
from pyrogram import Client, filters
from pyrogram.enums import ButtonStyle, ChatMemberStatus, ChatType
from pyrogram.types import CallbackQuery, Message
from pyrogram.types import InlineKeyboardButton as Ikb
from pyrogram.types import InlineKeyboardMarkup as Ikm

from db import get_session
from db.models.settings import SettingsScope
from i18n import t_
from plugins.helpers import format_label, parse_channel_ref
from repo.settings import DefaultMode, SettingsConfig
from repo.settings.schema import ConfigMetadata
from services import SettingsService, UserService
from services.settings import (
    AnySettingsTarget,
    ChannelSettingsTarget,
    ForumTopicMemberSettingsTarget,
    ForumTopicSettingsTarget,
    GroupMemberSettingsTarget,
    GroupSettingsTarget,
    UserSettingsTarget,
)

# /cfg callback 动作短码：t=选择配置目标, m=切换默认模式, b=切换布尔开关, p=切换平台, o=打开页面。
CfgAction = Literal["t", "m", "b", "p", "o"]
# /cfg 配置目标 scope 短码：u=用户, g=群组, gm=群组成员, ft=话题, ftm=话题成员, c=频道。
CfgScopeCode = Literal["u", "g", "gm", "ft", "ftm", "c"]
# /cfg 页面短码：main=配置主页, platform=平台管理页。
CfgPage = Literal["main", "platform"]


@dataclass(frozen=True, slots=True)
class CfgCQData:
    action: CfgAction
    value: str
    scope: CfgScopeCode | None = None
    channel_id: int | None = None

    @classmethod
    def parse(cls, data: str | bytes) -> Self:
        parts = str(data).split("|")
        _, action, value, *rest = parts
        scope = cast(CfgScopeCode, rest[0]) if rest else None
        channel_id = int(rest[1]) if len(rest) > 1 else None
        return cls(action=cast(CfgAction, action), value=value, scope=scope, channel_id=channel_id)

    def unparse(self) -> str:
        parts = ["cfg", self.action, self.value]
        if self.scope:
            parts.append(self.scope)
        if self.channel_id:
            parts.append(str(self.channel_id))
        return "|".join(parts)


@dataclass(frozen=True, slots=True)
class CfgTargetOption:
    label: str
    scope: CfgScopeCode
    target: AnySettingsTarget


@dataclass(frozen=True, slots=True)
class SettingsViewModel:
    config: SettingsConfig
    target: AnySettingsTarget | None
    allowed_fields: frozenset[str]
    target_label: str | None
    target_options: tuple[CfgTargetOption, ...] = ()


@dataclass(frozen=True, slots=True)
class BoolSwitchDTO:
    field: str
    code: str
    label: PostLocaleSelector
    get_value: Callable[[SettingsConfig], bool]
    patch: Callable[[SettingsService, AnySettingsTarget, bool], Awaitable[SettingsConfig]]


BOOL_SWITCHES = (
    BoolSwitchDTO(
        field="enable_inline_raw_url",
        code="ir",
        label=t_("内联发送原始 URL 选项"),
        get_value=lambda config: config.enable_inline_raw_url,
        patch=lambda settings, target, value: settings.patch_config(target, enable_inline_raw_url=value),
    ),
    BoolSwitchDTO(
        field="keep_error_log",
        code="el",
        label=t_("保留错误日志"),
        get_value=lambda config: config.keep_error_log,
        patch=lambda settings, target, value: settings.patch_config(target, keep_error_log=value),
    ),
    BoolSwitchDTO(
        field="hide_source",
        code="hs",
        label=t_("隐藏底部 Source 超链接"),
        get_value=lambda config: config.hide_source,
        patch=lambda settings, target, value: settings.patch_config(target, hide_source=value),
    ),
    BoolSwitchDTO(
        field="noprogress",
        code="np",
        label=t_("隐藏解析进度"),
        get_value=lambda config: config.noprogress,
        patch=lambda settings, target, value: settings.patch_config(target, noprogress=value),
    ),
    BoolSwitchDTO(
        field="auto_delete_url",
        code="ad",
        label=t_("自动删除链接消息"),
        get_value=lambda config: config.auto_delete_url,
        patch=lambda settings, target, value: settings.patch_config(target, auto_delete_url=value),
    ),
)

BOOL_SWITCH_MAP = {switch.code: switch for switch in BOOL_SWITCHES}


@Client.on_message(filters.command("cfg"))
async def cfg(client: Client, msg: Message) -> None:
    if not msg.from_user:
        return

    async with get_session() as session:
        lang = await UserService(session).get_lang(msg.from_user.id)
        _t = t_[lang]

    channel_ref = msg.command[1] if msg.command and msg.command[1:] else None
    if channel_ref:
        target = await resolve_channel_target(client, msg, _t, channel_ref)
        if not target:
            return
        async with get_session() as session:
            vm = await build_cfg_vm(SettingsService(session), _t, target, "频道配置")
        await msg.reply(format_label(_t("配置面板 - 频道配置")), reply_markup=build_cfg_markup(_t, vm))
        return

    options = await build_cfg_target_options(client, msg, _t)
    if not options:
        return

    if len(options) == 1:
        option = options[0]
        async with get_session() as session:
            vm = await build_cfg_vm(SettingsService(session), _t, option.target, option.label)
        await msg.reply(format_label(_t(f"配置面板 - {option.label}")), reply_markup=build_cfg_markup(_t, vm))
        return

    vm = SettingsViewModel(
        config=SettingsConfig(),
        target=None,
        allowed_fields=frozenset(),
        target_label=None,
        target_options=tuple(options),
    )
    await msg.reply(format_label(_t("选择配置目标")), reply_markup=build_cfg_markup(_t, vm))


@Client.on_callback_query(filters.regex(r"^cfg"))
async def cfg_callback(client: Client, cq: CallbackQuery) -> None:
    if not cq.data or not cq.message:
        return

    data = CfgCQData.parse(cq.data)
    async with get_session() as session:
        lang = await UserService(session).get_lang(cq.from_user.id)
        _t = t_[lang]
    target = restore_cfg_target(cq, data)
    if not target:
        await cq.answer(_t("无法识别配置目标"), show_alert=True)
        return

    if not await ensure_cfg_permission(client, cq, _t, target):
        return

    async with get_session() as session:
        settings = SettingsService(session)
        match data.action:
            case "t":
                pass
            case "m":
                selected = DefaultMode(data.value)
                if not await ensure_cfg_field(cq, _t, target, "default_mode"):
                    return
                await settings.patch_config(target, default_mode=selected)
            case "b":
                switch = BOOL_SWITCH_MAP.get(data.value)
                if not switch:
                    await cq.answer(_t("未知配置项"), show_alert=True)
                    return
                if not await ensure_cfg_field(cq, _t, target, switch.field):
                    return
                config = await settings.get_config(target)
                await switch.patch(settings, target, not switch.get_value(config))
            case "p":
                if not await ensure_cfg_field(cq, _t, target, "disabled_platforms"):
                    return
                config = await settings.get_config(target)
                disabled_platforms = config.disabled_platforms.copy()
                if data.value in disabled_platforms:
                    disabled_platforms.remove(data.value)
                else:
                    disabled_platforms.append(data.value)
                await settings.patch_config(target, disabled_platforms=disabled_platforms)
            case "o":
                pass

        match target:
            case UserSettingsTarget():
                label = _t("个人配置")
            case GroupSettingsTarget():
                label = _t("群组配置")
            case GroupMemberSettingsTarget():
                label = _t("群组个人配置")
            case ForumTopicSettingsTarget():
                label = _t("话题配置")
            case ForumTopicMemberSettingsTarget():
                label = _t("话题个人配置")
            case ChannelSettingsTarget():
                label = _t("频道配置")

        vm = await build_cfg_vm(settings, _t, target, label)

    page: CfgPage = "platform" if (data.action == "p" or data.value == "p") else "main"
    await cq.message.edit(
        format_label(t_[lang](f"配置面板 - {vm.target_label}")), reply_markup=build_cfg_markup(_t, vm, page)
    )


def build_cfg_markup(_t: PreLocaleSelector, vm: SettingsViewModel, page: CfgPage = "main") -> Ikm:
    if vm.target is None:
        return build_cfg_target_markup(vm)
    if page == "platform":
        return build_cfg_platform_markup(vm)
    return build_cfg_main_markup(_t, vm)


def build_cfg_target_markup(vm: SettingsViewModel) -> Ikm:
    return Ikm(
        [
            [
                Ikb(
                    option.label,
                    callback_data=CfgCQData(
                        action="t",
                        value="main",
                        scope=option.scope,
                        channel_id=get_cfg_channel_id(option.target),
                    ).unparse(),
                )
            ]
            for option in vm.target_options
        ]
    )


def build_cfg_main_markup(_t: PreLocaleSelector, vm: SettingsViewModel) -> Ikm:
    rows: list[list[Ikb]] = []
    mode_map = {
        DefaultMode.PREVIEW: _t("预览"),
        DefaultMode.RAW: _t("原始"),
        DefaultMode.ZIP: _t("压缩"),
    }
    if "default_mode" in vm.allowed_fields:
        rows.append([Ikb(_t("默认解析模式"), callback_data="placeholder", style=ButtonStyle.PRIMARY)])
        rows.append(
            [
                Ikb(
                    label,
                    callback_data=cfg_callback_data("m", value.value, vm.target),
                    style=ButtonStyle.PRIMARY if value == vm.config.default_mode else ButtonStyle.DEFAULT,
                )
                for value in DefaultMode
                for label in [mode_map[value]]
            ]
        )

    if "disabled_platforms" in vm.allowed_fields:
        rows.append([Ikb(_t("平台管理"), callback_data=cfg_callback_data("o", "p", vm.target))])

    switches = [switch for switch in BOOL_SWITCHES if switch.field in vm.allowed_fields]
    buttons = [
        Ikb(
            switch.label[_t.locale],
            callback_data=cfg_callback_data("b", switch.code, vm.target),
            style=reply_bool_style(switch.get_value(vm.config)),
        )
        for switch in switches
    ]
    rows.extend([list(row) for row in batched(buttons, 2)])

    return Ikm(rows)


def build_cfg_platform_markup(vm: SettingsViewModel) -> Ikm:
    ikbs = [
        Ikb(
            p.display_name,
            callback_data=cfg_callback_data("p", p.id, vm.target),
            style=ButtonStyle.DANGER if p.id in vm.config.disabled_platforms else ButtonStyle.SUCCESS,
        )
        for p in list(Platform)
    ]
    rows = [list(row) for row in batched(ikbs, 2)]
    rows.append([Ikb("返回", callback_data=cfg_callback_data("o", "main", vm.target))])
    return Ikm(rows)


def reply_bool_style(v: bool) -> ButtonStyle:
    return ButtonStyle.SUCCESS if v else ButtonStyle.DANGER


def cfg_callback_data(action: CfgAction, value: str, target: AnySettingsTarget | None) -> str:
    scope: CfgScopeCode | None
    match target:
        case UserSettingsTarget():
            scope = "u"
        case GroupSettingsTarget():
            scope = "g"
        case GroupMemberSettingsTarget():
            scope = "gm"
        case ForumTopicSettingsTarget():
            scope = "ft"
        case ForumTopicMemberSettingsTarget():
            scope = "ftm"
        case ChannelSettingsTarget():
            scope = "c"
        case None:
            scope = None

    return CfgCQData(
        action=action,
        value=value,
        scope=scope,
        channel_id=get_cfg_channel_id(target),
    ).unparse()


async def build_cfg_vm(
    settings: SettingsService, _t: PreLocaleSelector, target: AnySettingsTarget, target_label: str
) -> SettingsViewModel:
    return SettingsViewModel(
        config=await settings.get_config(target),
        target=target,
        allowed_fields=get_allowed_fields(target.scope),
        target_label=_t(target_label),
    )


async def build_cfg_target_options(client: Client, msg: Message, _t: PreLocaleSelector) -> list[CfgTargetOption]:
    if not msg.from_user or not msg.chat:
        return []

    chat_id = msg.chat.id
    if chat_id is None:
        return []
    thread_id = msg.message_thread_id
    if msg.chat.type == ChatType.PRIVATE:
        return [CfgTargetOption(_t("个人配置"), "u", UserSettingsTarget(telegram_user_id=msg.from_user.id))]

    if msg.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP, ChatType.FORUM]:
        await msg.reply(format_label(_t("请在私聊, 群组, 话题发送或使用 `/cfg <频道用户名/链接/id>` 进行配置")))
        return []

    is_admin = await is_chat_admin(client, chat_id, msg.from_user.id)
    if thread_id:
        if is_admin:
            return [
                CfgTargetOption(_t("个人配置"), "u", UserSettingsTarget(telegram_user_id=msg.from_user.id)),
                CfgTargetOption(
                    _t("话题个人配置"),
                    "ftm",
                    ForumTopicMemberSettingsTarget(
                        telegram_chat_id=chat_id,
                        telegram_thread_id=thread_id,
                        telegram_user_id=msg.from_user.id,
                    ),
                ),
                CfgTargetOption(
                    _t("话题配置"),
                    "ft",
                    ForumTopicSettingsTarget(telegram_chat_id=chat_id, telegram_thread_id=thread_id),
                ),
            ]
        return [
            CfgTargetOption(
                _t("话题个人配置"),
                "ftm",
                ForumTopicMemberSettingsTarget(
                    telegram_chat_id=chat_id,
                    telegram_thread_id=thread_id,
                    telegram_user_id=msg.from_user.id,
                ),
            )
        ]

    if is_admin:
        return [
            CfgTargetOption(_t("个人配置"), "u", UserSettingsTarget(telegram_user_id=msg.from_user.id)),
            CfgTargetOption(
                _t("群组个人配置"),
                "gm",
                GroupMemberSettingsTarget(telegram_chat_id=chat_id, telegram_user_id=msg.from_user.id),
            ),
            CfgTargetOption(_t("群组配置"), "g", GroupSettingsTarget(telegram_chat_id=chat_id)),
        ]
    return [
        CfgTargetOption(
            _t("群组个人配置"),
            "gm",
            GroupMemberSettingsTarget(telegram_chat_id=chat_id, telegram_user_id=msg.from_user.id),
        )
    ]


def restore_cfg_target(cq: CallbackQuery, data: CfgCQData) -> AnySettingsTarget | None:
    if not data.scope or not cq.message or not cq.message.chat:
        return None

    chat_id = cq.message.chat.id
    if chat_id is None:
        return None
    match data.scope:
        case "u":
            return UserSettingsTarget(telegram_user_id=cq.from_user.id)
        case "g":
            return GroupSettingsTarget(telegram_chat_id=chat_id)
        case "gm":
            return GroupMemberSettingsTarget(telegram_chat_id=chat_id, telegram_user_id=cq.from_user.id)
        case "ft":
            thread_id = cq.message.message_thread_id
            if not thread_id:
                return None
            return ForumTopicSettingsTarget(telegram_chat_id=chat_id, telegram_thread_id=thread_id)
        case "ftm":
            thread_id = cq.message.message_thread_id
            if not thread_id:
                return None
            return ForumTopicMemberSettingsTarget(
                telegram_chat_id=chat_id,
                telegram_thread_id=thread_id,
                telegram_user_id=cq.from_user.id,
            )
        case "c":
            if data.channel_id is None:
                return None
            return ChannelSettingsTarget(telegram_chat_id=data.channel_id)


def get_cfg_channel_id(target: AnySettingsTarget | None) -> int | None:
    if isinstance(target, ChannelSettingsTarget):
        return target.telegram_chat_id
    return None


def get_allowed_fields(scope: SettingsScope) -> frozenset[str]:
    fields = []
    for field_name, field in SettingsConfig.model_fields.items():
        for metadata in field.metadata:
            if isinstance(metadata, ConfigMetadata) and scope in metadata.scopes:
                fields.append(field_name)
                break
    return frozenset(fields)


async def ensure_cfg_field(
    cq: CallbackQuery, _t: PreLocaleSelector, target: AnySettingsTarget, field_name: str
) -> bool:
    if field_name not in get_allowed_fields(target.scope):
        await cq.answer(_t("不支持的配置项"), show_alert=True)
        return False
    return True


async def ensure_cfg_permission(
    client: Client, cq: CallbackQuery, _t: PreLocaleSelector, target: AnySettingsTarget
) -> bool:
    match target:
        case UserSettingsTarget():
            return True
        case GroupMemberSettingsTarget() | ForumTopicMemberSettingsTarget():
            return True
        case GroupSettingsTarget(telegram_chat_id=chat_id) | ForumTopicSettingsTarget(telegram_chat_id=chat_id):
            if await is_chat_admin(client, chat_id, cq.from_user.id):
                return True
            await cq.answer(_t("你不是该聊天的管理员, 无权修改配置"), show_alert=True)
            return False
        case ChannelSettingsTarget(telegram_chat_id=chat_id):
            if await is_chat_owner(client, chat_id, cq.from_user.id):
                return True
            await cq.answer(_t("你不是该频道的拥有者, 无权修改频道配置"), show_alert=True)
            return False


async def resolve_channel_target(
    client: Client,
    msg: Message,
    _t: PreLocaleSelector,
    channel_ref: str,
) -> ChannelSettingsTarget | None:
    if not msg.from_user:
        return None

    try:
        channel = parse_channel_ref(channel_ref)
    except Exception as e:
        await msg.reply(str(e))
        return None
    try:
        chat = await client.get_chat(channel)
    except Exception:
        await msg.reply(_t("Bot 未加入该频道, 请先将 Bot 加入频道后再配置"))
        return None

    if chat.id is None:
        await msg.reply(_t("Bot 未加入该频道, 请先将 Bot 加入频道后再配置"))
        return None

    if not await is_chat_owner(client, chat.id, msg.from_user.id):
        await msg.reply(_t("你不是该频道的拥有者, 无权修改频道配置"))
        return None

    return ChannelSettingsTarget(telegram_chat_id=chat.id)


async def is_chat_admin(client: Client, chat_id: int | str, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}


async def is_chat_owner(client: Client, chat_id: int | str, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status in {ChatMemberStatus.OWNER}
