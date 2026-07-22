"""plugins 共用的工具函数和数据类"""

import re
from urllib.parse import urlsplit

from easy_ai18n.core import LocaleContent
from markdown import markdown
from parsehub import ParseHub, Platform
from parsehub.types import AnyParseResult, RichTextParseResult
from pyrogram import Client
from pyrogram.enums import ChatType
from pyrogram.types import InlineQuery, Message

from i18n import t_
from log import logger
from services import (
    AnySettingsTarget,
    ChannelSettingsTarget,
    ForumTopicMemberSettingsTarget,
    GroupMemberSettingsTarget,
    UserSettingsTarget,
)
from utils.converter import clean_article_html
from utils.ph import Telegraph

logger = logger.bind(name="Helpers")

COMMANDS = {
    "start": t_("开始"),
    "jx": t_("解析"),
    "raw": t_("不处理媒体, 发送原始文件"),
    "zip": t_("不处理媒体, 保存解析结果, 发送压缩包"),
    "jxjx": t_("绕过缓存解析"),
    "lang": t_("选择语言"),
    "cfg": t_("配置"),
}


def build_start_text() -> LocaleContent:
    return t_(
        f"**发送分享链接以进行解析**\n\n"
        f"**支持的平台:**\n"
        f"<blockquote expandable>{get_supported_platforms()}</blockquote>\n\n"
        f"**命令列表:**\n"
        f"<blockquote expandable>"
        f"/jx <链接> - 解析并发送媒体\n"
        f"/raw <链接> - 不处理媒体, 发送原始文件\n"
        f"/zip <链接> - 不处理媒体, 保存解析结果, 发送压缩包\n"
        f"/jxjx <链接> - 绕过缓存解析并发送媒体\n"
        f"/lang - 选择语言\n"
        f"/cfg - 配置\n"
        f"/cfg <频道用户名/链接/id> - 频道配置\n"
        f"</blockquote>\n\n"
        f"**开源地址: [GitHub](https://github.com/z-mio/parse_hub_bot)**"
    )


def build_caption(parse_result: AnyParseResult, telegraph_url: str | None = None, *, hide_source: bool = False) -> str:
    return build_caption_by_str(
        parse_result.title, parse_result.content, parse_result.raw_url, telegraph_url, hide_source=hide_source
    )


def build_caption_by_str(
    title: str | None,
    content: str | None,
    raw_url: str,
    telegraph_url: str | None = None,
    *,
    hide_source: bool = False,
) -> str:
    """构建消息正文：标题 + 内容 + 来源链接"""
    title, content = title or "", content or ""

    if telegraph_url:
        label = (title or content[:15]).replace("\n", " ") or "无标题"
        body = f"**[{label}]({telegraph_url})**"
    else:
        parts = []
        if title:
            parts.append(f"**{title}**")
        if content:
            parts.append(content)
        body = format_text("\n\n".join(parts) or "**无标题**")
    if hide_source:
        return body
    return f"{body}\n\n{format_label(f"<a href='{raw_url}'>Source</a>")}"


def format_text(text: str) -> str:
    """格式化输出内容, 限制长度, 添加折叠块样式"""
    text = text.strip()
    if len(text) > 500 or len(text.splitlines()) > 10:
        if len(text) > 1000:
            text = text[:900] + "......"
        return f"<blockquote expandable>{text}</blockquote>"
    else:
        return text


async def create_telegraph_page(html_content: str, cli: Client, parse_result: AnyParseResult) -> str:
    """创建 Telegraph 页面，返回页面 URL"""
    logger.debug(f"创建 Telegraph 页面: title={parse_result.title}")
    me = await cli.get_me()
    page = await Telegraph().create_page(
        parse_result.title or "无标题",
        html_content=html_content,
        author_name=f"{me.full_name} | @{me.username}",
        author_url=parse_result.raw_url,
    )
    logger.debug(f"Telegraph 页面已创建: {page.url}")
    return page.url


async def create_richtext_telegraph(cli: Client, parse_result: RichTextParseResult) -> str:
    """将富文本解析结果转换为 Telegraph 页面，返回页面 URL"""
    logger.debug(f"富文本转 Telegraph: platform={parse_result.platform}, md_len={len(parse_result.markdown_content)}")
    md = parse_result.markdown_content
    match parse_result.platform:
        case Platform.WEIXIN:
            md = md.replace("mmbiz.qpic.cn", "qpic.cn.in/mmbiz.qpic.cn")
        case Platform.COOLAPK:
            md = md.replace("image.coolapk.com", "qpic.cn.in/image.coolapk.com")
    html = clean_article_html(markdown(md))
    return await create_telegraph_page(html, cli, parse_result)


def get_supported_platforms() -> str:
    text: list[str] = []
    for i in ParseHub().get_platforms():
        text.append(f"**{i['name']}** __({'__, __'.join(i['supported_types'])})__")
    text.sort(reverse=True)
    return "\n".join(text)


def format_label(text: str) -> str:
    return f"<b>▎{text}</b>"


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


def parse_channel_ref(value: str) -> int | str:
    """解析频道 ID、用户名或 Telegram 链接，忽略 URL 参数、片段和消息 ID。"""
    channel_id_re = re.compile(r"-100\d{1,19}\Z")
    internal_channel_id_re = re.compile(r"[1-9]\d{0,19}\Z")
    username_re = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}\Z")
    telegram_link_hosts = frozenset({"t.me", "telegram.me"})

    value = value.strip()
    if not value:
        raise ValueError("频道引用不能为空")

    if channel_id_re.fullmatch(value):
        return int(value)

    url = urlsplit(value if "://" in value else f"https://{value}")
    if url.netloc.lower() in telegram_link_hosts:
        if url.scheme not in {"http", "https"}:
            raise ValueError("仅支持 HTTP 或 HTTPS Telegram 链接")

        path_parts = tuple(part for part in url.path.split("/") if part)
        match path_parts:
            case ("c", internal_channel_id) | ("c", internal_channel_id, _):
                if not internal_channel_id_re.fullmatch(internal_channel_id):
                    raise ValueError("无效的 /c/ 频道链接")
                return int(f"-100{internal_channel_id}")

            case (username,):
                if username_re.fullmatch(username):
                    return f"@{username}"
                raise ValueError("频道用户名格式无效")

            case _:
                raise ValueError("Telegram 链接必须包含用户名或 /c/ 频道 ID")

    if "://" in value:
        raise ValueError("仅支持 t.me 或 telegram.me 链接")

    username = value.removeprefix("@")
    if not username_re.fullmatch(username):
        raise ValueError("频道用户名格式无效")

    return f"@{username}"
