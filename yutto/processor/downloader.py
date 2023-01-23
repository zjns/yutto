from __future__ import annotations

import asyncio
import functools
import os
from pathlib import Path
from typing import Any, Coroutine, Optional, Union

import aiohttp

from yutto._typing import AudioUrlMeta, DownloaderOptions, EpisodeData, VideoUrlMeta, SubtitleFileInfo
from yutto.bilibili_typing.quality import audio_quality_map, video_quality_map
from yutto.processor.progressbar import show_progress
from yutto.processor.selector import select_audio, select_video
from yutto.utils.console.colorful import colored_string
from yutto.utils.console.logger import Badge, Logger
from yutto.utils.danmaku import write_danmaku
from yutto.utils.fetcher import Fetcher
from yutto.utils.ffmpeg import FFmpeg
from yutto.utils.file_buffer import AsyncFileBuffer
from yutto.utils.funcutils import filter_none_value, xmerge
from yutto.utils.metadata import write_metadata
from yutto.utils.subtitle import write_subtitle


def slice_blocks(
    start: int, total_size: Optional[int], block_size: Optional[int] = None
) -> list[tuple[int, Optional[int]]]:
    """生成分块后的 (start, size) 序列

    ### Args

    - start (int): 总起始位置
    - total_size (Optional[int]): 需要分块的总大小
    - block_size (Optional[int], optional): 每块的大小. Defaults to None.

    ### Returns

    - list[tuple[int, Optional[int]]]: 分块大小序列，使用元组组织，格式为 (start, size)
    """
    if total_size is None:
        return [(0, None)]
    if block_size is None:
        return [(0, total_size - 1)]
    assert start <= total_size, f"起始地址（{start}）大于总地址（{total_size}）"
    offset_list: list[tuple[int, Optional[int]]] = [(i, block_size) for i in range(start, total_size, block_size)]
    if (total_size - start) % block_size != 0:
        offset_list[-1] = (
            start + (total_size - start) // block_size * block_size,
            total_size - start - (total_size - start) // block_size * block_size,
        )
    return offset_list


def show_videos_info(videos: list[VideoUrlMeta], selected: int):
    """显示视频详细信息"""
    if not videos:
        Logger.info("不包含任何视频流")
        return
    Logger.info(f"共包含以下 {len(videos)} 个视频流：")
    for i, video in enumerate(videos):
        log = "{}{:2} [{:^4}] [{:>4}x{:<4}] <{:^8}> #{}".format(
            "*" if i == selected else " ",
            i,
            video["codec"].upper(),
            video["width"],
            video["height"],
            video_quality_map[video["quality"]]["description"],
            len(video["mirrors"]) + 1,
        )
        if i == selected:
            log = colored_string(log, fore="blue")
        Logger.info(log)


def show_audios_info(audios: list[AudioUrlMeta], selected: int):
    """显示音频详细信息"""
    if not audios:
        Logger.info("不包含任何音频流")
        return
    Logger.info(f"共包含以下 {len(audios)} 个音频流：")
    for i, audio in enumerate(audios):
        log = "{}{:2} [{:^4}] <{:^8}>".format(
            "*" if i == selected else " ", i, audio["codec"].upper(), audio_quality_map[audio["quality"]]["description"]
        )
        if i == selected:
            log = colored_string(log, fore="magenta")
        Logger.info(log)


async def download_video_and_audio(
    session: aiohttp.ClientSession,
    video: Optional[VideoUrlMeta],
    video_path: Union[str, Path],
    audio: Optional[AudioUrlMeta],
    audio_path: Union[str, Path],
    options: DownloaderOptions,
):
    """下载音视频"""

    buffers: list[Optional[AsyncFileBuffer]] = [None, None]
    sizes: list[Optional[int]] = [None, None]
    coroutines_list: list[list[Coroutine[Any, Any, None]]] = []
    Fetcher.set_semaphore(options["num_workers"])
    if video is not None:
        vbuf = await AsyncFileBuffer(video_path, overwrite=options["overwrite"])
        vsize = await Fetcher.get_size(session, video["url"])
        video_coroutines = [
            Fetcher.download_file_with_offset(session, video["url"], video["mirrors"], vbuf, offset, block_size)
            for offset, block_size in slice_blocks(vbuf.written_size, vsize, options["block_size"])
        ]
        coroutines_list.append(video_coroutines)
        buffers[0], sizes[0] = vbuf, vsize

    if audio is not None:
        abuf = await AsyncFileBuffer(audio_path, overwrite=options["overwrite"])
        asize = await Fetcher.get_size(session, audio["url"])
        audio_coroutines = [
            Fetcher.download_file_with_offset(session, audio["url"], audio["mirrors"], abuf, offset, block_size)
            for offset, block_size in slice_blocks(abuf.written_size, asize, options["block_size"])
        ]
        coroutines_list.append(audio_coroutines)
        buffers[1], sizes[1] = abuf, asize

    # 为保证音频流和视频流尽可能并行，因此将两者混合一下～
    coroutines = list(xmerge(*coroutines_list))
    coroutines.insert(0, show_progress(list(filter_none_value(buffers)), sum(filter_none_value(sizes))))
    Logger.info("开始下载……")
    await asyncio.gather(*coroutines)
    Logger.info("下载完成！")

    for buffer in buffers:
        if buffer is not None:
            await buffer.close()


def merge_video_and_audio(
    video: Optional[VideoUrlMeta],
    video_path: Union[str, Path],
    audio: Optional[AudioUrlMeta],
    audio_path: Union[str, Path],
    output_path: Union[str, Path],
    options: DownloaderOptions,
):
    """合并音视频"""

    ffmpeg = FFmpeg()
    Logger.info("开始合并……")

    if video is not None and video["codec"] == options["video_save_codec"]:
        options["video_save_codec"] = "copy"
    if audio is not None and audio["codec"] == options["audio_save_codec"]:
        options["audio_save_codec"] = "copy"

    args_list: list[list[str]] = [
        ["-i", str(video_path)] if video is not None else [],
        ["-i", str(audio_path)] if audio is not None else [],
        ["-vcodec", options["video_save_codec"]] if video is not None else [],
        ["-acodec", options["audio_save_codec"]] if audio is not None else [],
        # see also: https://www.reddit.com/r/ffmpeg/comments/qe7oq1/comment/hi0bmic/?utm_source=share&utm_medium=web2x&context=3
        ["-strict", "unofficial"],
        ["-metadata:s:v:0", "VENDOR_ID="],
        ["-metadata:s:a:0", "VENDOR_ID="],
        ["-threads", str(os.cpu_count())],
        ["-y", str(output_path)],
    ]

    ffmpeg.exec(functools.reduce(lambda prev, cur: prev + cur, args_list))
    Logger.info("合并完成！")

    if video is not None:
        os.remove(video_path)
    if audio is not None:
        os.remove(audio_path)


def merge_video_and_audio_and_subtitles(
    video: Optional[VideoUrlMeta],
    video_path: Union[str, Path],
    audio: Optional[AudioUrlMeta],
    audio_path: Union[str, Path],
    output_path: Union[str, Path],
    subtitles: list[SubtitleFileInfo],
    options: DownloaderOptions,
):
    """合并音视频和字幕"""

    ffmpeg = FFmpeg()
    Logger.info("开始合并……")

    if video is not None and video["codec"] == options["video_save_codec"]:
        options["video_save_codec"] = "copy"
    if audio is not None and audio["codec"] == options["audio_save_codec"]:
        options["audio_save_codec"] = "copy"

    def default_subtitle():
        for sub in subtitles:
            if "zh-CN" == sub["info"]["lang_code"]:
                return sub
        for sub in subtitles:
            if "zh" in sub["info"]["lang_code"]:
                return sub
        return subtitles[0]

    def_sub = default_subtitle()
    subtitles.remove(def_sub)
    subtitles.insert(0, def_sub)

    args_list: list[list[str]] = [
        ["-i", str(video_path)] if video is not None else [],
        ["-i", str(audio_path)] if audio is not None else [],
    ]
    index = -1
    for sub in subtitles:
        args_list.append(["-i", sub["path"]])
    if video is not None:
        index += 1
        args_list.append(["-map", f"{index}"])
        if audio is not None:
            index += 1
            args_list.append(["-map", f"{index}"])
    elif audio is not None:
        index += 1
        args_list.append(["-map", f"{index}"])

    args_list += [
        ["-c:v", options["video_save_codec"]] if video is not None else [],
        ["-c:a", options["audio_save_codec"]] if audio is not None else [],
        ["-c:s", "copy"] if subtitles else [],
        ["-disposition:s:0", "default"] if subtitles else [],
        ["-metadata:s:v:0", "VENDOR_ID="],
        ["-metadata:s:a:0", "VENDOR_ID="],
        ["-threads", str(os.cpu_count())],
    ]
    for i, sub in enumerate(subtitles):
        # 从0开始，选择第 input_index+i+1 个输入的所有轨道
        # 例："-map 0:v:0"表示选择第0个输入的视频轨道的第0轨
        args_list.append(["-map", str(index + i + 1)])
        args_list.append([f"-metadata:s:s:{i}", "language=%s" % sub["info"]["lang_code"]])
        args_list.append([f"-metadata:s:s:{i}", "title=%s" % sub["info"]["lang"]])
    args_list.append(["-y", str(output_path)])

    ffmpeg.exec(functools.reduce(lambda prev, cur: prev + cur, args_list))
    Logger.info("合并完成！")

    if video is not None:
        os.remove(video_path)
    if audio is not None:
        os.remove(audio_path)


async def start_downloader(
    session: aiohttp.ClientSession,
    episode_data: EpisodeData,
    options: DownloaderOptions,
):
    """处理单个视频下载任务，包含弹幕、字幕的存储"""

    videos = episode_data["videos"]
    audios = episode_data["audios"]
    subtitles = episode_data["subtitles"]
    danmaku = episode_data["danmaku"]
    metadata = episode_data["metadata"]
    output_dir = Path(episode_data["output_dir"])
    tmp_dir = Path(episode_data["tmp_dir"])
    filename = episode_data["filename"]

    Logger.info(f"开始处理视频 {filename}")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    video_path = tmp_dir.joinpath(filename + "_video.m4s")
    audio_path = tmp_dir.joinpath(filename + "_audio.m4s")

    video = select_video(videos, options["require_video"], options["video_quality"], options["video_download_codec"])
    audio = select_audio(audios, options["require_audio"], options["audio_quality"], options["audio_download_codec"])

    # 显示音视频详细信息
    show_videos_info(videos, videos.index(video) if video is not None else -1)
    show_audios_info(audios, audios.index(audio) if audio is not None else -1)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_format = ".mkv"
    if video is None:
        if audio is not None and audio["codec"] == "fLaC":
            output_format = ".flac"
        else:
            output_format = ".aac"
    else:
        if audio is not None and audio["codec"] == "fLaC":
            output_format = ".mkv"  # MP4 does not support FLAC audio

    output_path = output_dir.joinpath(filename + output_format)
    if output_path.exists():
        if not options["overwrite"]:
            Logger.info(f"文件 {filename} 已存在")
            return
        else:
            Logger.info("文件已存在，因启用 overwrite 选项强制删除……")
            output_path.unlink()

    if video is None and audio is None:
        Logger.warning("没有音视频需要下载")
        # return

    subtitle_files = list[SubtitleFileInfo]()
    # 保存字幕
    if options["no_subtitle"]:
        Logger.custom("跳过字幕下载", badge=Badge("字幕", fore="black", back="yellow"))
    elif subtitles:
        video_path_no_ext = os.path.splitext(str(output_path))[0]
        for subtitle in subtitles:
            subtitle_path = "{}_{}.srt".format(video_path_no_ext, subtitle["lang"])
            subtitle_files.append({"info": subtitle, "path": subtitle_path})
            write_subtitle(subtitle["lines"], str(output_path), subtitle["lang"])
        Logger.custom(
            "{}字幕已全部生成".format(", ".join([subtitle["lang"] for subtitle in subtitles])),
            badge=Badge("字幕", fore="black", back="cyan"),
        )
    else:
        Logger.custom("未发现字幕", badge=Badge("字幕", fore="black", back="red"))

    # 保存弹幕
    if options["no_danmaku"]:
        Logger.custom("跳过弹幕下载", badge=Badge("弹幕", fore="black", back="yellow"))
    elif (danmaku["save_type"] != "ass" or video is not None) and danmaku["data"]:
        write_danmaku(
            danmaku,
            str(output_path),
            video["height"] if video is not None else 0,
            video["width"] if video is not None else 0,
        )
        Logger.custom("{} 弹幕已生成".format(danmaku["save_type"]).upper(), badge=Badge("弹幕", fore="black", back="cyan"))
    elif danmaku["save_type"] == "ass" and danmaku["data"] and video is None:
        Logger.custom("不下载视频无法生成 ASS 弹幕", badge=Badge("弹幕", fore="black", back="red"))
    else:
        Logger.custom("未发现弹幕", badge=Badge("弹幕", fore="black", back="red"))

    # 保存媒体描述文件
    if not options["with_metadata"]:
        Logger.custom("跳过 NFO 媒体描述文件生成", badge=Badge("描述文件", fore="black", back="yellow"))
    elif metadata is not None:
        write_metadata(metadata, str(output_path))
        Logger.custom("NFO 媒体描述文件已生成", badge=Badge("描述文件", fore="black", back="cyan"))
    else:
        Logger.custom("未发现 NFO 媒体描述文件", badge=Badge("描述文件", fore="black", back="red"))

    if video is None and audio is None:
        return

    # 下载视频 / 音频
    await download_video_and_audio(session, video, video_path, audio, audio_path, options)

    if not options["pack_subtitle"]:
        Logger.custom("不往视频文件打包字幕", badge=Badge("字幕", fore="black", back="yellow"))
        merge_video_and_audio(video, video_path, audio, audio_path, output_path, options)
    elif subtitles:
        merge_video_and_audio_and_subtitles(video, video_path, audio, audio_path, output_path, subtitle_files, options)
    else:
        # 合并视频 / 音频
        merge_video_and_audio(video, video_path, audio, audio_path, output_path, options)
