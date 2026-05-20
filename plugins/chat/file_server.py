"""
本地文件中转 HTTP —— 给 NapCat 提供可拉的视频 URL
- bot 把抖音视频下载到 /tmp/qqbot-relay-{token}.mp4
- 暴露 GET /file/{token} 返回该文件
- NapCat 通过 http://nonebot:8080/file/{token} 拉走（Docker 内部网络，公网拿不到）
- token 是 uuid hex，只允许字母数字，防路径穿越
"""
import os

import nonebot
from fastapi import HTTPException
from fastapi.responses import FileResponse
from nonebot.log import logger

RELAY_PREFIX = "qqbot-relay-"
RELAY_DIR = "/tmp"


def relay_path(token: str) -> str:
    return os.path.join(RELAY_DIR, f"{RELAY_PREFIX}{token}.mp4")


def install() -> None:
    """在 FastAPI 上挂 /file/{token} 路由。仅本地网络可达。"""
    try:
        app = nonebot.get_app()
    except Exception as e:
        logger.warning(f"file_server 拿不到 FastAPI app，跳过: {e}")
        return

    @app.get("/file/{token}")
    async def _serve(token: str):
        if not token.isalnum() or not (8 <= len(token) <= 64):
            raise HTTPException(status_code=404)
        path = relay_path(token)
        if not os.path.isfile(path):
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="video/mp4")

    logger.info("file relay 已挂载: GET /file/{token}")
