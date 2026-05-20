FROM python:3.11-slim

WORKDIR /app

# 中国用户构建时传 --build-arg USE_CN_MIRROR=true 加速
# 海外用户默认走官方源，无需修改
ARG USE_CN_MIRROR=false

# CJK 字体：长回复转备忘录图片所需（render_memo.py）
# 用文泉驿微米黑（fonts-wqy-microhei，~5MB），比 fonts-noto-cjk 小 10 倍且足够中文显示
RUN if [ "$USE_CN_MIRROR" = "true" ]; then \
      sed -i 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g; s|http://security.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖（先复制 requirements 利用 docker 层缓存）
# 注：ffmpeg 通过 pip 的 imageio-ffmpeg 自带二进制提供
COPY requirements.txt .
RUN pip install --no-cache-dir \
    $([ "$USE_CN_MIRROR" = "true" ] && echo "-i https://pypi.tuna.tsinghua.edu.cn/simple") \
    -r requirements.txt

# 项目代码
COPY . .

EXPOSE 8080

CMD ["python", "bot.py"]
