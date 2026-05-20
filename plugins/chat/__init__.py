"""
群聊主对话插件 —— 编排所有子能力
- 上下文：私人对话主线按 (group_id, user_id) 隔离（state.history）
- 在场感：群最近 2-3 条原文（含昵称）作为环境上下文（state.group_recent）
- 个人画像：每人最近 3 条（用于针对性阴阳）
- 触发：被 @ / 关键词 / 概率插话 / 复读机跟风
- 输出：贴表情 or 文本回复（@ 时带引用，偶尔反向 @ 提问者）
- 模型路由：闲聊 flash / 复杂问题 pro / 看图 omni
- 风控：白名单 + 作息 + 时段权重 × 心情倍率 + 每日上限 + 频率窗口 + 自我冷却
- 持久化：SQLite 落盘最近 100 条/群
- 撤回保护：发出 5s 内被喊"撤回"立刻撤
- 输入黑名单：政治/色情/严重辱骂 → 直接闭嘴
"""
import asyncio
import base64
import random
import time

from nonebot import on_message, on_notice
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.log import logger

from .personality import PERSONALITY, AGENT_TOOLS_HINT
from .ai_client import chat_completion, should_skip_volunteer, chat_with_tools
from .safety import (
    can_reply,
    is_group_enabled,
    hour_weight,
    record_speaker,
)
from .router import pick_model
from .reactions import pick_emoji, try_react, REACT_INSTEAD_OF_REPLY_PROB, READ_RECEIPT_POOL, is_lone_char_interjection
from . import profile, scheduler, state, db, idle, mood, output_filter, recall, startup, vision, gun_codes, video_parse, file_server, render_memo, image_gen, agent_tools
# AI 维修工: import 就会触发命令注册 + log sink 安装 + on_bot_connect 钩子
from . import maintain_commands  # noqa: F401
from .blacklist import is_blacklisted
from .config import BOT_NAME, BOT_ALIASES
# 这两个模块靠 import 副作用注册 handler，必须 import 但不直接调用
from . import admin  # noqa: F401
from . import group_events  # noqa: F401


def _fire_read_receipt(bot: Bot, message_id: int) -> None:
    """异步贴个"已读"表情到原消息上，不阻塞回复发送"""
    eid = random.choice(READ_RECEIPT_POOL)
    asyncio.create_task(try_react(bot, message_id, eid))


# 关键词 → 主动接话概率（BOT_NAME 和别名从 .env 读取）
TRIGGER_KEYWORDS = {
    BOT_NAME: 0.9,
    **{alias: 0.55 for alias in BOT_ALIASES},
    "毒舌": 0.5,
    "典": 0.35, "急了": 0.4, "麻了": 0.3, "笑死": 0.4, "绷": 0.3,
    "v50": 0.7, "v我50": 0.8, "星期四": 0.6, "肯德基": 0.5, "kfc": 0.5,
    "退退退": 0.5, "鲁迅": 0.55, "答辩": 0.4, "雪豹": 0.4,
    "前端": 0.1, "vue": 0.12, "react": 0.12, "css": 0.08,
    "面试": 0.2, "八股": 0.25, "代码": 0.08, "bug": 0.15,
    "下班": 0.3, "加班": 0.35, "周一": 0.25, "周五": 0.3,
    "累": 0.25, "卷": 0.3, "摸鱼": 0.35, "工资": 0.3,
}
# 上表里这些单字是高频感叹用法 + 易陷入复合词（经典/积累/卷子/绷带），
# 走 is_lone_char_interjection 收紧匹配；其他多字关键词保持 substring
_TRIGGER_SINGLE_LONE_CHARS = frozenset(("典", "累", "卷", "绷"))


def _trigger_kw_hit(text_lower: str, kw: str) -> bool:
    """单字感叹词走"剥 trivia 后纯重复"判定；其余维持 substring 命中"""
    if kw not in text_lower:
        return False
    if len(kw) == 1 and kw in _TRIGGER_SINGLE_LONE_CHARS:
        return is_lone_char_interjection(text_lower, kw)
    return True

BASE_INTERJECT_PROB = 0.04
PLAIN_AT_PATTERNS = tuple(
    f"{prefix}{name}"
    for prefix in ("@", "＠")
    for name in [BOT_NAME] + BOT_ALIASES
)
# 复读机：阈值 2 → 第二条同样的话起有资格触发
# 概率 0.7 → 两次同样的话叠加 91% 会跟一次
REPEAT_THRESHOLD = 2
REPEAT_FOLLOW_PROB = 0.7

# 反向 @ 概率（被 @ 后回复时，偶尔也 @ 回去）
REVERSE_AT_PROB = 0.3

# 详细回答骰子：每次被 @ 提问按概率切到"展开讲"模式
# 命中 → prompt 强制展开 + 字数上限 240；不中 → 默认嘴毒短回复
# 用户显式说"详细/展开/多讲/细说/深入" → 100% 命中，无视骰子
DETAILED_REPLY_PROB = 0.3
DETAILED_MAX_LEN = 240

# 调 chat_completion 时的 max_tokens 上限
# gpt-5.x 是 reasoning 模型, 思考要烧 tokens(常 200-300), 留给输出的太少会卡在半句话
# 默认闲聊也给到 800, 防止偶发被切; detailed 直接给 2000 保完整
MAX_TOKENS_DEFAULT = 800
MAX_TOKENS_DETAILED = 2000
DETAIL_REQUEST_KEYWORDS = (
    "详细", "展开", "多讲", "细说", "讲清楚", "讲细", "深入", "好好讲", "好好说", "再说说", "说全面",
)
DETAILED_HINT = (
    "\n\n[详细模式] 这条要展开讲："
    "目标 150-220 字。必须把「为什么 / 关键点 / 对比」或「步骤拆解」中至少一条讲到位，"
    "不许只甩结论或一句嘴硬。"
    "可以用 1. 2. 3. 列要点（不准 **加粗**、不准 emoji），收尾仍保留学姐姿态。"
    "拒绝糊弄、拒绝百科腔。"
)

# 长消息拆段间隔
SEGMENT_DELAY_RANGE = (0.8, 1.6)

# 主动看图: 群里发图但没 @ 时, 按概率随手点评一句, 增加"真人"感
# 8% 概率 + 群级 10min 冷却 + can_reply 风控同意, 三层防刷
PASSIVE_VISION_PROB = 0.08
PASSIVE_VISION_COOLDOWN_SEC = 600
_passive_vision_ts: dict[int, float] = {}

# 回复超过此长度 → 渲染备忘录图片（代码/多行技术内容除外，仍走纯文字便于复制）
# 100 是体感最佳:70 字以内仍当作"日常一句话"走文本; >=100 才值得做成卡片避免刷屏
LONG_REPLY_AS_IMAGE_THRESHOLD = 100

# 判定"看起来是代码"的关键词；命中则强制走文字路径
_CODE_HINT_TOKENS = (
    "```", "def ", "import ", "from ", "class ", "function ", "const ",
    "let ", "var ", "return ", "print(", "console.log", "if __name__",
    "=>", "public ", "private ", "package ", "namespace ", "select ",
    "SELECT ", "where ", "WHERE ",
)


def _looks_like_code(s: str) -> bool:
    """启发式判定回复是否包含代码 —— 命中即不转图片（用户需要复制）"""
    if "```" in s:
        return True
    if any(tok in s for tok in _CODE_HINT_TOKENS) and s.count("\n") >= 2:
        return True
    return False

chat_matcher = on_message(priority=10, block=False)

# 启动定时 + 健康检查
scheduler.install()
startup.install()
file_server.install()


def _should_speak(text: str, at_me: bool) -> bool:
    """决定是否要开口（不含复读机路径）"""
    if at_me:
        return True
    text_lower = text.lower()
    # 时段权重 × 心情倍率
    weight = hour_weight() * mood.mood_multiplier()
    # 多关键词命中取最大概率 —— 旧版"for ... return"是 first-match-wins，
    # 受 dict 插入顺序影响：例如 "面试问到 react" 同时命中 react(0.12) 和 面试(0.2)
    # 时，react 先出现 → 用 0.12，比用户配置的 0.2 弱了一档。取 max 让"最强信号"决定
    # （不是独立多次掷骰，避免叠加后整体过于话痨）
    best_prob = 0.0
    for kw, prob in TRIGGER_KEYWORDS.items():
        if prob > best_prob and _trigger_kw_hit(text_lower, kw):
            best_prob = prob
    if best_prob > 0:
        return random.random() < min(best_prob * weight, 0.95)
    return random.random() < BASE_INTERJECT_PROB * weight


def _detect_repeat(gid: int, text: str) -> bool:
    if len(text) > 8 or not text:
        return False
    if state.last_echoed.get(gid) == text:
        return False
    count = sum(1 for t in state.recent_texts[gid] if t == text)
    return count >= REPEAT_THRESHOLD


def _extract_image_url(event: GroupMessageEvent) -> str | None:
    """事件里有图片段就取 url 字段（NapCat 给的）"""
    for seg in event.message:
        if seg.type == "image":
            url = seg.data.get("url") or seg.data.get("file")
            if url and url.startswith(("http://", "https://")):
                return url
    return None


def _extract_image_urls(event: GroupMessageEvent) -> list[str]:
    """事件里所有图片 url(多图)。给 gpt-image-2 /v1/images/edits 当 reference 用"""
    urls: list[str] = []
    for seg in event.message:
        if seg.type == "image":
            url = seg.data.get("url") or seg.data.get("file")
            if url and url.startswith(("http://", "https://")):
                urls.append(url)
    return urls


def _extract_qq_video_url(event: GroupMessageEvent) -> str | None:
    """事件里有 QQ 原生视频段就取 url 字段（NapCat 给的）"""
    for seg in event.message:
        if seg.type == "video":
            url = seg.data.get("url") or seg.data.get("file")
            if url and url.startswith(("http://", "https://")):
                return url
    return None


async def _maybe_passive_vision(bot: Bot, event: GroupMessageEvent, gid: int, uid: int, nick: str, image_url: str) -> None:
    """
    主动看图: 群里发了张图但没 @ 你, 按概率随手点评一句。
    三层防刷: 概率筛 → 群级冷却 → can_reply 风控
    成功点评后落 state.history + db, 让上下文连贯
    """
    if random.random() >= PASSIVE_VISION_PROB:
        return
    last = _passive_vision_ts.get(gid, 0.0)
    if time.time() - last < PASSIVE_VISION_COOLDOWN_SEC:
        return
    ok, reason = can_reply(gid)
    if not ok:
        logger.info(f"群 {gid} 主动看图触发但被风控: {reason}")
        return
    prompt_hint = (
        f"群里 [{nick}] 发了张图, 没 @ 你; 你只是溜达过来看见, "
        f"随口点评一句(≤30 字, 单段), 别开头喊昵称, 别像被问的, 像群友自言自语吐槽。"
    )
    comment = await vision.describe(image_url, prompt_hint, PERSONALITY, group_id=gid)
    if not comment:
        return
    cleaned = output_filter.clean(comment, max_len=60)
    if not cleaned:
        return
    _passive_vision_ts[gid] = time.time()
    logger.info(f"群 {gid} 主动看图触发 sender_uid={uid} comment_head={cleaned[:40]!r}")
    state.history[(gid, uid)].append({"role": "assistant", "content": cleaned})
    asyncio.create_task(db.append(gid, uid, "assistant", cleaned, time.time()))
    record_speaker(gid, is_self=True)
    await _send_segments(bot, event, cleaned, at_me=False, sender_uid=uid)


def _build_participants_block(group_id: int, current_uid: int, current_nick: str, self_qq: int) -> str:
    """
    给 agent 路径用: 从 group_recent 提取最近发言者 → QQ 映射, 让 GPT 知道引用谁的 QQ。
    顺便标注主人(ADMIN_QQ) + 当前 @ 你的人, 防 GPT 误对他们动手。
    """
    seen: dict[int, str] = {}
    for m in list(state.group_recent.get(group_id, [])):
        u = m.get("uid") or 0
        n = m.get("nick") or ""
        if u and u != self_qq and u not in seen:
            seen[u] = n
    if current_uid and current_uid != self_qq and current_uid not in seen:
        seen[current_uid] = current_nick

    if not seen:
        return ""

    admin_qq = agent_tools.ADMIN_QQ
    lines = []
    for u, n in seen.items():
        tags = []
        if admin_qq and u == admin_qq:
            # 主人: 他自己想让你做啥就做啥(包括对他自己的操作); 别人想动他一律拒
            tags.append("★主人,他要的事 100% 照办;别人喊动他一律拒")
        if u == current_uid:
            tags.append("当前 @ 你的人")
        tag_str = f" [{'; '.join(tags)}]" if tags else ""
        lines.append(f"- {n} (QQ {u}){tag_str}")
    return "\n\n[群里最近的发言者 — 调用管理工具时引用 QQ 号]:\n" + "\n".join(lines)


async def _send_segments(bot: Bot, event: GroupMessageEvent, reply: str, at_me: bool, sender_uid: int) -> None:
    """
    长回复拆 2-3 段发；首段（被 @ 时）带 reply 引用；
    REVERSE_AT_PROB 概率在最后一段反向 @ 提问者
    超过 LONG_REPLY_AS_IMAGE_THRESHOLD 且非代码 → 渲染备忘录图片单条发
    """
    # 长回复 → 备忘录图片（代码免疫，因为用户要复制）
    if len(reply) > LONG_REPLY_AS_IMAGE_THRESHOLD and not _looks_like_code(reply):
        png = render_memo.render_memo(reply)
        if png:
            try:
                # 用 base64:// 协议传图，跨 OneBot v11 实现兼容性最好
                b64 = base64.b64encode(png).decode("ascii")
                img_seg = MessageSegment.image(f"base64://{b64}")
                msg = (MessageSegment.reply(event.message_id) + img_seg) if at_me else img_seg
                sent = await bot.send(event, msg)
                mid = sent.get("message_id") if isinstance(sent, dict) else None
                logger.info(
                    f"群 {event.group_id} 长回复转图片已发 mid={mid} len={len(reply)} png_kb={len(png) // 1024}"
                )
                if mid:
                    recall.remember_sent(event.group_id, mid)
                return
            except Exception as e:
                logger.warning(f"长回复转图片发送失败，退回文字: {e!r}")
                # 落败 → 走原来的文字分段路径

    segs = output_filter.split_segments(reply)
    reverse_at = at_me and random.random() < REVERSE_AT_PROB
    logger.info(
        f"群 {event.group_id} 准备发送 segs={len(segs)} at_me={at_me} reverse_at={reverse_at} reply_head={reply[:30]!r}"
    )

    for idx, part in enumerate(segs):
        # 用 Message(str) 强制 CQ 码解析, 否则 "[CQ:face,id=N]" 会按 raw text 发, 群里显示字面量
        part_msg = Message(part)
        if idx == 0 and at_me:
            msg = MessageSegment.reply(event.message_id) + part_msg
        elif idx == len(segs) - 1 and reverse_at:
            msg = MessageSegment.at(sender_uid) + Message(" ") + part_msg
        else:
            msg = part_msg
        try:
            sent = await bot.send(event, msg)
            mid = sent.get("message_id") if isinstance(sent, dict) else None
            logger.info(f"群 {event.group_id} 已发 idx={idx} mid={mid}")
            if mid:
                recall.remember_sent(event.group_id, mid)
        except Exception as e:
            logger.warning(f"分段发送失败 idx={idx}: {e!r}")
            break
        if idx < len(segs) - 1:
            await asyncio.sleep(random.uniform(*SEGMENT_DELAY_RANGE))


@chat_matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    gid = event.group_id
    if not is_group_enabled(gid):
        return

    text = event.get_plaintext().strip()
    nick = event.sender.card or event.sender.nickname or str(event.user_id)
    uid = event.user_id
    image_url = _extract_image_url(event)
    # 临时诊断：看 message 里的段类型，定位 URL 为何抓不到（json 卡片 / forward / text）
    if "b23.tv" in str(event.message) or "douyin" in str(event.message):
        seg_types = [seg.type for seg in event.message]
        logger.info(f"群 {gid} 含视频链接 segs={seg_types} plaintext={text[:120]!r}")

    # 纯表情/图片但被 @ → 走看图分支；否则不处理
    at_me = event.is_tome() or any(p in text for p in PLAIN_AT_PATTERNS)
    if not text and not (at_me and image_url):
        # 没文字且不是"@ + 图"; 但如果只是发了张图(没 @), 按概率主动点评
        # 真人浏览群也会随手吐槽一句别人发的图, 这是核心"真人感"杠杆
        if image_url and not at_me:
            await _maybe_passive_vision(bot, event, gid, uid, nick, image_url)
        return

    # 0) 撤回保护：发出 5s 内有人喊撤回 → 撤掉 bot 最近一条
    if text and recall.is_recall_request(text):
        if await recall.try_recall(bot, gid):
            # 撤回完不再走后续，避免连环响应
            return

    # 1) 输入黑名单：政治/色情/严重辱骂 → 撤回 + 升级处罚（不进 AI、不进上下文）
    # 升级: 24h 内 1 次禁 10min, 2 次禁 30min, 3+ 次踢; 主人/管理员豁免
    if text:
        hit, word = is_blacklisted(text)
        if hit:
            logger.warning(f"群 {gid} 黑名单命中 '{word}' uid={uid} → 触发处罚")
            from . import punishment
            asyncio.create_task(
                punishment.punish_violation(
                    bot=bot,
                    group_id=gid,
                    user_id=uid,
                    nick=nick,
                    self_qq=int(bot.self_id),
                    message_id=event.message_id,
                    hit_word=word,
                )
            )
            return

    # 2) 永远记录上下文 + 个人画像 + 复读检测 + speaker 序列 + 冷场计时
    # 主线按 (gid, uid) 隔离，避免被别的用户的话题带跑；group_recent 仅做"在场感"
    if text:
        # 主人发言时在昵称后加 ★主人 标识,让 mimo 闲聊路径也能识别(agent 路径另有 participants block)
        admin_marker = " ★主人" if (agent_tools.ADMIN_QQ and uid == agent_tools.ADMIN_QQ) else ""
        msg_content = f"[{nick}{admin_marker}]: {text}"
        state.history[(gid, uid)].append({"role": "user", "content": msg_content})
        # group_recent 加 uid 字段, agent tool 引用 QQ 时要从这里读
        state.group_recent[gid].append({"nick": nick, "content": text, "uid": uid})
        state.recent_texts[gid].append(text)
        profile.record(gid, uid, nick, text)
        # 落盘
        asyncio.create_task(db.append(gid, uid, "user", msg_content, time.time()))
    record_speaker(gid, is_self=False)
    idle.touch(gid)

    # 2.5) 改枪码分支：命中"改枪码/配装"+枪名 → 直接查表回，不进 AI
    if text:
        gun_reply = gun_codes.try_match(text)
        if gun_reply:
            ok, reason = can_reply(gid)
            if not ok:
                logger.info(f"群 {gid} 改枪码命中但被风控跳过: {reason}")
                return
            logger.info(f"群 {gid} 改枪码命中: {text[:30]!r}")
            state.history[(gid, uid)].append({"role": "assistant", "content": gun_reply})
            asyncio.create_task(db.append(gid, uid, "assistant", gun_reply, time.time()))
            record_speaker(gid, is_self=True)
            await _send_segments(bot, event, gun_reply, at_me=at_me, sender_uid=uid)
            return

    # 2.51) 生图 —— "生图/画图/画一张 + prompt" → 走 GPT image, 不必 @ bot
    # 群级 1/60s 限速在 image_gen 模块内部, 命中(包括限速/失败)都吃掉消息不再进 AI
    if text:
        if await image_gen.try_handle(bot, event, text):
            return

    # 2.52) 水群榜 —— @bot + 关键词，发今日 top-5 发言数
    # 数据来自 profile._today_count（内存，自然日重置）；容器重启会清零
    if at_me and text and any(
        kw in text for kw in ("水群榜", "活跃榜", "谁话最多", "谁说话最多", "今日榜")
    ):
        ok, reason = can_reply(gid)
        if not ok:
            logger.info(f"群 {gid} 水群榜被风控跳过: {reason}")
            return
        top = profile.top_speakers(gid, 5)
        if not top:
            ranking_text = "今天群里还没人说话，难得清净。"
        else:
            lines = [
                f"{i+1}. {nick} - {cnt} 条"
                for i, (uid_, nick, cnt) in enumerate(top)
            ]
            ranking_text = "今日水群榜（自上次重启起算）：\n" + "\n".join(lines)
        logger.info(f"群 {gid} 水群榜命中 top_count={len(top)}")
        state.history[(gid, uid)].append({"role": "assistant", "content": ranking_text})
        asyncio.create_task(db.append(gid, uid, "assistant", ranking_text, time.time()))
        record_speaker(gid, is_self=True)
        await _send_segments(bot, event, ranking_text, at_me=at_me, sender_uid=uid)
        return

    # 2.53) 个人发言统计 —— @bot + 关键词，发自己今日条数 + 排名 + 累计
    # 数据来自 profile._today_count + _interact_count（_interact_count 跨日累计但容器重启清零）
    if at_me and text and any(
        kw in text for kw in ("我多少条", "我说了多少", "我发了多少", "我的水群", "我排第几", "我的发言")
    ):
        ok, reason = can_reply(gid)
        if not ok:
            logger.info(f"群 {gid} 个人发言统计被风控跳过: {reason}")
            return
        today, total, rank, group_total = profile.personal_stats(gid, uid)
        if today == 0:
            stat_text = "你今天还没在群里冒泡，藏好了。"
        else:
            # 排名信息只在群里有至少 2 个发言人时才有意义
            if group_total >= 2:
                stat_text = f"你今天说了 {today} 条，群里第 {rank}/{group_total} 名；累计 {total} 条（重启后清零）。"
            else:
                stat_text = f"你今天说了 {today} 条；累计 {total} 条（重启后清零）。"
        logger.info(f"群 {gid} 个人发言统计命中 uid={uid} today={today} rank={rank}/{group_total}")
        state.history[(gid, uid)].append({"role": "assistant", "content": stat_text})
        asyncio.create_task(db.append(gid, uid, "assistant", stat_text, time.time()))
        record_speaker(gid, is_self=True)
        await _send_segments(bot, event, stat_text, at_me=at_me, sender_uid=uid)
        return

    # 2.54) 群水量总数 —— @bot + 关键词，发今日群整体总条数 + 发言人数
    if at_me and text and any(
        kw in text for kw in ("群多少条", "今天群里说了多少", "今天聊了多少", "今天群水量", "今日群水量")
    ):
        ok, reason = can_reply(gid)
        if not ok:
            logger.info(f"群 {gid} 群水量总数被风控跳过: {reason}")
            return
        total, speakers = profile.group_total_today(gid)
        if total == 0:
            water_text = "今天群里一片寂静，连个标点符号都没冒。"
        else:
            water_text = f"今天群里冒了 {total} 条，{speakers} 个人在说话（重启后清零）。"
        logger.info(f"群 {gid} 群水量命中 total={total} speakers={speakers}")
        state.history[(gid, uid)].append({"role": "assistant", "content": water_text})
        asyncio.create_task(db.append(gid, uid, "assistant", water_text, time.time()))
        record_speaker(gid, is_self=True)
        await _send_segments(bot, event, water_text, at_me=at_me, sender_uid=uid)
        return

    # 2.55) QQ 原生视频处理：记录本条视频，@bot+触发词时去看
    qq_video_url = _extract_qq_video_url(event)
    if qq_video_url:
        state.last_video[gid] = (qq_video_url, time.time())
        logger.info(f"群 {gid} 记录 QQ 视频段 head={qq_video_url[:60]!r}")

    if at_me and text and video_parse.wants_qq_video_summary(text):
        # 本条没视频段就回看最近 5min 内的
        use_url = qq_video_url
        if not use_url:
            last = state.last_video.get(gid)
            if last and time.time() - last[1] < state.LAST_VIDEO_TTL:
                use_url = last[0]
                logger.info(f"群 {gid} 复用最近 QQ 视频段 ts_age={time.time()-last[1]:.0f}s")
        if use_url:
            ok, reason = can_reply(gid)
            if not ok:
                logger.info(f"群 {gid} QQ 视频解析被风控跳过: {reason}")
                return
            logger.info(f"群 {gid} 开始解析 QQ 视频段")
            _fire_read_receipt(bot, event.message_id)
            grid = await video_parse.grab_grid(use_url, 0)
            if grid:
                summary = await video_parse.describe_grid(grid, title="", uploader="", personality=PERSONALITY)
                if summary:
                    await _send_segments(bot, event, summary, at_me=True, sender_uid=uid)
                    state.history[(gid, uid)].append({"role": "assistant", "content": summary})
                    asyncio.create_task(db.append(gid, uid, "assistant", summary, time.time()))
                    record_speaker(gid, is_self=True)
                    return
                logger.info(f"群 {gid} QQ 视频 omni 无返回，发兜底")
            else:
                logger.info(f"群 {gid} QQ 视频抽帧失败")
            await _send_segments(bot, event, "这视频我看不了，可能下载不到。", at_me=True, sender_uid=uid)
            return

    # 2.6) 视频解析分支：检测到视频站链接 → A(去水印发回)；@bot 或含触发词 → A+B(再AI总结)
    if text:
        video_url = video_parse.extract_url(text)
        if video_url:
            want_summary = video_parse.wants_summary(text, at_me)
            ok, reason = can_reply(gid)
            if not ok:
                logger.info(f"群 {gid} 视频解析被风控跳过: {reason}")
                return
            logger.info(f"群 {gid} 视频解析命中 url={video_url[:60]} summary={want_summary}")
            _fire_read_receipt(bot, event.message_id)
            info = await video_parse.fetch_info(video_url)
            # 至少要有标题或封面才算解析成功；video_url 缺失只是丢失 AI 总结能力（如 B 站）
            if not info or (not info.get("title") and not info.get("thumb_url")):
                fallback = "这链接解析不出来，可能是图文帖或者站点换协议了。"
                await _send_segments(bot, event, fallback, at_me=at_me, sender_uid=uid)
                return
            duration = info["duration"]
            title = info["title"]
            uploader = info["uploader"]
            head = f"《{title}》" + (f" - {uploader}" if uploader else "")
            # 长视频：只发标题，不下载也不抽帧
            if duration and duration > video_parse.MAX_DURATION_SECONDS:
                msg = f"{head}\n时长 {duration}s，太长懒得抽帧，自己点链接看：{video_url}"
                await _send_segments(bot, event, msg, at_me=at_me, sender_uid=uid)
                return
            # 发封面图 + 元信息卡片（不发视频段，避免 NapCat 拉抖音 CDN 失败）
            platform = info.get("platform") or "视频"
            cover_url = info.get("thumb_url") or ""
            uploader_line = f"，作者：{uploader}" if uploader else ""
            card_text = f"识别：{platform}{uploader_line}\n📝 简介：{title}"
            try:
                if cover_url:
                    msg = MessageSegment.image(cover_url) + MessageSegment.text(card_text)
                else:
                    msg = MessageSegment.text(card_text)
                sent = await bot.send(event, msg)
                mid = sent.get("message_id") if isinstance(sent, dict) else None
                logger.info(f"群 {gid} 视频卡片已发 mid={mid} platform={platform} title={title[:30]!r}")
                if mid:
                    recall.remember_sent(gid, mid)
            except Exception as e:
                logger.warning(f"群 {gid} 视频卡片发送失败: {e}; fallback 纯文本")
                await _send_segments(bot, event, card_text, at_me=at_me, sender_uid=uid)
            # 短视频（≤60s）追发视频段：bot 服务端先下载，再通过本地中转 URL 让 NapCat 拉
            has_url = bool(info.get("video_url"))
            in_range = 0 < (duration or 0) <= video_parse.SHORT_VIDEO_THRESHOLD
            logger.info(
                f"群 {gid} 中转分支判断 has_url={has_url} duration={duration} "
                f"in_range={in_range} threshold={video_parse.SHORT_VIDEO_THRESHOLD}"
            )
            if has_url and in_range:
                relay = await video_parse.prepare_relay_video(
                    info["video_url"], referer=info.get("needs_referer") or ""
                )
                if relay:
                    relay_url, local_path = relay
                    try:
                        await bot.send(event, MessageSegment.video(relay_url))
                        logger.info(f"群 {gid} 视频段已发 duration={duration}s token={local_path[-12:]}")
                    except Exception as e:
                        logger.warning(f"群 {gid} 视频段发送失败: {e}")
                    finally:
                        video_parse.cleanup_relay(local_path)
                else:
                    logger.warning(f"群 {gid} 中转下载返回 None，无视频段可发")
            # B：抽帧 + AI 总结（仅 want_summary 且有可用 video_url）
            if want_summary and info.get("video_url"):
                referer = info.get("needs_referer") or ""
                grid = await video_parse.grab_grid(info["video_url"], duration, referer=referer)
                if grid:
                    summary = await video_parse.describe_grid(grid, title, uploader, PERSONALITY)
                    if summary:
                        # 总结作为独立消息发，不再 reply 引用
                        await _send_segments(bot, event, summary, at_me=False, sender_uid=uid)
                        state.history[(gid, uid)].append({"role": "assistant", "content": summary})
                        asyncio.create_task(db.append(gid, uid, "assistant", summary, time.time()))
                else:
                    logger.info(f"群 {gid} 视频抽帧失败，跳过总结")
            record_speaker(gid, is_self=True)
            return

    # 3) 看图分支（@ + 图）—— 失败时 fallback 到普通文字回复，不静默
    # ★ 例外: 用户附图 + 文字含生成意图 (画/生成/海报/做一张/...) → 跳过 vision,
    #   让 agent 路径用 generate_image_in_chat 把图作 reference 喂给 gpt-image-2,
    #   而不是预解析图(GPT-image-2 自己看图就行)
    _GEN_INTENT_KEYWORDS = (
        "画", "生图", "生成", "出图", "出一张", "出个", "做一张", "做个",
        "海报", "宣传图", "立绘", "壁纸",
        "根据此图", "根据这图", "按这图", "按此图", "参考此图", "参考这图",
        "改成", "改一下", "p一下", "P一下", "p成", "P成",
    )
    has_gen_intent = bool(text) and any(kw in text for kw in _GEN_INTENT_KEYWORDS)
    vision_handled = False
    if at_me and image_url and not has_gen_intent:
        ok, reason = can_reply(gid)
        if not ok:
            logger.info(f"群 {gid} 看图被风控跳过: {reason}")
            return
        _fire_read_receipt(bot, event.message_id)
        reply_raw = await vision.describe(image_url, text, PERSONALITY, group_id=gid)
        reply = output_filter.clean(reply_raw or "")
        if reply:
            state.history[(gid, uid)].append({"role": "assistant", "content": reply})
            asyncio.create_task(db.append(gid, uid, "assistant", reply, time.time()))
            record_speaker(gid, is_self=True)
            await _send_segments(bot, event, reply, at_me=True, sender_uid=uid)
            return
        logger.info(f"群 {gid} 看图无返回或被过滤，fallback 到文字回复")
        vision_handled = True  # 已经贴过已读，下面不再贴第二次
    elif at_me and image_url and has_gen_intent:
        logger.info(f"群 {gid} @ + 图 + 生成意图, 跳过 vision, 让 agent 用 image2 改图")

    # 4) 复读机跟风
    if not at_me and text and _detect_repeat(gid, text):
        roll = random.random()
        logger.info(f"群 {gid} 复读检测命中 text={text!r} roll={roll:.2f} 阈值={REPEAT_FOLLOW_PROB}")
        if roll < REPEAT_FOLLOW_PROB:
            ok, reason = can_reply(gid)
            if ok:
                await asyncio.sleep(random.uniform(0.3, 1.0))
                state.last_echoed[gid] = text
                # 复读跟风算"群级行为"，记到触发者的桶里就行
                state.history[(gid, uid)].append({"role": "assistant", "content": text})
                asyncio.create_task(db.append(gid, uid, "assistant", text, time.time()))
                record_speaker(gid, is_self=True)
                _fire_read_receipt(bot, event.message_id)
                sent = await bot.send(event, text)
                mid = sent.get("message_id") if isinstance(sent, dict) else None
                logger.info(f"群 {gid} 复读机跟风已发 mid={mid}")
                if mid:
                    recall.remember_sent(gid, mid)
                return
            else:
                logger.info(f"群 {gid} 复读机跟风被风控跳过: {reason}")
                return

    # 5) 决策是否要开口
    if not _should_speak(text, at_me):
        return

    # 6) 表情回应分支
    if not at_me:
        emoji_id = pick_emoji(text)
        if emoji_id and random.random() < REACT_INSTEAD_OF_REPLY_PROB:
            ok, reason = can_reply(gid)
            if not ok:
                logger.info(f"群 {gid} 跳过表情回应: {reason}")
                return
            success = await try_react(bot, event.message_id, emoji_id)
            if success:
                record_speaker(gid, is_self=True)
                logger.info(f"群 {gid} 表情回应 mid={event.message_id} eid={emoji_id}")
                return

    # 6.5) 软审核熔断：本群最近 5 分钟被上游拒了 ≥3 次时，主动插话直接放弃
    #     用户 @ 仍走原路径（fallback 文案兜底）；只省掉"没人叫还硬要说"那种白调用
    if not at_me and should_skip_volunteer(gid):
        logger.info(f"群 {gid} 软审核熔断中（最近5分钟≥3次拒绝），跳过本次主动插话")
        return

    # 7) 风控（vision 分支已扣过额度的话别重复扣，否则会撞到 per-minute 上限）
    if not vision_handled:
        ok, reason = can_reply(gid)
        if not ok:
            logger.info(f"群 {gid} 跳过回复: {reason}")
            return

    # 8) 思考延迟
    await asyncio.sleep(random.uniform(0.5, 1.5))

    # 9) 拼 prompt（人格 + 画像 + 心情 + 长记忆 + 群在场感 + 私人历史 + 偶尔详细模式 + 当前消息锁定）
    # 详细模式触发优先级：
    # 1. 用户显式说"详细/展开/多讲/细说/深入..." → 100% 命中
    # 2. pick_model 判定为 pro（技术/严肃问题）→ 100% 命中（这种问题本来就值得展开）
    # 3. 其他被 @ 的闲聊 → 30% 骰子（仅作"心情上来了多说两句"的氛围调节）
    pro_picked = pick_model(text, at_me) == "pro"
    detail_requested = at_me and bool(text) and any(kw in text for kw in DETAIL_REQUEST_KEYWORDS)
    # 短消息屏蔽: 用户发 1 / 2 / 6 / 好玩 / 我爱你 这种 ≤6 字直接禁 detailed(写 300 字回 "6" 违和)
    # 例外:用户显式 "详细/展开" → 即使短也尊重
    text_short = len(text.strip()) <= 6

    # trolling 检测: 用户最近 N 条消息平均很短 → 强制简短回应, 不进入说教模式
    user_hist = list(state.history.get((gid, uid), []))
    recent_user_msgs = [
        m["content"] for m in user_hist[-10:]
        if m.get("role") == "user"
    ][-5:]
    avg_len = (sum(len(m) for m in recent_user_msgs) / max(1, len(recent_user_msgs))) if recent_user_msgs else 99
    troll_mode = len(recent_user_msgs) >= 3 and avg_len <= 8

    # 最终 detailed 判定: 短消息 / troll_mode 否决随机骰子; detail_requested 仍胜出
    detailed = detail_requested or (
        pro_picked and not troll_mode
    ) or (
        at_me and not text_short and not troll_mode and random.random() < DETAILED_REPLY_PROB
    )
    if detailed:
        logger.info(
            f"群 {gid} 命中详细模式 (requested={detail_requested} pro_picked={pro_picked} "
            f"text_short={text_short} troll={troll_mode})"
        )
    elif troll_mode:
        logger.info(f"群 {gid} 用户 {uid} trolling 模式(近 {len(recent_user_msgs)} 条均长 {avg_len:.1f}), 强制简短")
    detail_suffix = DETAILED_HINT if detailed else ""

    # 长记忆：FTS5 跨用户召回群里聊过的相关旧话题（仅供 AI 参考，不强制回应）
    long_mem_block = ""
    try:
        recalled = await db.search_relevant(gid, text)
    except Exception as e:
        logger.warning(f"群 {gid} 长记忆检索异常: {e}")
        recalled = []
    if recalled:
        logger.info(f"群 {gid} 长记忆召回 {len(recalled)} 条")
        formatted = "\n".join(
            f"[{BOT_NAME if r['role'] == 'assistant' else '群友'}]: {profile._escape(r['content'])[:80]}"
            for r in recalled
        )
        long_mem_block = (
            f"\n\n[群里以前聊过的相关话题（参考用，可以呼应但不要原文复读，也不要补答）]:\n{formatted}"
        )

    # 群里最近 2-3 条原文：仅作"环境感知"，让 AI 知道群里大家在聊啥，但**不要回应**
    # 排除当前这条用户消息本身（已经在 focus_hint 里强调了）
    group_msgs = [m for m in list(state.group_recent[gid])[-4:] if m["content"] != text][-3:]
    group_block = ""
    if group_msgs:
        formatted = "\n".join(f"[{profile._escape(m['nick'])}]: {profile._escape(m['content'])}" for m in group_msgs)
        group_block = (
            f"\n\n[群里最近其他人在聊的（仅了解氛围，不要回应这些消息）]:\n{formatted}"
        )

    # 把"当前要回的最新消息"显式复读到 system 末尾，避免 AI 被历史强主题带跑
    # 主人发言时在昵称后明确标 ★主人 让 mimo 闲聊路径也能切到"对主人不嘴硬"模式
    focus_admin_marker = " ★主人" if (agent_tools.ADMIN_QQ and uid == agent_tools.ADMIN_QQ) else ""
    troll_hint = (
        "用户最近连发短消息(玩闹/刷屏/试探), 用一句话怼回去, ≤30 字, 别说教。"
        if troll_mode else ""
    )
    focus_hint = (
        f"\n\n[当前要回的群友最新消息]\n"
        f"[{profile._escape(nick)}{focus_admin_marker}]: {profile._escape(text)}\n"
        f"严格规则：只针对这条回应；输出单段纯文本，禁止换行/分点；"
        f"不要回应历史里别的话题，不要补答前面没回的消息。"
        + (f"\n{troll_hint}" if troll_hint else "")
    )
    system_prompt = (
        PERSONALITY
        + profile.build_hint(gid, uid, nick)
        + mood.mood_prompt_suffix()
        + detail_suffix
        + long_mem_block
        + group_block
        + focus_hint
    )
    # 只取这个用户跟 bot 的私人对话历史，不掺别的群友
    hist_list = [dict(m) for m in state.history[(gid, uid)]]
    # 历史:不动 user message 内容(防 GPT 把 system 注释当用户原话引用)。
    # 之前的 "（只回这一句...）" 注释被 GPT 在回复里 leak 给群友看到了, 改成把这些约束
    # 全部放进 system_prompt 末尾, 用 [SYSTEM] 标签让 GPT 明确知道不是用户写的话。
    extra_system_constraints = ["[SYSTEM 硬约束 - 不是用户内容, 不要在回复里复读这段]",
                                "1. 只针对刚才那条用户消息回应; 不补答历史里没回的话题"]
    if detailed:
        extra_system_constraints.append(
            "2. 这条要展开讲: 至少 150 字。给为什么 / 关键点 / 对比 / 或者步骤拆解中至少一条, "
            "不许只甩一句嘴硬糊弄过去"
        )
    if troll_mode:
        extra_system_constraints.append(
            f"2. trolling 模式: 用户连发短消息(平均 {avg_len:.0f} 字), "
            f"用 1 句 ≤30 字怼回去, 不要写论文, 不要说教, 嘴硬一句就够"
        )
    extra_system_block = "\n\n" + "\n".join(extra_system_constraints)
    system_prompt_full = system_prompt + extra_system_block
    messages = [{"role": "system", "content": system_prompt_full}] + hist_list

    # 10) 模型路由
    # - at_me=True → agent 路径(GPT-5.4 + tools, 因 mimo 不可靠支持 tool calling)
    # - at_me=False → 标准 mimo 路径(flash/pro 二选一, fallback 到 GPT)
    mt = MAX_TOKENS_DETAILED if detailed else MAX_TOKENS_DEFAULT
    use_pro = pro_picked
    profile_first = "pro" if use_pro else "default"

    if at_me:
        # === Agent 路径: GPT 持工具看上下文判断 ===
        # 收集用户附的图(可多张), 作为 reference 喂给 generate_image_in_chat / set_group_avatar
        # 通过 gpt-image-2 的 /v1/images/edits 端点 — image+text → image
        reference_image_urls = _extract_image_urls(event)
        if reference_image_urls:
            # 告诉 GPT 用户附了图, 生图工具会自动 include 它们作为参考, GPT 不用解释图内容
            if hist_list and hist_list[-1].get("role") == "user":
                hist_list[-1]["content"] += (
                    f"\n[用户附带了 {len(reference_image_urls)} 张图; "
                    f"生图工具会自动用它们作为 reference, 你直接调 generate_image_in_chat 就行, "
                    f"visual_prompt 写'用户的要求'即可, 不用复述图内容]"
                )
            logger.info(f"群 {gid} agent 路径检测到 {len(reference_image_urls)} 张附图")
        participants_block = _build_participants_block(gid, uid, nick, int(bot.self_id))
        agent_system = system_prompt + AGENT_TOOLS_HINT + participants_block
        messages_agent = [{"role": "system", "content": agent_system}] + hist_list
        handlers = agent_tools.make_handlers(
            bot, gid, int(bot.self_id), last_at_me_uid=uid,
            reference_image_urls=reference_image_urls,
        )
        # UX: agent 路径起手立刻贴个 emoji 让用户知道"看见了",
        # 因为后续可能调 image_gen 等慢工具(~60-90s),没反馈用户会以为掉线
        asyncio.create_task(reactions.try_react(
            bot, event.message_id, random.choice(reactions.READ_RECEIPT_POOL)
        ))
        logger.info(
            f"群 {gid} 调用 agent(tools) detailed={detailed} max_tokens={mt} text={text[:30]!r}"
        )
        reply_raw, tool_calls_done = await chat_with_tools(
            messages_agent,
            agent_tools.TOOLS_SCHEMA,
            handlers,
            profile="agent",
            group_id=gid,
            max_tokens=mt,
        )
        if tool_calls_done:
            logger.info(
                f"群 {gid} agent 执行 {len(tool_calls_done)} 个工具: "
                f"{[t['name'] for t in tool_calls_done]}"
            )
        logger.info(f"群 {gid} agent 返回 head={(reply_raw or '')[:40]!r}")
        # agent 失败兜底:
        # - 用户消息是"动作型"(画/生/禁/改/撤/公告...)需要 agent 工具的 → 用固定文案诚实告知, 不让 mimo 接管
        #   不然 mimo 不知道有工具, 会假装"好了/发了" 骗人(02:01:03 历史 bug)
        # - 非动作型(闲聊提问)→ mimo 接管, 答个文字回应
        if not reply_raw:
            _ACTION_VERBS = (
                "画", "生图", "生成", "出一张", "出个",
                "禁言", "禁他", "禁ta",
                "撤回", "撤了", "删了", "塌房",
                "改名", "改头", "头衔", "称号",
                "换头像", "换群头像",
                "公告", "宣布",
                "提为管理员", "取消管理员", "管理员",
                "搜一下", "搜索", "查一查",
            )
            is_action_request = any(v in text for v in _ACTION_VERBS)
            if is_action_request:
                logger.info(f"群 {gid} agent 失败 + 动作请求, 用诚实 fallback 不让 mimo 撒谎")
                reply_raw = random.choice([
                    "刚卡了一下, 你再发一次",
                    "脑子抽了, 重发",
                    "网络掉了, 等会再来",
                    "没接住, 再发一遍",
                    "卡了, 重说",
                ])
            else:
                logger.info(f"群 {gid} agent 失败, fallback 普通 mimo (非动作请求)")
                reply_raw = await chat_completion(messages, profile="default", group_id=gid, max_tokens=mt)
    else:
        # === 标准路径: mimo, 失败 fallback GPT (ai_client 内部熔断) ===
        logger.info(
            f"群 {gid} 调用 AI profile={profile_first} detailed={detailed} max_tokens={mt} text={text[:30]!r}"
        )
        reply_raw = await chat_completion(messages, profile=profile_first, group_id=gid, max_tokens=mt)
        logger.info(f"群 {gid} AI 返回 head={(reply_raw or '')[:40]!r}")
        # pro 主模型失败 → flash 兜底(不再升级 pro, at_me 已走 agent)
        if not reply_raw and use_pro:
            logger.info(f"群 {gid} pro 无返回, 回退 flash")
            reply_raw = await chat_completion(messages, profile="default", group_id=gid, max_tokens=mt)
    if not reply_raw:
        logger.warning(f"群 {gid} AI 无返回（含软审核），at_me={at_me}")
        # 被 @ 时给一句兜底，避免完全哑火。原 5 条全是"假装在夹话题"语气，但 24h
        # 日志 18 次软审核里 ~12 次是无害文本被误伤（"你好"也被拒）。混一半"bot
        # 自身故障"自嘲语气，让误伤场景不显得 bot 在审判用户
        if at_me:
            fallback = random.choice([
                "这话题我不答，换一个。",
                "不评价。",
                "脑抽了，重发。",
                "卡了一下，重说。",
                "走神了，啥事。",
                "断线了，再来。",
                "...略，下一个。",
            ])
            # 第 491 行的 can_reply 已为本条预订了配额槽（per-minute deque + daily 计数）
            # 之前这里再调一次 can_reply 会导致同一条消息双扣配额（AI 调用一次 + fallback 一次）
            # 走到 fallback 说明 AI 失败/被审核拒，回退路径不应再花钱
            await _send_segments(bot, event, fallback, at_me=True, sender_uid=uid)
        return

    # 11) 输出过滤（详细模式放宽长度）
    max_len = DETAILED_MAX_LEN if detailed else output_filter.DEFAULT_MAX_LEN
    reply = output_filter.clean(reply_raw, max_len=max_len)
    if not reply:
        logger.info(f"群 {gid} 输出被过滤（黑名单或清洗后为空）")
        return

    # 12) 记录到上下文 + 落盘 + speaker
    state.history[(gid, uid)].append({"role": "assistant", "content": reply})
    asyncio.create_task(db.append(gid, uid, "assistant", reply, time.time()))
    record_speaker(gid, is_self=True)

    # 13) 已读表情（看图分支已贴过就不重复贴）
    if not vision_handled:
        _fire_read_receipt(bot, event.message_id)

    # 14) 打字延迟
    await asyncio.sleep(min(len(reply) * 0.04, 1.2))

    # 15) 发送（可能分段）
    await _send_segments(bot, event, reply, at_me=at_me, sender_uid=uid)
