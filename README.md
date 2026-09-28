# QQ 群聊 AI Bot

基于 [NoneBot2](https://nonebot.dev/) + [NapCat](https://github.com/NapNeko/NapCatQQ) 的 QQ 群聊 AI 机器人。

有性格、有记忆、会看图、会解析视频、能管群、偶尔还会主动插话。完成必要配置后，可用 `docker compose up -d` 启动。

## 功能

- **AI 对话** — 支持任何 OpenAI 兼容 API（GPT / DeepSeek / 通义 / Kimi / MiMo 等），主模型挂了自动切备用
- **人设系统** — 内置"傲娇学姐"人格，`.env` 改名 + `personality.py` 改人设即可自定义角色
- **看图说话** — @bot 发图，AI 读图回复（需多模态模型）
- **视频解析** — 群里发抖音/B站/TikTok 链接自动解析，发封面 + 元信息，@bot 还能 AI 总结
- **群管理** — @bot 可禁言、改头衔、改名片、换群头像、发公告（bot 需要群主/管理员权限）
- **AI 生图** — "画一张xxx" 触发 AI 生图（需 DALL-E / gpt-image-2 API）
- **记忆系统** — SQLite 落盘历史对话，FTS5 全文检索长记忆
- **复读机** — 群友复读 2 次以上概率跟风
- **心情系统** — 随时间波动的心情影响发言概率和语气
- **风控** — 白名单群、作息时段、每日上限、频率窗口、输入黑名单、软审核熔断
- **联网搜索** — @bot 问时事，自动联网搜索再回答（需 Tavily / Serper API key）
- **AI 自维护** — Claude 自动诊断日志、修 bug、安全回滚（可选，需 Anthropic API key）

## 快速开始

### 前置条件

- 一台 Linux 服务器（推荐 2C4G+）
- Docker + Docker Compose
- 一个 QQ 号给 bot 用（建议小号）
- 至少一个 AI API key（OpenAI / DeepSeek / 通义等任选）

### 部署步骤

```bash
# 1. 克隆项目
git clone https://github.com/YouRen1320/qqbot.git
cd qqbot

# 2. 创建数据目录
mkdir -p data napcat/QQ

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env，至少填写：
#   - ONEBOT_ACCESS_TOKEN（随便填一串，NapCat 配置要一致）
#   - AI_PRIMARY_API_KEY + AI_PRIMARY_BASE_URL（你的 AI 提供商）
#   - ENABLED_GROUPS（允许 bot 工作的群号）
#   - ADMIN_QQ（你自己的 QQ，用于管理命令）

# 4. 配置 NapCat（把 123456789 替换成你的 bot QQ 号）
cp napcat/config/onebot11.json.example napcat/config/onebot11_123456789.json
cp napcat/config/napcat.json.example napcat/config/napcat.json
# 编辑 onebot11_123456789.json，把 token 改成和 .env 里 ONEBOT_ACCESS_TOKEN 一致的值

# 5. 启动（中国服务器加 --build-arg USE_CN_MIRROR=true 加速构建）
docker compose up -d

# 6. 扫码登录 QQ
# 浏览器打开 http://你的服务器IP:6099
# WebUI token 在 NapCat 首次启动时自动生成，查看方式：
docker compose logs napcat | grep -i token
# 用 bot 的 QQ 扫码登录
```

登录成功后 bot 即上线，在 `ENABLED_GROUPS` 配置的群里 @它 试试。

### 必填与可选配置

首次启动前，至少需要完成以下配置：

| 配置 | 是否必填 | 用途 |
|------|----------|------|
| `ONEBOT_ACCESS_TOKEN` | 必填 | NoneBot 与 NapCat 之间的连接鉴权；两处必须一致 |
| `AI_PRIMARY_API_KEY` / `AI_PRIMARY_BASE_URL` | 必填 | 主聊天模型凭据与兼容接口地址 |
| `ENABLED_GROUPS` | 必填 | 群白名单；留空时 bot 不在任何群响应 |
| `ADMIN_QQ` | 建议填写 | 私聊管理命令与高风险群管理工具的唯一授权账号 |
| `AI_FALLBACK_*` | 可选 | 主模型失败时自动降级；留空时不启用备用模型 |
| `IMAGE_GEN_*` | 可选 | AI 生图；留空时相关能力不可用 |
| `SEARCH_API_KEY` | 可选 | 联网搜索；留空时相关能力不可用 |
| `ANTHROPIC_API_KEY` | 可选 | AI 自维护；留空时自维护命令和定时巡逻均不启用 |

不要提交 `.env`、QQ 登录数据或运行时数据库；这些路径已经加入 `.gitignore`。

### 查看日志

```bash
docker compose logs -f nonebot    # bot 日志
docker compose logs -f napcat     # NapCat 日志
```

## 配置说明

### 改名 / 改人设

bot 名称通过 `.env` 配置，改一处全局生效：

```env
BOT_NAME=你的bot名字
BOT_ALIASES=别名1,别名2
```

人设（性格、语气、回复风格）编辑 `plugins/chat/personality.py` 里的 `PERSONALITY` 字符串。默认是"墨七七"——傲娇学姐型导师，你可以改成任何角色。

### AI 模型

项目通过 [litellm](https://github.com/BerriAI/litellm) 统一接入，支持所有 OpenAI 兼容 API。`.env` 里配置：

| 变量 | 说明 |
|------|------|
| `AI_PRIMARY_API_KEY` | 主模型 API key |
| `AI_PRIMARY_BASE_URL` | 主模型 API 地址（如 `https://api.deepseek.com/v1`） |
| `AI_PRIMARY_MODEL` | 闲聊用的模型（如 `deepseek-chat`） |
| `AI_PRIMARY_MODEL_PRO` | 复杂问题用的模型（被 @ 时升级） |
| `AI_FALLBACK_*` | 备用模型（主模型挂了自动切，可选） |
| `IMAGE_GEN_*` | 图片生成（可选） |
| `SEARCH_API_KEY` | 联网搜索（可选，支持 Tavily / Serper / Brave） |
| `ANTHROPIC_*` | AI 自维护（可选） |

### NapCat 配置

NapCat 是 QQ 协议实现，负责收发 QQ 消息。关键配置：

- `napcat/config/onebot11_<QQ号>.json` — 反向 WS 连接配置和 token
- `napcat/config/napcat.json` — 日志和运行参数

文件名里的 `<QQ号>` 必须是 bot 登录的那个 QQ 号。NapCat 和 NoneBot 在同一个 Docker 网络内，通过 `ws://nonebot:8080` 直连，无需配置宿主机 IP。

## 项目结构

```
.
├── bot.py                   # NoneBot2 入口
├── plugins/chat/            # 主插件目录
│   ├── __init__.py          # 消息处理主循环
│   ├── config.py            # Bot 名称配置（从 .env 读取）
│   ├── personality.py       # 人设 / system prompt
│   ├── ai_client.py         # AI 模型调用（litellm 统一接入）
│   ├── agent_tools.py       # 群管理工具（禁言/头衔/生图等）
│   ├── safety.py            # 风控（频率/时段/每日上限）
│   ├── vision.py            # 看图回复
│   ├── video_parse.py       # 视频链接解析
│   ├── image_gen.py         # AI 生图
│   ├── db.py                # SQLite 持久化 + FTS5 全文检索
│   ├── mood.py              # 心情系统
│   ├── blacklist.py         # 输入黑名单
│   ├── self_maintain.py     # AI 自维护（Claude 诊断/修复）
│   └── ...                  # 其他子模块
├── napcat/config/           # NapCat 配置模板
├── docker-compose.yml       # 三合一容器编排（NapCat + Bot + 抖音解析）
├── Dockerfile               # 支持 --build-arg USE_CN_MIRROR=true 国内加速
└── .env.example             # 环境变量模板（所有配置项均有注释）
```

## 安全说明

- **AI 自维护**（`self_maintain.py`）允许 Claude 读取日志和修改 `plugins/chat/` 下的代码。安全措施：严格相对路径白名单、自动备份、Python 语法检查、精确变更清单，以及宿主机 90 秒未恢复连接时自动回滚。需要 `ANTHROPIC_API_KEY`，不配置则完全禁用。

如需启用自动提交和故障回滚，还要把 `scripts/maintain_watchdog.sh` 部署到宿主机并加入每分钟执行的 cron。脚本默认项目路径是 `/root/qqbot/nonebot`；实际路径不同时，请先修改脚本顶部的 `ROOT`，再按脚本注释中的命令安装。升级本仓库后也应同步更新宿主机上的脚本。
- **敏感词黑名单**（`plugins/chat/data/sensitive_words.txt`）来源于 [konsheng/Sensitive-lexicon](https://github.com/konsheng/Sensitive-lexicon)（MIT），可根据需要自行增删。

## 常见问题

**Q: NapCat 连不上 NoneBot？**

三个容器在同一个 Docker Compose 网络内，NapCat 通过 `ws://nonebot:8080` 连接，无需配置宿主机 IP。请确认：1) `onebot11_<QQ号>.json` 里 `url` 是 `ws://nonebot:8080/onebot/v11/ws`；2) `token` 和 `.env` 的 `ONEBOT_ACCESS_TOKEN` 一致。

**Q: NapCat WebUI 打不开？**

默认端口 6099，确保防火墙放行。WebUI token 在首次启动时自动生成，运行 `docker compose logs napcat | grep -i token` 查看。

**Q: 怎么换 AI 模型？**

改 `.env` 里的 `AI_PRIMARY_BASE_URL` 和 `AI_PRIMARY_MODEL`。支持 DeepSeek、通义千问、Kimi、GLM 等任何 OpenAI 兼容 API。改完 `docker compose restart nonebot` 生效。

**Q: 中国服务器构建很慢？**

编辑 `docker-compose.yml`，在 nonebot 的 `build.args` 里把 `USE_CN_MIRROR` 改成 `"true"`，apt 和 pip 会走清华镜像。

**Q: 不需要某些功能？**

- 视频解析：`docker-compose.yml` 里注释掉 `douyin-api` 服务
- AI 生图：`.env` 里不填 `IMAGE_GEN_API_KEY`
- 联网搜索：`.env` 里不填 `SEARCH_API_KEY`
- AI 自维护：`.env` 里不填 `ANTHROPIC_API_KEY`

## 本地检查

项目使用 Python 3.11。测试只覆盖本地逻辑，不连接真实 QQ、AI 模型或第三方接口：

```bash
python -m unittest discover -s tests -v
python -m compileall -q bot.py plugins tests
```

GitHub Actions 会在提交到 `main` 或发起 Pull Request 时自动执行相同检查。

## 使用边界与免责声明

- 本项目仅供学习交流，请遵守腾讯 QQ 用户协议与相关法律法规。使用机器人可能违反平台自动化条款，**账号存在被风控或封禁的风险，需自行承担**。
- 仅将机器人用于你自有或已获得管理员授权的群：`ENABLED_GROUPS` 白名单就是为此设计的，请勿配置到未经授权的群。
- 不得用于骚扰、刷屏、垃圾营销、价格欺诈或任何违法用途；群管理功能（禁言、改头衔等）仅应作用于你有权管理的群。
- NapCat 为第三方独立项目，与本仓库作者无关；其行为与风险请查阅其自身文档。
- 你配置的外部 AI 服务（模型 API、搜索 API）产生的费用与数据处理责任由使用者自行承担；请勿让 bot 处理或转发你不愿公开的个人信息。

## 致谢

- [NoneBot2](https://nonebot.dev/) — Python 异步聊天机器人框架
- [NapCat](https://github.com/NapNeko/NapCatQQ) — 基于 NTQQ 的 QQ 协议实现
- [litellm](https://github.com/BerriAI/litellm) — 100+ LLM 统一调用
- [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) — 抖音/TikTok 数据采集

## License

[MIT](LICENSE)
