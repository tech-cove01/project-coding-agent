"""评测脚本共享的配置/客户端加载工具。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 确保能 import 项目根下的 coding_agent 包。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from coding_agent.config import AppConfig, ProviderConfig, load_config  # noqa: E402
from coding_agent.client import create_client, LLMClient  # noqa: E402

DEFAULT_CONFIG_PATHS = [
    Path.home() / ".coding_agent" / "config.yaml",
    Path(_PROJECT_ROOT) / ".coding_agent" / "config.yaml",
    Path.cwd() / ".coding_agent" / "config.yaml",
]


def find_config_path() -> Path | None:
    """返回第一个存在的配置文件路径，找不到返回 None。"""
    for p in DEFAULT_CONFIG_PATHS:
        if p.exists():
            return p
    return None


def load_app_config(path: Path | None = None) -> AppConfig:
    """加载 AppConfig。指定 path 则只加载该文件，否则走默认候选链。"""
    if path is not None:
        return load_config(path)
    return load_config(None)


def pick_provider(config: AppConfig, name: str | None = None) -> ProviderConfig | None:
    """按 name（或第一个可用）选取 provider。"""
    if not config.providers:
        return None
    if name:
        for p in config.providers:
            if p.name == name:
                return p
    return config.providers[0]


def require_provider(path: Path | None = None) -> ProviderConfig:
    """加载配置并确保至少有一个 provider，否则抛出带指引的错误。"""
    config_path = path or find_config_path()
    config = load_app_config(config_path)
    provider = pick_provider(config)
    if provider is None:
        raise SystemExit(
            "未找到可用的 provider。请先在配置文件中配置 providers，"
            f"或在 {config_path} 检查。"
        )
    return provider


def make_client(provider: ProviderConfig | None = None) -> LLMClient | None:
    """构造一个 LLMClient。provider 为空或缺 api key 时返回 None（评测应优雅跳过）。"""
    provider = provider or require_provider()
    if not provider.resolve_api_key():
        return None
    try:
        return create_client(provider)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 无法构造 client: {e}", file=sys.stderr)
        return None


def env_bool(name: str, default: bool = False) -> bool:
    """读取布尔环境变量。"""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")


def token_estimate(text: str, chars_per_token: float = 3.5) -> int:
    """字符数 -> 近似 token 数，与 conversation.estimate_tokens 的启发式一致。"""
    if not text:
        return 0
    return max(1, int(len(text) / chars_per_token))
