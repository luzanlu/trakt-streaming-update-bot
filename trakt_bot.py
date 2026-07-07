import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import unquote, urljoin, urlparse

import requests


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


TRAKT_API_VERSION = "2"
TRAKT_CALENDAR_DAYS = 1
TRAKT_REDIRECT_URI = os.getenv("TRAKT_REDIRECT_URI", "urn:ietf:wg:oauth:2.0:oob").strip()
REQUEST_TIMEOUT = 20
MAX_STATE_RECORDS = 500

DEFAULT_JUSTWATCH_PROVIDERS = [
    # JustWatch 没有公开的中国大陆区域；默认用港台区的主流中文平台。
    "TW:iqi",  # iQIYI
    "TW:tcv",  # WeTV
    "TW:lin",  # LINE TV
    "HK:iqi",  # iQIYI
    "HK:tcv",  # WeTV
    "HK:viu",  # Viu
]
JUSTWATCH_ENABLED = parse_bool_env("JUSTWATCH_ENABLED", False)
JUSTWATCH_PROVIDERS = split_env_list(os.getenv("JUSTWATCH_PROVIDERS", "")) or DEFAULT_JUSTWATCH_PROVIDERS
JUSTWATCH_URLS = split_env_list(os.getenv("JUSTWATCH_URLS", ""))
JUSTWATCH_BOOTSTRAP_ONLY = parse_bool_env("JUSTWATCH_BOOTSTRAP_ONLY", True)
JUSTWATCH_ITEMS_PER_PROVIDER = max(1, parse_int_env("JUSTWATCH_ITEMS_PER_PROVIDER", 10))
JUSTWATCH_MAX_ITEMS = max(1, parse_int_env("JUSTWATCH_MAX_ITEMS", 100))
JUSTWATCH_MAX_AGE_DAYS = max(0, parse_int_env("JUSTWATCH_MAX_AGE_DAYS", 14))
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

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "pushed_episodes.json"

TRAKT_CLIENT_ID = os.getenv("TRAKT_CLIENT_ID", "").strip()
TRAKT_CLIENT_SECRET = os.getenv("TRAKT_CLIENT_SECRET", "").strip()
TRAKT_ACCESS_TOKEN = os.getenv("TRAKT_ACCESS_TOKEN", "").strip()
TRAKT_REFRESH_TOKEN = os.getenv("TRAKT_REFRESH_TOKEN", "").strip()
TRAKT_FORCE_REFRESH = parse_bool_env("TRAKT_FORCE_REFRESH", False)

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

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


def require_config() -> None:
    """校验必要配置，避免运行时静默失败。"""
    missing = []
    if not TRAKT_CLIENT_ID:
        missing.append("TRAKT_CLIENT_ID")

    has_access_token = bool(TRAKT_ACCESS_TOKEN)
    has_refresh_credentials = bool(TRAKT_CLIENT_SECRET and TRAKT_REFRESH_TOKEN)
    if not has_access_token and not has_refresh_credentials:
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
    """原子写入状态文件，限制最多保留最新 500 条记录。"""
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
    """调用 Trakt 个人日历接口，401 时自动刷新 token 并重试一次。"""
    response = request_calendar(tokens, media_type)
    if response.status_code == 401 and can_refresh_trakt_token(tokens):
        logging.warning("Trakt access_token 已失效，开始自动续期。")
        refresh_trakt_token(tokens)
        response = request_calendar(tokens, media_type)

    return parse_calendar_response(response, media_type)


def fetch_today_items(tokens: TraktTokens) -> List[Dict[str, str]]:
    """获取今天的剧集和电影，并解析成统一的推送条目。"""
    if TRAKT_FORCE_REFRESH or not tokens.access_token:
        logging.info("开始刷新 Trakt token。")
        refresh_trakt_token(tokens)

    items = []
    show_entries = fetch_calendar(tokens, "shows")
    movie_entries = fetch_calendar(tokens, "movies")

    for entry in show_entries:
        item = extract_episode(entry)
        if item:
            items.append(item)

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
        key = item["key"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= JUSTWATCH_MAX_ITEMS:
            break

    return deduped


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
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise RuntimeError("JUSTWATCH_PROVIDERS 条目格式必须是 COUNTRY:provider_code，例如 TW:lin")

    country = parts[0].strip().upper()
    provider_code = parts[1].strip()
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
    country = country.strip().upper()
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
        return fetch_justwatch_country_items(justwatch_country_from_url(url))

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
    country = justwatch_country_from_url(response.url or url)

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
    if country.upper() in {"HK", "TW"}:
        return "zh"
    return "en"


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
            lines.insert(-1, f"• 链接：{escape_markdown_v2(item['url'])}")
    return "\n".join(lines).strip()


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


def send_telegram(items: Sequence[Dict[str, str]]) -> bool:
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return True

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


def main() -> int:
    try:
        require_config()

        tokens = TraktTokens(
            access_token=TRAKT_ACCESS_TOKEN,
            refresh_token=TRAKT_REFRESH_TOKEN,
        )

        pushed_ids = load_pushed_ids()
        pushed_set = set(pushed_ids)

        calendar_items = fetch_today_items(tokens)
        justwatch_items = fetch_justwatch_items()

        baseline_ids: List[str] = []
        if justwatch_items and JUSTWATCH_BOOTSTRAP_ONLY and not any(
            item.startswith("justwatch:") for item in pushed_ids
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

        new_items = []
        new_ids = []
        seen_this_run = set()

        for item in calendar_items:
            item_key = item["key"]
            if item_key in pushed_set or item_key in seen_this_run:
                continue

            new_items.append(item)
            new_ids.append(item_key)
            seen_this_run.add(item_key)

        if not new_items:
            if baseline_ids:
                save_pushed_ids(pushed_ids + baseline_ids)
                logging.info("已更新 pushed_episodes.json，新增 %s 条 JustWatch 基线记录。", len(baseline_ids))
            logging.info("没有发现需要推送的新剧集或电影。")
            return 0

        logging.info("发现 %s 个新条目，准备推送。", len(new_items))

        telegram_ok = send_telegram(new_items)
        discord_ok = send_discord(new_items)

        # 只有所有已配置渠道都成功后才写入去重文件，避免通知失败却被标记为已推送。
        if not (telegram_ok and discord_ok):
            logging.error("至少一个推送渠道失败，本次不更新 pushed_episodes.json。")
            return 1

        save_pushed_ids(pushed_ids + baseline_ids + new_ids)
        logging.info("已更新 pushed_episodes.json，新增 %s 条记录。", len(baseline_ids) + len(new_ids))
        return 0

    except Exception as exc:
        logging.exception("运行失败：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
