"""
用量统计：每 key 累计调用 / token，按模型和按日维度。
JSON 文件持久化 + 内存缓存。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, Optional

from . import config

logger = logging.getLogger("usage")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


@dataclass
class KeyUsageStats:
    key_name: str
    total_requests: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    last_used_at: str = ""
    # model -> {"requests": N, "prompt_tokens": N, "completion_tokens": N}
    by_model: Dict[str, Dict[str, int]] = field(default_factory=dict)
    # "YYYY-MM-DD" -> {"requests": N, "prompt_tokens": N, "completion_tokens": N}
    by_day: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def daily(self, day: Optional[str] = None) -> Dict[str, int]:
        return self.by_day.get(day or _today(), {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0})

    @property
    def total_tokens(self) -> int:
        return self.total_prompt_tokens + self.total_completion_tokens


class UsageManager:
    """每次 chat 调用完成时 record()，每次 admin 端调 get/all。"""

    def __init__(self, file_path: Optional[str] = None):
        self._file = file_path or os.path.join(config.STATE_DIR, "usage.json")
        self._stats: Dict[str, KeyUsageStats] = {}
        self._lock = threading.Lock()
        self._load()

    # ---- 持久化 -------------------------------------------------------------

    def _load(self):
        if not os.path.exists(self._file):
            logger.info("usage 文件不存在，从空开始: %s", self._file)
            return
        with open(self._file) as f:
            data = json.load(f)
        for key_name, stats_dict in data.get("stats", {}).items():
            self._stats[key_name] = KeyUsageStats(
                key_name=key_name,
                total_requests=stats_dict.get("total_requests", 0),
                total_prompt_tokens=stats_dict.get("total_prompt_tokens", 0),
                total_completion_tokens=stats_dict.get("total_completion_tokens", 0),
                last_used_at=stats_dict.get("last_used_at", ""),
                by_model=stats_dict.get("by_model", {}),
                by_day=stats_dict.get("by_day", {}),
            )
        logger.info("加载 usage 统计：%d 个 key", len(self._stats))

    def _save(self):
        os.makedirs(os.path.dirname(self._file), exist_ok=True)
        with open(self._file, "w") as f:
            json.dump(
                {"stats": {k: asdict(v) for k, v in self._stats.items()}},
                f, indent=2, ensure_ascii=False,
            )

    # ---- 记录 ---------------------------------------------------------------

    def record(self, key_name: str, model: str, prompt_tokens: int, completion_tokens: int):
        """每次成功调用后记录一次。失败请求不记 token，仅记 request。"""
        with self._lock:
            stats = self._stats.get(key_name)
            if not stats:
                stats = KeyUsageStats(key_name=key_name)
                self._stats[key_name] = stats

            # 全局累计
            stats.total_requests += 1
            stats.total_prompt_tokens += prompt_tokens
            stats.total_completion_tokens += completion_tokens
            stats.last_used_at = datetime.now().isoformat(timespec="seconds")

            # 按模型
            m = stats.by_model.setdefault(model, {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0})
            m["requests"] += 1
            m["prompt_tokens"] += prompt_tokens
            m["completion_tokens"] += completion_tokens

            # 按日
            day = _today()
            d = stats.by_day.setdefault(day, {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0})
            d["requests"] += 1
            d["prompt_tokens"] += prompt_tokens
            d["completion_tokens"] += completion_tokens

            self._save()

    def record_failed(self, key_name: str, model: str):
        """记一次失败调用（只 +1 request，不加 token）。"""
        with self._lock:
            stats = self._stats.get(key_name)
            if not stats:
                stats = KeyUsageStats(key_name=key_name)
                self._stats[key_name] = stats
            stats.total_requests += 1
            stats.last_used_at = datetime.now().isoformat(timespec="seconds")

            m = stats.by_model.setdefault(model, {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0})
            m["requests"] += 1

            day = _today()
            d = stats.by_day.setdefault(day, {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0})
            d["requests"] += 1

            self._save()

    # ---- 查询 ---------------------------------------------------------------

    def get(self, key_name: str) -> Optional[KeyUsageStats]:
        return self._stats.get(key_name)

    def all(self) -> Dict[str, KeyUsageStats]:
        return dict(self._stats)

    def daily_token_count(self, key_name: str, day: Optional[str] = None) -> int:
        """单 key 单日 token 累计（用于配额检查）。"""
        stats = self._stats.get(key_name)
        if not stats:
            return 0
        daily = stats.daily(day)
        return daily.get("prompt_tokens", 0) + daily.get("completion_tokens", 0)

    def global_today(self) -> Dict[str, int]:
        """全局今日汇总（用于 dashboard 顶部 stats card）。"""
        day = _today()
        total_req = 0
        total_p = 0
        total_c = 0
        for s in self._stats.values():
            d = s.daily(day)
            total_req += d.get("requests", 0)
            total_p += d.get("prompt_tokens", 0)
            total_c += d.get("completion_tokens", 0)
        return {
            "requests": total_req,
            "prompt_tokens": total_p,
            "completion_tokens": total_c,
            "total_tokens": total_p + total_c,
        }


# 单例
manager = UsageManager()
