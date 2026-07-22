import asyncio
from typing import Any

from easy_ai18n import PreLocaleSelector
from parsehub.types import AniRef, PostType
from pyrogram import Client, enums, filters
from pyrogram.types import LinkPreviewOptions, Message

from core import bs
from db import get_session
from i18n import t_
from log import logger
from plugins.context import get_config_target
from plugins.filters import forwarded_from_bot_filter, platform_filter, via_me_filter
from plugins.helpers import build_caption, create_richtext_telegraph, format_label
from plugins.parse.reporters import MessageStatusReporter
from plugins.parse.sender import build_gif_button, send_cached, send_media, send_raw, send_with_rate_limit, send_zip
from repo.settings import ParseMode, SettingsConfig
from services import CacheEntry, CacheParseResult, ParsePipeline, ParseService, SettingsService, UserService
from services.cache import parse_cache, persistent_cache
from utils.helpers import to_list, with_request_id
from utils.rate_limit import ParseRateLimitExceeded, parse_rate_limit

logger = logger.bind(name="Parse")
SKIP_DOWNLOAD_THRESHOLD = 0
GIF_ONLY_SKIP_DOWNLOAD_COUNT_THRESHOLD = 5


@Client.on_message(
    filters.command(["jx", "jxjx", "raw", "zip"])
    | ((filters.text | filters.caption) & ~via_me_filter & platform_filter(True) & ~forwarded_from_bot_filter)
)
async def jx(cli: Client, msg: Message) -> None:
    bypass_cache = False
    lang = None
    mode = ParseMode.PREVIEW

    async with get_session() as session:
        if msg.from_user:
            lang = await UserService(session).get_lang(msg.from_user.id)
        config = await SettingsService(session).get_config(get_config_target(msg))
        mode = config.default_mode

    _t = t_[lang]

    if msg.command:
        match msg.command[0]:
            case "raw":
                mode = ParseMode.RAW
            case "jx":
                mode = ParseMode.PREVIEW
            case "jxjx":
                mode = ParseMode.PREVIEW
                bypass_cache = True
            case "zip":
                mode = ParseMode.ZIP

        text = " ".join(msg.command[1:]) if msg.command[1:] else ""
        if not text and msg.reply_to_message:
            text = msg.reply_to_message.text or msg.reply_to_message.caption or ""
        if not text:
            await msg.reply_text(format_label(_t("请加上链接或回复一条消息")))
            return
    else:
        text = msg.text or msg.caption or ""

    tokens = text.strip().split()
    urls = list({i for i in tokens if ParseService().parser.get_platform(i)})[:10]

    if not urls:
        await msg.reply_text(format_label(_t("不支持的平台")))
        return

    tasks = [
        _handle_parse_request(
            cli,
            msg,
            url=url,
            mode=mode,
            delete_share_url_msg=config.auto_delete_url,
            bypass_cache=bypass_cache,
            _t=_t,
            config=config,
        )
        for url in urls
    ]
    await asyncio.gather(*tasks)


@with_request_id
async def _handle_parse_request(
    cli: Client,
    msg: Message,
    *,
    url: str,
    mode: ParseMode = ParseMode.PREVIEW,
    delete_share_url_msg: bool = False,
    bypass_cache: bool = False,
    _t: PreLocaleSelector,
    config: SettingsConfig,
) -> None:
    try:
        await handle_parse(
            cli,
            msg,
            url=url,
            mode=mode,
            delete_share_url_msg=delete_share_url_msg,
            bypass_cache=bypass_cache,
            _t=_t,
            config=config,
        )
    except ParseRateLimitExceeded as e:
        if e.should_notify:
            logger.warning(
                f"速率限制 {e.retry_after:.1f}s, chat_id={msg.chat.id if msg.chat else None}, msg_id={msg.id}"
            )
            text = format_label(_t(f"解析过于频繁, 请在 {e.retry_after:.1f}s 后重试"))
            if bs.demo_mode:
                text += _t(
                    "\n\n>**为保障所有用户的使用体验, 当前已启用速率限制**\n\n"
                    ">本项目为开源项目, 如有高频或批量解析需求, 建议自行部署实例, "
                    "以免触发 Telegram API 全局速率限制\n\n"
                    "**开源地址: [GitHub](https://github.com/z-mio/parse_hub_bot)**"
                )
            msg = await msg.reply_text(text, link_preview_options=LinkPreviewOptions(is_disabled=True))

            async def fn(retry_after: float) -> None:
                await asyncio.sleep(retry_after)
                await msg.delete()

            loop = asyncio.get_running_loop()
            loop.create_task(fn(e.retry_after))


def _get_parse_user_id(_: Client, msg: Message, **__: Any) -> int | None:
    return msg.chat.id if msg.chat else None


@parse_rate_limit(_get_parse_user_id)
async def handle_parse(
    cli: Client,
    msg: Message,
    *,
    url: str,
    mode: ParseMode = ParseMode.PREVIEW,
    delete_share_url_msg: bool = False,
    bypass_cache: bool = False,
    _t: PreLocaleSelector,
    config: SettingsConfig,
) -> None:
    chat_id = msg.chat.id if msg.chat else None
    logger.info(f"收到解析请求: url={url}, chat_id={chat_id}, msg_id={msg.id}, mode={mode}")
    if bypass_cache:
        logger.debug("bypass_cache=True 绕过缓存")
    if delete_share_url_msg:
        logger.debug(f"自动删除分享链接消息: chat_id={chat_id}, msg_id: {msg.id}")
        try:
            await msg.delete()
        except Exception as e:
            logger.warning(f"删除分享链接消息失败: chat_id={chat_id}, msg_id: {msg.id}, error: {e}")

    reporter = MessageStatusReporter(msg, _t=_t, config=config)
    if mode == ParseMode.RAW:
        use_caching = False
        skip_media_processing = True
        singleflight = False
        save_metadata = False
    elif mode == ParseMode.ZIP:
        use_caching = False
        skip_media_processing = True
        singleflight = False
        save_metadata = True
    else:
        use_caching = True
        skip_media_processing = False
        singleflight = not bypass_cache
        save_metadata = False
    try:
        raw_url = await ParseService().get_raw_url(url)
    except Exception as e:
        await reporter.report_error(_t("获取原始链接"), e)
        return

    if use_caching and not bypass_cache and (cached := await persistent_cache.get(raw_url)):
        logger.debug("file_id 缓存命中, 直接发送")
        await send_cached(msg, cached, raw_url, user_config=config)
        return

    cached_parse_result = None if bypass_cache else await parse_cache.get(raw_url)
    with ParsePipeline(
        url,
        raw_url,
        reporter,
        parse_result=cached_parse_result,
        singleflight=singleflight,
        skip_media_processing=skip_media_processing,
        skip_download_threshold=SKIP_DOWNLOAD_THRESHOLD,
        gif_only_skip_download_count_threshold=(
            GIF_ONLY_SKIP_DOWNLOAD_COUNT_THRESHOLD if mode == ParseMode.PREVIEW else 0
        ),
        save_metadata=save_metadata,
        _t=_t,
    ) as pipeline:
        if (result := await pipeline.run()) is None:
            if pipeline.waited:
                logger.debug("Singleflight 等待完成, 重新检查缓存")
                if not bypass_cache and (cached := await persistent_cache.get(raw_url)):
                    await send_cached(msg, cached, raw_url, user_config=config)
                else:
                    await handle_parse(cli, msg, url=url, mode=mode, bypass_cache=bypass_cache, _t=_t, config=config)
                    return
            else:
                logger.debug("Pipeline 返回 None, 跳过后续处理")
            return

        parse_result = result.parse_result
        await parse_cache.set(raw_url, parse_result)

        if parse_result.type == PostType.RICHTEXT:
            logger.debug(f"富文本类型, 创建 Telegraph 页面: title={parse_result.title}")
            await msg.reply_chat_action(enums.ChatAction.TYPING)
            ph_url = await create_richtext_telegraph(cli, parse_result)
            logger.debug(f"Telegraph 页面创建完成: {ph_url}")
            caption = build_caption(parse_result, ph_url, hide_source=config.hide_source)
            await send_with_rate_limit(
                lambda: msg.reply_text(
                    caption,
                    link_preview_options=LinkPreviewOptions(show_above_text=True),
                )
            )
            await persistent_cache.set(
                raw_url,
                CacheEntry(
                    parse_result=CacheParseResult(title=parse_result.title, content=parse_result.content),
                    telegraph_url=ph_url,
                ),
            )
            await reporter.dismiss()
            return

        caption = build_caption(parse_result, hide_source=config.hide_source)
        gif_only = all(isinstance(i, AniRef) for i in to_list(parse_result.media))
        if (
            mode == ParseMode.PREVIEW
            and gif_only
            and len(to_list(parse_result.media)) > GIF_ONLY_SKIP_DOWNLOAD_COUNT_THRESHOLD
        ):
            await send_with_rate_limit(
                lambda: msg.reply_text(
                    caption,
                    reply_markup=build_gif_button(to_list(parse_result.media)),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            )
            await reporter.dismiss()
            return

        if not result.processed_list:
            logger.debug("无媒体文件, 仅发送文本")
            await msg.reply_chat_action(enums.ChatAction.TYPING)
            await send_with_rate_limit(
                lambda: msg.reply_text(
                    caption,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            )
            cache_entry = CacheEntry(
                parse_result=CacheParseResult(title=parse_result.title, content=parse_result.content)
            )
            await persistent_cache.set(raw_url, cache_entry)
            await reporter.dismiss()
            return

        if mode == ParseMode.RAW:
            await send_raw(msg, result, reporter, _t=_t, user_config=config)
            return
        if mode == ParseMode.ZIP:
            await send_zip(msg, result, reporter, _t=_t, user_config=config)
            return

        logger.debug(f"开始上传媒体: media_count={len(result.processed_list)}")
        await reporter.report(_t("上 传 中..."))
        try:
            media_cache_entry = await send_media(msg, parse_result, result.processed_list, caption, _t=_t)
            if media_cache_entry:
                await persistent_cache.set(raw_url, media_cache_entry)
            await reporter.dismiss()
        except Exception as e:
            logger.opt(exception=e).debug("详细堆栈")
            logger.error(f"上传失败: {e}")
            await reporter.report_error(_t("上传"), e)
            return
