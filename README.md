# Trakt Streaming Update Bot

GitHub Actions 每小时运行一次，读取 Trakt.tv 个人剧集和电影日历，并推送新上架内容到 Telegram 或 Discord。

## GitHub Secrets

必须配置：

- `TRAKT_CLIENT_ID`
- `TRAKT_ACCESS_TOKEN`

建议同时配置，用于 Trakt token 自动续期：

- `TRAKT_CLIENT_SECRET`
- `TRAKT_REFRESH_TOKEN`

至少配置一个推送渠道：

- Telegram：`TG_BOT_TOKEN` 和 `TG_CHAT_ID`
- Discord：`DISCORD_WEBHOOK_URL`

## 自动续期

当 Trakt 返回 401 时，脚本会使用 `TRAKT_REFRESH_TOKEN` 自动换取新的 `access_token` 和 `refresh_token`。

刷新成功后，脚本会通过 GitHub CLI 写回仓库 Secrets：

- `TRAKT_ACCESS_TOKEN`
- `TRAKT_REFRESH_TOKEN`

敏感 token 不会写入仓库文件。

## 运行方式

推送到 GitHub 后，工作流会按 UTC 每小时整点运行，也可以在 Actions 页面手动触发。

脚本会读取：

- `/calendars/my/shows/{YYYY-MM-DD}/1`
- `/calendars/my/movies/{YYYY-MM-DD}/1`

因此在播剧当天出现新的集数时，只要它出现在你的 Trakt 个人日历里，就会被推送。

去重状态保存在 `pushed_episodes.json`，每次成功推送后由 GitHub Actions 自动提交回仓库。新记录会使用 `episode:ID` 和 `movie:ID` 区分类型。
