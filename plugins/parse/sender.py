import asyncio
import os
from collections.abc import Awaitable, Callable, Sequence
from itertools import batched
from typing import BinaryIO, cast

from easy_ai18n import PreLocaleSelector
from parsehub.types import AniFile, AniRef, AnyMediaRef, AnyParseResult, ImageFile, LivePhotoFile, VideoFile
from pyrogram import enums
from pyrogram.errors import FloodWait, Forbidden, SlowmodeWait, WebpageCurlFailed, WebpageMediaEmpty
from pyrogram.types import (
    InlineKeyboardButton as Ikb,
)
from pyrogram.types import (
    InlineKeyboardMarkup as Ikm,
)
from pyrogram.types import (
    InputMediaAnimation,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    Message,
)

from core import bs
from log import logger
from plugins.helpers import build_caption, build_caption_by_str, format_label
from plugins.parse.cache import build_cached_media_group, cache_media_from_message, make_cache_entry
from plugins.parse.reporters import MessageStatusReporter
from repo.settings import SettingsConfig
from services import CacheEntry, CacheMedia, CacheMediaType, PipelineResult
from services.media import ProcessedMedia, resolve_media_info
from utils.helpers import pack_dir_to_tar_gz, to_list

logger = logger.bind(name="ParseSender")
MAX_RETRIES = 5
GIF_ONLY_SKIP_DOWNLOAD_COUNT_THRESHOLD = 5


async def send_with_rate_limit[T](
    send_coro_fn: Callable[[], Awaitable[T]],
) -> T:
    """带自动重试的发送包装器。

    Args:
        send_coro_fn: 返回协程的可调用对象（lambda 或函数），每次重试会重新调用
    """
    for attempt in range(MAX_RETRIES):
        try:
            return await send_coro_fn()
        except (FloodWait, SlowmodeWait) as e:
            if attempt < MAX_RETRIES - 1:
                logger.warning(f"{e.ID} 重试 ({attempt + 1}/{MAX_RETRIES})，等待 {e.value}s")
                await asyncio.sleep(e.value)
            else:
                raise
        except Forbidden as e:
            logger.warning(f"消息发送失败, Bot 无权限: {e}")
    raise RuntimeError("发送重试失败")


async def send_raw(
    msg: Message,
    result: PipelineResult,
    reporter: MessageStatusReporter,
    *,
    _t: PreLocaleSelector,
    user_config: SettingsConfig,
) -> None:
    """Raw 模式：将文件以原始文档形式上传。"""
    logger.debug("Raw 模式, 直接上传文件")
    await reporter.report(_t("上 传 中..."))
    try:
        caption = build_caption(result.parse_result, hide_source=user_config.hide_source)
        docs: list[InputMediaDocument] = []
        gifs = []
        livephoto_videos: dict[int, InputMediaDocument] = {}

        for processed in result.processed_list:
            file_paths = processed.output_paths or [processed.source.path]
            file_path = file_paths[0]
            doc = InputMediaDocument(media=str(file_path))
            if isinstance(processed.source, AniFile):
                gifs.append(doc)
            elif isinstance(processed.source, LivePhotoFile):
                docs.append(doc)
                livephoto_videos[len(docs) - 1] = InputMediaDocument(media=str(processed.source.video_path))
            else:
                docs.append(doc)

        if len(docs + gifs) == 1:
            all_docs = docs + gifs
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
            sent_msg = await send_with_rate_limit(
                lambda: msg.reply_document(media_input(all_docs[0].media), caption=caption, force_document=True)
            )
            if livephoto_videos and sent_msg:
                await send_with_rate_limit(
                    lambda: sent_msg.reply_document(media_input(livephoto_videos[0].media), force_document=True)
                )
        else:
            msgs: list[Message] = []
            for batch in batched(docs, 10):
                await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
                mg = await send_with_rate_limit(lambda b=list(batch): msg.reply_media_group(b))  # type: ignore
                msgs.extend(mg)
            if livephoto_videos:
                for idx, media_doc in livephoto_videos.items():
                    await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
                    await send_with_rate_limit(
                        lambda m_=media_doc, idx_=idx: msgs[idx_].reply_document(  # type: ignore
                            media_input(m_.media), force_document=True
                        )
                    )
            if gifs:
                await send_with_rate_limit(
                    lambda: msg.reply_text(
                        format_label(_t("GIF 下载链接")),
                        reply_markup=build_gif_button(to_list(result.parse_result.media)),
                    )
                )
            await send_with_rate_limit(
                lambda: msg.reply_text(
                    caption,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            )

    except Exception as e:
        logger.opt(exception=e).debug("详细堆栈")
        logger.error(f"Raw 模式上传失败: {e}")
        await reporter.report_error(_t("上传"), e)
        return
    finally:
        result.cleanup()

    await reporter.dismiss()


async def send_zip(
    msg: Message,
    result: PipelineResult,
    reporter: MessageStatusReporter,
    *,
    _t: PreLocaleSelector,
    user_config: SettingsConfig,
) -> None:
    logger.debug("Zip 模式, 开始打包")
    await reporter.report(_t("打 包 中..."))
    try:
        caption = build_caption(result.parse_result, hide_source=user_config.hide_source)
        if result.output_dir is None:
            raise ValueError("缺少打包目录")
        pack_path = await asyncio.to_thread(pack_dir_to_tar_gz, result.output_dir)
    except Exception as e:
        logger.opt(exception=e).debug("详细堆栈")
        logger.error(f"打包失败: {e}")
        await reporter.report_error(_t("打包"), Exception("..."))
        return
    finally:
        result.cleanup()

    await reporter.report(_t("上 传 中..."))
    try:
        await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
        await send_with_rate_limit(lambda: msg.reply_document(str(pack_path), caption=caption))
    except Exception as e:
        logger.opt(exception=e).debug("详细堆栈")
        logger.error(f"上传失败: {e}")
        await reporter.report_error(_t("上传"), e)
        return
    finally:
        if not bs.debug_skip_cleanup:
            logger.debug("清理压缩包")
            os.remove(pack_path)

    await reporter.dismiss()


async def send_media(
    msg: Message,
    parse_result: AnyParseResult,
    processed_list: list[ProcessedMedia],
    caption: str,
    *,
    _t: PreLocaleSelector,
    video_cover: bool,
) -> CacheEntry | None:
    """构建、发送媒体，并返回缓存条目。"""
    media_refs = to_list(parse_result.media)
    photos_videos, animations = build_input_media(media_refs, processed_list, video_cover=video_cover)
    all_count = len(photos_videos) + len(animations)
    logger.debug(f"媒体分类完成: animations={len(animations)}, photos_videos={len(photos_videos)}")

    if all_count == 1:
        logger.debug("单媒体模式发送")
        media_list = await send_single(msg, photos_videos, animations, caption)
    else:
        logger.debug(f"多媒体模式发送: total={all_count}")
        media_list = await send_multi(msg, photos_videos, animations, caption, media_refs, _t=_t)

    if media_list is None:
        return None
    return make_cache_entry(parse_result, media_list)


async def send_cached(msg: Message, entry: CacheEntry, url: str, *, config: SettingsConfig) -> None:
    """从 file_id 缓存直接发送，跳过解析/下载/转码。"""
    logger.debug(f"缓存发送: media={entry.media}")
    caption = build_caption_by_str(
        entry.parse_result.title,
        entry.parse_result.content,
        url,
        entry.telegraph_url,
        hide_source=config.hide_source,
    )

    if entry.telegraph_url:
        await msg.reply_text(
            caption,
            link_preview_options=LinkPreviewOptions(show_above_text=True),
        )
        return

    if not entry.media:
        await msg.reply_text(
            caption,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    if len(entry.media) == 1:
        await send_cached_single(msg, entry.media[0], caption, video_cover=config.video_cover)
    else:
        await send_cached_multi(msg, entry.media, caption, video_cover=config.video_cover)


def media_input(media: str | BinaryIO | None) -> str | BinaryIO:
    return cast(str | BinaryIO, media)


def build_input_media(
    media_refs: Sequence[AnyMediaRef], processed_list: list[ProcessedMedia], *, video_cover: bool
) -> tuple[list[InputMediaPhoto | InputMediaVideo], list[InputMediaAnimation]]:
    """根据处理结果和媒体引用构建 Telegram InputMedia 列表。"""
    photos_videos: list[InputMediaPhoto | InputMediaVideo] = []
    animations: list[InputMediaAnimation] = []

    for media_ref, processed in zip(media_refs, processed_list, strict=False):
        file_paths = processed.output_paths or [processed.source.path]
        for file_path in file_paths:
            file_path_str = str(file_path)
            width, height, duration = resolve_media_info(processed, file_path_str)

            match processed.source:
                case ImageFile():
                    photos_videos.append(InputMediaPhoto(media=file_path_str))
                case AniFile():
                    animations.append(InputMediaAnimation(media=file_path_str))
                case VideoFile():
                    photos_videos.append(
                        InputMediaVideo(
                            media=file_path_str,
                            video_cover=media_ref.thumb_url if video_cover else None,
                            duration=duration,
                            width=width,
                            height=height,
                            supports_streaming=True,
                        )
                    )
                case LivePhotoFile():
                    photos_videos.append(
                        InputMediaVideo(
                            media=processed.source.video_path,
                            video_cover=file_path_str if video_cover else None,
                            duration=duration,
                            width=width,
                            height=height,
                            supports_streaming=True,
                        )
                    )

    return photos_videos, animations


async def send_single(
    msg: Message,
    photos_videos: list[InputMediaPhoto | InputMediaVideo],
    animations: list[InputMediaAnimation],
    caption: str,
) -> list[CacheMedia] | None:
    """发送单个媒体，返回 CacheMedia 列表。上传失败时降级为 document。"""
    media_list: list[CacheMedia] = []
    all_media = animations + photos_videos

    try:
        sent: Message | None = None
        if animations:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
            sent = await send_with_rate_limit(
                lambda: msg.reply_animation(media_input(animations[0].media), caption=caption)
            )
        else:
            single = photos_videos[0]
            match single:
                case InputMediaPhoto():
                    await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
                    sent = await send_with_rate_limit(
                        lambda: msg.reply_photo(media_input(single.media), caption=caption)
                    )
                case InputMediaVideo():
                    await msg.reply_chat_action(enums.ChatAction.UPLOAD_VIDEO)
                    try:
                        sent = await send_with_rate_limit(
                            lambda: msg.reply_video(
                                media_input(single.media),
                                caption=caption,
                                video_cover=single.video_cover,
                                duration=single.duration,
                                width=single.width,
                                height=single.height,
                                supports_streaming=True,
                            )
                        )
                    except (WebpageCurlFailed, WebpageMediaEmpty):
                        logger.warning("Tg 获取封面失败, 移除封面上传")
                        sent = await send_with_rate_limit(
                            lambda: msg.reply_video(
                                media_input(single.media),
                                caption=caption,
                                duration=single.duration,
                                width=single.width,
                                height=single.height,
                                supports_streaming=True,
                            )
                        )

        if sent and (cm := cache_media_from_message(sent)):
            media_list.append(cm)
    except Exception as e:
        logger.warning(f"上传失败 {e}, 使用兼容模式上传")
        await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
        await send_with_rate_limit(
            lambda: msg.reply_document(media_input(all_media[0].media), caption=caption, force_document=True)
        )
        return None

    return media_list


def build_gif_button(media_refs: Sequence[AnyMediaRef]) -> Ikm:
    buttons = []
    n = 5
    for i, v in enumerate(media_refs):
        if isinstance(v, AniRef):
            buttons.append(Ikb(f"{i + 1}", url=v.url))
    ikbs = [list(i) for i in batched(buttons, n)]
    return Ikm(ikbs)


async def send_multi(
    msg: Message,
    photos_videos: list[InputMediaPhoto | InputMediaVideo],
    animations: list[InputMediaAnimation],
    caption: str,
    media_refs: Sequence[AnyMediaRef],
    *,
    _t: PreLocaleSelector,
) -> list[CacheMedia] | None:
    """发送多个媒体（动图逐条、图片视频分批），返回 CacheMedia 列表。"""
    media_list: list[CacheMedia] = []
    not_cache = False
    if len([i for i in media_refs if isinstance(i, AniRef)]) > GIF_ONLY_SKIP_DOWNLOAD_COUNT_THRESHOLD:
        not_cache = True
        await send_with_rate_limit(
            lambda: msg.reply_text(
                format_label(_t("GIF 过多跳过上传, 请自行下载")), reply_markup=build_gif_button(media_refs)
            )
        )
    else:
        for ani in animations:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
            caption_ = caption if ani == animations[-1] and not photos_videos else ""
            try:
                sent = await send_with_rate_limit(
                    lambda a=ani, c=caption_: msg.reply_animation(  # type: ignore[misc]
                        media_input(a.media),
                        caption=c,
                    )
                )
            except Exception as e:
                logger.warning(f"上传失败 {e}, 使用兼容模式上传")
                not_cache = True
                await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
                await send_with_rate_limit(
                    lambda a=ani, c=caption_: msg.reply_document(media_input(a.media), caption=c, force_document=True)  # type: ignore[misc]
                )
            else:
                if sent and sent.document:
                    media_list.append(CacheMedia(type=CacheMediaType.DOCUMENT, file_id=sent.document.file_id))
                elif sent and sent.animation:
                    media_list.append(CacheMedia(type=CacheMediaType.ANIMATION, file_id=sent.animation.file_id))

    try:
        for batch in batched(photos_videos, 10):
            if batch[-1] == photos_videos[-1]:
                batch[0].caption = caption

            await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
            sent_msgs = await send_with_rate_limit(lambda b=list(batch): msg.reply_media_group(media=b))  # type: ignore[misc]
            for m in sent_msgs:
                if cm := cache_media_from_message(m):
                    media_list.append(cm)
    except Exception as e:
        logger.warning(f"上传失败 {e}, 使用兼容模式上传")
        input_documents: list[InputMediaDocument] = [
            InputMediaDocument(media=media_input(item.media)) for item in photos_videos
        ]
        for document_batch in batched(input_documents, 10):
            if document_batch[-1] == input_documents[-1]:
                document_batch[-1].caption = caption

            await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
            await send_with_rate_limit(lambda b=list(document_batch): msg.reply_media_group(media=b))  # type: ignore
        return None

    return None if not_cache else media_list


async def send_cached_single(msg: Message, m: CacheMedia, caption: str, *, video_cover: bool) -> None:
    """从缓存发送单个媒体。"""
    match m.type:
        case CacheMediaType.PHOTO:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
            await send_with_rate_limit(lambda: msg.reply_photo(m.file_id, caption=caption))
        case CacheMediaType.VIDEO:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_VIDEO)
            await send_with_rate_limit(
                lambda: msg.reply_video(
                    m.file_id,
                    caption=caption,
                    supports_streaming=True,
                    video_cover=m.cover_file_id if video_cover else None,
                )
            )
        case CacheMediaType.ANIMATION:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
            await send_with_rate_limit(lambda: msg.reply_animation(m.file_id, caption=caption))
        case CacheMediaType.DOCUMENT:
            await msg.reply_chat_action(enums.ChatAction.UPLOAD_DOCUMENT)
            await send_with_rate_limit(lambda: msg.reply_document(m.file_id, caption=caption, force_document=True))


async def send_cached_multi(msg: Message, media: list[CacheMedia], caption: str, *, video_cover: bool) -> None:
    """从缓存发送多个媒体。"""
    animations = [m for m in media if m.type == CacheMediaType.ANIMATION]
    others = [m for m in media if m.type != CacheMediaType.ANIMATION]

    for ani in animations:
        await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
        await send_with_rate_limit(
            lambda a=ani: msg.reply_animation(  # type: ignore[misc]
                a.file_id,
                caption=caption if a == animations[-1] and not others else "",
            )
        )

    media_group = build_cached_media_group(others, video_cover=video_cover)
    for batch in batched(media_group, 10):
        if batch[-1] == media_group[-1]:
            batch[0].caption = caption

        await msg.reply_chat_action(enums.ChatAction.UPLOAD_PHOTO)
        await send_with_rate_limit(lambda m=list(batch): msg.reply_media_group(m))  # type: ignore[misc]
