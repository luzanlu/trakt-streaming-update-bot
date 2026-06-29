# Trakt Streaming Update Bot

GitHub Actions 每小时运行一次，读取 Trakt.tv 个人剧集日历，并推送新上架剧集到 Telegram 或 Discord。

## GitHub Secrets

必须配置：

- `TRAKT_CLIENT_ID`
- `TRAKT_ACCESS_TOKEN`

至少配置一个推送渠道：

- Telegram：`TG_BOT_TOKEN` 和 `TG_CHAT_ID`
- Discord：`DISCORD_WEBHOOK_URL`

## 运行方式

推送到 GitHub 后，工作流会按 UTC 每小时整点运行，也可以在 Actions 页面手动触发。

去重状态保存在 `pushed_episodes.json`，每次成功推送后由 GitHub Actions 自动提交回仓库。
