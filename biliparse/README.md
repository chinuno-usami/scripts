# bili-dynamic

抓取 B 站「关注动态」信息流（`x/polymer/web-dynamic/v1/feed/all`），把每条动态解析成统一结构后以 **JSON 数组** 输出到 stdout，方便接 RSS / 推送 / 归档。

含两个脚本：

| 文件 | 作用 |
|---|---|
| `bili.rb` | 拉取并解析动态，输出 JSON |
| `slim-cookie.sh` | 精简 `cookie.txt`，只留登录真正需要的字段 |

## 环境要求

- Ruby ≥ 2.6（macOS 自带的 `/usr/bin/ruby` 2.6 可用；`bili.rb` 已避开 2.7 才有的 API）
- 无第三方 gem，只用标准库 `net/http` / `json`
- `slim-cookie.sh` 是 POSIX sh，macOS / Linux 通用

## 快速开始

```bash
# 1) 准备 cookie（见下节），写入同目录的 cookie.txt
# 2) 精简掉无用字段
./slim-cookie.sh

# 3) 抓取
./bili.rb
```

输出示例（截断）：

```json
[
  {
    "link": "https://t.bilibili.com/1261622604304744450",
    "author_name": "谜之声",
      "timestamp": "Wed, 21 Oct 2026 11:40:03 GMT",
    "title": "【谜之声】居然是认识的开发者！当年的《乌鸦旅馆》还挺喜欢的…",
    "desc": "<br><img src=\"https://i0.hdslb.com/…\" referrerpolicy=\"no-referrer\"><br>标题：…<br>链接：<a href=\"…\">…</a>"
  }
]
```

- `link` / `title` / `desc` 可直接喂给 RSS 的 `description`（`desc` 是 HTML 片段）
- `timestamp` 为 RFC 1123 / RFC 2822 格式（GMT），对应 `module_author.pub_ts`
- `DYNAMIC_TYPE_LIVE_RCMD`（直播推荐位）会被过滤掉

## cookie.txt 怎么来（最容易踩的坑）

**不能**用控制台 `document.cookie` —— B 站的 `SESSDATA` 带 `HttpOnly` 标志，JS 永远读不到它。只贴 `document.cookie` 的结果，请求会一直返回：

```json
{"code":-101,"message":"账号未登录","ttl":1}
```

正确姿势（任选其一，登录状态下操作页面）：

1. **DevTools → Application（应用）→ Storage → Cookies → `https://www.bilibili.com`**，抄 `SESSDATA` 的值。
2. **DevTools → Network → 任一 `api.bilibili.com` 请求 → Request Headers → `Cookie:`**，整行复制（推荐，一次拿全）。
3. 右键该请求 → **Copy as cURL**，从 `-H 'cookie: …'` 里取整串。

`bili.rb` 对粘贴格式很宽容：单行、多行、`Copy as cURL` 带行尾 `\` 续行、带 BOM、字段前有空格、值被引号包住、结尾多打分号，都能正确解析。

## cookie 字段取舍

实测（同一份 cookie 反复验证）：**只带 `SESSDATA` 一项即可正常返回 `code=0`**；只带 `buvid3`+`buvid4` 或只带 `bili_jct`+`DedeUserID` 仍然是 `-101`。

| 字段 | 必要性 | 说明 |
|---|---|---|
| `SESSDATA` | **必须** | 唯一登录凭证，有效期可长达数月 |
| `buvid3` / `buvid4` | 建议保留 | 设备指纹；删掉也能跑，但风控抽查时可能需要（报 `-352`） |
| `bili_jct` | 只读不需要 | CSRF token，将来做点赞/取关等写操作（POST 带 `csrf=`）才必须 |
| `bili_ticket` / `bili_ticket_expires` | 只读不需要 | 短效风控票据，会过期 |
| `b_nut` `_uuid` `user_site` `sid` `default_sites` `CURRENT_FNVAL` `home_feed_column` `browser_resolution` `bp_t_offset_<uid>` `hit-dyn-v2` `bmg_af_*` `b_lsid` `DedeUserID` `DedeUserID__ckMd5` `buvid_fp` | **可直接删** | 纯前端 UI 偏好 / 埋点，与登录态无关 |

精简后 `cookie.txt` 只有 400 字节左右，泄露面也小得多。

## slim-cookie.sh

```bash
./slim-cookie.sh                 # 就地精简 ./cookie.txt，自动备份 cookie.txt.bak，权限置 600
./slim-cookie.sh -n              # dry-run，只打印结果
./slim-cookie.sh -K              # 不留 .bak
./slim-cookie.sh -k "SESSDATA buvid3 buvid4 bili_jct"   # 自定义保留字段
./slim-cookie.sh /path/to/cookie.txt
```

安全性设计：

- 结果里没有 `SESSDATA` → **拒绝写入** 并 `exit 1`，原文件不动
- 空文件直接报错
- 原子写入：临时文件 → `chmod 600` → `mv`
- 幂等：已经精简过会提示「无需修改」
- 若 `cookie.txt` 没被 git 忽略会给出警告

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 成功，stdout 是 JSON 数组 |
| `1` | 失败（缺 cookie 文件 / 缺 SESSDATA / API 报错）。**失败时仍会向 stdout 打印 `[]`**，避免下游解析崩溃 |

## 支持解析的动态类型

- `MAJOR_TYPE_ARCHIVE`（投稿视频）、`MAJOR_TYPE_DRAW`（图文）、`MAJOR_TYPE_OPUS`（新版图文/专栏，`features=itemOpusStyle` 格式）、`MAJOR_TYPE_ARTICLE`（旧专栏）、`MAJOR_TYPE_MUSIC`
- `additional`：`ADDITIONAL_TYPE_UGC`、`ADDITIONAL_TYPE_GOODS`（商品）
- `DYNAMIC_TYPE_FORWARD`（转发，递归解析原动态）

其他类型不会崩，只会在 `desc` 里留下 `未处理类型：XXX`，方便按需补充。

## 常见错误码

| code | 原因 | 处理 |
|---|---|---|
| `-101` | 未登录 / `SESSDATA` 缺失或过期 | 按上文重新取 cookie |
| `-352` | 风控校验失败 | 补回 `buvid3`/`buvid4`，或带更新鲜的 `bili_ticket`；也可换 IP/降频 |
| `-412` | 请求被拦截 | 检查 `User-Agent` / `Referer`，降低请求频率 |
| 非 JSON 响应 | 被网关/CDN 挡了 | 脚本会打印 HTTP 状态码和前 200 字节便于排查 |

## 已知限制

- 单页 20 条，`page=1` 固定写在 URL 里；要翻页需自己改 `page` 参数并处理 `data.offset`
- `timezone_offset=-480` 固定为东八区
- 不做重试与限速，别高频调用

## 安全提醒

`cookie.txt` 里的 `SESSDATA` 等同于账号密码，`cookie.txt` / `cookie.txt.bak` 已在 `.gitignore` 中忽略。**如果不慎提交过，立刻到 B 站「退出所有设备」使旧 SESSDATA 失效，再重新取一份。**

## 目录结构

```
bili.rb              # 抓取 + 解析
slim-cookie.sh       # 精简 cookie
cookie.txt           # 你的凭证（被 git 忽略）
cookie.txt.bak       # 精简前的备份（被 git 忽略）
cookie.example.txt   # 脱敏示例
log.txt              # 历史运行输出（被 git 忽略）
.gitignore
README.md
```
