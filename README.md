# Pica-Comics — AstrBot 哔咔漫画插件

搜索、查看、下载哔咔漫画（picacomic）本子，支持排行榜、收藏、每日签到。

## 参考项目

本项目 API 签名机制参照 [zhenxun_plugin_pica](https://github.com/CCYellowStar2/zhenxun_plugin_pica)
（原作者：CCYellowStar2，[真寻 bot](https://github.com/HibiKier/zhenxun_bot) 的哔咔插件）逆向实现。

原项目存在以下问题，本插件已重写修复：
- 硬编码 token 过期后无法使用（原项目大部分请求失败）
- aiohttp 3.8.4 版本过老，与新版 API 不兼容
- 签名 path 带前导斜杠时 pica 返回假的 `{"code":200}` 空响应（本插件已正确处理）
- 中文参数未 URL 编码导致分区浏览返回空（本插件已修复）
- 无登录态管理（token 过期不会自动重登）

打包功能参考 [astrbot_plugin_jm_cosmos](https://github.com/Soulter/astrbot_plugin_jm_cosmos) 的打包器实现。

## 安装

```bash
cp -r astrbot_plugin_pica ~/astrbot/data/plugins/
docker restart astrbot
```

然后在 AstrBot Web UI（http://localhost:6185）插件管理页为插件填写配置。

## 配置项

| 配置 | 说明 |
|---|---|
| pica_account | 哔咔账号（邮箱） |
| pica_password | 哔咔密码 |
| use_proxy / proxy_url | 是否走代理（默认关闭，国内可直连） |
| max_retry / timeout | 请求重试次数 / 超时秒数 |
| page_size | 列表每页显示数量（默认 10） |
| max_concurrent | 章节图片下载并发数（默认 5） |
| send_cover | 详情是否附带封面图（默认开） |
| rank_cover_pdf | `/picarank` 是否额外附带排行榜封面 PDF（默认关，见下） |
| rank_new_highlight | 排行榜「新上榜」提示（默认开，见下） |
| rank_cron_h24 | 日榜定时推送时间，5 字段 cron（默认 `0 9 * * *`，见下） |
| rank_cron_d7 | 周榜定时推送时间（默认 `0 9 * * 1`，每周一 09:00） |
| rank_cron_d30 | 月榜定时推送时间（默认 `0 9 1 * *`，每月 1 日 09:00） |
| modify_md5 | 下载时修改图片 MD5 防平台风控（默认开） |
| pack_format | 章节下载打包格式：`images`=直接发前 10 张图 / `zip`=压缩包(可加密) / `pdf`=PDF / `long_img`=纵向长图(过长自动分段打包) / `none`=仅本地保存不发送（默认 `zip`） |
| pack_password | 打包密码，为 ZIP/PDF 加密，留空不加密（默认空） |
| send_batch_mb | 整本下载单文件大小上限(MB)，超过后按章节分批打包发送（默认 500，0=不分批） |
| send_api_timeout | 大文件上传时 OneBot API 调用超时下限(秒)，默认 900；框架硬编码 180s，大文件上传常超时导致「假失败」（0=不改动） |
| progress_step_pct | 整本下载进度提示间隔(百分比)，默认 10（每 10% 提示一次） |
| cache_clean_interval_hours | 缓存自动清理间隔(小时)，0 关闭（默认 12） |
| cache_max_age_days | 超过 N 天的缓存自动删除，0 关闭（默认 7） |
| cache_max_size_mb | 缓存超上限自动删最旧，0 关闭（默认 2048） |
| admin_only / admin_ids | 仅管理员模式（可选） |

### 缓存清理

- **自动清理**：按 `cache_clean_interval_hours` 定时检查，删除超过 `cache_max_age_days`
  天的缓存；总大小超过 `cache_max_size_mb` 时按最旧优先删除（下载缓存 + 打包产物）
- **手动清理**：`/picaclean` 清空全部；`/picaclean <天数>` 只清理 N 天前的缓存

### 排行榜封面 PDF（可选，默认关闭）

开启 `rank_cover_pdf` 后，`/picarank` 会在排行榜文本之外，额外下载榜单上**全部**
漫画（数量等于 `page_size`）的封面，按榜单顺序打包成一个 PDF 一并发送：

- 默认关闭：不开启时行为与旧版完全一致，只发送排行榜文本
- 页序与榜单一致：第 1 名在第 1 页
- 单张封面下载失败只跳过该张，不影响其余封面和排行榜文本
- 需要 `pymupdf`（`requirements.txt` 已包含），缺失时会提示「封面 PDF 生成失败」
- 封面 PDF **不加密**（与 `pack_password` 无关），仅作榜单预览
- 该 PDF 属上传类消息：上传超时不会重发（沿用 v1.4.1 的防重复发送语义）
- 封面下载与打包全程尽力而为，任何意外只提示用户，不会让排行榜命令失败
- 封面较多 / 网络较慢时命令耗时会变长，过程中会提示「正在下载…」
- 产物保存在 `packs/`（`排行榜封面_<H24|D7|D30>.pdf`），同名会覆盖

### 排行榜「新上榜」提示（默认开启）

每次发送排行榜后，会把该榜单的快照保存到
`data/plugin_data/astrbot_plugin_pica/rank_history/{H24,D7,D30}.json`
（持久化目录，**不**受 `/picaclean` 影响）；下次发送同一榜单时自动对比，
额外发一条「🆕 新上榜」消息，标出名次与标题：

- 快照按榜单类型独立（日榜/周榜/月榜互不影响）
- 只有 `page_size` 以内（用户看得见）的名次才会被高亮，
  但**全量**榜单都会进快照 —— 这样调整 `page_size` 不会误报
- 榜单接口返回空结果时不更新快照，避免把整榜误判成「新上榜」
- 配置项 `rank_new_highlight=false` 可关闭提示（快照仍会保存，
  手动 `/picarank` 与定时推送共用同一份快照，不会互相重复报）
- 首次使用没有对比基准，因此第一次不会出现「新上榜」消息

### 排行榜定时推送（可选，默认 09:00 推送）

- `/picasub [H24|D7|D30]` 为当前群订阅排行榜推送（缺省日榜）；
  参数非法（如 `/picasub H2`）会直接返回用法提示，**不会**静默订阅日榜
- `/picasub list` 查看本群订阅；`/picasub off` 取消本群全部订阅
- 推送时间用 5 字段 cron（分 时 日 月 星期）配置：
  - `rank_cron_h24` 默认 `0 9 * * *`（每天 09:00）
  - `rank_cron_d7` 默认 `0 9 * * 1`（每周一 09:00，0 和 7 都表示周日）
  - `rank_cron_d30` 默认 `0 9 1 * *`（每月 1 日 09:00）
  - 支持 `*`、`a`、`a-b`、`a,b,c`、`*/n`、`a-b/n`、`a/n`（`a/n` 语义同 Vixie cron：
    从 `a` 按步进走到字段上限，例如分钟字段 `5/10` = 5,15,…,55）；
    日与星期同时限定时需同时满足；**留空表示该榜单不推送**
- 每 30 秒检查一次，同一分钟只推送一次；只推给订阅了该榜单类型的会话
- 榜单接口返回空结果（限流 / 风控 / 抖动）时**跳过本次推送**并记日志，
  不会向订阅群发「排行榜为空」；手动 `/picarank` 仍会提示
- 推送内容与 `/picarank` 一致：榜单文本 + 新上榜高亮（+ 封面 PDF，若开启）；
  封面 PDF 每轮推送**只构建一次**，多个群复用同一份文件
- 推送账号：优先用订阅者（执行 `/picasub` 的人）自己绑定的哔咔账号，
  其次回落到配置的默认账号；两者都不可用时本次跳过并记日志
- 订阅关系保存在 `data/plugin_data/astrbot_plugin_pica/rank_subscriptions.json`
- **长文本折叠**：主动推送不经过 AstrBot 的响应管线，所以插件自行复刻了管线的
  这层处理 —— 纯文本超过该会话的 `platform_settings.forward_threshold`
  （框架默认 1500 字）时，会折叠成 QQ 的「合并转发」再发；阈值取会话自己的
  平台配置，短文本行为完全不变，取不到机器人账号时会退化成普通文本并记 warning
- **调试**：`/picadebug [H24|D7|D30|all]` 可无视 `rank_cron_*` 的时间设置，
  立即触发一次订阅推送（不带参数=全部榜单）。该命令**只允许 `admin_ids`
  中的用户**使用（未配置 `admin_ids` 时拒绝，避免群成员触发全量广播）；
  它会写入榜单快照，因此随后的定时推送不会重复提醒新上榜

## 命令

| 命令 | 说明 |
|---|---|
| `/pica` / `/picahelp` | 帮助 |
| `/picalogin <邮箱> <密码>` | 绑定当前 QQ 自己的哔咔账号（隔离） |
| `/picalogout` | 解绑当前 QQ 的账号 |
| `/picastatus` | 查看当前 QQ 账号绑定状态 |
| `/picasearch <关键词> [页码]` | 搜索本子 |
| `/picainfo <ID>` | 本子详情（带封面） |
| `/picaeps <ID>` | 章节列表 |
| `/picadl <ID>` | 整本下载（后台任务，完成后自动通知） |
| `/picadl <ID> <章节号>` | 下载单章节（按打包格式发送） |
| `/picarank [H24\|D7\|D30]` | 排行榜（日/周/月），附带新上榜提示与可选封面 PDF |
| `/picasub [H24\|D7\|D30\|list\|off]` | 订阅 / 查看 / 取消本群排行榜定时推送 |
| `/picadebug [H24\|D7\|D30\|all]` | 调试：无视时间立即触发订阅推送（仅 `admin_ids` 可用） |
| `/picacomics <分区名> [页码]` | 按分区浏览 |
| `/picacat` | 全部分区列表 |
| `/picafav <ID>` | 收藏 / 取消收藏 |
| `/picamyfav [页码]` | 我的收藏 |
| `/picapunch` | 每日签到领币 |
| `/picaclean` | 清理图片缓存 |

## 账号隔离

- 每个 QQ 用户可发送 `/picalogin <邮箱> <密码>` 绑定**自己的**哔咔账号，
  token 按 QQ 号独立存储（`data/tokens/{qq号}.json`），互不共用。
- 配置项 `allow_default_account`：
  - `true`（默认）：未绑定用户回退使用配置的默认账号
  - `false`：未绑定用户被拒绝并提示先绑定，**避免群成员共用你的个人账号**（推荐）

## 安全提示

- `/picalogin <邮箱> <密码>` 的账号密码会明文出现在聊天消息里（群内全员可见），
  请**私聊机器人**使用绑定，群内尽量用 `/picalogin` 不带参数走默认账号兜底。
- 绑定的账号密码与 token 以 JSON 明文保存在本机 AstrBot 数据目录
  （`data/tokens/{qq号}.json`），请勿将 AstrBot 主机开放给不可信的人。
- 若 bot 部署在公共/半开放环境，建议开启 `admin_only` 并配置 `admin_ids`，
  避免任何群成员直接操作你的默认账号。

## 结构

```
astrbot_plugin_pica/
├── main.py              # 插件入口，命令注册
├── metadata.yaml        # 插件元信息
├── _conf_schema.json    # 配置 Schema
├── requirements.txt
└── core/
    ├── client.py        # Pica API 封装（HMAC-SHA256 签名、登录、搜索、下载）
    ├── auth.py          # token 管理（持久化、JWT 过期自动重登）
    ├── downloader.py    # 图片下载（并发、MD5 修改、批量封面）
    ├── packer.py        # 打包器（ZIP/PDF/长图，可选加密）
    ├── formatter.py     # 消息格式化
    ├── rank_history.py  # 排行榜快照与「新上榜」对比
    ├── subscriptions.py # 排行榜定时推送订阅（按会话）
    ├── cron.py          # cron 表达式匹配（定时推送用）
    └── constants.py     # 签名密钥、分区、排序等常量
```

## 说明

- 所有接口（含图片下载）都需要登录 token，插件首次使用自动登录并缓存 token（7 天有效）。
- 图片必须本地下载后经 AstrBot 发送（pica 图片 URL 需要 authorization 请求头，无法直接外链）。
- Web UI 修改账号密码后免重启即时生效。
