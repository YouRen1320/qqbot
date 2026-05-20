"""
AI 模型调用 —— 走 litellm 统一接入,换 provider 只改 .env 不动业务代码
- default profile: 闲聊主力; 失败 fallback → 备用模型
- pro profile: 被 @ 复杂问题; 失败 fallback → 备用 pro 模型
- omni profile: 看图回复(多模态)
- fallback 触发条件:任何 None 结果(4xx/超时/空回复/软审核拒绝)
- 主模型熔断:同 profile 5min 内连续失败 ≥3 次 → 后续 5min 直接走 fallback,跳过主调用
  避免主模型真挂时反复试错延迟; 自动恢复
- usage 落库时 profile 字段区分 default / default_fallback,方便事后查"主模型用没用完"
- 想换 deepseek/kimi/claude/glm 改 env 即可(litellm 按 provider 前缀识别)
"""
import os
import time
import warnings
from collections import defaultdict, deque
# litellm 的流返回有时缺字段，pydantic 会打 "Pydantic serializer warnings: ..."
# 一天能刷十几条纯噪音，运行毫无影响。在 import litellm 之前装 filter。
warnings.filterwarnings(
    "ignore",
    message="Pydantic serializer warnings",
    category=UserWarning,
)
import litellm
from litellm import acompletion
from litellm.exceptions import (
    AuthenticationError,
    BadRequestError,
    RateLimitError,
    APIError,
    Timeout,
)
from nonebot.log import logger

# 关 litellm 自带 verbose 和遥测，避免污染日志和外联
litellm.suppress_debug_info = True
litellm.telemetry = False
litellm.drop_params = True  # 自动丢弃 provider 不支持的参数（容错）

# 6 个 profile: 3 个面向上层(default/pro/omni/agent) + 2 个 fallback 内部用
# fallback 字段指向"主模型失败时退一步"的 profile 名; None 表示不再降级
# 主模型 + fallback 备用; agent profile 需要支持 tool calling 的模型
# 写代码用 Claude(self_maintain 单独走 anthropic SDK, 不在这里)
_PROFILES = {
    # 主聊天; 失败 fallback 到备用模型
    "default": {
        "api_key": os.getenv("AI_PRIMARY_API_KEY", ""),
        "api_base": os.getenv("AI_PRIMARY_BASE_URL", "").rstrip("/"),
        "model": os.getenv("AI_PRIMARY_MODEL", "gpt-4o"),
        "fallback": "default_fallback",
    },
    "default_fallback": {
        "api_key": os.getenv("AI_FALLBACK_API_KEY", ""),
        "api_base": os.getenv("AI_FALLBACK_BASE_URL", "").rstrip("/"),
        "model": os.getenv("AI_FALLBACK_MODEL", ""),
        "fallback": None,
    },
    # 被 @ 复杂问题; 失败 fallback 到备用 pro
    "pro": {
        "api_key": os.getenv("AI_PRIMARY_API_KEY", ""),
        "api_base": os.getenv("AI_PRIMARY_BASE_URL", "").rstrip("/"),
        "model": os.getenv("AI_PRIMARY_MODEL_PRO", "gpt-4o"),
        "fallback": "pro_fallback",
    },
    "pro_fallback": {
        "api_key": os.getenv("AI_FALLBACK_API_KEY_PRO") or os.getenv("AI_FALLBACK_API_KEY", ""),
        "api_base": (os.getenv("AI_FALLBACK_BASE_URL_PRO") or os.getenv("AI_FALLBACK_BASE_URL", "")).rstrip("/"),
        "model": os.getenv("AI_FALLBACK_MODEL_PRO", ""),
        "fallback": None,
    },
    # 看图(多模态)
    "omni": {
        "api_key": os.getenv("AI_OMNI_API_KEY") or os.getenv("AI_FALLBACK_API_KEY_PRO") or os.getenv("AI_FALLBACK_API_KEY", ""),
        "api_base": (os.getenv("AI_OMNI_BASE_URL") or os.getenv("AI_FALLBACK_BASE_URL_PRO") or os.getenv("AI_FALLBACK_BASE_URL", "")).rstrip("/"),
        "model": os.getenv("AI_MODEL_OMNI", "gpt-4o"),
        "fallback": None,
    },
    # Agent 工具调用: 需要支持 tool calling 的模型; 失败不 fallback
    # AI_PRIMARY_API_KEY 为空时, _call_one 返 None → __init__.py 兜底退到普通 default 路径
    "agent": {
        "api_key": os.getenv("AI_PRIMARY_API_KEY", ""),
        "api_base": os.getenv("AI_PRIMARY_BASE_URL", "").rstrip("/"),
        "model": os.getenv("AI_PRIMARY_MODEL", "gpt-4o"),
        "fallback": None,
    },
}

# 主模型熔断: 5 min 内主 profile 连续失败 ≥3 次 → 后续 5 min 直接走 fallback
_FALLBACK_BREAKER_WINDOW = 300
_FALLBACK_BREAKER_THRESHOLD = 3
_fallback_failures: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))


def _is_primary_circuit_open(profile: str) -> bool:
    """主模型是否处于熔断中; 是 → caller 应直接走 fallback 不再试主"""
    dq = _fallback_failures[profile]
    cutoff = time.time() - _FALLBACK_BREAKER_WINDOW
    while dq and dq[0] < cutoff:
        dq.popleft()
    return len(dq) >= _FALLBACK_BREAKER_THRESHOLD


def _record_primary_failure(profile: str) -> None:
    _fallback_failures[profile].append(time.time())

# 想换 provider 改这里前缀：openai/ -> deepseek/, anthropic/, gemini/ 等
_PROVIDER_PREFIX = os.getenv("AI_PROVIDER_PREFIX", "openai")

# 单次调用超时; 缩到 15s 让 GPT 偶发抽风时 fallback 更快接管 (原 25s 体感 ~50s 太慢)
_TIMEOUT = 15.0
_MAX_RETRIES = 1

# 上游"软审核"信号：200 OK 但 content 直接是拒绝文案。当 content 命中任意片段时视为无返回。
_REJECT_PATTERNS = (
    "The request was rejected because it was considered high risk",
    "rejected because it was considered high risk",
    "considered high risk",
    "content was filtered",
    "I cannot assist with",
    "I can't assist with",
    "抱歉，我无法",
    "抱歉，我不能",
    "无法回答这个问题",
    "出于安全考虑",
    "存在合规风险",
)


def _is_rejected(content: str) -> bool:
    if not content:
        return False
    low = content.strip()
    return any(p in low for p in _REJECT_PATTERNS)


# 软审核熔断：某群短时间连续被上游拒绝时，主动插话（at_me=False）暂停一段
# 防止 bot 反复试错把 token 烧掉，也避免上游把这个 key 标为高风险源
_REJECT_WINDOW_SEC = 300       # 5 分钟滑窗
_REJECT_THRESHOLD = 3          # 窗口内 >=3 次 → 熔断
_reject_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=20))


def _record_soft_reject(group_id: int) -> None:
    if not group_id or group_id <= 0:
        return
    _reject_history[group_id].append(time.time())


def should_skip_volunteer(group_id: int) -> bool:
    """供 caller 在 at_me=False 时探测：本群是否处于软审核熔断中。"""
    if not group_id or group_id <= 0:
        return False
    dq = _reject_history[group_id]
    cutoff = time.time() - _REJECT_WINDOW_SEC
    while dq and dq[0] < cutoff:
        dq.popleft()
    return len(dq) >= _REJECT_THRESHOLD


def breaker_status() -> dict[int, int]:
    """admin /status 用：返回当前熔断中的群 → 窗内拒次数。
    顺手把过期条目清掉（懒清理）。"""
    cutoff = time.time() - _REJECT_WINDOW_SEC
    out: dict[int, int] = {}
    for gid, dq in _reject_history.items():
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= _REJECT_THRESHOLD:
            out[gid] = len(dq)
    return out


async def _call_one(
    profile: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    group_id: int,
) -> str | None:
    """单次调用某 profile, 失败返回 None。chat_completion 的内部 worker。"""
    cfg = _PROFILES.get(profile) or _PROFILES["default"]
    if not cfg["api_key"]:
        logger.error(f"AI key({profile}) 未配置")
        return None

    # litellm 通过 "openai/<model>" 走 OpenAI 兼容协议(GPT / MiMo / DeepSeek 都兼容)
    model = f"{_PROVIDER_PREFIX}/{cfg['model']}"

    try:
        resp = await acompletion(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            api_base=cfg["api_base"],
            api_key=cfg["api_key"],
            timeout=_TIMEOUT,
            num_retries=_MAX_RETRIES,
        )
        content = resp.choices[0].message.content
        # 不管 content 空不空都记 usage —— 空回复也烧了 token; profile 区分主/fallback 便于查账
        _log_usage_safe(resp, group_id, profile, cfg["model"])
        if content and _is_rejected(content):
            logger.warning(
                f"AI({profile}/{cfg['model']}) 软审核拒绝 head={content.strip()[:60]!r}"
            )
            _record_soft_reject(group_id)
            return None
        if content:
            return content.strip()
        logger.warning(
            f"AI({profile}/{cfg['model']}) content 为空(可能 reasoning 吃满 max_tokens)"
        )
        return None
    except (AuthenticationError, BadRequestError) as e:
        logger.warning(f"AI({profile}/{cfg['model']}) 4xx 不重试: {e}")
        return None
    except (RateLimitError, APIError, Timeout) as e:
        logger.warning(f"AI({profile}/{cfg['model']}) 网络/超时/限速重试用尽: {e}")
        return None
    except Exception as e:
        logger.warning(f"AI({profile}/{cfg['model']}) 未预期异常: {e}")
        return None


async def chat_completion(
    messages: list[dict],
    temperature: float = 0.95,
    max_tokens: int = 500,
    profile: str = "default",
    group_id: int = 0,
) -> str | None:
    """
    调用 AI;profile = 'default' | 'pro' | 'omni'。失败返回 None。
    主模型失败 → 自动 fallback 到配置的副 profile;主模型连续失败 ≥3次/5min → 熔断
    期间直接走 fallback,避开重复试错的延迟。
    """
    cfg = _PROFILES.get(profile) or _PROFILES["default"]
    fb_profile = cfg.get("fallback")

    # 熔断中: 直接 fallback, 跳过主调用(避免 30s 超时白等)
    if fb_profile and _is_primary_circuit_open(profile):
        logger.info(f"AI({profile}) 主模型熔断中, 直接走 fallback={fb_profile}")
        return await _call_one(fb_profile, messages, temperature, max_tokens, group_id)

    # 正常路径: 先主, 失败立即降级
    result = await _call_one(profile, messages, temperature, max_tokens, group_id)
    if result is not None:
        return result
    if not fb_profile:
        return None
    _record_primary_failure(profile)
    logger.info(f"AI({profile}) 主模型返回 None, fallback → {fb_profile}")
    return await _call_one(fb_profile, messages, temperature, max_tokens, group_id)


async def chat_with_tools(
    messages: list[dict],
    tools: list[dict],
    tool_handlers: dict,
    profile: str = "agent",
    group_id: int = 0,
    max_iterations: int = 5,
    temperature: float = 0.7,
    max_tokens: int = 1500,
) -> tuple[str | None, list[dict]]:
    """
    Agent tool calling 多轮对话(给群管理用)。
    - GPT 决定要不要调工具; 调了 → 我们执行 → 把结果塞回下一轮 → GPT 给最终人话回复
    - tool_handlers: {tool_name: async fn(args:dict) → str (人话, 进下一轮 messages)}
    - max_iterations 防 GPT 死循环重复调工具
    - 返回: (最终用户回复文本, 执行过的 tool_calls 摘要列表)
    """
    import json as _json
    cfg = _PROFILES.get(profile) or _PROFILES["default"]
    if not cfg["api_key"]:
        logger.error(f"AI key({profile}) 未配置")
        return None, []
    model = f"{_PROVIDER_PREFIX}/{cfg['model']}"
    executed: list[dict] = []

    for iteration in range(max_iterations):
        try:
            resp = await acompletion(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=temperature,
                max_tokens=max_tokens,
                api_base=cfg["api_base"],
                api_key=cfg["api_key"],
                timeout=_TIMEOUT,
                num_retries=_MAX_RETRIES,
            )
        except (AuthenticationError, BadRequestError) as e:
            logger.warning(f"agent chat({cfg['model']}) 4xx iter={iteration}: {e}")
            return None, executed
        except (RateLimitError, APIError, Timeout) as e:
            logger.warning(f"agent chat({cfg['model']}) 网络/限速 iter={iteration}: {e}")
            return None, executed
        except Exception as e:
            logger.warning(f"agent chat({cfg['model']}) 未预期 iter={iteration}: {e}")
            return None, executed

        msg = resp.choices[0].message
        _log_usage_safe(resp, group_id, profile, cfg["model"])
        tc = getattr(msg, "tool_calls", None) or []

        if not tc:
            # 没工具调用 → 最终回复
            content = msg.content or ""
            return (content.strip() if content else None), executed

        # 有 tool_calls: 把这条 assistant 消息(含 tool_calls)塞回 messages, 然后逐个执行
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": t.id,
                    "type": "function",
                    "function": {"name": t.function.name, "arguments": t.function.arguments},
                }
                for t in tc
            ],
        })
        for tool_call in tc:
            name = tool_call.function.name
            try:
                args = _json.loads(tool_call.function.arguments or "{}")
            except Exception:
                args = {}
            handler = tool_handlers.get(name)
            if handler is None:
                result = f"工具 {name} 不存在"
            else:
                try:
                    result = await handler(args)
                except Exception as e:
                    result = f"执行失败: {type(e).__name__}: {e}"
            executed.append({"name": name, "args": args, "result": result})
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

    # 超出最大迭代仍未给最终文本回复
    logger.warning(f"agent chat 超过 {max_iterations} 轮仍未收敛, 放弃")
    return None, executed


def _log_usage_safe(resp, group_id: int, profile: str, model: str) -> None:
    """从 litellm response 抽 usage 落库 —— 容错，任何异常都吞掉"""
    import asyncio
    try:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        total = int(getattr(usage, "total_tokens", 0) or prompt + completion)
        # 延迟 import 避免循环
        from . import db
        asyncio.create_task(db.log_usage(group_id, profile, model, prompt, completion, total))
    except Exception as e:
        logger.debug(f"usage 抽取失败（不影响主流程）: {e}")
