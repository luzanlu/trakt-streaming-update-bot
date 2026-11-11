import base64
import binascii
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

try:  # 可选依赖：真实浏览器 TLS 指纹，降低 Cloudflare 拦截概率
    from curl_cffi import requests as curl_cffi_requests
except Exception:  # pragma: no cover - 未安装时退回 requests
    curl_cffi_requests = None


def parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logging.warning("环境变量 %s=%r 不是整数，将使用默认值 %s。", name, raw, default)
        return default


def split_env_list(raw: str) -> List[str]:
    return [item.strip() for item in re.split(r"[\n,]+", raw or "") if item.strip()]


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "bot_config.json"


def load_bot_config() -> Dict[str, Any]:
    raw_path = os.getenv("BOT_CONFIG_FILE", "").strip()
    config_file = Path(raw_path) if raw_path else CONFIG_FILE
    if not config_file.is_absolute():
        config_file = BASE_DIR / config_file

    if not config_file.exists():
        return {}

    try:
        raw = json.loads(config_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{config_file.name} 不是合法 JSON：{exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"读取 {config_file.name} 失败：{exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError(f"{config_file.name} 顶层结构必须是对象")
    return raw


BOT_CONFIG = load_bot_config()
MISSING_CONFIG_VALUE = object()


def config_section(name: str) -> Dict[str, Any]:
    section = BOT_CONFIG.get(name, {})
    return section if isinstance(section, dict) else {}


def config_value(section_name: str, key: str) -> Any:
    section = config_section(section_name)
    if key not in section:
        return MISSING_CONFIG_VALUE
    return section[key]


def env_override(name: str) -> Optional[str]:
    raw = os.getenv(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw if raw else None


def parse_bool_value(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def config_bool(section_name: str, key: str, env_name: str, default: bool) -> bool:
    raw_env = env_override(env_name)
    if raw_env is not None:
        return parse_bool_value(raw_env, default)

    value = config_value(section_name, key)
    if value is MISSING_CONFIG_VALUE:
        return default
    return parse_bool_value(value, default)


def config_int(section_name: str, key: str, env_name: str, default: int) -> int:
    raw_env = env_override(env_name)
    value = raw_env if raw_env is not None else config_value(section_name, key)
    if value is MISSING_CONFIG_VALUE or value is None or str(value).strip() == "":
        return default

    try:
        return int(str(value).strip())
    except ValueError:
        logging.warning("配置 %s.%s=%r 不是整数，将使用默认值 %s。", section_name, key, value, default)
        return default


def config_str(section_name: str, key: str, env_name: str, default: str) -> str:
    raw_env = env_override(env_name)
    value = raw_env if raw_env is not None else config_value(section_name, key)
    if value is MISSING_CONFIG_VALUE or value is None:
        return default
    text = str(value).strip()
    return text if text else default


def coerce_config_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return split_env_list(value)
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def config_list(
    section_name: str,
    key: str,
    env_name: str,
    default: Sequence[str],
) -> List[str]:
    raw_env = env_override(env_name)
    if raw_env is not None:
        return split_env_list(raw_env)

    value = config_value(section_name, key)
    if value is MISSING_CONFIG_VALUE:
        return list(default)
    return coerce_config_list(value)


def config_list_optional(section_name: str, key: str, env_name: str) -> Optional[List[str]]:
    raw_env = env_override(env_name)
    if raw_env is not None:
        return split_env_list(raw_env)

    value = config_value(section_name, key)
    if value is MISSING_CONFIG_VALUE:
        return None
    return coerce_config_list(value)


def unique_keep_order(values: Sequence[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def normalize_justwatch_country(value: str) -> str:
    country = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        logging.warning("JustWatch 国家代码 %r 无效，将使用 TW。", value)
        return "TW"
    return country


def justwatch_country_new_url(country: str) -> str:
    return f"https://www.justwatch.com/{normalize_justwatch_country(country).lower()}/new"


def normalize_config_token(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(value).strip().lower())


TRAKT_ENABLED = config_bool("trakt", "enabled", "TRAKT_ENABLED", True)
TRAKT_SHOWS_ENABLED = config_bool("trakt", "shows", "TRAKT_SHOWS_ENABLED", True)
TRAKT_MOVIES_ENABLED = config_bool("trakt", "movies", "TRAKT_MOVIES_ENABLED", True)
TRAKT_API_VERSION = "2"
TRAKT_CALENDAR_DAYS = 1
TRAKT_REDIRECT_URI = os.getenv("TRAKT_REDIRECT_URI", "urn:ietf:wg:oauth:2.0:oob").strip()
REQUEST_TIMEOUT = 20
MAX_STATE_RECORDS = 5000

DEFAULT_JUSTWATCH_COUNTRY = "TW"
DEFAULT_JUSTWATCH_PROVIDERS: List[str] = []
JUSTWATCH_ENABLED = config_bool("justwatch", "enabled", "JUSTWATCH_ENABLED", False)
JUSTWATCH_COUNTRY = normalize_justwatch_country(
    config_str("justwatch", "country", "JUSTWATCH_COUNTRY", DEFAULT_JUSTWATCH_COUNTRY)
)
JUSTWATCH_LANGUAGE = config_str("justwatch", "language", "JUSTWATCH_LANGUAGE", "zh")
JUSTWATCH_MEDIA_TYPES = unique_keep_order(
    normalize_config_token(item)
    for item in config_list("justwatch", "media_types", "JUSTWATCH_MEDIA_TYPES", ["show", "movie"])
)
JUSTWATCH_PROVIDERS = config_list(
    "justwatch",
    "providers",
    "JUSTWATCH_PROVIDERS",
    DEFAULT_JUSTWATCH_PROVIDERS,
)
configured_justwatch_urls = config_list_optional("justwatch", "urls", "JUSTWATCH_URLS")
JUSTWATCH_URLS = (
    configured_justwatch_urls
    if configured_justwatch_urls is not None
    else [justwatch_country_new_url(JUSTWATCH_COUNTRY)]
)
JUSTWATCH_BOOTSTRAP_ONLY = config_bool("justwatch", "bootstrap_only", "JUSTWATCH_BOOTSTRAP_ONLY", True)
JUSTWATCH_ITEMS_PER_PROVIDER = max(1, config_int("justwatch", "items_per_provider", "JUSTWATCH_ITEMS_PER_PROVIDER", 10))
JUSTWATCH_MAX_ITEMS = max(1, config_int("justwatch", "max_items", "JUSTWATCH_MAX_ITEMS", 100))
JUSTWATCH_MAX_AGE_DAYS = max(0, config_int("justwatch", "max_age_days", "JUSTWATCH_MAX_AGE_DAYS", 14))
JUSTWATCH_USER_AGENT = os.getenv(
    "JUSTWATCH_USER_AGENT",
    "Mozilla/5.0 (compatible; trakt-streaming-update-bot/1.0)",
).strip()
JUSTWATCH_GRAPHQL_URL = "https://apis.justwatch.com/graphql"
JUSTWATCH_GRAPHQL_QUERY = """
query GetNewTitles(
  $country: Country!
  $date: Date
  $filter: TitleFilter!
  $first: Int!
  $language: Language!
  $pageType: NewPageType!
  $priceDrops: Boolean!
) {
  newTitles(
    country: $country
    date: $date
    filter: $filter
    first: $first
    pageType: $pageType
    priceDrops: $priceDrops
  ) {
    totalCount
    edges {
      cursor
      newOffer(platform: WEB) {
        id
        dateCreated
        newElementCount
        package {
          clearName
          shortName
        }
      }
      node {
        __typename
        id
        objectType
        content(country: $country, language: $language) {
          title
          fullPath
        }
        ... on Season {
          show {
            content(country: $country, language: $language) {
              title
              fullPath
            }
          }
        }
      }
    }
  }
}
"""

DEFAULT_FFZY_DOMAINS = [
    "ffzy1.tv",
    "ffzy2.tv",
    "ffzy3.tv",
    "ffzy4.tv",
    "ffzy5.tv",
]
DEFAULT_FFZY_TYPE_IDS = ["13", "14", "15", "16", "29", "30", "31"]
DEFAULT_FFZY_TYPE_NAMES = ["国产剧", "香港剧", "韩国剧", "欧美剧", "国产动漫", "日韩动漫", "欧美动漫"]
FFZY_CATEGORY_IDS = dict(zip(DEFAULT_FFZY_TYPE_NAMES, DEFAULT_FFZY_TYPE_IDS))
FFZY_ENABLED = config_bool("ffzy", "enabled", "FFZY_ENABLED", False)
FFZY_DOMAINS = config_list("ffzy", "domains", "FFZY_DOMAINS", DEFAULT_FFZY_DOMAINS)
FFZY_USER_AGENT = os.getenv(
    "FFZY_USER_AGENT",
    "Mozilla/5.0 (compatible; trakt-streaming-update-bot/1.0)",
).strip()

DEFAULT_BFZY_DOMAINS = [
    "bfzyapi.com",
    "bfzy.tv",
]
DEFAULT_BFZY_TYPE_IDS = ["31", "33", "34", "32", "40", "41", "42"]
DEFAULT_BFZY_TYPE_NAMES = [
    "国产剧",
    "香港剧",
    "韩国剧",
    "欧美剧",
    "国产动漫",
    "日韩动漫",
    "欧美动漫",
]
BFZY_CATEGORY_IDS = dict(zip(DEFAULT_BFZY_TYPE_NAMES, DEFAULT_BFZY_TYPE_IDS))
BFZY_ENABLED = config_bool("bfzy", "enabled", "BFZY_ENABLED", False)
BFZY_DOMAINS = config_list("bfzy", "domains", "BFZY_DOMAINS", DEFAULT_BFZY_DOMAINS)
BFZY_USER_AGENT = os.getenv(
    "BFZY_USER_AGENT",
    "Mozilla/5.0 (compatible; trakt-streaming-update-bot/1.0)",
).strip()


def resolve_resource_type_config(
    section_name: str,
    env_name: str,
    default_ids: Sequence[str],
    category_ids: Dict[str, str],
) -> Tuple[List[str], List[str]]:
    env_ids = env_override(env_name)
    if env_ids is not None:
        type_ids = split_env_list(env_ids)
        return type_ids, resource_type_names_for_ids(type_ids, category_ids)

    configured_type_ids = config_value(section_name, "type_ids")
    if configured_type_ids is not MISSING_CONFIG_VALUE:
        type_ids = coerce_config_list(configured_type_ids)
        return type_ids, resource_type_names_for_ids(type_ids, category_ids)

    env_categories = env_override(f"{section_name.upper()}_CATEGORIES")
    configured_categories = (
        split_env_list(env_categories)
        if env_categories is not None
        else config_value(section_name, "categories")
    )
    if configured_categories is MISSING_CONFIG_VALUE:
        type_ids = list(default_ids)
        return type_ids, resource_type_names_for_ids(type_ids, category_ids)

    category_lookup = {normalize_config_token(name): type_id for name, type_id in category_ids.items()}
    selected_ids = []
    for category in coerce_config_list(configured_categories):
        category = category.strip()
        if re.fullmatch(r"\d+", category):
            selected_ids.append(category)
            continue
        type_id = category_lookup.get(normalize_config_token(category))
        if not type_id:
            logging.warning("%s.categories 包含未知分类 %r，将跳过。", section_name, category)
            continue
        selected_ids.append(type_id)

    type_ids = unique_keep_order(selected_ids)
    return type_ids, resource_type_names_for_ids(type_ids, category_ids)


def resource_type_names_for_ids(type_ids: Sequence[str], category_ids: Dict[str, str]) -> List[str]:
    id_to_name = {type_id: name for name, type_id in category_ids.items()}
    return [id_to_name[type_id] for type_id in type_ids if type_id in id_to_name]


FFZY_TYPE_IDS, FFZY_TYPE_NAMES = resolve_resource_type_config(
    "ffzy",
    "FFZY_TYPE_IDS",
    DEFAULT_FFZY_TYPE_IDS,
    FFZY_CATEGORY_IDS,
)
FFZY_BOOTSTRAP_ONLY = config_bool("ffzy", "bootstrap_only", "FFZY_BOOTSTRAP_ONLY", True)
FFZY_ITEMS_PER_TYPE = max(1, config_int("ffzy", "items_per_type", "FFZY_ITEMS_PER_TYPE", 20))
FFZY_PAGES_PER_TYPE = max(1, config_int("ffzy", "pages_per_type", "FFZY_PAGES_PER_TYPE", 1))
FFZY_MAX_ITEMS = max(1, config_int("ffzy", "max_items", "FFZY_MAX_ITEMS", 200))
FFZY_MAX_AGE_HOURS = max(0, config_int("ffzy", "max_age_hours", "FFZY_MAX_AGE_HOURS", 168))
FFZY_REQUEST_TIMEOUT = max(1, config_int("ffzy", "request_timeout", "FFZY_REQUEST_TIMEOUT", 8))

BFZY_TYPE_IDS, BFZY_TYPE_NAMES = resolve_resource_type_config(
    "bfzy",
    "BFZY_TYPE_IDS",
    DEFAULT_BFZY_TYPE_IDS,
    BFZY_CATEGORY_IDS,
)
BFZY_BOOTSTRAP_ONLY = config_bool("bfzy", "bootstrap_only", "BFZY_BOOTSTRAP_ONLY", True)
BFZY_ITEMS_PER_TYPE = max(1, config_int("bfzy", "items_per_type", "BFZY_ITEMS_PER_TYPE", 20))
BFZY_PAGES_PER_TYPE = max(1, config_int("bfzy", "pages_per_type", "BFZY_PAGES_PER_TYPE", 1))
BFZY_MAX_ITEMS = max(1, config_int("bfzy", "max_items", "BFZY_MAX_ITEMS", 200))
BFZY_MAX_AGE_HOURS = max(0, config_int("bfzy", "max_age_hours", "BFZY_MAX_AGE_HOURS", 168))
BFZY_REQUEST_TIMEOUT = max(1, config_int("bfzy", "request_timeout", "BFZY_REQUEST_TIMEOUT", 8))

MYDRAMALIST_CALENDAR_URL = "https://mydramalist.com/episode-calendar"
MYDRAMALIST_ENABLED = config_bool("mydramalist", "enabled", "MYDRAMALIST_ENABLED", False)
MYDRAMALIST_COUNTRIES = config_list("mydramalist", "countries", "MYDRAMALIST_COUNTRIES", ["South Korea"])
MYDRAMALIST_TYPES = config_list("mydramalist", "types", "MYDRAMALIST_TYPES", ["Drama"])
MYDRAMALIST_TIMEZONE = config_str("mydramalist", "timezone", "MYDRAMALIST_TIMEZONE", "Asia/Shanghai")
MYDRAMALIST_ONLY_AIRED = config_bool("mydramalist", "only_aired", "MYDRAMALIST_ONLY_AIRED", True)
MYDRAMALIST_LOOKAHEAD_HOURS = max(0, config_int("mydramalist", "lookahead_hours", "MYDRAMALIST_LOOKAHEAD_HOURS", 0))
MYDRAMALIST_BOOTSTRAP_ONLY = config_bool("mydramalist", "bootstrap_only", "MYDRAMALIST_BOOTSTRAP_ONLY", True)
MYDRAMALIST_MAX_ITEMS = max(1, config_int("mydramalist", "max_items", "MYDRAMALIST_MAX_ITEMS", 200))
MYDRAMALIST_REMIND_AT_AIR = config_bool("mydramalist", "remind_at_air", "MYDRAMALIST_REMIND_AT_AIR", True)
MYDRAMALIST_REMIND_WINDOW_MINUTES = max(
    1, config_int("mydramalist", "remind_window_minutes", "MYDRAMALIST_REMIND_WINDOW_MINUTES", 90)
)
MYDRAMALIST_ALERT_KEY_PREFIX = "mydramalist-alert:"
# 首次启用这个源时，只把最近几天到点的条目推出来，更远的先记账，避免一次刷几十条。
MYDRAMALIST_INITIAL_PUSH_DAYS = max(0, config_int("mydramalist", "initial_push_days", "MYDRAMALIST_INITIAL_PUSH_DAYS", 3))
# 键里必须带冒号：load_pushed_ids 会把没有冒号的旧键改写成 episode:xxx，
# 原来的 mydramalist-initial-v2 因此读回来就失配，导致每轮都被当成首次运行。
MYDRAMALIST_INITIAL_MARKER = "mydramalist:initial:v2"


def mydramalist_initial_done(keys: Sequence[str]) -> bool:
    """标记是否已经写过；兼容旧版本存下的 episode:mydramalist-initial-v2。"""
    return any(
        key == MYDRAMALIST_INITIAL_MARKER or key.endswith("mydramalist-initial-v2") for key in keys
    )
MYDRAMALIST_REQUEST_TIMEOUT = max(1, config_int("mydramalist", "request_timeout", "MYDRAMALIST_REQUEST_TIMEOUT", 20))
MYDRAMALIST_USER_AGENT = os.getenv(
    "MYDRAMALIST_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36",
).strip()
MYDRAMALIST_COOKIE = os.getenv("MYDRAMALIST_COOKIE", "").strip()
MYDRAMALIST_CARD_MARKER = '<div class="episode-card-large">'


JUSTWATCH_BUCKETS_GRAPHQL_QUERY = """
query GetNewTitleBuckets(
  $country: Country!
  $filter: TitleFilter!
  $first: Int!
  $pageType: NewPageType!
  $priceDrops: Boolean!
) {
  newTitleBuckets(
    country: $country
    filter: $filter
    first: $first
    bucketSize: 0
    groupBy: DATE_PACKAGE
    pageType: $pageType
    priceDrops: $priceDrops
  ) {
    edges {
      key {
        ... on DatePackageAggregationKey {
          date
          package {
            clearName
            shortName
          }
        }
      }
      node {
        totalCount
      }
    }
  }
}
"""

STATE_FILE = BASE_DIR / "pushed_episodes.json"

TRAKT_CLIENT_ID = os.getenv("TRAKT_CLIENT_ID", "").strip()
TRAKT_CLIENT_SECRET = os.getenv("TRAKT_CLIENT_SECRET", "").strip()
TRAKT_ACCESS_TOKEN = os.getenv("TRAKT_ACCESS_TOKEN", "").strip()
TRAKT_REFRESH_TOKEN = os.getenv("TRAKT_REFRESH_TOKEN", "").strip()
TRAKT_FORCE_REFRESH = parse_bool_env("TRAKT_FORCE_REFRESH", False)

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
# 一次可能连发十几张图，Telegram 对同一会话有速率限制，发送之间留一点间隔。
TELEGRAM_PHOTO_INTERVAL = 1.2

GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "").strip()
GH_SECRET_WRITE_TOKEN = os.getenv("GH_SECRET_WRITE_TOKEN", "").strip()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


class ConfigError(RuntimeError):
    pass


@dataclass
class TraktTokens:
    access_token: str
    refresh_token: str


@dataclass
class ResourceSiteConfig:
    code: str
    display_name: str
    enabled: bool
    domains: List[str]
    type_ids: List[str]
    type_names: List[str]
    bootstrap_only: bool
    items_per_type: int
    pages_per_type: int
    max_items: int
    max_age_hours: int
    request_timeout: int
    user_agent: str


def require_config() -> None:
    """校验必要配置，避免运行时静默失败。"""
    missing = []
    if TRAKT_ENABLED and not TRAKT_CLIENT_ID:
        missing.append("TRAKT_CLIENT_ID")

    has_access_token = bool(TRAKT_ACCESS_TOKEN)
    has_refresh_credentials = bool(TRAKT_CLIENT_SECRET and TRAKT_REFRESH_TOKEN)
    if TRAKT_ENABLED and not has_access_token and not has_refresh_credentials:
        missing.append("TRAKT_ACCESS_TOKEN 或 TRAKT_CLIENT_SECRET + TRAKT_REFRESH_TOKEN")

    if missing:
        raise ConfigError(f"缺少必要环境变量：{', '.join(missing)}")

    if TRAKT_FORCE_REFRESH and not has_refresh_credentials:
        raise ConfigError("TRAKT_FORCE_REFRESH=true 时必须配置 TRAKT_CLIENT_SECRET 和 TRAKT_REFRESH_TOKEN")

    if bool(TRAKT_CLIENT_SECRET) != bool(TRAKT_REFRESH_TOKEN):
        raise ConfigError("Trakt 自动续期配置不完整：TRAKT_CLIENT_SECRET 和 TRAKT_REFRESH_TOKEN 必须同时提供")

    if bool(TG_BOT_TOKEN) != bool(TG_CHAT_ID):
        raise ConfigError("Telegram 配置不完整：TG_BOT_TOKEN 和 TG_CHAT_ID 必须同时提供")

    if not ((TG_BOT_TOKEN and TG_CHAT_ID) or DISCORD_WEBHOOK_URL):
        raise ConfigError("至少需要配置 Telegram 或 Discord 的一个推送目标")


def load_pushed_ids() -> List[str]:
    """读取已推送条目 ID；文件不存在时初始化为空列表。"""
    if not STATE_FILE.exists():
        logging.info("未找到 pushed_episodes.json，将使用空状态启动。")
        return []

    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"pushed_episodes.json 不是合法 JSON：{exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"读取 pushed_episodes.json 失败：{exc}") from exc

    if not isinstance(raw, list):
        raise RuntimeError("pushed_episodes.json 顶层结构必须是数组")

    ids = []
    for item in raw:
        if item is None:
            continue
        text = str(item)
        # 兼容旧版本保存的纯 episode id，迁移成带类型前缀的去重键。
        if ":" not in text:
            text = f"episode:{text}"
        ids.append(text)

    return dedupe_keep_order(ids)[-MAX_STATE_RECORDS:]


def save_pushed_ids(ids: Sequence[str]) -> None:
    """原子写入状态文件，限制最多保留最新 MAX_STATE_RECORDS 条记录。"""
    normalized = dedupe_keep_order([str(item) for item in ids if item is not None])
    normalized = normalized[-MAX_STATE_RECORDS:]

    tmp_file = STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp_file.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp_file.replace(STATE_FILE)
    except OSError as exc:
        raise RuntimeError(f"写入 pushed_episodes.json 失败：{exc}") from exc


def dedupe_keep_order(items: Sequence[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def can_refresh_trakt_token(tokens: TraktTokens) -> bool:
    return bool(TRAKT_CLIENT_ID and TRAKT_CLIENT_SECRET and tokens.refresh_token)


def refresh_trakt_token(tokens: TraktTokens) -> None:
    """使用 refresh_token 换取新的 Trakt access_token，并写回 GitHub Secrets。"""
    if not can_refresh_trakt_token(tokens):
        raise ConfigError("无法刷新 Trakt token：缺少 TRAKT_CLIENT_SECRET 或 TRAKT_REFRESH_TOKEN")

    # 先确认 GitHub Secrets 可写，再调用 Trakt 刷新接口，避免拿到新 refresh_token 后无法保存。
    verify_secret_write_access(tokens)

    payload = {
        "refresh_token": tokens.refresh_token,
        "client_id": TRAKT_CLIENT_ID,
        "client_secret": TRAKT_CLIENT_SECRET,
        "redirect_uri": TRAKT_REDIRECT_URI,
        "grant_type": "refresh_token",
    }

    try:
        response = requests.post(
            "https://api.trakt.tv/oauth/token",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"刷新 Trakt token 请求失败：{exc}") from exc

    if response.status_code >= 400:
        raise RuntimeError(
            f"刷新 Trakt token 失败：HTTP {response.status_code} - {response.text[:500]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"刷新 Trakt token 响应不是合法 JSON：{exc}") from exc

    new_access_token = safe_text(data.get("access_token"), "")
    new_refresh_token = safe_text(data.get("refresh_token"), tokens.refresh_token)

    if not new_access_token:
        raise RuntimeError("刷新 Trakt token 失败：响应中缺少 access_token")

    tokens.access_token = new_access_token
    tokens.refresh_token = new_refresh_token

    persist_github_secret("TRAKT_ACCESS_TOKEN", tokens.access_token)
    persist_github_secret("TRAKT_REFRESH_TOKEN", tokens.refresh_token)

    expires_in = data.get("expires_in")
    if expires_in:
        logging.info("Trakt token 已刷新并写回 GitHub Secrets，expires_in=%s。", expires_in)
    else:
        logging.info("Trakt token 已刷新并写回 GitHub Secrets。")


def verify_secret_write_access(tokens: TraktTokens) -> None:
    """刷新前做一次 Secrets 写权限预检，降低 refresh_token 轮换后丢失的风险。"""
    if tokens.access_token:
        persist_github_secret("TRAKT_ACCESS_TOKEN", tokens.access_token, quiet=True)
    persist_github_secret("TRAKT_REFRESH_TOKEN", tokens.refresh_token, quiet=True)


def persist_github_secret(name: str, value: str, quiet: bool = False) -> None:
    """通过 gh CLI 更新仓库 Secret；不把敏感值写入日志或仓库文件。"""
    if not value:
        raise RuntimeError(f"无法写入 GitHub Secret {name}：值为空")

    if not GITHUB_REPOSITORY:
        raise RuntimeError("无法写入 GitHub Secret：缺少 GITHUB_REPOSITORY 环境变量")

    gh_path = shutil.which("gh")
    if not gh_path:
        raise RuntimeError("无法写入 GitHub Secret：当前环境没有安装 gh CLI")

    env = os.environ.copy()
    token = GH_SECRET_WRITE_TOKEN or env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    if token:
        env["GH_TOKEN"] = token

    if not env.get("GH_TOKEN"):
        raise RuntimeError(
            "无法写入 GitHub Secret：缺少 GH_SECRET_WRITE_TOKEN、GH_TOKEN 或 GITHUB_TOKEN"
        )

    try:
        completed = subprocess.run(
            [gh_path, "secret", "set", name, "--repo", GITHUB_REPOSITORY],
            input=value,
            text=True,
            capture_output=True,
            timeout=60,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"写入 GitHub Secret {name} 超时") from exc
    except OSError as exc:
        raise RuntimeError(f"写入 GitHub Secret {name} 失败：{exc}") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        hint = ""
        if "Resource not accessible by integration" in stderr or "HTTP 403" in stderr:
            hint = (
                "；当前 token 没有仓库 Actions Secrets 写权限。"
                "在 GitHub Actions 中请配置 GH_SECRET_WRITE_TOKEN 为具备 Secrets: write 权限的 PAT，"
                "不要使用 github.token"
            )
        raise RuntimeError(f"写入 GitHub Secret {name} 失败：{stderr[:500]}{hint}")

    if not quiet:
        logging.info("已更新 GitHub Secret：%s。", name)


def trakt_headers(access_token: str) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "trakt-api-version": TRAKT_API_VERSION,
        "trakt-api-key": TRAKT_CLIENT_ID,
        "Authorization": f"Bearer {access_token}",
    }


def request_calendar(tokens: TraktTokens, media_type: str) -> requests.Response:
    start_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = f"https://api.trakt.tv/calendars/my/{media_type}/{start_date}/{TRAKT_CALENDAR_DAYS}"

    try:
        return requests.get(url, headers=trakt_headers(tokens.access_token), timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f"请求 Trakt {media_type} 日历失败：{exc}") from exc


def parse_calendar_response(response: requests.Response, media_type: str) -> List[Dict[str, Any]]:
    if response.status_code >= 400:
        raise RuntimeError(
            f"Trakt {media_type} 日历返回错误：HTTP {response.status_code} - {response.text[:500]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Trakt {media_type} 日历响应不是合法 JSON：{exc}") from exc

    if not isinstance(data, list):
        raise RuntimeError(f"Trakt {media_type} 日历响应格式异常：顶层结构不是数组")

    return data


def fetch_calendar(tokens: TraktTokens, media_type: str) -> List[Dict[str, Any]]:
    """调用 Trakt 个人日历接口，认证失败时刷新 token 并重试一次。"""
    response = request_calendar(tokens, media_type)
    if response.status_code in (401, 403) and can_refresh_trakt_token(tokens):
        logging.warning(
            "Trakt %s 日历返回 HTTP %s，开始刷新 access_token 并重试。",
            media_type,
            response.status_code,
        )
        refresh_trakt_token(tokens)
        response = request_calendar(tokens, media_type)

    return parse_calendar_response(response, media_type)


def fetch_today_items(tokens: TraktTokens) -> List[Dict[str, str]]:
    """获取今天的剧集和电影，并解析成统一的推送条目。"""
    if not TRAKT_ENABLED:
        return []

    if TRAKT_FORCE_REFRESH or not tokens.access_token:
        logging.info("开始刷新 Trakt token。")
        refresh_trakt_token(tokens)

    items = []

    if TRAKT_SHOWS_ENABLED:
        show_entries = fetch_calendar(tokens, "shows")
        for entry in show_entries:
            item = extract_episode(entry)
            if item:
                items.append(item)

    if TRAKT_MOVIES_ENABLED:
        movie_entries = fetch_calendar(tokens, "movies")
        for entry in movie_entries:
            item = extract_movie(entry)
            if item:
                items.append(item)

    return items


def extract_episode(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """从 Trakt 日历条目中提取剧集字段。"""
    episode = item.get("episode") or {}
    show = item.get("show") or {}

    if not isinstance(episode, dict) or not isinstance(show, dict):
        logging.warning("跳过格式异常的剧集日历条目：%s", item)
        return None

    episode_ids = episode.get("ids") or {}
    trakt_id = episode_ids.get("trakt")
    if trakt_id is None:
        logging.warning("跳过缺少 Trakt Episode ID 的条目：%s", item)
        return None

    season = episode.get("season")
    number = episode.get("number")

    return {
        "key": f"episode:{trakt_id}",
        "type": "episode",
        "label": "剧集",
        "title": safe_text(show.get("title"), "未知剧集"),
        "subtitle": safe_text(episode.get("title"), "未命名单集"),
        "code": f"S{season if season is not None else '?'}E{number if number is not None else '?'}",
        "aired": format_air_time(item.get("first_aired") or episode.get("first_aired")),
    }


def extract_movie(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """从 Trakt 日历条目中提取电影字段。"""
    movie = item.get("movie") or {}

    if not isinstance(movie, dict):
        logging.warning("跳过格式异常的电影日历条目：%s", item)
        return None

    movie_ids = movie.get("ids") or {}
    trakt_id = movie_ids.get("trakt")
    if trakt_id is None:
        logging.warning("跳过缺少 Trakt Movie ID 的条目：%s", item)
        return None

    year = movie.get("year")
    title = safe_text(movie.get("title"), "未知电影")
    if year:
        title = f"{title} ({year})"

    return {
        "key": f"movie:{trakt_id}",
        "type": "movie",
        "label": "电影",
        "title": title,
        "subtitle": "",
        "code": "Movie",
        "aired": format_air_time(item.get("first_aired") or movie.get("released")),
    }


def fetch_justwatch_items() -> List[Dict[str, str]]:
    """抓取 JustWatch 最新上架页面，解析成统一推送条目。"""
    if not JUSTWATCH_ENABLED:
        return []

    items: List[Dict[str, str]] = []
    for provider in JUSTWATCH_PROVIDERS:
        try:
            provider_items = fetch_justwatch_provider_items(provider)
        except Exception as exc:
            logging.exception("查询 JustWatch 平台失败，本次跳过该平台：%s - %s", provider, exc)
            continue

        logging.info("JustWatch 平台 %s 解析到 %s 个最新条目。", provider, len(provider_items))
        items.extend(provider_items)

    for url in JUSTWATCH_URLS:
        try:
            page_items = fetch_justwatch_page_items(url)
        except Exception as exc:
            logging.exception("抓取 JustWatch 页面失败，本次跳过该页面：%s - %s", url, exc)
            continue

        logging.info("JustWatch 页面 %s 解析到 %s 个最新条目。", url, len(page_items))
        items.extend(page_items)

    deduped: List[Dict[str, str]] = []
    seen = set()
    for item in items:
        if not justwatch_item_allowed(item):
            continue
        key = item["key"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= JUSTWATCH_MAX_ITEMS:
            break

    return deduped


def justwatch_item_allowed(item: Dict[str, str]) -> bool:
    item_country = safe_text(item.get("country"), "").upper()
    if item_country and item_country != JUSTWATCH_COUNTRY:
        return False

    media_type = "show" if item.get("type") == "justwatch_show" else "movie"
    if not JUSTWATCH_MEDIA_TYPES:
        return True

    allowed = set(JUSTWATCH_MEDIA_TYPES)
    if media_type == "show":
        return bool(allowed & {"show", "shows", "tv", "tvshow", "tvshows", "series", "剧集", "电视剧"})
    return bool(allowed & {"movie", "movies", "film", "films", "motionpicture", "电影"})


def justwatch_empty_filter(packages: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    return {
        "ageCertifications": [],
        "excludeGenres": [],
        "excludeIrrelevantTitles": False,
        "excludeProductionCountries": [],
        "genres": [],
        "monetizationTypes": [],
        "objectTypes": [],
        "packages": list(packages or []),
        "presentationTypes": [],
        "productionCountries": [],
        "subgenres": [],
    }


def fetch_justwatch_provider_items(
    provider: str,
    date: Optional[str] = None,
    first: Optional[int] = None,
) -> List[Dict[str, str]]:
    parts = provider.split(":", 1)
    if len(parts) == 1 and parts[0].strip():
        country = JUSTWATCH_COUNTRY
        provider_code = parts[0].strip()
    elif len(parts) == 2 and parts[0].strip() and parts[1].strip():
        country = normalize_justwatch_country(parts[0])
        provider_code = parts[1].strip()
    else:
        raise RuntimeError("JUSTWATCH_PROVIDERS 条目格式必须是 provider_code 或 COUNTRY:provider_code，例如 lin 或 TW:lin")

    variables = {
        "country": country,
        "date": date,
        "filter": justwatch_empty_filter([provider_code]),
        "first": min(first or JUSTWATCH_ITEMS_PER_PROVIDER, 50),
        "language": justwatch_language(country),
        "pageType": "NEW",
        "priceDrops": False,
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": JUSTWATCH_USER_AGENT,
    }

    try:
        response = requests.post(
            JUSTWATCH_GRAPHQL_URL,
            json={"query": JUSTWATCH_GRAPHQL_QUERY, "variables": variables},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"请求 JustWatch GraphQL 失败：{exc}") from exc

    if response.status_code >= 400:
        raise RuntimeError(
            f"JustWatch GraphQL 返回错误：HTTP {response.status_code} - {response.text[:300]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"JustWatch GraphQL 响应不是合法 JSON：{exc}") from exc

    if data.get("errors"):
        raise RuntimeError(f"JustWatch GraphQL 返回错误：{str(data['errors'])[:500]}")

    connection = ((data.get("data") or {}).get("newTitles") or {})
    items = []
    for edge in connection.get("edges") or []:
        item = extract_justwatch_edge(edge, country, "")
        if item:
            items.append(item)
    return items


def fetch_justwatch_country_items(country: str) -> List[Dict[str, str]]:
    country = normalize_justwatch_country(country)
    if not country:
        raise RuntimeError("JustWatch 国家代码为空")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": JUSTWATCH_USER_AGENT,
    }
    variables = {
        "country": country,
        "filter": justwatch_empty_filter(),
        "first": min(JUSTWATCH_MAX_ITEMS, 20),
        "pageType": "NEW",
        "priceDrops": False,
    }

    try:
        response = requests.post(
            JUSTWATCH_GRAPHQL_URL,
            json={"query": JUSTWATCH_BUCKETS_GRAPHQL_QUERY, "variables": variables},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"请求 JustWatch GraphQL buckets 失败：{exc}") from exc

    if response.status_code >= 400:
        raise RuntimeError(
            f"JustWatch GraphQL buckets 返回错误：HTTP {response.status_code} - {response.text[:300]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"JustWatch GraphQL buckets 响应不是合法 JSON：{exc}") from exc

    if data.get("errors"):
        raise RuntimeError(f"JustWatch GraphQL buckets 返回错误：{str(data['errors'])[:500]}")

    items: List[Dict[str, str]] = []
    for edge in (((data.get("data") or {}).get("newTitleBuckets") or {}).get("edges") or []):
        key = edge.get("key") or {}
        date = safe_text(key.get("date"), "")
        package = key.get("package") or {}
        provider_code = safe_text(package.get("shortName"), "")
        total_count = edge.get("node", {}).get("totalCount") if isinstance(edge.get("node"), dict) else None

        if not provider_code or not is_recent_justwatch_date(date):
            continue

        provider_first = JUSTWATCH_ITEMS_PER_PROVIDER
        if isinstance(total_count, int) and total_count > 0:
            provider_first = min(provider_first, total_count)

        items.extend(fetch_justwatch_provider_items(f"{country}:{provider_code}", date=date, first=provider_first))
        if len(items) >= JUSTWATCH_MAX_ITEMS:
            break

    return items[:JUSTWATCH_MAX_ITEMS]


def fetch_justwatch_page_items(url: str) -> List[Dict[str, str]]:
    if is_justwatch_country_new_url(url):
        return fetch_justwatch_country_items(JUSTWATCH_COUNTRY)

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "User-Agent": JUSTWATCH_USER_AGENT,
    }

    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f"请求 JustWatch 失败：{exc}") from exc

    if response.status_code >= 400:
        raise RuntimeError(f"JustWatch 返回错误：HTTP {response.status_code} - {response.text[:300]}")

    apollo_state = extract_justwatch_apollo_state(response.text)
    root_query = resolve_apollo_value(apollo_state, apollo_state.get("ROOT_QUERY", {}))
    country = normalize_justwatch_country(justwatch_country_from_url(response.url or url) or JUSTWATCH_COUNTRY)

    items: List[Dict[str, str]] = []
    for query_key, connection in root_query.items():
        if not query_key.startswith("newTitles(") or '"pageType":"NEW"' not in query_key:
            continue

        query_date = extract_justwatch_query_date(query_key)
        for edge in connection.get("edges") or []:
            item = extract_justwatch_edge(edge, country, query_date)
            if item:
                items.append(item)

    return items


def mydramalist_calendar_url() -> str:
    """按国家和内容类型拼出 MyDramaList 剧集日历地址。"""
    params: List[Tuple[str, str]] = [("view", "large"), ("scope", "all"), ("tz", MYDRAMALIST_TIMEZONE)]
    params.extend(("country[]", country) for country in MYDRAMALIST_COUNTRIES)
    params.extend(("type[]", media_type) for media_type in MYDRAMALIST_TYPES)
    return f"{MYDRAMALIST_CALENDAR_URL}?{urlencode(params)}"


def mydramalist_headers() -> Dict[str, str]:
    headers = {
        "User-Agent": MYDRAMALIST_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if MYDRAMALIST_COOKIE:
        headers["Cookie"] = MYDRAMALIST_COOKIE
    return headers


def fetch_mydramalist_items() -> List[Dict[str, str]]:
    """抓取 MyDramaList 剧集日历页面，解析成统一推送条目。"""
    if not MYDRAMALIST_ENABLED:
        return []

    url = mydramalist_calendar_url()
    page_html = request_mydramalist_html(url)

    items = parse_mydramalist_calendar(page_html)
    logging.info(
        "MyDramaList 解析到 %s 个条目（国家：%s；类型：%s）。",
        len(items),
        ",".join(MYDRAMALIST_COUNTRIES) or "全部",
        ",".join(MYDRAMALIST_TYPES) or "全部",
    )
    return items[:MYDRAMALIST_MAX_ITEMS]


def request_mydramalist_html(url: str) -> str:
    """优先用浏览器指纹抓取，失败后退回 requests；两者都带有 Cloudflare 检出判断。"""
    errors: List[str] = []
    headers = mydramalist_headers()

    if curl_cffi_requests is not None:
        try:
            response = curl_cffi_requests.get(
                url,
                headers=headers,
                impersonate="chrome",
                timeout=MYDRAMALIST_REQUEST_TIMEOUT,
            )
            if response.status_code < 400 and MYDRAMALIST_CARD_MARKER in response.text:
                logging.info("MyDramaList 抓取成功（curl_cffi / chrome 指纹）。")
                return response.text
            errors.append(f"curl_cffi: HTTP {response.status_code}")
        except Exception as exc:
            errors.append(f"curl_cffi: {type(exc).__name__} {exc}")

    try:
        response = requests.get(url, headers=headers, timeout=MYDRAMALIST_REQUEST_TIMEOUT)
        if response.status_code < 400 and MYDRAMALIST_CARD_MARKER in response.text:
            logging.info("MyDramaList 抓取成功（requests）。")
            return response.text
        errors.append(f"requests: HTTP {response.status_code}")
    except requests.RequestException as exc:
        errors.append(f"requests: {exc}")

    raise RuntimeError(
        "MyDramaList 请求失败，可能被 Cloudflare 拦截：" + "; ".join(errors[-3:])
    )


def parse_mydramalist_calendar(page_html: str) -> List[Dict[str, str]]:
    """解析日历页：按天分组，再逐张卡片提取剧名、集数和播出时间。"""
    day_matches = list(
        re.finditer(r'<div id="d\d+">\s*<small>\s*([^<]*?)\s*</small>', page_html)
    )
    if not day_matches:
        logging.warning("MyDramaList 页面未找到日历分组，可能被拦截或页面结构已变。")
        return []

    now = datetime.now(timezone.utc)
    allowed_until = now + timedelta(hours=MYDRAMALIST_LOOKAHEAD_HOURS)

    items: List[Dict[str, str]] = []
    seen = set()

    for index, match in enumerate(day_matches):
        day_text = match.group(1)
        block_start = match.end()
        block_end = day_matches[index + 1].start() if index + 1 < len(day_matches) else len(page_html)
        block = page_html[block_start:block_end]

        for card in block.split(MYDRAMALIST_CARD_MARKER)[1:]:
            item = extract_mydramalist_item(card, day_text, now, allowed_until)
            if not item or item["key"] in seen:
                continue
            seen.add(item["key"])
            items.append(item)

    items.sort(key=lambda item: item.get("aired", ""), reverse=True)
    return items


def extract_mydramalist_item(
    card_html: str,
    day_text: str,
    now: datetime,
    allowed_until: datetime,
) -> Optional[Dict[str, str]]:
    title = first_mydramalist_text(card_html, r'<div class="title">\s*([^<]*?)\s*</div>')
    if not title:
        return None

    content_type = first_mydramalist_text(card_html, r'<div class="content-type">\s*([^<]*?)\s*</div>')
    release_time = first_mydramalist_text(
        card_html, r'release-time calendar-popover-source"[^>]*>\s*([^<]*?)\s*</div>'
    )
    show_timezone = first_mydramalist_text(
        card_html, r'<div class="calendar-popover-title">\s*([^<]*?)\s*</div>'
    )
    score = first_mydramalist_text(
        card_html, r'<span class="p-l-xs score">\s*([^<]*?)\s*</span>'
    )
    image = first_mydramalist_text(
        card_html, r'<img class="img-responsive[^"]*"[^>]*\ssrc="([^"]+)"', re.S
    )
    if not image:
        # 页面靠后的卡片是懒加载，只有 data-src，没有 src。
        image = first_mydramalist_text(
            card_html, r'<img class="img-responsive[^"]*"[^>]*\sdata-src="([^"]+)"', re.S
        )
    # 列表页给的是 4c 小图，换成 4f 原图，Telegram 卡片更清晰。
    if image.endswith("_4c.jpg"):
        image = image[: -len("_4c.jpg")] + "_4f.jpg"
    synopsis = first_mydramalist_text(
        card_html, r'<p class="synopsis">\s*(.*?)\s*</p>', re.S
    )
    popover_time = first_mydramalist_text(
        card_html, r'<span class="text-muted">\(([^)]*)\)</span>'
    )
    genres = [
        genre.strip()
        for genre in re.findall(r'<div class="genre">\s*([^<]*?)\s*</div>', card_html)
        if genre.strip()
    ]

    show_match = re.search(r'href="/(\d+)-[^"]*"', card_html)
    show_id = show_match.group(1) if show_match else ""
    if not show_id:
        return None

    show_path = show_match.group(0).split('"')[1] if show_match else ""

    # 卡片自己的 airing-at 才是当前这一格播的集数；弹层里那串 episode 链接是全剧列表，
    # 顺序不固定，取第一个会取到别的集。
    airing_text = first_mydramalist_text(
        card_html, r'<div class="airing-at">\s*(.*?)\s*</div>', re.S
    )
    episode_match = re.search(r"Episode\s+(\d+)", airing_text, re.I)
    episode_number = episode_match.group(1) if episode_match else ""
    episode_path = f"{show_path}/episode/{episode_number}" if episode_number else ""

    aired_dt, aired_iso = mydramalist_air_time(
        day_text, popover_time, release_time, show_timezone
    )
    aired_text = f"{day_text} {release_time}".strip()
    if aired_dt is not None:
        if MYDRAMALIST_ONLY_AIRED and aired_dt > allowed_until:
            return None
        aired_text = mydramalist_display_time(aired_dt, show_timezone)

    kind = "episode" if episode_number else "movie"
    air_ts = int(aired_dt.timestamp()) if aired_dt is not None else 0
    # 集号不能拿来当主键：MyDramaList 对同一次播出会随机返回不同的集号
    # （同一部剧抓到过 E1/E3/E5/E12，总集数也从 35 跳到 50）。
    # 播出时刻是稳定的，用它做去重键，同一次播出只推一条。
    if air_ts:
        slot = str(air_ts)
    elif aired_iso:
        slot = aired_iso.replace(" ", "-").replace(":", "")
    else:
        slot = re.sub(r"\W+", "-", day_text).strip("-")
    key = f"mydramalist:{show_id}:{slot}"
    air_status = ""
    if air_ts:
        air_status = "已开播" if air_ts <= int(now.timestamp()) else "待开播"

    subtitle_parts = list(genres)

    source_parts = ["MyDramaList"]
    if content_type:
        source_parts.append(content_type)
    if show_timezone:
        source_parts.append(show_timezone)

    page_path = episode_path or show_path
    return {
        "key": key,
        "type": kind,
        "label": mydramalist_label(content_type),
        "title": title,
        "subtitle": " · ".join(subtitle_parts),
        # 只显示集号，不显示「总集数」：那个数字也在随机变化，展示出来会误导。
        "code": f"E{episode_number}" if episode_number else "Movie",
        "aired": aired_text or f"{day_text} {release_time}".strip(),
        "time_label": "播出时间",
        "source": " · ".join(source_parts),
        "url": f"https://mydramalist.com{page_path}" if page_path else "",
        "country": content_type,
        "image": image,
        "score": score,
        "synopsis": clean_ffzy_text(synopsis, ""),
        "air_ts": air_ts,
        "air_status": air_status,
        "remind_key": key.replace("mydramalist:", MYDRAMALIST_ALERT_KEY_PREFIX, 1),
    }


def first_mydramalist_text(card_html: str, pattern: str, flags: int = 0) -> str:
    match = re.search(pattern, card_html, flags)
    if not match:
        return ""
    return html.unescape(match.group(1)).strip()


_ZONE_CACHE: Dict[str, Any] = {}


def mydramalist_zone(name: str):
    cached = _ZONE_CACHE.get(name)
    if cached is not None:
        return cached
    try:
        zone = ZoneInfo(name)
    except Exception:
        # 缺 tzdata 时退回 UTC，会把播出时间整体偏移，日志里明确提示。
        logging.warning(
            "时区 %s 不可用（缺少 tzdata？），本次按 UTC 处理播出时间，可能偏移。", name or "UTC"
        )
        zone = timezone.utc
    _ZONE_CACHE[name] = zone
    return zone


def mydramalist_air_time(
    day_text: str,
    popover_text: str,
    release_time: str,
    show_timezone: str,
) -> Tuple[Optional[datetime], str]:
    """把播出时间换算成 UTC。

    页面上的粗体时间是按访问者 IP 所在时区渲染的，不能直接用；
    弹层括号里的 (November 11, 2026 8:30 PM) 才是剧集当地时间的绝对值。
    """
    zone = mydramalist_zone(show_timezone)

    absolute = (popover_text or "").strip()
    if absolute:
        try:
            local = datetime.strptime(absolute, "%B %d, %Y %I:%M %p").replace(tzinfo=zone)
            return local.astimezone(timezone.utc), local.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
        for date_format in ("%B %d, %Y", "%B %d %Y"):
            try:
                day_only = datetime.strptime(absolute, date_format).date()
                local = datetime(day_only.year, day_only.month, day_only.day, tzinfo=zone)
                return local.astimezone(timezone.utc), f"{day_only:%Y-%m-%d} 全天"
            except ValueError:
                continue

    try:
        day = datetime.strptime(day_text.strip(), "%B %d, %Y").date()
    except ValueError:
        return None, ""

    match = re.match(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])", release_time.strip())
    if not match:
        # 电影等条目只有日期没有时间，按当天 00:00 处理。
        local_day = datetime(day.year, day.month, day.day, tzinfo=zone)
        return local_day.astimezone(timezone.utc), f"{day.year:04d}-{day.month:02d}-{day.day:02d} 全天"

    hour = int(match.group(1)) % 12
    if match.group(3).upper() == "PM":
        hour += 12
    minute = int(match.group(2))

    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
    return local.astimezone(timezone.utc), f"{day.year:04d}-{day.month:02d}-{day.day:02d} {hour:02d}:{minute:02d}"

VIEWER_ZONE_LABELS = {"Asia/Shanghai": "北京时间", "Asia/Seoul": "首尔时间"}


def mydramalist_display_time(aired_dt: datetime, show_timezone: str) -> str:
    """把播出时刻同时写成「观看者时区」和「剧集当地」两种说法。

    页面粗体时间是按访问者时区渲染的，括号里是剧集当地时间，两者其实是同一时刻。
    只写其中一个，读者很容易按自己的钟去对，误以为差了 1 小时。
    """
    try:
        viewer_zone = ZoneInfo(MYDRAMALIST_TIMEZONE)
    except Exception:
        viewer_zone = timezone.utc

    viewer_label = VIEWER_ZONE_LABELS.get(MYDRAMALIST_TIMEZONE, MYDRAMALIST_TIMEZONE)
    viewer_local = aired_dt.astimezone(viewer_zone)
    text = f"{viewer_local:%Y-%m-%d %H:%M} {viewer_label}"

    if show_timezone and show_timezone != MYDRAMALIST_TIMEZONE:
        show_local = aired_dt.astimezone(mydramalist_zone(show_timezone))
        text += f"（当地 {show_local:%m-%d %H:%M} · {show_timezone}）"

    return text


MYDRAMALIST_COUNTRY_LABELS = {
    "Korean": "韩",
    "Japanese": "日",
    "Chinese": "国产",
    "Hong Kong": "港",
    "Taiwanese": "台",
    "Thai": "泰",
    "Filipino": "菲",
    "Philippine": "菲",
    "Singaporean": "新加坡",
}

MYDRAMALIST_KIND_LABELS = {
    "Drama": "剧",
    "Movie": "电影",
    "TV Show": "综艺",
    "Special": "特辑",
}

MYDRAMALIST_LABEL_OVERRIDES = {
    ("Korean", "Movie"): "韩影",
    ("Japanese", "Movie"): "日影",
    ("Hong Kong", "Movie"): "港片",
    ("Taiwanese", "Movie"): "台片",
    ("Thai", "Movie"): "泰影",
    ("Korean", "TV Show"): "韩综",
    ("Japanese", "TV Show"): "日综",
    ("Hong Kong", "TV Show"): "港综",
    ("Taiwanese", "TV Show"): "台综",
    ("Thai", "TV Show"): "泰综",
    ("Chinese", "TV Show"): "国产综艺",
}


def mydramalist_label(content_type: str) -> str:
    text = content_type.strip()
    if not text:
        return "MDL 剧集"

    match = re.match(r"^(.+?)\s+(Drama|Movie|TV Show|Special)$", text)
    if not match:
        return f"MDL {text}"

    raw_country = match.group(1)
    raw_kind = match.group(2)
    override = MYDRAMALIST_LABEL_OVERRIDES.get((raw_country, raw_kind))
    if override:
        return f"MDL {override}"

    country = MYDRAMALIST_COUNTRY_LABELS.get(raw_country, raw_country)
    kind = MYDRAMALIST_KIND_LABELS.get(raw_kind, raw_kind)
    return f"MDL {country}{kind}"


def ffzy_site_config() -> ResourceSiteConfig:
    return ResourceSiteConfig(
        code="ffzy",
        display_name="FFZY",
        enabled=FFZY_ENABLED,
        domains=list(FFZY_DOMAINS),
        type_ids=list(FFZY_TYPE_IDS),
        type_names=list(FFZY_TYPE_NAMES),
        bootstrap_only=FFZY_BOOTSTRAP_ONLY,
        items_per_type=FFZY_ITEMS_PER_TYPE,
        pages_per_type=FFZY_PAGES_PER_TYPE,
        max_items=FFZY_MAX_ITEMS,
        max_age_hours=FFZY_MAX_AGE_HOURS,
        request_timeout=FFZY_REQUEST_TIMEOUT,
        user_agent=FFZY_USER_AGENT,
    )


def bfzy_site_config() -> ResourceSiteConfig:
    return ResourceSiteConfig(
        code="bfzy",
        display_name="BFZY",
        enabled=BFZY_ENABLED,
        domains=list(BFZY_DOMAINS),
        type_ids=list(BFZY_TYPE_IDS),
        type_names=list(BFZY_TYPE_NAMES),
        bootstrap_only=BFZY_BOOTSTRAP_ONLY,
        items_per_type=BFZY_ITEMS_PER_TYPE,
        pages_per_type=BFZY_PAGES_PER_TYPE,
        max_items=BFZY_MAX_ITEMS,
        max_age_hours=BFZY_MAX_AGE_HOURS,
        request_timeout=BFZY_REQUEST_TIMEOUT,
        user_agent=BFZY_USER_AGENT,
    )


def fetch_ffzy_items() -> List[Dict[str, str]]:
    """抓取 FFZY 指定分类，解析成统一推送条目。"""
    return fetch_resource_site_items(ffzy_site_config())


def fetch_bfzy_items() -> List[Dict[str, str]]:
    """抓取 BFZY 指定分类，解析成统一推送条目。"""
    return fetch_resource_site_items(bfzy_site_config())


def fetch_resource_site_items(config: ResourceSiteConfig) -> List[Dict[str, str]]:
    """Fetch selected resource-site categories and convert them to push items."""
    if not config.enabled:
        return []

    domains = [domain for domain in config.domains if domain.strip()]
    if not domains:
        logging.warning("%s 已启用但没有配置可用域名。", config.display_name)
        return []

    items: List[Dict[str, str]] = []
    seen = set()

    for type_id in config.type_ids:
        type_count = 0
        for page in range(1, config.pages_per_type + 1):
            if type_count >= config.items_per_type or len(items) >= config.max_items:
                break

            try:
                data, api_base = request_resource_site_json(
                    {"ac": "detail", "t": type_id, "pg": page},
                    domains,
                    config,
                )
            except Exception as exc:
                logging.exception("查询 %s 分类失败，本次跳过分类 %s：%s", config.display_name, type_id, exc)
                break

            entries = data.get("list") or []
            if not isinstance(entries, list):
                logging.warning("%s 分类 %s 响应 list 不是数组，本次跳过。", config.display_name, type_id)
                break
            if not entries:
                break

            for entry in entries:
                if type_count >= config.items_per_type or len(items) >= config.max_items:
                    break
                if not isinstance(entry, dict):
                    continue

                item = extract_resource_site_item(entry, api_base, config)
                if not item:
                    continue
                item_keys = item_state_keys(item)
                if any(key in seen for key in item_keys):
                    continue

                items.append(item)
                seen.update(item_keys)
                type_count += 1

            page_count = parse_positive_int(data.get("pagecount"), 1)
            if page >= page_count:
                break

        logging.info("%s 分类 %s 解析到 %s 个最新条目。", config.display_name, type_id, type_count)

    items.sort(key=lambda item: item.get("aired", ""), reverse=True)
    return items[: config.max_items]


def request_resource_site_json(
    params: Dict[str, Any],
    domains: List[str],
    config: ResourceSiteConfig,
) -> Tuple[Dict[str, Any], str]:
    errors = []
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": config.user_agent,
    }

    for domain in list(domains):
        api_base = normalize_resource_api_base(domain, config)
        if not api_base:
            continue

        try:
            response = requests.get(api_base, params=params, headers=headers, timeout=config.request_timeout)
        except requests.RequestException as exc:
            errors.append(f"{domain}: {exc}")
            continue

        if response.status_code >= 400:
            errors.append(f"{domain}: HTTP {response.status_code}")
            continue

        try:
            data = response.json()
        except ValueError as exc:
            errors.append(f"{domain}: JSON 解析失败 {exc}")
            continue

        if not isinstance(data, dict):
            errors.append(f"{domain}: 响应顶层不是对象")
            continue

        if "list" not in data:
            errors.append(f"{domain}: 响应缺少 list 字段")
            continue

        if domain != domains[0]:
            domains.remove(domain)
            domains.insert(0, domain)
            logging.info("%s 当前使用备用域名：%s", config.display_name, domain)

        return data, api_base

    raise RuntimeError(f"所有 {config.display_name} 域名都请求失败：" + "; ".join(errors[-5:]))


def normalize_resource_api_base(domain: str, config: ResourceSiteConfig) -> str:
    base = domain.strip()
    if not base:
        return ""
    if "://" not in base:
        scheme = "https" if config.code == "bfzy" else "http"
        base = f"{scheme}://{base}"

    parsed = urlparse(base)
    host = parsed.netloc.lower()
    if config.code == "bfzy" and host in {"bfzy.tv", "www.bfzy.tv"} and not parsed.path.strip("/"):
        base = "https://bfzyapi.com"

    base = base.rstrip("/")
    path = urlparse(base).path.rstrip("/")
    if path.endswith("/api.php/provide/vod"):
        return f"{base}/"
    return f"{base}/api.php/provide/vod/"


def extract_resource_site_item(
    entry: Dict[str, Any],
    api_base: str,
    config: ResourceSiteConfig,
) -> Optional[Dict[str, str]]:
    type_id = safe_text(entry.get("type_id"), "")
    type_name = clean_ffzy_text(entry.get("type_name"), "")
    if type_id and type_id not in set(config.type_ids):
        return None
    if not type_id and type_name not in set(config.type_names):
        return None

    vod_id = safe_text(entry.get("vod_id"), "")
    title = clean_ffzy_text(entry.get("vod_name"), "")
    if not title:
        return None

    updated_at = clean_ffzy_text(entry.get("vod_time"), "")
    if updated_at and not is_recent_resource_site_time(updated_at, config.max_age_hours):
        return None

    remarks = clean_ffzy_text(entry.get("vod_remarks"), "")
    key_id = vod_id or clean_ffzy_key_part(title)
    key_time = updated_at or remarks or safe_text(entry.get("vod_en"), "")
    key = f"{config.code}:{clean_ffzy_key_part(key_id)}:{clean_ffzy_key_part(key_time)}"

    dedupe_keys = []
    equivalence_key = build_resource_equivalence_key(title, remarks)
    if equivalence_key:
        dedupe_keys.append(equivalence_key)

    domain = resource_domain_label(api_base)
    source_parts = [config.display_name, domain]
    if type_name:
        source_parts.append(type_name)

    return {
        "key": key,
        "dedupe_keys": dedupe_keys,
        "type": config.code,
        "label": f"{config.display_name} {type_name or type_id}",
        "title": title,
        "subtitle": remarks,
        "code": type_name or type_id,
        "aired": updated_at or "未知",
        "time_label": "更新时间",
        "source": " · ".join(source_parts),
        "url": resource_detail_api_url(api_base, vod_id),
    }


def clean_ffzy_text(value: Any, fallback: str) -> str:
    text = safe_text(value, "")
    if not text:
        return fallback
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or fallback


def clean_ffzy_key_part(value: Any) -> str:
    text = safe_text(value, "")
    text = re.sub(r"\s+", "_", text)
    return re.sub(r"[^0-9A-Za-z_.:-]+", "_", text).strip("_") or "unknown"


def item_state_keys(item: Dict[str, Any]) -> List[str]:
    keys = [safe_text(item.get("key"), "")]
    extra_keys = item.get("dedupe_keys") or []
    if isinstance(extra_keys, (list, tuple, set)):
        keys.extend(safe_text(key, "") for key in extra_keys)

    return dedupe_keep_order([key for key in keys if key])


def item_seen(item: Dict[str, Any], seen: set) -> bool:
    return any(key in seen for key in item_state_keys(item))


def remember_item(item: Dict[str, Any], seen: set, target: Optional[List[str]] = None) -> None:
    for key in item_state_keys(item):
        seen.add(key)
        if target is not None:
            target.append(key)


def add_baseline_items(
    items: Sequence[Dict[str, Any]],
    pushed_set: set,
    baseline_seen: set,
    baseline_ids: List[str],
) -> int:
    item_count = 0
    for item in items:
        item_added = False
        for key in item_state_keys(item):
            if key in pushed_set or key in baseline_seen:
                continue
            baseline_seen.add(key)
            baseline_ids.append(key)
            item_added = True
        if item_added:
            item_count += 1
    return item_count


def sort_resource_site_items(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(items, key=resource_site_sort_key)


def resource_site_sort_key(item: Dict[str, Any]) -> Tuple[datetime, str, str]:
    item_time = parse_ffzy_datetime(safe_text(item.get("aired"), ""))
    return (
        item_time or datetime.max,
        safe_text(item.get("title"), ""),
        safe_text(item.get("key"), ""),
    )


def build_resource_equivalence_key(title: str, remarks: str) -> str:
    normalized_title = normalize_resource_equivalence_text(title)
    episode_marker = normalize_resource_episode_marker(remarks)
    if not normalized_title or not episode_marker:
        return ""
    return f"resource:{normalized_title}:{episode_marker}"


def normalize_resource_equivalence_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", clean_ffzy_text(value, ""))
    text = text.lower()
    text = re.sub(r"\s+", "", text)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text)


def normalize_resource_episode_marker(value: str) -> str:
    text = unicodedata.normalize("NFKC", clean_ffzy_text(value, ""))
    text = text.lower().replace("話", "话").replace("集数", "集")
    match = re.search(r"(?:第)?([0-9]+)\s*[集话話期]", text)
    if match:
        return f"ep{int(match.group(1))}"

    cn_match = re.search(r"(?:第)?([零〇一二两三四五六七八九十百千]+)\s*[集话話期]", text)
    if cn_match:
        number = parse_chinese_episode_number(cn_match.group(1))
        if number is not None:
            return f"ep{number}"

    date_match = re.search(r"(20[0-9]{6})", text)
    if date_match:
        return f"date{date_match.group(1)}"

    compact = normalize_resource_equivalence_text(text)
    if compact in {"完结", "全集", "全"}:
        return compact
    return ""


def parse_chinese_episode_number(value: str) -> Optional[int]:
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000}

    if not value:
        return None
    if all(char in digits for char in value):
        return int("".join(str(digits[char]) for char in value))

    total = 0
    section = 0
    for char in value:
        if char in digits:
            section = digits[char]
            continue
        unit = units.get(char)
        if unit is None:
            return None
        if section == 0:
            section = 1
        total += section * unit
        section = 0

    total += section
    return total if total > 0 else None


def resource_detail_api_url(api_base: str, vod_id: str) -> str:
    if not vod_id:
        return ""
    return f"{api_base}?ac=detail&ids={vod_id}"


def resource_domain_label(api_base: str) -> str:
    parsed = urlparse(api_base)
    return parsed.netloc or api_base


def is_recent_resource_site_time(value: str, max_age_hours: int) -> bool:
    if max_age_hours <= 0 or not value or value == "未知":
        return True

    item_time = parse_ffzy_datetime(value)
    if item_time is None:
        return True

    cn_tz = timezone(timedelta(hours=8))
    now = datetime.now(cn_tz).replace(tzinfo=None)
    return now - timedelta(hours=max_age_hours) <= item_time <= now + timedelta(hours=2)


def parse_ffzy_datetime(value: str) -> Optional[datetime]:
    text = value.strip()
    for fmt, length in (
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%d %H:%M", 16),
        ("%Y-%m-%d", 10),
    ):
        try:
            return datetime.strptime(text[:length], fmt)
        except ValueError:
            continue
    return None


def parse_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def extract_justwatch_apollo_state(html: str) -> Dict[str, Any]:
    match = re.search(
        r'<script type="application/json" data-nuxt-data="nuxt-app"[^>]*>(.*?)</script>',
        html,
        re.S,
    )
    if not match:
        raise RuntimeError("JustWatch 页面中未找到 Nuxt 数据")

    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"JustWatch Nuxt 数据不是合法 JSON：{exc}") from exc

    cache: Dict[int, Any] = {}

    def revive_index(index: int) -> Any:
        if index in cache:
            return cache[index]

        value = payload[index]
        if isinstance(value, dict):
            revived = {
                key: revive_index(item) if isinstance(item, int) else revive_list(item)
                for key, item in value.items()
            }
        elif isinstance(value, list):
            revived = revive_list(value)
        else:
            revived = value

        cache[index] = revived
        return revived

    def revive_list(values: List[Any]) -> Any:
        if (
            values
            and isinstance(values[0], str)
            and values[0] in {"ShallowReactive", "Reactive", "Ref", "ShallowRef"}
            and len(values) >= 2
            and isinstance(values[1], int)
        ):
            return revive_index(values[1])

        return [
            revive_index(item)
            if isinstance(item, int)
            else revive_list(item)
            if isinstance(item, list)
            else item
            for item in values
        ]

    try:
        data = revive_index(4)
    except (IndexError, TypeError) as exc:
        raise RuntimeError("JustWatch Nuxt 数据结构异常") from exc

    if not isinstance(data, dict) or "ROOT_QUERY" not in data:
        raise RuntimeError("JustWatch Apollo 缓存结构异常")

    return data


def resolve_apollo_value(apollo_state: Dict[str, Any], value: Any, seen: Optional[set] = None) -> Any:
    if seen is None:
        seen = set()

    if isinstance(value, dict):
        if set(value.keys()) == {"__ref"}:
            ref = value["__ref"]
            if ref in seen:
                return value
            return resolve_apollo_value(apollo_state, apollo_state.get(ref, value), seen | {ref})

        return {key: resolve_apollo_value(apollo_state, item, seen) for key, item in value.items()}

    if isinstance(value, list):
        return [resolve_apollo_value(apollo_state, item, seen) for item in value]

    return value


def extract_justwatch_edge(
    edge: Dict[str, Any],
    country: str,
    query_date: str,
) -> Optional[Dict[str, str]]:
    node = edge.get("node") or {}
    offer = edge.get("newOffer") or find_prefixed_value(edge, "newOffer(") or {}
    if not offer:
        return None

    content = node.get("content") or find_prefixed_value(node, "content(") or {}
    show = node.get("show") or {}
    show_content = {}
    if isinstance(show, dict):
        show_content = show.get("content") or find_prefixed_value(show, "content(") or {}

    object_type = safe_text(node.get("objectType") or node.get("__typename"), "")
    package = offer.get("package") or {}
    package_name = safe_text(package.get("clearName"), "")
    package_code = safe_text(package.get("shortName"), "")
    date_created = safe_text(offer.get("dateCreated") or query_date, "未知")
    if not is_recent_justwatch_date(date_created):
        return None

    is_show = object_type in {"SHOW", "SHOW_SEASON", "EPISODE"}
    title = safe_text(show_content.get("title") if is_show else content.get("title"), "")
    if not title:
        title = safe_text(content.get("title") or show_content.get("title"), "")
    if not title:
        return None

    full_path = safe_text(show_content.get("fullPath") or content.get("fullPath"), "")
    url = urljoin("https://www.justwatch.com", full_path) if full_path else ""

    source_parts = ["JustWatch"]
    if country:
        source_parts.append(country)
    if package_name:
        source_parts.append(package_name)
    source = " · ".join(source_parts)

    subtitle_parts = []
    season_title = safe_text(content.get("title"), "")
    if is_show and season_title and season_title != title:
        subtitle_parts.append(season_title)

    new_count = offer.get("newElementCount")
    if isinstance(new_count, int) and new_count > 0:
        subtitle_parts.append(f"新增 {new_count} 集")

    offer_id = safe_text(offer.get("id"), "")
    node_id = safe_text(node.get("id"), "")
    key_id = offer_id or f"{country}:{package_code}:{node_id}:{date_created}"
    key = f"justwatch:{key_id}"

    return {
        "key": key,
        "type": "justwatch_show" if is_show else "justwatch_movie",
        "label": "JustWatch剧集" if is_show else "JustWatch电影",
        "title": title,
        "subtitle": "，".join(subtitle_parts),
        "code": package_name or "JustWatch",
        "aired": date_created,
        "time_label": "上架日期",
        "source": source,
        "url": url,
        "country": country,
    }


def find_prefixed_value(data: Dict[str, Any], prefix: str) -> Any:
    if not isinstance(data, dict):
        return None

    for key, value in data.items():
        if key.startswith(prefix):
            return value
    return None


def justwatch_country_from_url(url: str) -> str:
    path_parts = [part for part in unquote(urlparse(url).path).split("/") if part]
    if not path_parts:
        return ""
    return path_parts[0].upper()


def is_justwatch_country_new_url(url: str) -> bool:
    path_parts = [part for part in unquote(urlparse(url).path).split("/") if part]
    if len(path_parts) < 2:
        return False
    return path_parts[1].lower() in {"new", "最新"}


def justwatch_language(country: str) -> str:
    return JUSTWATCH_LANGUAGE or ("zh" if country.upper() in {"HK", "TW"} else "en")


def justwatch_state_country(key: str) -> str:
    if not key.startswith("justwatch:"):
        return ""

    key_id = key.split(":", 1)[1]
    candidates = [key_id]
    try:
        padded = key_id + ("=" * (-len(key_id) % 4))
        candidates.append(base64.b64decode(padded).decode("utf-8", errors="ignore"))
    except (binascii.Error, ValueError, TypeError):
        pass

    for candidate in candidates:
        match = re.search(r"(?:^|[:|])([A-Z]{2})(?=[:|])", candidate)
        if match:
            return match.group(1)
    return ""


def has_justwatch_state_for_country(pushed_ids: Sequence[str], country: str) -> bool:
    country = normalize_justwatch_country(country)
    for key in pushed_ids:
        if not key.startswith("justwatch:"):
            continue
        state_country = justwatch_state_country(key)
        if state_country == country:
            return True
    return False


def extract_justwatch_query_date(query_key: str) -> str:
    match = re.search(r'"date":"([^"]+)"', query_key)
    return match.group(1) if match else ""


def is_recent_justwatch_date(value: str) -> bool:
    if JUSTWATCH_MAX_AGE_DAYS <= 0 or not value or value == "未知":
        return True

    try:
        item_date = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return True

    today = datetime.now(timezone.utc).date()
    return today - timedelta(days=JUSTWATCH_MAX_AGE_DAYS) <= item_date <= today + timedelta(days=1)


def safe_text(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def format_air_time(value: Any) -> str:
    if not value:
        return "未知"

    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text

    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")

    return dt.strftime("%Y-%m-%d %H:%M")


def build_discord_message(items: Sequence[Dict[str, str]]) -> str:
    lines = ["**今日流媒体更新**", ""]
    for item in items:
        lines.append(f"**[{item['label']}] {item['title']}**")
        if item["type"] == "episode":
            lines.append(f"- `{item['code']}` {item['subtitle']}")
        elif item.get("subtitle"):
            lines.append(f"- {item['subtitle']}")
        if item.get("source"):
            lines.append(f"- 来源：{item['source']}")
        lines.extend(
            [
                f"- {item.get('time_label', '上线时间')}：{item['aired']}",
                "",
            ]
        )
        if item.get("url"):
            lines.insert(-1, f"- 链接：{item['url']}")
    return "\n".join(lines).strip()


def escape_markdown_v2(text: str) -> str:
    """转义 Telegram MarkdownV2 特殊字符。"""
    special_chars = r"_*[]()~`>#+-=|{}.!"
    escaped = []
    for char in text:
        if char == "\\":
            escaped.append("\\\\")
        elif char in special_chars:
            escaped.append("\\" + char)
        else:
            escaped.append(char)
    return "".join(escaped)


def build_telegram_message(items: Sequence[Dict[str, str]]) -> str:
    lines = ["*今日流媒体更新*", ""]
    for item in items:
        label = escape_markdown_v2(item["label"])
        title = escape_markdown_v2(item["title"])
        aired = escape_markdown_v2(item["aired"])
        time_label = escape_markdown_v2(item.get("time_label", "上线时间"))

        lines.append(f"*\\[{label}\\] {title}*")
        if item["type"] == "episode":
            code = escape_markdown_v2(item["code"])
            subtitle = escape_markdown_v2(item["subtitle"])
            lines.append(f"• `{code}` {subtitle}")
        elif item.get("subtitle"):
            lines.append(f"• {escape_markdown_v2(item['subtitle'])}")
        if item.get("source"):
            lines.append(f"• 来源：{escape_markdown_v2(item['source'])}")
        lines.extend(
            [
                f"• {time_label}：{aired}",
                "",
            ]
        )
        if item.get("url"):
            lines.insert(-1, f"• 链接：[打开]({escape_markdown_v2_url(item['url'])})")
    return "\n".join(lines).strip()


def escape_markdown_v2_url(url: str) -> str:
    """MarkdownV2 链接地址里只需要转义反斜杠和右括号。"""
    return str(url).replace("\\", "\\\\").replace(")", "\\)")


def split_message(text: str, limit: int) -> List[str]:
    """按空行切分长消息，避免超过平台单条消息长度限制。"""
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""

    for block in text.split("\n\n"):
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)

        if len(block) <= limit:
            current = block
        else:
            # 极端情况下单个块过长，硬切分，避免接口拒收。
            for start in range(0, len(block), limit):
                chunks.append(block[start : start + limit])
            current = ""

    if current:
        chunks.append(current)

    return chunks


def telegram_html(value: Any) -> str:
    return html.escape(str(value), quote=False)


def build_telegram_caption(item: Dict[str, Any]) -> str:
    """图文卡片的说明文字：标题可点、集数、播出时间、题材、评分、简介。"""
    title = telegram_html(item.get("title", ""))
    caption_url = telegram_html(item.get("url", ""))
    lines = [f'<b><a href="{caption_url}">{title}</a></b>'] if caption_url else [f"<b>{title}</b>"]

    meta = [part for part in (item.get("label"), item.get("code")) if part]
    if meta:
        lines.append(" · ".join(telegram_html(part) for part in meta))

    if item.get("aired"):
        lines.append(f"播出：{telegram_html(item['aired'])}")
    if item.get("air_status") == "待开播":
        lines.append("状态：待开播")
    if item.get("subtitle"):
        lines.append(f"题材：{telegram_html(item['subtitle'])}")
    if item.get("score"):
        lines.append(f"评分：{telegram_html(item['score'])}")

    synopsis = (item.get("synopsis") or "").strip()
    if synopsis:
        if len(synopsis) > 160:
            synopsis = synopsis[:160].rstrip() + "…"
        lines.append(f"简介：{telegram_html(synopsis)}")

    return "\n".join(lines)[:1024]


def fetch_photo_bytes(url: str) -> Optional[bytes]:
    """下载封面图，优先用浏览器指纹，失败后回落 requests；原图 404 时退回小图。"""
    if not url:
        return None
    headers = {
        "User-Agent": MYDRAMALIST_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": "https://mydramalist.com/",
    }

    candidates = [url]
    if url.endswith("_4f.jpg"):
        candidates.append(url[: -len("_4f.jpg")] + "_4c.jpg")

    for candidate in candidates:
        if curl_cffi_requests is not None:
            try:
                response = curl_cffi_requests.get(
                    candidate, headers=headers, impersonate="chrome", timeout=MYDRAMALIST_REQUEST_TIMEOUT
                )
                if response.status_code < 400 and response.content:
                    return response.content
            except Exception as exc:
                logging.warning("curl_cffi 下载封面失败：%s", exc)

        try:
            response = requests.get(candidate, headers=headers, timeout=MYDRAMALIST_REQUEST_TIMEOUT)
            if response.status_code < 400 and response.content:
                return response.content
            logging.warning("下载封面返回 HTTP %s：%s", response.status_code, candidate)
        except requests.RequestException as exc:
            logging.warning("下载封面失败：%s - %s", candidate, exc)

    return None


def send_telegram_photo(item: Dict[str, Any]) -> bool:
    """发一条带封面的图文消息，并附「在 MyDramaList 查看」按钮。"""
    api = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
    caption = build_telegram_caption(item)
    data: Dict[str, Any] = {
        "chat_id": TG_CHAT_ID,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if item.get("url"):
        data["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": "在 MyDramaList 查看", "url": item["url"]}]]},
            ensure_ascii=False,
        )

    image = fetch_photo_bytes(item.get("image", ""))
    try:
        if image:
            response = requests.post(
                api, data=data, files={"photo": ("cover.jpg", image)}, timeout=REQUEST_TIMEOUT * 3
            )
        else:
            data["photo"] = item["image"]
            response = requests.post(api, data=data, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logging.error("发送 Telegram 图文失败：%s", exc)
        return False

    if response.status_code == 429:
        # 连发多张图可能触发限流，按 Telegram 给的 retry_after 等一次再发。
        retry_after = 5
        try:
            retry_after = int(response.json().get("parameters", {}).get("retry_after", 5))
        except Exception:
            pass
        logging.warning("Telegram 限流，等待 %s 秒后重发：%s", retry_after, item.get("title"))
        time.sleep(min(max(retry_after, 1), 60))
        try:
            if image:
                response = requests.post(
                    api, data=data, files={"photo": ("cover.jpg", image)}, timeout=REQUEST_TIMEOUT * 3
                )
            else:
                response = requests.post(api, data=data, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logging.error("发送 Telegram 图文失败：%s", exc)
            return False

    if response.status_code >= 400:
        logging.error(
            "Telegram sendPhoto 返回错误：HTTP %s - %s",
            response.status_code,
            response.text[:500],
        )
        return False

    logging.info("Telegram 图文发送成功：%s %s", item.get("title"), item.get("code"))
    return True


def send_telegram(items: Sequence[Dict[str, str]]) -> bool:
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return True

    # 带封面的条目（目前为 MyDramaList）单独发图文，其余合并成文本摘要。
    photo_items = [item for item in items if item.get("image") and item.get("url")]
    text_items = [item for item in items if not (item.get("image") and item.get("url"))]

    if text_items and not send_telegram_digest(text_items):
        return False

    for item in photo_items:
        time.sleep(TELEGRAM_PHOTO_INTERVAL)
        if send_telegram_photo(item):
            continue
        logging.warning("图文推送失败，回退为文本：%s %s", item.get("title"), item.get("code"))
        if not send_telegram_digest([item]):
            return False

    return True


def send_telegram_alert(item: Dict[str, Any]) -> bool:
    """开播到点提醒：一条短消息，标题可点，附「在 MyDramaList 查看」按钮。"""
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return True

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    title = telegram_html(item.get("title", ""))
    link = telegram_html(item.get("url", ""))
    head = f'🔔 <b><a href="{link}">{title}</a></b>' if link else f"🔔 <b>{title}</b>"
    lines = [head, f"{telegram_html(item.get('code', ''))} 已开播"]
    if item.get("aired"):
        lines.append(f"播出：{telegram_html(item['aired'])}")

    payload: Dict[str, Any] = {
        "chat_id": TG_CHAT_ID,
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if item.get("url"):
        payload["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": "在 MyDramaList 查看", "url": item["url"]}]]},
            ensure_ascii=False,
        )

    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logging.error("发送开播提醒失败：%s", exc)
        return False

    if response.status_code >= 400:
        logging.error(
            "Telegram 开播提醒返回错误：HTTP %s - %s", response.status_code, response.text[:500]
        )
        return False

    logging.info("开播提醒发送成功：%s %s", item.get("title"), item.get("code"))
    return True


def send_telegram_digest(items: Sequence[Dict[str, str]]) -> bool:
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    message = build_telegram_message(items)
    chunks = split_message(message, 3900)

    for index, chunk in enumerate(chunks, start=1):
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": chunk,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }

        try:
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logging.error("发送 Telegram 消息失败：%s", exc)
            return False

        if response.status_code >= 400:
            logging.error(
                "Telegram API 返回错误：HTTP %s - %s",
                response.status_code,
                response.text[:500],
            )
            return False

        logging.info("Telegram 消息发送成功：%s/%s", index, len(chunks))

    return True


def send_discord(items: Sequence[Dict[str, str]]) -> bool:
    if not DISCORD_WEBHOOK_URL:
        return True

    message = build_discord_message(items)
    chunks = split_message(message, 1900)

    for index, chunk in enumerate(chunks, start=1):
        payload = {
            "content": chunk,
            "allowed_mentions": {"parse": []},
        }

        try:
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logging.error("发送 Discord 消息失败：%s", exc)
            return False

        if response.status_code not in (200, 204):
            logging.error(
                "Discord Webhook 返回错误：HTTP %s - %s",
                response.status_code,
                response.text[:500],
            )
            return False

        logging.info("Discord 消息发送成功：%s/%s", index, len(chunks))

    return True


def main(only_source: str = "") -> int:
    try:
        require_config()
        only = (only_source or "").strip().lower()

        def want(name: str) -> bool:
            """--only 模式下只抓指定源；默认抓全部已启用源。"""
            return only in ("", "all") or only == name

        tokens = TraktTokens(
            access_token=TRAKT_ACCESS_TOKEN,
            refresh_token=TRAKT_REFRESH_TOKEN,
        )

        pushed_ids = load_pushed_ids()
        pushed_set = set(pushed_ids)

        calendar_items: List[Dict[str, str]] = []
        if want("trakt"):
            try:
                calendar_items = fetch_today_items(tokens)
            except Exception as exc:
                # Trakt refresh tokens can be revoked independently of the other
                # feeds. Keep the scheduled job healthy so JustWatch/FFZY/BFZY
                # continue to run while the Trakt credentials are re-authorized.
                logging.error("Trakt 获取失败，本次跳过 Trakt：%s", exc)
                calendar_items = []
        justwatch_items = fetch_justwatch_items() if want("justwatch") else []
        ffzy_items = fetch_ffzy_items() if want("ffzy") else []
        bfzy_items = fetch_bfzy_items() if want("bfzy") else []
        mydramalist_items: List[Dict[str, str]] = []
        if want("mydramalist"):
            try:
                mydramalist_items = fetch_mydramalist_items()
            except Exception as exc:
                # MyDramaList 可能触发风控，失败时只跳过这个源，不影响其他渠道。
                logging.error("MyDramaList 获取失败，本次跳过：%s", exc)
                mydramalist_items = []

        baseline_ids: List[str] = []
        if justwatch_items and JUSTWATCH_BOOTSTRAP_ONLY and not any(
            has_justwatch_state_for_country(pushed_ids, safe_text(item.get("country"), JUSTWATCH_COUNTRY))
            for item in justwatch_items
        ):
            for item in justwatch_items:
                item_key = item["key"]
                if item_key in pushed_set:
                    continue
                baseline_ids.append(item_key)
                pushed_set.add(item_key)

            logging.info(
                "JustWatch 首次运行已建立 %s 条基线记录，本次不推送这些历史条目。",
                len(baseline_ids),
            )
        else:
            calendar_items.extend(justwatch_items)

        baseline_seen = set()

        if mydramalist_items and MYDRAMALIST_BOOTSTRAP_ONLY and not any(
            item.startswith("mydramalist:") for item in pushed_ids
        ):
            baseline_count = add_baseline_items(
                mydramalist_items, pushed_set, baseline_seen, baseline_ids
            )
            logging.info(
                "MyDramaList 首次运行已建立 %s 条基线记录，本次不推送这些历史条目。",
                baseline_count,
            )
        elif (
            mydramalist_items
            and MYDRAMALIST_INITIAL_PUSH_DAYS > 0
            and not mydramalist_initial_done(pushed_set)
        ):
            # 新模式的第一次运行：只推最近几天到点的，剩下的先记账不推，避免一次刷几十条。
            horizon = int(
                (datetime.now(timezone.utc) + timedelta(days=MYDRAMALIST_INITIAL_PUSH_DAYS)).timestamp()
            )
            near_term: List[Dict[str, str]] = []
            deferred = 0
            for item in mydramalist_items:
                air_ts = int(item.get("air_ts") or 0)
                if air_ts and air_ts <= horizon:
                    near_term.append(item)
                    continue
                if item["key"] not in pushed_set:
                    baseline_ids.append(item["key"])
                    pushed_set.add(item["key"])
                    deferred += 1

            if not mydramalist_initial_done(baseline_ids):
                baseline_ids.append(MYDRAMALIST_INITIAL_MARKER)
                pushed_set.add(MYDRAMALIST_INITIAL_MARKER)

            logging.info(
                "MyDramaList 首次启用新模式：只推送 %s 天内到点的 %s 条，其余 %s 条先记为已见。",
                MYDRAMALIST_INITIAL_PUSH_DAYS,
                len(near_term),
                deferred,
            )
            calendar_items.extend(near_term)
        else:
            calendar_items.extend(mydramalist_items)

        resource_site_batches = [
            (ffzy_site_config(), ffzy_items),
            (bfzy_site_config(), bfzy_items),
        ]
        resource_candidates: List[Dict[str, Any]] = []
        for config, items in resource_site_batches:
            if not items:
                continue

            has_existing_state = any(item.startswith(f"{config.code}:") for item in pushed_ids)
            if config.bootstrap_only and not has_existing_state:
                baseline_count = add_baseline_items(items, pushed_set, baseline_seen, baseline_ids)
                logging.info(
                    "%s 首次运行已建立 %s 条基线记录，本次不推送这些历史条目。",
                    config.display_name,
                    baseline_count,
                )
                continue

            resource_candidates.extend(items)

        calendar_items.extend(sort_resource_site_items(resource_candidates))

        new_items = []
        new_ids = []
        seen_this_run = set()

        for item in calendar_items:
            if item_seen(item, pushed_set) or item_seen(item, seen_this_run):
                continue

            new_items.append(item)
            remember_item(item, seen_this_run, new_ids)

        new_id_set = set(new_ids)

        # 开播提醒：日历里已到播出时间、预告已发过、且提醒键还没写进状态的条目。
        remind_items: List[Dict[str, Any]] = []
        remind_ids: List[str] = []
        if MYDRAMALIST_REMIND_AT_AIR and mydramalist_items:
            now_ts = int(datetime.now(timezone.utc).timestamp())
            window_start = now_ts - MYDRAMALIST_REMIND_WINDOW_MINUTES * 60
            announced = pushed_set | new_id_set
            for item in mydramalist_items:
                air_ts = int(item.get("air_ts") or 0)
                remind_key = str(item.get("remind_key") or "")
                if not air_ts or not remind_key:
                    continue
                if not (window_start <= air_ts <= now_ts):
                    continue
                # 预告还没发，或就是本轮刚发的预告，都不再额外补一条提醒。
                if item["key"] not in announced or item["key"] in new_id_set:
                    continue
                if remind_key in pushed_set or remind_key in remind_ids:
                    continue
                remind_items.append(item)
                remind_ids.append(remind_key)

        if not new_items and not remind_items:
            if baseline_ids:
                save_pushed_ids(pushed_ids + baseline_ids)
                logging.info("已更新 pushed_episodes.json，新增 %s 条基线记录。", len(baseline_ids))
            logging.info("没有发现需要推送的新剧集或电影。")
            return 0

        if new_items:
            logging.info("发现 %s 个新条目，准备推送。", len(new_items))

            telegram_ok = send_telegram(new_items)
            discord_ok = send_discord(new_items)

            # 只有所有已配置渠道都成功后才写入去重文件，避免通知失败却被标记为已推送。
            if not (telegram_ok and discord_ok):
                logging.error("至少一个推送渠道失败，本次不更新 pushed_episodes.json。")
                return 1
        else:
            new_ids = []

        alert_ids: List[str] = []
        if remind_items:
            logging.info("发现 %s 条到点开播的提醒，准备推送。", len(remind_items))
            for item in remind_items:
                if not send_telegram_alert(item):
                    logging.warning("开播提醒发送失败，留到下一轮重试：%s", item.get("title"))
                    break
                alert_ids.append(str(item["remind_key"]))

        save_pushed_ids(pushed_ids + baseline_ids + new_ids + alert_ids)
        logging.info(
            "已更新 pushed_episodes.json，新增 %s 条记录。",
            len(baseline_ids) + len(new_ids) + len(alert_ids),
        )
        return 0

    except Exception as exc:
        logging.exception("运行失败：%s", exc)
        return 1


def parse_only_argument(argv: Sequence[str]) -> str:
    """解析 --only 参数，例如 python trakt_bot.py --only mydramalist。"""
    args = list(argv)
    for index, arg in enumerate(args):
        if arg == "--only" and index + 1 < len(args):
            return args[index + 1].strip().lower()
        if arg.startswith("--only="):
            return arg.split("=", 1)[1].strip().lower()
    return ""


if __name__ == "__main__":
    sys.exit(main(parse_only_argument(sys.argv[1:])))
