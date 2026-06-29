import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import requests


TRAKT_API_VERSION = "2"
TRAKT_CALENDAR_DAYS = 1
REQUEST_TIMEOUT = 20
MAX_STATE_RECORDS = 500

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "pushed_episodes.json"

TRAKT_CLIENT_ID = os.getenv("TRAKT_CLIENT_ID", "").strip()
TRAKT_ACCESS_TOKEN = os.getenv("TRAKT_ACCESS_TOKEN", "").strip()
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


class ConfigError(RuntimeError):
    pass


def require_config() -> None:
    """校验必要配置，避免运行时静默失败。"""
    missing = []
    if not TRAKT_CLIENT_ID:
        missing.append("TRAKT_CLIENT_ID")
    if not TRAKT_ACCESS_TOKEN:
        missing.append("TRAKT_ACCESS_TOKEN")

    if missing:
        raise ConfigError(f"缺少必要环境变量：{', '.join(missing)}")

    if bool(TG_BOT_TOKEN) != bool(TG_CHAT_ID):
        raise ConfigError("Telegram 配置不完整：TG_BOT_TOKEN 和 TG_CHAT_ID 必须同时提供")

    if not ((TG_BOT_TOKEN and TG_CHAT_ID) or DISCORD_WEBHOOK_URL):
        raise ConfigError("至少需要配置 Telegram 或 Discord 的一个推送目标")


def load_pushed_ids() -> List[str]:
    """读取已推送剧集 ID；文件不存在时初始化为空列表。"""
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

    # 统一转为字符串，避免 Trakt ID 在不同地方以 int/string 表示导致去重失效。
    ids = [str(item) for item in raw if item is not None]
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


def fetch_today_calendar() -> List[Dict[str, Any]]:
    """调用 Trakt 个人日历接口，获取今天起 1 天内的剧集。"""
    start_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = f"https://api.trakt.tv/calendars/my/shows/{start_date}/{TRAKT_CALENDAR_DAYS}"

    headers = {
        "Content-Type": "application/json",
        "trakt-api-version": TRAKT_API_VERSION,
        "trakt-api-key": TRAKT_CLIENT_ID,
        "Authorization": f"Bearer {TRAKT_ACCESS_TOKEN}",
    }

    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f"请求 Trakt API 失败：{exc}") from exc

    if response.status_code >= 400:
        raise RuntimeError(
            f"Trakt API 返回错误：HTTP {response.status_code} - {response.text[:500]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Trakt API 响应不是合法 JSON：{exc}") from exc

    if not isinstance(data, list):
        raise RuntimeError("Trakt API 响应格式异常：顶层结构不是数组")

    return data


def extract_episode(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """从 Trakt 日历条目中提取推送所需字段。"""
    episode = item.get("episode") or {}
    show = item.get("show") or {}

    if not isinstance(episode, dict) or not isinstance(show, dict):
        logging.warning("跳过格式异常的日历条目：%s", item)
        return None

    episode_ids = episode.get("ids") or {}
    trakt_id = episode_ids.get("trakt")
    if trakt_id is None:
        logging.warning("跳过缺少 Trakt Episode ID 的条目：%s", item)
        return None

    season = episode.get("season")
    number = episode.get("number")

    return {
        "id": str(trakt_id),
        "show_title": safe_text(show.get("title"), "未知剧集"),
        "season": str(season) if season is not None else "?",
        "number": str(number) if number is not None else "?",
        "episode_title": safe_text(episode.get("title"), "未命名单集"),
        "first_aired": format_air_time(item.get("first_aired") or episode.get("first_aired")),
    }


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


def build_discord_message(episodes: Sequence[Dict[str, str]]) -> str:
    lines = ["**今日流媒体剧集更新**", ""]
    for ep in episodes:
        code = f"S{ep['season']}E{ep['number']}"
        lines.extend(
            [
                f"**{ep['show_title']}**",
                f"- `{code}` {ep['episode_title']}",
                f"- 上线时间：{ep['first_aired']}",
                "",
            ]
        )
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


def build_telegram_message(episodes: Sequence[Dict[str, str]]) -> str:
    lines = ["*今日流媒体剧集更新*", ""]
    for ep in episodes:
        code = escape_markdown_v2(f"S{ep['season']}E{ep['number']}")
        show_title = escape_markdown_v2(ep["show_title"])
        episode_title = escape_markdown_v2(ep["episode_title"])
        first_aired = escape_markdown_v2(ep["first_aired"])

        lines.extend(
            [
                f"*{show_title}*",
                f"• `{code}` {episode_title}",
                f"• 上线时间：{first_aired}",
                "",
            ]
        )
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


def send_telegram(episodes: Sequence[Dict[str, str]]) -> bool:
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return True

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    message = build_telegram_message(episodes)
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


def send_discord(episodes: Sequence[Dict[str, str]]) -> bool:
    if not DISCORD_WEBHOOK_URL:
        return True

    message = build_discord_message(episodes)
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

        pushed_ids = load_pushed_ids()
        pushed_set = set(pushed_ids)

        calendar_items = fetch_today_calendar()

        new_episodes = []
        new_ids = []
        seen_this_run = set()

        for item in calendar_items:
            episode = extract_episode(item)
            if not episode:
                continue

            episode_id = episode["id"]
            if episode_id in pushed_set or episode_id in seen_this_run:
                continue

            new_episodes.append(episode)
            new_ids.append(episode_id)
            seen_this_run.add(episode_id)

        if not new_episodes:
            logging.info("没有发现需要推送的新剧集。")
            return 0

        logging.info("发现 %s 个新剧集，准备推送。", len(new_episodes))

        telegram_ok = send_telegram(new_episodes)
        discord_ok = send_discord(new_episodes)

        # 只有所有已配置渠道都成功后才写入去重文件，避免通知失败却被标记为已推送。
        if not (telegram_ok and discord_ok):
            logging.error("至少一个推送渠道失败，本次不更新 pushed_episodes.json。")
            return 1

        save_pushed_ids(pushed_ids + new_ids)
        logging.info("已更新 pushed_episodes.json，新增 %s 条记录。", len(new_ids))
        return 0

    except Exception as exc:
        logging.exception("运行失败：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
