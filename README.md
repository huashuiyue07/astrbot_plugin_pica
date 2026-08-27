# Pica-Comics — AstrBot 哔咔漫画插件

搜索、查看、下载哔咔漫画（picacomic）本子，支持排行榜、收藏、每日签到。
API 签名机制参照旧版 [zhenxun_plugin_pica](https://github.com/CCYellowStar2/zhenxun_plugin_pica)
逆向实现并修复（旧项目硬编码 token 过期、aiohttp 版本过老导致大部分请求失败），
已实测全部接口可用。

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
| modify_md5 | 下载时修改图片 MD5 防平台风控（默认开） |
| pack_format | 章节下载打包格式：`images`=直接发前 10 张图 / `zip`=压缩包(可加密) / `pdf`=PDF / `long_img`=纵向长图(过长自动分段打包) / `none`=仅本地保存不发送（默认 `zip`） |
| pack_password | 打包密码，为 ZIP/PDF 加密，留空不加密（默认空） |
| cache_clean_interval_hours | 缓存自动清理间隔(小时)，0 关闭（默认 12） |
| cache_max_age_days | 超过 N 天的缓存自动删除，0 关闭（默认 7） |
| cache_max_size_mb | 缓存超上限自动删最旧，0 关闭（默认 2048） |
| admin_only / admin_ids | 仅管理员模式（可选） |

### 缓存清理

- **自动清理**：按 `cache_clean_interval_hours` 定时检查，删除超过 `cache_max_age_days`
  天的缓存；总大小超过 `cache_max_size_mb` 时按最旧优先删除（下载缓存 + 打包产物）
- **手动清理**：`/picaclean` 清空全部；`/picaclean <天数>` 只清理 N 天前的缓存

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
| `/picarank [H24\|D7\|D30]` | 排行榜（日/周/月） |
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
    ├── downloader.py    # 图片下载（并发、MD5 修改）
    ├── packer.py        # 打包器（ZIP/PDF/长图，可选加密）
    ├── formatter.py     # 消息格式化
    └── constants.py     # 签名密钥、分区、排序等常量
```

## 说明

- 所有接口（含图片下载）都需要登录 token，插件首次使用自动登录并缓存 token（7 天有效）。
- 图片必须本地下载后经 AstrBot 发送（pica 图片 URL 需要 authorization 请求头，无法直接外链）。
- Web UI 修改账号密码后免重启即时生效。
