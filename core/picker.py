"""让 LLM 看候选视频并挑一个最有话说的，生成分享文案。"""

from __future__ import annotations

import base64
import re
from typing import Any

from astrbot.api import logger

PICK_PROMPT = """你刚在刷B站。下面是 {n} 个视频，每个有标题、几张画面截图，还有评论区的高赞评论。

评论区是大家看完的反应，能让你立刻知道这视频有意思在哪。

【先说挑哪个】
挑那个评论区最有意思的。大家在那儿笑、在那儿骂、在那儿玩梗的那个，就是有东西的。

【再说那句话怎么写，分两步想】

第一步：先从评论区里找一条最说到点子上的。
        别急，先想清楚它到底在说什么——是在吐槽价格？在笑某个画面？还是在阴阳怪气？
        想明白了，你就抓到这视频的点了。

第二步：用你自己的话把这个点说出来。
        换掉它的用词、换成你的口气，但说的还是同一件事。
        不是另起一个话题，不是给它加注解，就是同一件事换个说法。

【重要：允许你说得跟评论一样短】
不用因为"要显得是自己说的"就硬加内容。
如果一条评论已经把话说完、说得很巧，你说得跟它差不多短就行，甚至就是它换个说法。

硬性要求：
- 10 到 20 字
- 不要句号
- 说完就停。不要补一句评价、感叹或者总结
- 不要"这个视频""讲述""展现了""配上"这类词
- 用现在的说法，别用老掉牙的词（别说"好魔性""绝绝子""yyds"）

对照一下「说完就停」：
- 好：「涨两千叫微涨 跌五十叫暴跌」
- 差：「涨两千叫微涨 跌五十叫暴跌 确实没脾气」← 后半句是多余的，像在解释笑点
- 好：「6米8 是认真的吗 这谁量的」
- 差：「6米8 是认真的吗 这谁量的 太离谱了」← "太离谱了"是硬加的

你不用特意证明自己有态度。你选了哪个点、用什么口气说，本身就是态度。

只输出两行：
选：<编号>
说：<那句话>
"""


def _img_b64(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return None


def build_video_block(index: int, video: dict, comments: list[dict]) -> str:
    minutes = video["duration"] // 60
    seconds = video["duration"] % 60
    dur = f"{minutes}分{seconds}秒" if minutes else f"{seconds}秒"
    lines = [
        f"【视频 {index}】",
        f"标题：{video['title']}",
        f"UP主：{video.get('owner', '')}",
        f"时长：{dur}",
    ]
    if video.get("desc"):
        lines.append(f"简介：{video['desc'][:120]}")
    if comments:
        lines.append("高赞评论：")
        for c in comments:
            lines.append(f"  · {c['text']}")
    return "\n".join(lines)


def parse_pick(text: str, count: int) -> tuple[int, str]:
    pick = 0
    say = ""

    m = re.search(r"选[：:]\s*(\d+)", text)
    if m:
        try:
            pick = int(m.group(1))
        except ValueError:
            pick = 0

    m = re.search(r"说[：:]\s*(.+)", text, re.S)
    if m:
        say = m.group(1).strip().split("\n")[0].strip()
        say = say.strip("“”\"'「」")
        say = say.rstrip("。")

    if not (1 <= pick <= count):
        pick = 1
    return pick, say


def enforce_length(say: str, max_len: int = 22) -> str:
    if len(say) <= max_len:
        return say
    cut = say[:max_len]
    for sep in ("，", " ", ",", "、"):
        idx = cut.rfind(sep)
        if idx > 8:
            return cut[:idx].rstrip("，,、 ")
    return cut


async def pick_and_say(
    provider,
    candidates: list[dict],
    frames_by_index: dict[int, list[str]],
    comments_by_index: dict[int, list[dict]] | None = None,
    persona_prompt: str = "",
) -> tuple[int, str]:
    comments_by_index = comments_by_index or {}
    blocks = []
    image_urls = []
    image_labels = []

    for i, video in enumerate(candidates, start=1):
        blocks.append(build_video_block(i, video, comments_by_index.get(i, [])))
        for fp in frames_by_index.get(i, []):
            b64 = _img_b64(fp)
            if b64:
                image_urls.append(b64)
                image_labels.append(i)

    prompt = "\n\n".join(blocks)
    if image_labels:
        prompt += "\n\n（上面截图按顺序属于视频 " + "、".join(
            dict.fromkeys(str(x) for x in image_labels)
        ) + "）"

    system_prompt = PICK_PROMPT.format(n=len(candidates))
    if persona_prompt:
        system_prompt = "# Persona Instructions\n\n" + persona_prompt + "\n\n" + system_prompt

    try:
        resp = await provider.text_chat(
            system_prompt=system_prompt,
            prompt=prompt,
            image_urls=image_urls if image_urls else None,
        )
        raw = resp.completion_text or ""
    except Exception as e:
        logger.error(f"[BiliScroll] LLM 调用失败: {e}")
        return 0, ""

    logger.info(f"[BiliScroll] LLM 原始回复: {raw[:200]}")
    pick, say = parse_pick(raw, len(candidates))
    say = enforce_length(say, 22)
    return pick, say
