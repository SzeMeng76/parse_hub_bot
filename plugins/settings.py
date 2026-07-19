from dataclasses import dataclass
from itertools import batched
from typing import Self, cast

from parsehub.types import Platform
from pyrogram import Client, filters
from pyrogram.enums import ButtonStyle
from pyrogram.types import CallbackQuery, Message
from pyrogram.types import InlineKeyboardButton as Ikb
from pyrogram.types import InlineKeyboardMarkup as Ikm

from db import get_session
from i18n import LANG_MAP, t_
from repo.settings import Config, DefaultMode
from services import SettingsService, TelegramSettingsTarget, UserService


@dataclass
class CQData:
    key: str
    """键放在最前面, 可用 filters.regex(r"^key") 过滤"""
    value: str
    """值"""
    uid: int
    """user id"""

    @classmethod
    def parse(cls, data: str | bytes) -> Self:
        key, value, uid = str(data).split(",")
        return cls(key=key, value=value, uid=int(uid))

    def unparse(self) -> str:
        return f"{self.key},{self.value},{self.uid}"

    def __str__(self) -> str:
        return self.unparse()

    def __repr__(self) -> str:
        return self.__str__()


@Client.on_message(filters.command("lang"))
async def select_lang(_: Client, msg: Message) -> None:
    if not msg.from_user:
        return

    async with get_session() as session:
        lang = await UserService(session, msg.from_user.id).get_lang()

    ikbs = [
        Ikb(
            v,
            callback_data=CQData(key="lang", value=k, uid=msg.from_user.id).unparse(),
            style=ButtonStyle.PRIMARY if k == lang else ButtonStyle.DEFAULT,
        )
        for k, v in LANG_MAP.items()
    ]

    reply_markup = Ikm([ikbs[i : i + 2] for i in range(0, len(ikbs), 2)])
    await msg.reply_text("**▎选择语言 / Select Language**", reply_markup=reply_markup)


@Client.on_callback_query(filters.regex(r"^lang"))
async def selected_lang(_: Client, cq: CallbackQuery) -> None:
    if not cq.data:
        return

    cqdata = CQData.parse(cq.data)
    if cq.from_user.id != cqdata.uid:
        async with get_session() as session:
            lang = await UserService(session, cq.from_user.id).get_lang()
        await cq.answer(t_[lang]("这不是你的操作"), show_alert=True)
        return

    selected = cqdata.value
    async with get_session() as session:
        user = await UserService(session, cq.from_user.id).set_language(selected)

    await cq.message.edit(t_[user.language_code](f"**▎已切换为: {LANG_MAP[selected]}**"))


MODE_MAP = {
    "preview": t_("预览"),
    "raw": t_("原始"),
    "zip": t_("压缩"),
}


@Client.on_message(filters.command("mode"))
async def select_mode(_: Client, msg: Message) -> None:
    """设置默认解析模式"""
    if not msg.from_user:
        return

    async with get_session() as session:
        user = await UserService(session, msg.from_user.id).ensure_user()
        user_config = await SettingsService(session).get_config(TelegramSettingsTarget.user(msg.from_user.id))
        lang = user.language_code

    ikbs = [
        Ikb(
            v[lang],
            callback_data=CQData(uid=msg.from_user.id, key="mode", value=k).unparse(),
            style=ButtonStyle.PRIMARY if k == user_config.default_mode else ButtonStyle.DEFAULT,
        )
        for k, v in MODE_MAP.items()
    ]
    reply_markup = Ikm([ikbs])
    await msg.reply_text(t_[lang]("**▎选择默认解析模式**"), reply_markup=reply_markup)


@Client.on_callback_query(filters.regex(r"^mode"))
async def selected_mode(_: Client, cq: CallbackQuery) -> None:
    if not cq.data:
        return

    cqdata = CQData.parse(cq.data)
    if cq.from_user.id != cqdata.uid:
        async with get_session() as session:
            lang = await UserService(session, cq.from_user.id).get_lang()
        await cq.answer(t_[lang]("这不是你的操作"), show_alert=True)
        return

    selected = cast(DefaultMode, cqdata.value)
    async with get_session() as session:
        lang = await UserService(session, cq.from_user.id).get_lang()
        settings = SettingsService(session)
        await settings.patch_config(target=TelegramSettingsTarget.user(cq.from_user.id), default_mode=selected)

    await cq.message.edit(t_[lang](f"**▎已切换为: {MODE_MAP[selected]}**"))


@Client.on_message(filters.command("switch_auto_delete"))
async def switch_auto_delete_url(_: Client, msg: Message) -> None:
    if not msg.from_user:
        return

    async with get_session() as session:
        lang = await UserService(session, msg.from_user.id).get_lang()
        settings = SettingsService(session)
        config = await settings.get_config(TelegramSettingsTarget.user(msg.from_user.id))
        config = await settings.patch_config(
            TelegramSettingsTarget.user(msg.from_user.id), auto_delete_url=not config.auto_delete_url
        )
    _t = t_[lang]
    status = _t("启用") if config.auto_delete_url else _t("禁用")
    await msg.reply_text(
        f"{_t(f'** ▎已 {status} 自动删除分享链接消息 **')}\n"
        f"{_t('▎**群内使用需要授予 Bot 删除消息权限**') if config.auto_delete_url else ''}"
    )


@Client.on_message(filters.command("switch_platform"))
async def switch_platform(_: Client, msg: Message) -> None:
    if not msg.from_user:
        return

    async with get_session() as session:
        lang = await UserService(session, msg.from_user.id).get_lang()
        config = await SettingsService(session).get_config(TelegramSettingsTarget.user(msg.from_user.id))

    ikbs = [
        Ikb(
            p.display_name,
            callback_data=CQData(key="switch_platform", value=p.id, uid=msg.from_user.id).unparse(),
            style=ButtonStyle.DANGER if p.id in config.disabled_platforms else ButtonStyle.SUCCESS,
        )
        for p in list(Platform)
    ]
    reply_markup = Ikm([ikbs[i : i + 2] for i in range(0, len(ikbs), 2)])
    await msg.reply_text(t_[lang]("**▎启用 / 禁用 平台解析**"), reply_markup=reply_markup)


@Client.on_callback_query(filters.regex(r"^switch_platform"))
async def switch_platform_callback(_: Client, cq: CallbackQuery) -> None:
    if not cq.data:
        return

    cqdata = CQData.parse(cq.data)
    if cq.from_user.id != cqdata.uid:
        async with get_session() as session:
            lang = await UserService(session, cq.from_user.id).get_lang()
        await cq.answer(t_[lang]("这不是你的操作"), show_alert=True)
        return

    selected = cqdata.value
    async with get_session() as session:
        settings = SettingsService(session)
        config = await settings.get_config(TelegramSettingsTarget.user(cq.from_user.id))

        disabled_platforms = config.disabled_platforms.copy()
        if selected in disabled_platforms:
            disabled_platforms.remove(selected)
        else:
            disabled_platforms.append(selected)
        config = await settings.patch_config(
            TelegramSettingsTarget.user(cq.from_user.id), disabled_platforms=disabled_platforms
        )

    ikbs = [
        Ikb(
            p.display_name,
            callback_data=CQData(key="switch_platform", value=p.id, uid=cqdata.uid).unparse(),
            style=ButtonStyle.DANGER if p.id in config.disabled_platforms else ButtonStyle.SUCCESS,
        )
        for p in list(Platform)
    ]
    reply_markup = Ikm([list(i) for i in batched(ikbs, 2)])
    await cq.message.edit_reply_markup(reply_markup)


def build_switches_button(uid: int, lang: str, config: Config) -> Ikm:
    key = "switches"
    _t = t_[lang]

    def reply_bool_style(v: bool) -> ButtonStyle:
        return ButtonStyle.SUCCESS if v else ButtonStyle.DANGER

    def cq(v: str) -> str:
        return CQData(key=key, value=v, uid=uid).unparse()

    return Ikm(
        [
            [
                Ikb(
                    _t("内联发送原始 URL 选项"),
                    callback_data=cq("enable_inline_raw_url"),
                    style=reply_bool_style(config.enable_inline_raw_url),
                ),
                Ikb(
                    _t("保留错误日志"),
                    callback_data=cq("keep_error_log"),
                    style=reply_bool_style(config.keep_error_log),
                ),
            ],
            [
                Ikb(
                    _t("隐藏底部 Source 超链接"),
                    callback_data=cq("hide_source"),
                    style=reply_bool_style(config.hide_source),
                ),
                Ikb(
                    _t("隐藏解析进度"),
                    callback_data=cq("noprogress"),
                    style=reply_bool_style(config.noprogress),
                ),
            ],
        ]
    )


@Client.on_message(filters.command("switches"))
async def switches(_: Client, msg: Message) -> None:
    if not msg.from_user:
        return
    async with get_session() as session:
        user = await UserService(session, msg.from_user.id).ensure_user()
        config = await SettingsService(session).get_config(TelegramSettingsTarget.user(msg.from_user.id))
    reply_markup = build_switches_button(msg.from_user.id, user.language_code, config)
    await msg.reply(t_[user.language_code]("**▎功能开关**"), reply_markup=reply_markup)


@Client.on_callback_query(filters.regex(r"^switches"))
async def switches_callback(_: Client, cq: CallbackQuery) -> None:
    if not cq.data:
        return

    cqdata = CQData.parse(cq.data)
    if cq.from_user.id != cqdata.uid:
        async with get_session() as session:
            lang = await UserService(session, cq.from_user.id).get_lang()
        await cq.answer(t_[lang]("这不是你的操作"), show_alert=True)
        return

    selected = cqdata.value
    async with get_session() as session:
        lang = await UserService(session, cq.from_user.id).get_lang()
        settings = SettingsService(session)
        tst = TelegramSettingsTarget.user(cq.from_user.id)
        config = await settings.get_config(tst)

        match selected:
            case "enable_inline_raw_url":
                config = await settings.patch_config(tst, enable_inline_raw_url=not config.enable_inline_raw_url)
            case "keep_error_log":
                config = await settings.patch_config(tst, keep_error_log=not config.keep_error_log)
            case "hide_source":
                config = await settings.patch_config(tst, hide_source=not config.hide_source)
            case "noprogress":
                config = await settings.patch_config(tst, noprogress=not config.noprogress)

    await cq.message.edit_reply_markup(reply_markup=build_switches_button(cq.from_user.id, lang, config))
