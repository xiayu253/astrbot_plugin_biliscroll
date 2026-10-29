"""抓评论区高赞评论，作为"大家在关注什么"的参考。"""

from __future__ import annotations

import json
import re
import urllib.request

from astrbot.api import logger

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 过滤用的脏词（命中即丢弃该评论）
# 只保留明确的辱骂/脏话，避免误伤正常词
BAD_WORDS = [
    "傻逼", "煞笔", "沙比", "妈的", "他妈", "死妈", "操你", "草泥马",
    "去死", "脑残", "智障", "弱智", "婊子", "贱货", "黄图", "色情",
    "粪坑", "吃屎", "傻卵", "狗东西",
]

# 纯标点/纯符号（不含中文和字母数字）
EMOJI_ONLY = re.compile(r"^[\s\[\]()（）.!?？~…\-—、，,。:：;；'\"]*$")


class CommentFetcher:
    def __init__(self, cookie: str = ""):
        self.cookie = cookie.strip()

    def _get(self, url: str, referer: str = "https://www.bilibili.com/") -> dict:
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

    def get_aid(self, bvid: str) -> int | None:
        try:
            d = self._get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
            return (d.get("data") or {}).get("aid")
        except Exception as e:
            logger.debug(f"[BiliScroll] 取 aid 失败 {bvid}: {e}")
            return None

    def _is_clean(self, text: str) -> bool:
        """判断这条评论能不能用"""
        t = text.strip()
        if len(t) < 4:
            return False
        # 去掉表情标记后再判断长度
        stripped = re.sub(r"\[[^\]]{1,10}\]", "", t).strip()
        if len(stripped) < 2:
            return False
        if EMOJI_ONLY.match(stripped):
            return False
        # 至少要有两个中文汉字或字母数字，排除纯符号和纯 @
        meaningful = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]", stripped)
        if len(meaningful) < 2:
            return False
        # 纯 @ 一堆人的评论不要
        if re.match(r"^[@\s,，]+$", stripped):
            return False
        low = t.lower()
        for w in BAD_WORDS:
            if w.lower() in low:
                return False
        # 太长的大段文字不要
        if len(t) > 120:
            return False
        return True

    def top_comments(self, bvid: str, limit: int = 8) -> list[dict]:
        """返回高赞评论 [{'text':..., 'like':...}]"""
        aid = self.get_aid(bvid)
        if not aid:
            return []
        # 注意：Referer 必须是视频页，用首页会被 -400 拒绝
        referer = f"https://www.bilibili.com/video/{bvid}/"
        try:
            d = self._get(
                f"https://api.bilibili.com/x/v2/reply?type=1&oid={aid}&sort=2&ps=20&pn=1",
                referer=referer,
            )
        except Exception as e:
            logger.debug(f"[BiliScroll] 取评论失败 {bvid}: {e}")
            return []

        replies = ((d.get("data") or {}).get("replies")) or []
        out = []
        for r in replies:
            msg = ((r.get("content") or {}).get("message") or "").replace("\n", " ").strip()
            if not self._is_clean(msg):
                continue
            # 清理表情标记，保留文字
            clean = re.sub(r"\[[^\]]{1,10}\]", "", msg).strip()
            if not clean:
                continue
            out.append({"text": clean[:100], "like": r.get("like", 0), "replies": r.get("rcount", 0)})
            if len(out) >= limit:
                break

        logger.info(f"[BiliScroll] {bvid} 取到 {len(out)} 条可用评论")
        return out
