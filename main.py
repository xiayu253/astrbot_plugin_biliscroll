"""刷B站：让 Bot 真的去刷 B 站，看到有意思的主动分享。"""

from __future__ import annotations

import asyncio
import os
import random
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.api.message_components import File, Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.permission import PermissionType

from .core import bili_api, video as vid
from .core.comments import CommentFetcher
from .core.picker import pick_and_say


class BiliScrollPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config
        self.api = bili_api.BiliAPI(str(config.get("bilibili_cookie", "")))
        self.comments = CommentFetcher(str(config.get("bilibili_cookie", "")))
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._sent_today: list[str] = []
        self._today = ""
        # 防止定时与手动同时执行
        self._share_lock = asyncio.Lock()

    # ---------- 生命周期 ----------

    async def initialize(self):
        ok, msg = vid.available()
        if not ok:
            logger.error(f"[BiliScroll] 依赖缺失: {msg}")
            return

        logged_in, uname = await self.api.check_login()
        if logged_in:
            logger.info(f"[BiliScroll] B站登录正常: {uname}")
        else:
            logger.warning("[BiliScroll] B站 Cookie 无效或未配置，将退回热门榜")

        if not self.cfg.get("enable", False):
            logger.info("[BiliScroll] 未启用自动刷B站")
            return

        self._task = asyncio.create_task(self._daily_loop())
        logger.info("[BiliScroll] 已启动每日刷B站任务")

    async def terminate(self):
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("[BiliScroll] 已停止")

    # ---------- 调度 ----------

    def _roll_today(self) -> int:
        lo = max(int(self.cfg.get("daily_min", 2)), 0)
        hi = max(int(self.cfg.get("daily_max", 3)), lo)
        if hi <= 0:
            return 0
        return random.randint(lo, hi)

    def _in_quiet_hours(self, now: datetime) -> bool:
        start = int(self.cfg.get("quiet_hours_start", 1)) % 24
        end = int(self.cfg.get("quiet_hours_end", 8)) % 24
        if start == end:
            return False
        h = now.hour
        if start < end:
            return start <= h < end
        return h >= start or h < end

    async def _daily_loop(self):
        """每天零点重掷次数，然后在这一天里随机挑时间执行"""
        while not self._stopped:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if self._today != today:
                self._today = today
                self._sent_today = []
                count = self._roll_today()
                logger.info(f"[BiliScroll] {today} 今天分享 {count} 个")

                # 把当天的执行时刻平摊开、各自随机
                plan = []
                if count > 0:
                    day_start = 8
                    day_end = 24
                    window = (day_end - day_start) * 3600
                    for i in range(count):
                        lo = (i * window) // count
                        hi = ((i + 1) * window) // count
                        sec = random.randint(lo, max(lo, hi - 1))
                        base = now.replace(hour=day_start, minute=0, second=0, microsecond=0)
                        from datetime import timedelta

                        t = base + timedelta(seconds=sec)
                        if t > now:
                            plan.append(t)
                    plan.sort()
                    if plan:
                        logger.info(
                            "[BiliScroll] 今天的分享时刻："
                            + "、".join(p.strftime("%H:%M") for p in plan)
                        )

                for target in plan:
                    if self._stopped:
                        return
                    wait = (target - datetime.now()).total_seconds()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    if self._stopped or self._in_quiet_hours(datetime.now()):
                        continue
                    try:
                        await self._share_one()
                    except Exception as e:
                        logger.exception(f"[BiliScroll] 分享失败: {e}")

            # 睡到下一个整点附近再检查日期
            await asyncio.sleep(600)

    # ---------- 核心流程 ----------

    async def _share_one(self):
        if self._share_lock.locked():
            logger.info("[BiliScroll] 上一次分享还没结束，跳过本次")
            return
        async with self._share_lock:
            await self._share_one_inner()

    async def _share_one_inner(self):
        n_cand = max(int(self.cfg.get("candidates", 3)), 1)
        max_seconds = int(self.cfg.get("max_duration_minutes", 10)) * 60
        n_frames = max(int(self.cfg.get("frames_per_video", 6)), 1)

        videos = await self.api.recommend(30)
        if not videos:
            logger.warning("[BiliScroll] 没拿到推荐，跳过")
            return

        # 避免重复推同样的
        videos = [v for v in videos if v["bvid"] not in self._sent_today]
        candidates = self.api.pick_candidates(videos, n_cand, max_seconds)
        if not candidates:
            logger.warning("[BiliScroll] 没有合适的候选，跳过")
            return
        logger.info(
            "[BiliScroll] 候选: " + " | ".join(c["title"][:20] for c in candidates)
        )

        workdir = vid.make_workdir()
        frames_by_index: dict[int, list[str]] = {}
        paths: dict[int, str] = {}
        comments_by_index: dict[int, list[dict]] = {}

        try:
            for i, c in enumerate(candidates, start=1):
                # 先抓评论（快，不需要下载）
                try:
                    comments_by_index[i] = await self.comments.top_comments(
                        c["bvid"], limit=8
                    )
                except Exception as e:
                    logger.debug(f"[BiliScroll] 评论抓取失败 {c['bvid']}: {e}")
                    comments_by_index[i] = []

                sub = os.path.join(workdir, str(i))
                os.makedirs(sub, exist_ok=True)
                path = await vid.download(c["bvid"], sub)
                if not path:
                    continue
                paths[i] = path
                frames_by_index[i] = await vid.extract_frames(path, n_frames, sub)

            if not paths:
                logger.warning("[BiliScroll] 所有候选都下载失败")
                return

            # 只保留下载成功的
            valid = {i: candidates[i - 1] for i in paths}
            frames_valid = {i: frames_by_index.get(i, []) for i in valid}
            comments_valid = {i: comments_by_index.get(i, []) for i in valid}

            provider = self._get_provider()
            if provider is None:
                logger.error("[BiliScroll] 没有可用的 LLM provider")
                return

            pick, say = await pick_and_say(
                provider,
                list(valid.values()),
                frames_valid,
                comments_valid,
                self._persona_prompt(),
            )

            if pick <= 0 or not say:
                logger.warning(f"[BiliScroll] LLM 没给出有效结果 pick={pick} say={say!r}")
                return

            chosen_index = list(valid.keys())[pick - 1]
            chosen = valid[chosen_index]
            chosen_path = paths[chosen_index]

            await self._send(chosen, say, chosen_path)
            self._sent_today.append(chosen["bvid"])
            logger.info(f"[BiliScroll] 已分享: {chosen['title'][:30]} — {say}")

        finally:
            vid.cleanup(workdir)

    def _persona_prompt(self) -> str:
        try:
            cfg = self.context.get_config()
            persona = cfg.get("persona")
            if isinstance(persona, list) and persona:
                return str(persona[0].get("prompt") or "")
            if isinstance(persona, dict):
                return str(persona.get("prompt") or "")
        except Exception:
            pass
        return ""

    def _get_provider(self):
        pid = str(self.cfg.get("llm_provider_id", "") or "").strip()
        try:
            if pid:
                p = self.context.get_provider_by_id(pid)
                if p is not None:
                    return p
            return self.context.get_using_provider()
        except Exception as e:
            logger.error(f"[BiliScroll] 获取 provider 失败: {e}")
            return None

    async def _send(self, video: dict, say: str, path: str):
        umo = str(self.cfg.get("target_umo", "") or "").strip()
        if not umo:
            logger.error("[BiliScroll] 未配置 target_umo，无法发送")
            return

        # 1. 先说感想
        try:
            await self.context.send_message(umo, MessageChain([Plain(say)]))
        except Exception as e:
            logger.error(f"[BiliScroll] 发送感想失败: {e}")

        # 2. 再发视频文件（NapCat 的视频消息段有 bug，用文件段）
        try:
            await self.context.send_message(
                umo,
                MessageChain(
                    [
                        File(
                            name=os.path.basename(path),
                            file=str(Path(path).resolve()),
                        )
                    ]
                ),
            )
        except Exception as e:
            logger.error(f"[BiliScroll] 发送视频失败: {e}")

    # ---------- 手动命令 ----------

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("刷b站")
    async def manual_share(self, event):
        """立即刷一次B站（管理员）"""
        yield event.plain_result("我去刷会儿")
        try:
            await self._share_one()
        except Exception as e:
            logger.exception(f"[BiliScroll] 手动分享失败: {e}")
            yield event.plain_result(f"翻车了: {e}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("刷b站状态")
    async def status(self, event):
        """查看刷B站状态"""
        ok, msg = vid.available()
        logged, uname = await self.api.check_login()
        lines = [
            f"依赖: {'正常' if ok else msg}",
            f"B站登录: {uname if logged else '未登录'}",
            f"自动分享: {'开' if self.cfg.get('enable') else '关'}",
            f"今天已分享: {len(self._sent_today)} 个",
            f"每天次数: {self.cfg.get('daily_min')}~{self.cfg.get('daily_max')}",
        ]
        yield event.plain_result("\n".join(lines))
