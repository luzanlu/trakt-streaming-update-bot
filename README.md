# Trakt Streaming Update Bot

GitHub Actions 每 30 分钟运行一次，读取 Trakt.tv 个人剧集和电影日历，并推送新上架内容到 Telegram 或 Discord。

脚本也支持 JustWatch 最新上架追踪。JustWatch 没有公开的中国大陆区域，默认追踪台湾 JustWatch 最新上架页。

脚本还支持 FFZY 和 BFZY 采集接口追踪，只推送指定剧集和动漫分类的新更新。FFZY 与 BFZY 会按剧名和集数做跨站去重：同一部同一集哪个站先更新就推送哪个，后更新的站不会重复推送。

## GitHub Secrets

必须配置：

- `TRAKT_CLIENT_ID`
- `TRAKT_ACCESS_TOKEN`

建议同时配置，用于 Trakt token 自动续期：

- `TRAKT_CLIENT_SECRET`
- `TRAKT_REFRESH_TOKEN`
- `GH_SECRET_WRITE_TOKEN`

`GH_SECRET_WRITE_TOKEN` 需要是一个 GitHub PAT，并且对当前仓库具备 Actions Secrets 写权限。推荐使用 fine-grained PAT，只选择这个仓库，并授予 Repository permissions -> Secrets -> Read and write。也可以使用 classic PAT，私有仓库至少需要 `repo` scope。不要使用工作流自带的 `github.token`，它不能更新仓库 Secrets。

至少配置一个推送渠道：

- Telegram：`TG_BOT_TOKEN` 和 `TG_CHAT_ID`
- Discord：`DISCORD_WEBHOOK_URL`

## 自动续期

当 Trakt 返回 401 时，脚本会使用 `TRAKT_REFRESH_TOKEN` 自动换取新的 `access_token` 和 `refresh_token`。

刷新成功后，脚本会通过 GitHub CLI 和 `GH_SECRET_WRITE_TOKEN` 写回仓库 Secrets：

- `TRAKT_ACCESS_TOKEN`
- `TRAKT_REFRESH_TOKEN`

敏感 token 不会写入仓库文件。

## 运行方式

推送到 GitHub 后，工作流会按 UTC 每 30 分钟运行一次，也可以在 Actions 页面手动触发。

脚本会读取：

- `/calendars/my/shows/{YYYY-MM-DD}/1`
- `/calendars/my/movies/{YYYY-MM-DD}/1`

因此在播剧当天出现新的集数时，只要它出现在你的 Trakt 个人日历里，就会被推送。

去重状态保存在 `pushed_episodes.json`，每次成功推送后由 GitHub Actions 自动提交回仓库。新记录会使用 `episode:ID` 和 `movie:ID` 区分类型；资源站还会额外写入 `resource:剧名:集数` 形式的跨站去重键，用来避免 FFZY/BFZY 重复推送同一集。

## JustWatch 追踪

GitHub Actions 默认启用 JustWatch。首次启用时，脚本会把当前 JustWatch 页面内容写入基线状态，但不会把这些历史条目一次性推送出去；后续页面出现新上架条目才会推送。

可以在仓库 Settings -> Secrets and variables -> Actions -> Variables 中调整：

- `JUSTWATCH_ENABLED`：是否启用，默认 `true`
- `JUSTWATCH_PROVIDERS`：要额外追踪的平台，多个值用逗号或换行分隔，格式为 `COUNTRY:provider_code`；默认留空
- `JUSTWATCH_URLS`：要追踪的 JustWatch 最新页，多个 URL 用逗号或换行分隔；默认 `https://www.justwatch.com/tw/%E6%9C%80%E6%96%B0`
- `JUSTWATCH_BOOTSTRAP_ONLY`：首次启用是否只建立基线，默认 `true`
- `JUSTWATCH_ITEMS_PER_PROVIDER`：每个平台每次最多取多少个 JustWatch 条目，默认 `10`
- `JUSTWATCH_MAX_ITEMS`：每次最多处理多少个 JustWatch 条目，默认 `100`
- `JUSTWATCH_MAX_AGE_DAYS`：只处理最近多少天的 JustWatch 上架条目，默认 `14`；设为 `0` 表示不过滤日期

## FFZY 追踪

GitHub Actions 默认启用 FFZY。首次启用时，脚本会把当前 FFZY 最新条目写入基线状态，但不会把这些历史条目一次性推送出去；后续同一资源出现新的 `vod_time` 才会推送。

默认备用域名：

- `ffzy1.tv`
- `ffzy2.tv`
- `ffzy3.tv`
- `ffzy4.tv`
- `ffzy5.tv`

默认只追踪这些分类：

- 连续剧：`13` 国产剧、`14` 香港剧、`15` 韩国剧、`16` 欧美剧
- 动漫片：`29` 国产动漫、`30` 日韩动漫、`31` 欧美动漫

可以在仓库 Settings -> Secrets and variables -> Actions -> Variables 中调整：

- `FFZY_ENABLED`：是否启用，默认 `true`
- `FFZY_DOMAINS`：备用域名列表，多个值用逗号或换行分隔，默认 `ffzy1.tv,ffzy2.tv,ffzy3.tv,ffzy4.tv,ffzy5.tv`
- `FFZY_BOOTSTRAP_ONLY`：首次启用是否只建立基线，默认 `true`
- `FFZY_TYPE_IDS`：要追踪的分类 ID，多个值用逗号或换行分隔，默认 `13,14,15,16,29,30,31`
- `FFZY_ITEMS_PER_TYPE`：每个分类每次最多处理多少条，默认 `20`
- `FFZY_PAGES_PER_TYPE`：每个分类每次最多请求多少页，默认 `1`
- `FFZY_MAX_ITEMS`：每次最多处理多少个 FFZY 条目，默认 `200`
- `FFZY_MAX_AGE_HOURS`：只处理最近多少小时的 FFZY 更新，默认 `168`；设为 `0` 表示不过滤时间
- `FFZY_REQUEST_TIMEOUT`：单个 FFZY 域名请求超时时间，默认 `8` 秒

## BFZY 追踪

GitHub Actions 默认启用 BFZY。首次启用时，脚本会把当前 BFZY 最新条目写入基线状态，但不会把这些历史条目一次性推送出去；后续出现新更新才会推送。

默认备用域名：

- `bfzyapi.com`
- `bfzy.tv`

`bfzy.tv` 是站点地址；默认会优先使用公开采集接口域名 `bfzyapi.com`。

默认只追踪这些分类：

- 连续剧：`31` 国产剧、`33` 香港剧、`34` 韩国剧、`32` 欧美剧
- 动漫片：`40` 国产动漫、`41` 日韩动漫、`42` 欧美动漫

可以在仓库 Settings -> Secrets and variables -> Actions -> Variables 中调整：

- `BFZY_ENABLED`：是否启用，默认 `true`
- `BFZY_DOMAINS`：备用域名列表，多个值用逗号或换行分隔，默认 `bfzyapi.com,bfzy.tv`
- `BFZY_BOOTSTRAP_ONLY`：首次启用是否只建立基线，默认 `true`
- `BFZY_TYPE_IDS`：要追踪的分类 ID，多个值用逗号或换行分隔，默认 `31,33,34,32,40,41,42`
- `BFZY_ITEMS_PER_TYPE`：每个分类每次最多处理多少条，默认 `20`
- `BFZY_PAGES_PER_TYPE`：每个分类每次最多请求多少页，默认 `1`
- `BFZY_MAX_ITEMS`：每次最多处理多少个 BFZY 条目，默认 `200`
- `BFZY_MAX_AGE_HOURS`：只处理最近多少小时的 BFZY 更新，默认 `168`；设为 `0` 表示不过滤时间
- `BFZY_REQUEST_TIMEOUT`：单个 BFZY 域名请求超时时间，默认 `8` 秒

## 资源站去重

FFZY 和 BFZY 会同时保存站点原始键和跨站等价键。比如 BFZY 先更新 `明天也要上班` 第 6 集并推送成功后，状态文件会写入类似 `resource:明天也要上班:ep6` 的记录；FFZY 后面再更新同一集时会被跳过。
