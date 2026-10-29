"""B站接口封装：首页推荐、视频详情。

所有网络请求都通过 asyncio.to_thread 放到线程池执行，
避免阻塞 AstrBot 主事件循环。
"""

from __future__ import annotations

import asyncio
import json
import random
import urllib.request
from typing import Any

from astrbot.api import logger

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class BiliAPI:
    def __init__(self, cookie: str = ""):
        self.cookie = cookie.strip()

    def _get_sync(self, url: str, referer: str = "https://www.bilibili.com/") -> dict:
        """同步请求，只能在非事件循环线程中调用"""
        headers = {
            "User-Agent": UA,
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))

    async def _get(self, url: str, referer: str = "https://www.bilibili.com/") -> dict:
        """异步请求：阻塞 IO 放到线程池"""
        return await asyncio.to_thread(self._get_sync, url, referer)

    async def check_login(self) -> tuple[bool, str]:
        """检查 cookie 是否有效，返回 (是否登录, 昵称)"""
        try:
            d = await self._get("https://api.bilibili.com/x/web-interface/nav")
            data = d.get("data") or {}
            if data.get("isLogin"):
                return True, data.get("uname") or ""
            return False, ""
        except Exception as e:
            logger.warning(f"[BiliScroll] 检查登录态失败: {e}")
            return False, ""

    async def recommend(self, count: int = 30) -> list[dict[str, Any]]:
        """拉首页推荐，返回清洗后的视频列表"""
        url = (
            "https://api.bilibili.com/x/web-interface/index/top/feed/rcmd"
            f"?ps={count}&fresh_type=4&feed_version=V8&fresh_idx=1&fresh_idx_1h=1"
        )
        try:
            d = await self._get(url)
        except Exception as e:
            logger.error(f"[BiliScroll] 拉推荐失败: {e}")
            return []

        items = (d.get("data") or {}).get("item") or []
        videos = []
        for it in items:
            bvid = it.get("bvid") or ""
            title = (it.get("title") or "").strip()
            if not bvid or not title:
                continue  # 广告位或特殊卡片
            duration = it.get("duration") or 0
            if not isinstance(duration, (int, float)) or duration <= 0:
                continue
            owner = (it.get("owner") or {}).get("name") or ""
            view = (it.get("stat") or {}).get("view") or 0
            desc = (it.get("desc") or "").strip()
            cover = (it.get("pic") or "").replace("http://", "https://")
            videos.append(
                {
                    "bvid": bvid,
                    "title": title,
                    "duration": int(duration),
                    "owner": owner,
                    "view": view,
                    "desc": desc,
                    "cover": cover,
                }
            )

        logger.info(f"[BiliScroll] 拉到 {len(videos)} 条推荐")
        return videos

    def pick_candidates(
        self, videos: list[dict], n: int, max_seconds: int
    ) -> list[dict]:
        """从推荐里挑候选：过滤掉太长的，随机抽样"""
        pool = [v for v in videos if v["duration"] <= max_seconds]
        if not pool:
            logger.warning("[BiliScroll] 过滤后没有可用视频")
            return []
        if len(pool) <= n:
            return pool
        return random.sample(pool, n)
