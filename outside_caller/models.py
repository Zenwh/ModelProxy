"""
模型白名单 + 对外名 ↔ MP 实际名 映射 + MP 端点路由 + fallback 列表。

MP 三种端点：
  - "messages"  → /v1/messages   (Anthropic claude_native 格式)
  - "responses" → /v1/responses  (OpenAI Responses API 格式)
  - "chat"      → /v1/chat/completions  (OpenAI Chat 格式)

20260525-v2: 引入 fallback 字段。gateway 解析后将 (upstream_model, endpoint, fallback)
            放入 v3 payload 透传给 bot，bot 不再依赖本地表（架构 A）。

20260525: 对外名（key）已与 mp_name 对齐，不再做大小写/分隔符转换。
新增 gpt-5.4-mini / deepseek-v4-pro / deepseek-v4-flash 三个模型。
"""
from __future__ import annotations

from typing import List, Optional

# 对外暴露的模型
# fallback: 上游主模型不支持/不可用时按顺序尝试的备选 upstream 名
MODELS = {
    # Anthropic Messages
    "claude-opus-4-7":    {"mp_name": "claude-opus-4-7",   "endpoint": "messages",  "vendor": "anthropic", "fallback": ["claude-opus-4-7-qianli"]},
    "claude-opus-4-6":    {"mp_name": "claude-opus-4-6",   "endpoint": "messages",  "vendor": "anthropic", "fallback": ["claude-opus-4-6-qianli"]},
    "claude-sonnet-4-6":  {"mp_name": "claude-sonnet-4-6", "endpoint": "messages",  "vendor": "anthropic", "fallback": ["claude-sonnet-4-6-qianli"]},
    # OpenAI Responses
    "gpt-5.5":            {"mp_name": "gpt-5.5",           "endpoint": "responses", "vendor": "openai",   "fallback": []},
    "gpt-5.4":            {"mp_name": "gpt-5.4",           "endpoint": "responses", "vendor": "openai",   "fallback": []},
    "gpt-5.4-mini":       {"mp_name": "gpt-5.4-mini",      "endpoint": "responses", "vendor": "openai",   "fallback": []},
    # OpenAI Chat
    "kimi-k2.6":          {"mp_name": "kimi-k2.6",         "endpoint": "chat",      "vendor": "moonshot", "fallback": ["kimi-k2.6-aliyun"]},
    "glm-5.1":            {"mp_name": "glm-5.1",           "endpoint": "chat",      "vendor": "zhipu",    "fallback": ["glm-5.1-aliyun"]},
    "deepseek-v3.2-think-ks": {"mp_name": "deepseek-v3.2-think-ks", "endpoint": "chat", "vendor": "deepseek", "fallback": []},
    "deepseek-v3.2-ks":   {"mp_name": "deepseek-v3.2-ks",  "endpoint": "chat",      "vendor": "deepseek", "fallback": []},
    "ccr/deepseek-v3.2-think-ks": {"mp_name": "ccr/deepseek-v3.2-think-ks", "endpoint": "chat", "vendor": "deepseek", "fallback": []},
    "ccr/deepseek-v3.2-ks": {"mp_name": "ccr/deepseek-v3.2-ks", "endpoint": "chat", "vendor": "deepseek", "fallback": []},
    "deepseek-v4-pro":    {"mp_name": "deepseek-v4-pro",   "endpoint": "chat",      "vendor": "deepseek", "fallback": []},
    "deepseek-v4-flash":  {"mp_name": "deepseek-v4-flash", "endpoint": "chat",      "vendor": "deepseek", "fallback": []},
}


def is_supported(pub_name: str) -> bool:
    """对外名是否在白名单。"""
    return pub_name in MODELS


def to_mp_name(pub_name: str) -> Optional[str]:
    """对外名 → MP 实际名。"""
    info = MODELS.get(pub_name)
    return info["mp_name"] if info else None


def to_endpoint(pub_name: str) -> Optional[str]:
    """对外名 → MP 端点类型 (messages / responses / chat)。"""
    info = MODELS.get(pub_name)
    return info["endpoint"] if info else None


def get_fallback(pub_name: str) -> List[str]:
    """对外名 → 备选 upstream 名列表（可能为空）。"""
    info = MODELS.get(pub_name)
    return list(info.get("fallback") or []) if info else []


def list_models() -> List[str]:
    """列出所有对外名。"""
    return list(MODELS.keys())


def get_info(pub_name: str) -> Optional[dict]:
    """获取模型详细信息。"""
    return MODELS.get(pub_name)
