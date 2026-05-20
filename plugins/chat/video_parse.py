"""
视频解析 —— 抖音/快手/小红书/B站/皮皮虾 等去水印 + AI 看内容
- A：yt-dlp 拿无水印 video URL + metadata（标题/作者/时长）
- B：ffmpeg 抽 4 帧合成 2×2 grid → omni 多模态 → 一句总结
- 触发：检测到视频链接走 A；@bot 或含 "解析/讲讲/讲一下" 走 A+B
- 长视频上限 5 分钟（超过只发标题，不下载也不抽帧）
"""
import asyncio
import base64
import os
import re
import tempfile
import time
import uuid
from typing import Optional

import httpx
from nonebot.log import logger

from . import file_server
from .ai_client import _PROFILES, _TIMEOUT


def _ffmpeg_bin() -> str:
    """优先用 imageio-ffmpeg 自带二进制，回退到系统 ffmpeg。"""
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"

# 链接识别正则（短链 + 长链都覆盖）
_VIDEO_URL_PATTERNS = [
    re.compile(r"https?://v\.douyin\.com/\S+"),
    re.compile(r"https?://(?:www\.)?douyin\.com/video/\S+"),
    re.compile(r"https?://www\.iesdouyin\.com/share/\S+"),
    re.compile(r"https?://v\.kuaishou\.com/\S+"),
    re.compile(r"https?://(?:www\.)?kuaishou\.com/short-video/\S+"),
    re.compile(r"https?://xhslink\.com/\S+"),
    re.compile(r"https?://(?:www\.)?xiaohongshu\.com/\S+"),
    re.compile(r"https?://b23\.tv/\S+"),
    re.compile(r"https?://(?:www|m)\.bilibili\.com/video/\S+"),
    re.compile(r"https?://h5\.pipix\.com/\S+"),
    re.compile(r"https?://(?:www\.)?pipix\.com/\S+"),
]

# 短链域名（需要 follow 301/302 拿到 canonical URL，再交给 yt-dlp）
_SHORT_LINK_DOMAINS = (
    "b23.tv",
    "v.douyin.com",
    "v.kuaishou.com",
    "xhslink.com",
    "h5.pipix.com",
)

# 通用浏览器 UA（部分站点 HEAD 不带 UA 直接 412）
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# cookies 文件路径：抖音/快手等站点风控要 cookies 才解得动
# 用户从浏览器导出 cookies.txt（Netscape 格式）丢到 data/cookies.txt 即可
# 容器内路径优先环境变量，回退到 /app/data/cookies.txt（已挂卷）
_COOKIES_FILE = os.environ.get("YT_DLP_COOKIES", "/app/data/cookies.txt")
if os.path.isfile(_COOKIES_FILE):
    logger.info(f"yt-dlp cookies 已加载: {_COOKIES_FILE}")
else:
    logger.info(f"yt-dlp cookies 未配置（{_COOKIES_FILE} 不存在），抖音/快手等可能失败")

# 触发"AI 看内容"（B 模式）的关键词
_TRIGGER_WORDS = ("解析", "讲讲", "讲一下", "什么内容", "啥内容", "看一下", "看下", "总结一下", "总结下")

# 长视频上限（秒）—— 超过只发标题
MAX_DURATION_SECONDS = 300

# 短视频自动发视频段的上限；超过这个只发卡片，不下载
SHORT_VIDEO_THRESHOLD = 60

# NapCat 拉本地中转视频的 base URL（Docker 内部网络）
_RELAY_BASE_URL = os.environ.get("FILE_RELAY_BASE_URL", "http://nonebot:8080/file/")


def extract_url(text: str) -> Optional[str]:
    """从消息文本里抓第一个视频站点 URL。
    QQ 客户端有时把短链中间换行（如 `v.douyin.com/R1Hxg4Ax\\nPBE/`），需拼回来。"""
    for pat in _VIDEO_URL_PATTERNS:
        m = pat.search(text)
        if m:
            url = m.group(0)
            # 短链且不以 / 收尾 → 尝试把紧随的字母数字续段拼回来
            if any(d in url for d in _SHORT_LINK_DOMAINS) and not url.endswith("/"):
                tail = text[m.end():]
                cont = re.match(r"\s+([A-Za-z0-9_-]+/?)", tail)
                if cont:
                    url = url + cont.group(1)
                    logger.info(f"URL 换行修复: {url[-40:]!r}")
            return url
    return None


def wants_summary(text: str, at_me: bool) -> bool:
    """是否走 A+B 模式（外站链接场景：@bot 或含触发词）"""
    if at_me:
        return True
    return any(w in text for w in _TRIGGER_WORDS)


def wants_qq_video_summary(text: str) -> bool:
    """QQ 原生视频场景：仅在 @bot + 含显式触发词时才看（避免一切 @bot 都误触发）"""
    return any(w in text for w in _TRIGGER_WORDS)


_DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com", "tiktok.com")
# 改走自建 Evil0ctal sidecar:公网 api.douyin.wtf 长期 timeout,自建版用本地 cookies + 内置 a_bogus 签名稳定可用
# 通过 docker compose 服务名解析,无需暴露端口到宿主机
_DOUYIN_API = os.environ.get("DOUYIN_API", "http://qqbot-douyin-api/api/hybrid/video_data")

# B 站公共 web API（免 cookies，能拿元信息 + 封面；视频流要 WBI 签名暂不取）
_BILIBILI_HOSTS = ("bilibili.com", "b23.tv")
_BILIBILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
_BVID_RE = re.compile(r"/video/(BV[0-9A-Za-z]+)")

# 单视频下载上限 30MB，超出说明视频太长，跳过
_MAX_VIDEO_BYTES = 30 * 1024 * 1024


def _is_douyin(url: str) -> bool:
    """是否抖音/TikTok 域名（短链 v.douyin.com 也算）"""
    u = url.lower()
    return any(h in u for h in _DOUYIN_HOSTS)


def _is_bilibili(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in _BILIBILI_HOSTS)


async def _fetch_via_bilibili_api(url: str) -> Optional[dict]:
    """B 站公共 web API 拿元信息（免 cookies）。
    yt-dlp 在新版风控下常 412；走官方 view API 能稳定拿到 title/owner/duration/cover。
    注意：不返回 video_url（视频流要 WBI 签名，暂不实现）—— X1 卡片够用，AI 总结暂时缺货。"""
    # 短链先解析成 canonical bilibili.com/video/BVxxx
    resolved = await _resolve_short_url(url) if "b23.tv" in url else url
    m = _BVID_RE.search(resolved)
    if not m:
        logger.warning(f"B 站 URL 抽不出 BVID: {resolved[:80]}")
        return None
    bvid = m.group(1)
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": _BROWSER_UA}) as client:
            resp = await client.get(_BILIBILI_VIEW_API, params={"bvid": bvid})
            if resp.status_code != 200:
                logger.warning(f"bilibili view API HTTP {resp.status_code}")
                return None
            payload = resp.json()
            if payload.get("code") != 0:
                logger.warning(f"bilibili view code={payload.get('code')} msg={payload.get('message')!r}")
                return None
            d = payload.get("data") or {}
            # hdslb.com 默认走 http，QQ/NapCat 抓 http 偶发会失败 —— 改成 https 更稳
            pic = d.get("pic") or ""
            if pic.startswith("http://"):
                pic = "https://" + pic[7:]
            return {
                "title": d.get("title") or "(无标题)",
                "uploader": (d.get("owner") or {}).get("name") or "",
                "duration": int(d.get("duration") or 0),
                "video_url": "",  # 视频流要 WBI 签名，暂不取；AI 总结路径会自动跳过
                "thumb_url": pic,
                "platform": "B站",
                "needs_referer": "",
            }
    except Exception as e:
        logger.warning(f"bilibili view API 异常 type={type(e).__name__} msg={e!r}")
        return None


async def prepare_relay_video(remote_url: str, referer: str = "") -> Optional[tuple[str, str]]:
    """下载视频到 /tmp/qqbot-relay-{token}.mp4，返回 (NapCat 可拉的 URL, 文件路径)。
    调用方应在 bot.send 之后 cleanup_relay(path)。失败返回 None。"""
    token = uuid.uuid4().hex
    path = file_server.relay_path(token)
    # Range 头是必需的：抖音 CDN 对无 Range 的请求会回 200 然后挂住 body
    headers = {
        "User-Agent": _BROWSER_UA,
        "Range": "bytes=0-",
        "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.5",
    }
    if referer:
        headers["Referer"] = referer
    logger.info(
        f"中转下载开始 url={remote_url[:100]}... referer={referer or '(none)'}"
    )
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
            async with client.stream("GET", remote_url) as resp:
                # Range 请求成功返回 206 Partial Content；某些 CDN 也回 200
                if resp.status_code not in (200, 206):
                    logger.warning(f"中转下载 HTTP {resp.status_code} url={remote_url[:80]}")
                    return None
                total = 0
                with open(path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                        total += len(chunk)
                        if total > _MAX_VIDEO_BYTES:
                            logger.warning(f"视频超过 {_MAX_VIDEO_BYTES // 1024 // 1024}MB，放弃中转")
                            cleanup_relay(path)
                            return None
                        f.write(chunk)
                if total == 0:
                    cleanup_relay(path)
                    return None
                logger.info(f"中转视频已下载: token={token[:8]}... size={total // 1024}KB")
                return f"{_RELAY_BASE_URL}{token}", path
    except Exception as e:
        logger.warning(f"中转下载异常 type={type(e).__name__} msg={e!r} url={remote_url[:100]}")
        cleanup_relay(path)
        return None


def cleanup_relay(path: str) -> None:
    """删 /tmp 中转文件。失败静默。"""
    try:
        if path and os.path.isfile(path):
            os.unlink(path)
    except OSError:
        pass


async def _download_to_tmp(url: str, referer: str = "") -> Optional[str]:
    """流式下载视频到 /tmp，返回本地 mp4 路径。失败 None。
    抖音 CDN 防盗链严，NapCat 和 ffmpeg 都拿不到直链；服务端下载后存本地，
    再把路径喂给两者就 OK。容器重启 /tmp 自动清，但运行期间也要清，否则
    长链接里超大文件或网络异常断流会在 /tmp 留下半文件。"""
    headers = {"User-Agent": _BROWSER_UA}
    if referer:
        headers["Referer"] = referer
    fd, path = tempfile.mkstemp(suffix=".mp4", dir="/tmp")
    os.close(fd)
    success = False
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code not in (200, 206):
                    logger.warning(f"下载视频 HTTP {resp.status_code} url={url[:80]}")
                    return None
                total = 0
                with open(path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                        total += len(chunk)
                        if total > _MAX_VIDEO_BYTES:
                            logger.warning(f"视频超过 {_MAX_VIDEO_BYTES // 1024 // 1024}MB，放弃")
                            return None
                        f.write(chunk)
                if total == 0:
                    return None
                logger.info(f"视频已下载: {path} size={total // 1024}KB")
                success = True
                return path
    except Exception as e:
        logger.warning(f"下载视频异常 type={type(e).__name__} msg={e!r} url={url[:80]}")
        return None
    finally:
        # 任何非成功路径都要清掉残留：超大放弃、HTTP 错、断流异常都会留半文件
        if not success:
            try:
                os.unlink(path)
            except OSError:
                pass


async def _fetch_via_douyin_api(url: str) -> Optional[dict]:
    """调自建 Evil0ctal sidecar (/api/hybrid/video_data) 拿视频元信息。失败 None。
    自建版返回的是抖音原生字段(data.video.play_addr...),不是公网 douyin.wtf 的 hybrid 后处理格式;
    Evil0ctal 内置 a_bogus 签名 + 我们提供的 cookies,自带无水印 URL(play_addr.url_list 直接可用)。
    返回字段保持原函数 schema(title/uploader/duration/video_url/thumb_url/platform/needs_referer)
    不变,下游 fetch_info 等无需改动。"""
    payload = None
    last_err = ""
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _BROWSER_UA}) as client:
                resp = await client.get(_DOUYIN_API, params={"url": url})
                if resp.status_code >= 500:
                    last_err = f"HTTP {resp.status_code}"
                    if attempt == 1:
                        await asyncio.sleep(1.0)
                        continue
                    logger.warning(f"douyin api {last_err}（重试也失败）: {resp.text[:160]!r}")
                    return None
                if resp.status_code != 200:
                    logger.warning(f"douyin api HTTP {resp.status_code}: {resp.text[:160]!r}")
                    return None
                payload = resp.json()
                break
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_err = f"{type(e).__name__}"
            if attempt == 1:
                await asyncio.sleep(1.0)
                continue
            logger.warning(f"douyin api 网络异常（重试也失败）: {last_err}")
            return None
    if payload is None:
        return None
    try:
        if payload.get("code") != 200:
            logger.warning(f"douyin api code={payload.get('code')} msg={payload.get('message')!r}")
            return None
        d = payload.get("data") or {}
        # Evil0ctal 原生字段:
        #   data.video.play_addr.url_list   → 无水印视频 URL (playback CDN, v11-weba.douyinvod.com)
        #   data.video.download_addr.url_list → 下载 CDN 兜底
        #   data.video.cover.url_list       → 封面
        #   data.video.duration             → 毫秒
        #   data.images                     → 图文帖,每项有 url_list
        #   data.music.duration             → BGM 时长(秒),video.duration 缺失时兜底
        video = d.get("video") or {}
        images = d.get("images") or []
        music = d.get("music") or {}
        author = d.get("author") or {}

        play_urls = (video.get("play_addr") or {}).get("url_list") or []
        download_urls = (video.get("download_addr") or {}).get("url_list") or []
        remote_url = (play_urls[0] if play_urls else "") or (download_urls[0] if download_urls else "")

        # 图文帖:images 是 [{url_list:[...]}, ...],取每张图的第一个 url
        image_list = []
        for img in images:
            urls = (img or {}).get("url_list") or []
            if urls:
                image_list.append(urls[0])
        is_image_post = (not remote_url) and bool(image_list)

        if not remote_url and not is_image_post:
            logger.warning("douyin api 既无视频 URL 也无图片列表")
            return None

        if is_image_post:
            thumb_url = image_list[0]
        else:
            cover_list = (video.get("cover") or {}).get("url_list") or []
            thumb_url = cover_list[0] if cover_list else ""

        # 图文帖时长一律给 0,避开下游用 BGM 长度判定短视频中转的误判
        if is_image_post:
            duration_sec = 0
        else:
            dur_video_ms = video.get("duration") or 0
            if dur_video_ms:
                duration_sec = int(dur_video_ms / 1000) if dur_video_ms > 1000 else int(dur_video_ms)
            else:
                duration_sec = int(music.get("duration") or 0)
        platform_label = "抖音图文" if is_image_post else "抖音"
        logger.info(
            f"douyin api 解析成功 type={platform_label} duration={duration_sec}s "
            f"video_url_len={len(remote_url)} image_count={len(image_list)}"
        )
        return {
            "title": d.get("desc") or "(无标题)",
            "uploader": author.get("nickname") or "",
            "duration": duration_sec,
            "video_url": remote_url,  # 图文帖时为空 → 后续跳过中转/AI 总结
            "thumb_url": thumb_url,
            "platform": platform_label,
            "needs_referer": "https://www.douyin.com/",
        }
    except Exception as e:
        logger.warning(f"douyin api 解析异常 type={type(e).__name__} msg={e!r}")
        return None


async def _resolve_short_url(url: str) -> str:
    """对 b23.tv / v.douyin / xhslink 等短链 follow 301/302，拿到 canonical 长链。
    失败原样返回。"""
    if not any(d in url for d in _SHORT_LINK_DOMAINS):
        return url
    headers = {"User-Agent": _BROWSER_UA}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=10, headers=headers
        ) as client:
            # 用 GET 而不是 HEAD：部分站点 HEAD 直接 412（如 bilibili）
            resp = await client.get(url)
            final = str(resp.url)
            if final != url:
                logger.info(f"短链解析: {url[:50]} → {final[:80]}")
            return final
    except Exception as e:
        logger.warning(f"短链解析失败 url={url[:60]}: {e}")
        return url


# yt-dlp extractor_key → 中文站点名（用于"识别：xxx"那一行）
_EXTRACTOR_LABELS = {
    "BiliBili": "B站",
    "Bilibili": "B站",
    "BilibiliBangumi": "B站",
    "Kuaishou": "快手",
    "KuaiShou": "快手",
    "Xiaohongshu": "小红书",
    "TikTok": "TikTok",
    "Douyin": "抖音",
    "Youtube": "YouTube",
    "YouTube": "YouTube",
    "Pipix": "皮皮虾",
}


def _label_from_extractor(extractor: str) -> str:
    if not extractor:
        return "视频"
    return _EXTRACTOR_LABELS.get(extractor, extractor)


async def fetch_info(url: str) -> Optional[dict]:
    """视频元信息解析，返回 {title, uploader, duration, video_url, thumb_url, platform}；失败 None。
    抖音/TikTok 优先走 douyin.wtf 公网 API（免 cookies），失败回退 yt-dlp；
    其他站点直接走 yt-dlp。"""
    # 抖音短链 + 长链都走 API（API 自己会 follow 短链）
    if _is_douyin(url):
        api_result = await _fetch_via_douyin_api(url)
        if api_result:
            return api_result
        logger.info("douyin.wtf 失败，尝试回退 yt-dlp")

    # B 站走官方 view API（yt-dlp 在新版风控下 412）
    if _is_bilibili(url):
        api_result = await _fetch_via_bilibili_api(url)
        if api_result:
            return api_result
        logger.info("bilibili view API 失败，尝试回退 yt-dlp")

    resolved = await _resolve_short_url(url)
    # B 站短链跳转后带 redirect 的 buvid，yt-dlp 拿这个 buvid 请求会被反爬 412，去掉 query 让它重起 session
    if "bilibili.com/video/" in resolved and "?" in resolved:
        resolved = resolved.split("?")[0]
        logger.info(f"B 站 URL 去 query: {resolved}")

    def _run() -> Optional[dict]:
        try:
            import yt_dlp  # type: ignore
        except ImportError:
            logger.warning("yt-dlp 未安装，跳过视频解析")
            return None
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 15,
            "http_headers": {"User-Agent": _BROWSER_UA},
            # 部分国内站点要 referer/UA，yt-dlp 自带 extractor 默认带
        }
        if os.path.isfile(_COOKIES_FILE):
            opts["cookiefile"] = _COOKIES_FILE
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(resolved, download=False)
                if not info:
                    return None
                # 兼容 playlist（取第一条）
                if "entries" in info and info["entries"]:
                    info = info["entries"][0]
                return {
                    "title": info.get("title") or "(无标题)",
                    "uploader": info.get("uploader") or info.get("uploader_id") or "",
                    "duration": int(info.get("duration") or 0),
                    "video_url": info.get("url") or "",
                    "thumb_url": info.get("thumbnail") or "",
                    "platform": _label_from_extractor(info.get("extractor_key") or info.get("extractor") or ""),
                    "needs_referer": "",
                }
        except Exception as e:
            msg = str(e)
            # cookies 类错误单独标记，方便上层给友好提示
            if "cookies" in msg.lower() or "fresh cookies" in msg.lower():
                logger.warning(f"yt-dlp 需要 cookies url={resolved[:80]}: {msg[:120]}")
            else:
                logger.warning(f"yt-dlp 解析失败 url={resolved[:80]}: {msg[:200]}")
            return None

    return await asyncio.to_thread(_run)


async def grab_grid(video_url: str, duration: int, referer: str = "") -> Optional[bytes]:
    """
    从视频抽 4 帧合 2×2 缩略图，返回 JPEG bytes。失败 None。
    采样间隔自适应：duration/5（让 4 帧均匀散在视频里），最低 1 秒一帧。
    传 referer 时（抖音 CDN 防盗链）先服务端下载到 /tmp，再喂 ffmpeg；否则 ffmpeg 直接拉。
    """
    # 抖音 CDN 必须带 Referer 才能下载，ffmpeg 不方便设 header，干脆先下到本地
    local_path: Optional[str] = None
    cleanup_local = False
    if referer:
        local_path = await _download_to_tmp(video_url, referer=referer)
        if not local_path:
            return None
        cleanup_local = True
        ffmpeg_input = local_path
    else:
        ffmpeg_input = video_url

    fps = max(1.0, duration / 5) if duration > 0 else 1.0
    fps_expr = f"1/{fps:.2f}"  # ffmpeg fps 滤镜支持分数

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        out_path = f.name
    try:
        cmd = [
            _ffmpeg_bin(), "-y",
            "-i", ffmpeg_input,
            "-vf", f"fps={fps_expr},scale=480:270:force_original_aspect_ratio=decrease,pad=480:270:-1:-1:color=black,tile=2x2",
            "-frames:v", "1",
            "-q:v", "5",
            "-loglevel", "error",
            out_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(f"ffmpeg 抽帧超时 url={ffmpeg_input[:60]}")
            return None
        if proc.returncode != 0:
            logger.warning(f"ffmpeg 抽帧失败 rc={proc.returncode} stderr={stderr.decode()[:200]!r}")
            return None
        size = os.path.getsize(out_path)
        if size == 0:
            return None
        with open(out_path, "rb") as fr:
            return fr.read()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        if cleanup_local and local_path:
            try:
                os.unlink(local_path)
            except OSError:
                pass


# omni 401 熔断：连续 3 次以上 401 就 skip 30 分钟，避免无效 API 调用
_OMNI_AUTH_FAIL_COUNT = 0
_OMNI_AUTH_FAIL_THRESHOLD = 3
_OMNI_CIRCUIT_OPEN_UNTIL = 0.0
_OMNI_CIRCUIT_COOLDOWN = 1800.0


async def describe_grid(grid_bytes: bytes, title: str, uploader: str, personality: str) -> Optional[str]:
    """base64 喂给 omni，让 AI 看 grid 总结视频。失败 None。"""
    global _OMNI_AUTH_FAIL_COUNT, _OMNI_CIRCUIT_OPEN_UNTIL
    cfg = _PROFILES.get("omni") or _PROFILES["default"]
    if not cfg["api_key"]:
        logger.warning("omni key 未配置，跳过视频总结")
        return None
    now = time.time()
    if now < _OMNI_CIRCUIT_OPEN_UNTIL:
        remaining = int(_OMNI_CIRCUIT_OPEN_UNTIL - now)
        logger.info(f"omni 401 熔断中（剩余 {remaining}s），跳过总结")
        return None

    b64 = base64.b64encode(grid_bytes).decode()
    data_uri = f"data:image/jpeg;base64,{b64}"
    hint = (
        f"这是一段视频的 4 帧缩略图（2×2 按时间顺序）。"
        f"标题：{title}。作者：{uploader}。"
        f"用一句话（80 字内）总结视频在讲什么，别复读标题、别罗列画面。"
    )
    messages = [
        {"role": "system", "content": personality},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": hint},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        },
    ]
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.8,
        "max_tokens": 400,
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    endpoint = f"{cfg['api_base']}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
            if 400 <= resp.status_code < 500:
                logger.warning(f"omni 视频总结 4xx: status={resp.status_code} body={resp.text[:200]!r}")
                # 401 单独累计；其他 4xx（如 429 限流）不进熔断逻辑
                if resp.status_code == 401:
                    _OMNI_AUTH_FAIL_COUNT += 1
                    if _OMNI_AUTH_FAIL_COUNT >= _OMNI_AUTH_FAIL_THRESHOLD:
                        _OMNI_CIRCUIT_OPEN_UNTIL = time.time() + _OMNI_CIRCUIT_COOLDOWN
                        logger.warning(
                            f"omni 连续 {_OMNI_AUTH_FAIL_COUNT} 次 401，熔断 "
                            f"{int(_OMNI_CIRCUIT_COOLDOWN)}s（检查 .env 里的 OMNI_API_KEY）"
                        )
                return None
            resp.raise_for_status()
            data = resp.json()
            # 走到这里说明成功，重置熔断计数
            _OMNI_AUTH_FAIL_COUNT = 0
            content = data["choices"][0]["message"]["content"]
            if not content:
                return None
            return content.strip()
    except Exception as e:
        logger.warning(f"omni 视频总结失败 type={type(e).__name__} msg={e!r}")
        return None
