"""视频下载与抽帧。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from astrbot.api import logger


def _ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _yt_dlp():
    try:
        import yt_dlp

        return yt_dlp
    except Exception:
        return None


def available() -> tuple[bool, str]:
    """检查依赖是否齐全"""
    if not _yt_dlp():
        return False, "缺少 yt-dlp，请执行: pip install yt-dlp"
    if not _ffmpeg_exe():
        return False, "缺少 ffmpeg，请执行: pip install imageio-ffmpeg"
    return True, "ok"


def download(bvid: str, dest_dir: str) -> str | None:
    """下载视频（带音频，720p 以内），返回文件路径"""
    yt_dlp = _yt_dlp()
    ffmpeg = _ffmpeg_exe()
    if not yt_dlp or not ffmpeg:
        return None

    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bv*[height<=720]+ba/b[height<=720]/b",
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(dest_dir, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "ffmpeg_location": ffmpeg,
        "retries": 2,
        "socket_timeout": 30,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(
                f"https://www.bilibili.com/video/{bvid}", download=True
            )
    except Exception as e:
        logger.warning(f"[BiliScroll] 下载 {bvid} 失败: {e}")
        return None

    files = [f for f in os.listdir(dest_dir) if f.endswith(".mp4")]
    if not files:
        return None
    path = os.path.join(dest_dir, files[0])
    size_mb = os.path.getsize(path) / 1024 / 1024
    logger.info(f"[BiliScroll] 下载完成 {bvid} {size_mb:.1f}MB")
    return path


def extract_frames(video: str, n: int, out_dir: str) -> list[str]:
    """平均抽 n 帧，返回图片路径列表"""
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        return []

    # 取时长
    duration = 0.0
    try:
        r = subprocess.run(
            [ffmpeg, "-i", video], capture_output=True, text=True, timeout=30
        )
        for line in r.stderr.splitlines():
            if "Duration:" in line:
                hms = line.split("Duration:")[1].split(",")[0].strip()
                h, m, s = hms.split(":")
                duration = int(h) * 3600 + int(m) * 60 + float(s)
                break
    except Exception as e:
        logger.warning(f"[BiliScroll] 读取时长失败: {e}")

    if duration <= 0:
        duration = 60.0

    frames = []
    for i in range(n):
        t = duration * (i + 0.5) / n
        jpg = os.path.join(out_dir, f"frame_{i}.jpg")
        try:
            subprocess.run(
                [
                    ffmpeg, "-y", "-ss", f"{t:.2f}", "-i", video,
                    "-frames:v", "1", "-vf", "scale=640:-1", "-q:v", "4", jpg,
                ],
                capture_output=True,
                timeout=60,
            )
            if os.path.exists(jpg) and os.path.getsize(jpg) > 1024:
                frames.append(jpg)
        except Exception as e:
            logger.debug(f"[BiliScroll] 抽帧 {i} 失败: {e}")

    logger.info(f"[BiliScroll] 抽到 {len(frames)}/{n} 帧")
    return frames


def make_workdir() -> str:
    return tempfile.mkdtemp(prefix="biliscroll_")


def cleanup(path: str) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def human_duration(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}小时{m}分"
    return f"{m}分{s}秒"
