# 手工测试用例（v1.4.2 / v1.4.3 新增功能）

本文件覆盖 `new-feature` 分支相对上游 `main` 的全部新增能力，供发起 PR 前手工验收。
自动化测试（`pytest`，208 例）已覆盖逻辑分支，但下面这些**只有真实 QQ 环境才能验证**：
消息的真实送达形态、封面/PDF 渲染效果、cron 实际触发时刻、跨进程持久化与重启行为。

- 目标版本：**v1.4.3**（含 v1.4.2 的排行榜封面 PDF）
- 实测环境：AstrBot + `aiocqhttp`(OneBot v11) + **LLBot**；订阅/推送等与协议端无关的能力在其他平台同样生效
- 建议：用一个**测试群** + 一个**管理员 QQ**，避免打扰真实群；每次改配置后在 Web UI 里保存并**重载插件**

## 0. 环境准备

| 项 | 说明 |
|---|---|
| 插件数据目录 | `data/plugin_data/astrbot_plugin_pica/`（下文记作 `<DATA>`） |
| 关键日志 | `Prepare to send`、`排行榜定时推送完成`、`合并转发发送失败`、`rank_cron_* 不是合法的 cron 表达式`、`获取机器人账号信息失败` |
| 重置状态 | 删除 `<DATA>/rank_history/`（榜单快照）、`<DATA>/rank_subscriptions.json`（订阅关系）、`<DATA>/packs/`（打包产物） |
| 需要账号 | 至少一个可用的哔咔账号（`pica_account`/`pica_password`），或先 `/picalogin` 绑定 |

**配置基线（默认值）**——除非用例另有说明，保持默认：

| 配置 | 默认 | 本文件用到的取值 |
|---|---|---|
| `page_size` | 10 | 2 / 10 / 30 |
| `rank_push_size` | 0（跟随 `page_size`） | 不改 / 5 |
| `rank_cover_pdf` | false | true |
| `rank_new_highlight` | true | false |
| `rank_cron_h24` | `0 9 * * *` | `*/2 * * * *`（2 分钟一次，便于验证） |
| `rank_cron_d7` | `0 9 * * 1` | `""`（留空=不推送） |
| `rank_cron_d30` | `0 9 1 * *` | 不改 |
| `admin_ids` | 空 | 你的 QQ 号 |
| AstrBot 平台设置 `forward_threshold` | 1500 | 100（便于触发折叠） |

---

## A. 排行榜封面 PDF（v1.4.2，配置项 `rank_cover_pdf`）

| 编号 | 用例 | 前置 | 步骤 | 预期 |
|---|---|---|---|---|
| A1 | 默认关闭不产生 PDF | `rank_cover_pdf=false`，清空 `<DATA>/packs/` | 发 `/picarank` | 只收到排行榜文本；`<DATA>/packs/` 无新增文件 |
| A2 | 开启后发送封面 PDF | `rank_cover_pdf=true` | 发 `/picarank` | 先收到 `🖼️ 正在下载排行榜 N 张封面并生成 PDF...`，随后收到 `排行榜封面_H24.pdf` |
| A3 | 页序与榜单一致 | 同 A2 | 打开 PDF，与榜单文本逐条对照 | 第 1 名是 PDF 第 1 页，顺序完全一致，共 `page_size` 页 |
| A4 | 三种榜单各自命名 | 同 A2 | 依次发 `/picarank H24`、`D7`、`D30` | 生成 `排行榜封面_H24.pdf`、`_D7.pdf`、`_D30.pdf` 三个文件 |
| A5 | 重复执行不堆积 | 同 A2 | 连续发两次 `/picarank` | `packs/` 里同名 PDF 被覆盖，不出现第二份；内容为最新榜单 |
| A6 | PDF 不加密 | 配置了 `pack_password` 且 `rank_cover_pdf=true` | 发 `/picarank`，打开 PDF | PDF 可直接打开、无密码提示（封面 PDF 与 `pack_password` 无关） |
| A7 | 上传达大文件时的发送语义 | 同 A2 | 观察日志 | PDF 属上传类消息，日志中不应出现同一份 PDF 的重复发送 |

---

## B. 排行榜「新上榜」提示（配置项 `rank_new_highlight`）

> 构造「新上榜」的技巧：先发一次建立快照，然后**把 `page_size` 调大**（例如 2 → 10）再发一次，
> 新进入可见范围的条目就会被判为新上榜；或等待真实榜单变化（时长不可控）。

| 编号 | 用例 | 前置 | 步骤 | 预期 |
|---|---|---|---|---|
| B1 | 首次不提示并写入快照 | 删除 `<DATA>/rank_history/` | 发 `/picarank` | 只有排行榜文本，**没有**「新上榜」消息；生成 `<DATA>/rank_history/H24.json` |
| B2 | 快照内容正确 | 接 B1 | 查看 `H24.json` | 含 `tt`、`updated_at`、`items[]`（每项有 `_id`/`title`/`rank`），顺序即榜单顺序 |
| B3 | 新上榜高亮 | `page_size=2` 发一次 → 改成 `page_size=10` 再发 | 发 `/picarank` | 额外收到一条 `🆕 新上榜 · 日榜(24H)`，列出新条目（名次/标题/ID/❤️），且排在榜单文本之后 |
| B4 | 榜单未变化不提示 | 接 B1，不改任何东西 | 再发一次 `/picarank` | 没有「新上榜」消息 |
| B5 | 按榜单类型独立 | 接 B1 | 发 `/picarank D7` | 周榜没有历史 → 不提示；生成 `D7.json`（与 `H24.json` 互不影响） |
| B6 | 关闭开关仍保存快照 | `rank_new_highlight=false`，删除 `rank_history/` | 按 B3 的方式制造新条目并发榜 | **没有**「新上榜」消息，但 `H24.json` 已更新为本次榜单 |
| B7 | 快照持久化 | 接 B1 | 重启 AstrBot / 重载插件后再发 `/picarank` | 仍能对比上一次快照（不会退化成「首次」） |
| B8 | 快照损坏不影响出榜 | 手工把 `H24.json` 内容改成 `{坏` | 发 `/picarank` | 正常返回排行榜，不提示新上榜；日志有读取失败的 warning；文件被本次榜单覆盖 |
| B9 | 空榜不污染快照 | 接 B1 | 断网/改坏账号使榜单返回空后再发榜（或直接观察日志） | 空结果不覆盖快照；恢复后再发时不会把整榜误报成新上榜 |
| B10 | 高亮只在可见范围内 | `page_size=2` | 制造一个只出现在第 3 名之后的新条目（把 `page_size` 临时调小再调大） | 用户可见列表里没有该条目时不提示，但快照已包含它（避免它升上来又被算作新） |

---

## C. `/picasub` 订阅命令

| 编号 | 用例 | 前置 | 步骤 | 预期 |
|---|---|---|---|---|
| C1 | 默认订阅日榜 | 删除 `rank_subscriptions.json` | 群内发 `/picasub` | 回复「已为本群订阅『日榜(24H)』排行榜」+ `⏰ 定时推送: 0 9 * * *` + 订阅状态列表；文件里出现该群 |
| C2 | 指定榜单类型 | 接 C1 | 发 `/picasub D7`、`/picasub d30`（小写） | 分别成功订阅周榜/月榜；状态列表里三项标记正确 |
| C3 | 重复订阅幂等 | 接 C1 | 再发一次 `/picasub H24` | 回复「本群早已订阅『日榜(24H)』」，不产生重复项 |
| C4 | 查看订阅 | 接 C2 | 发 `/picasub list` | 列出三项，已订阅的显示 ✅ 与其 cron，未订阅的显示 ➖ |
| C5 | 取消订阅 | 接 C2 | 发 `/picasub off` | 回复「已取消本群的排行榜订阅：日榜(24H)、周榜(7D)…」；再发 `/picasub off` 回复「本群当前没有排行榜订阅」 |
| C6 | 未配置刷新时间时告警 | `rank_cron_h24=""` | 发 `/picasub H24` | 回复带 `⚠️ 未配置刷新时间（rank_cron_h24），订阅后不会推送` |
| C7 | cron 非法时告警 | `rank_cron_h24="每天九点"` | 发 `/picasub H24` | 回复带 `⚠️ 刷新时间配置有误`，不崩溃；日志出现一次非法表达式告警 |
| C8 | 参数非法只报用法 | 接 C1 | 发 `/picasub 不存在的榜单`、`/picasub H2` | 回复用法提示；`rank_subscriptions.json` 里**没有**新增订阅（不再静默订阅日榜） |
| C9 | 会话隔离 | 两个测试群 | 群 1 发 `/picasub H24`，群 2 发 `/picasub D7`，各自 `/picasub list` | 两群互不影响；`rank_subscriptions.json` 里两条独立记录 |
| C10 | 仅管理员模式生效 | `admin_only=true`、`admin_ids` 不含当前用户 | 发 `/picasub` | 回复「你没有权限使用此插件」，且不写入订阅 |
| C11 | 订阅持久化 | 接 C1 | 重启 AstrBot 后 `/picasub list` | 订阅关系仍在 |

---

## D. cron 定时推送

> 为了控制等待时间，把 `rank_cron_h24` 临时设为 `*/2 * * * *`（每 2 分钟命中一次）。

| 编号 | 用例 | 前置 | 步骤 | 预期 |
|---|---|---|---|---|
| D1 | 到点推送 | 有 H24 订阅；`rank_cron_h24="*/2 * * * *"` | 等待 ≤2 分钟 | 群里收到排行榜推送（榜单文本 + 必要时的新上榜）；日志出现 `排行榜定时推送完成: H24 → N 个会话` |
| D2 | 未到点不推送 | 同上，但把 cron 改成 `0 9 * * *`（或改成明天才命中的时刻） | 等待 ≥3 分钟 | 无推送；日志无「定时推送完成」 |
| D3 | 同一分钟只触发一次 | 同 D1，注意观察命中那一分钟 | 持续观察 60 秒 | 该分钟内只推送一次（不会因为 30 秒轮询而重复） |
| D4 | 只推给对应订阅者 | 群 1 订 H24、群 2 订 D7；`rank_cron_h24="*/2 * * * *"`、`rank_cron_d7=""` | 等到 H24 命中 | 只有群 1 收到 H24 榜单；群 2 无消息 |
| D5 | 留空即不推送 | `rank_cron_d30=""` 且群 1 订阅了 D30 | 等待 | D30 永不推送 |
| D6 | 非法 cron 不刷屏 | `rank_cron_h24="坏表达式"` | 等待 3 分钟，观察日志 | 不推送；该表达式只告警一次，不会每 30 秒刷一条 |
| D7 | 推送账号优先级（订阅者） | 订阅者已 `/picalogin` 绑定自己的账号；`allow_default_account=false` | 等到命中 | 推送成功（用订阅者自己的账号取榜单），日志无「缺少可用账号」 |
| D8 | 推送账号优先级（默认账号） | 订阅者未绑定；`pica_account`/`pica_password` 已配置 | 等到命中 | 推送成功（回落到配置默认账号） |
| D9 | 无可用账号时跳过 | 订阅者未绑定，且**没有**默认账号 | 等到命中 | 不崩溃；日志出现「排行榜定时推送缺少可用账号，本次跳过」；其他榜单/其他群不受影响 |
| D10 | 与手动命令共享快照 | 先手动 `/picarank` 一次 | 等到 D1 命中 | 定时推送**不会**重复报同一批「新上榜」（两次共享同一份快照） |
| D11 | 推送内容含封面 PDF | `rank_cover_pdf=true` + H24 订阅 | 等到命中 | 推送里包含榜单文本与 `排行榜封面_H24.pdf` |
| D12 | 长榜单+封面 PDF 组合 | `page_size=30` + `rank_cover_pdf=true` | 等到命中 | 榜单文本形态见 F 组；PDF 页数为 `rank_push_size`（未配置时等于 `page_size`） |

> 边界路径（空榜单跳过、部分会话失败只计成功数、`/picasub` 非法参数不写订阅、
> cron 的 `a/n` 步进语义）由自动化测试覆盖：`tests/test_rank_subscribe.py`、
> `tests/test_rank_debug.py`、`tests/test_cron.py`。

---

## E. `/picadebug` 调试命令

| 编号 | 用例 | 前置 | 步骤 | 预期 |
|---|---|---|---|---|
| E1 | 未配置 admin_ids 时拒绝 | `admin_ids=""` | 发 `/picadebug` | 回复提示需要先配置 `admin_ids`；不产生任何推送 |
| E2 | 非管理员拒绝 | `admin_ids` 里只有别人的 QQ | 用未授权 QQ 发 `/picadebug` | 回复「你没有权限使用调试命令」；无推送 |
| E3 | 管理员可用 | `admin_ids` 含自己 | 发 `/picadebug` | 先回复 `🔧 调试触发订阅推送（无视时间）: 日榜(24H)、周榜(7D)、月榜(30D)`，随后回复触发结果明细 |
| E4 | 无视时间 | `rank_cron_h24="0 9 * * *"`（当前不是 9:00） | 发 `/picadebug` | 立即推送成功（证明不依赖 cron 命中） |
| E5 | 指定单一榜单 | 同 E3 | 发 `/picadebug D7` | 只触发周榜；回执里不含日榜/月榜 |
| E6 | `all` 关键字 | 同 E3 | 发 `/picadebug all` | 与 E3 等价 |
| E7 | 参数非法提示用法 | 同 E3 | 发 `/picadebug 乱写` | 回复用法说明；不推送 |
| E8 | 无订阅时的回执 | 删除 `rank_subscriptions.json` | 发 `/picadebug` | 回执里每项都是「➖ 某榜: 无人订阅」，`共推送 0 个会话` |
| E9 | 不占用定时额度 | 同 E3，且 cron 会在 1 分钟内命中 | 先 `/picadebug`，等 cron 命中 | 定时推送**照常**触发（调试命令不会「用掉」这一分钟） |
| E10 | 会写入快照 | 先 `/picadebug` 建立快照 | 等到 cron 命中同一榜单 | 定时推送不再重复报「新上榜」（回执里已提示这一点） |
| E11 | 取不到账号时的回执 | 无绑定、无默认账号 | 发 `/picadebug` | 回执显示「⚠️ 某榜: N 个订阅会话推送失败（请看日志）」，不崩溃 |

---

## F. 主动推送的长文本折叠

> 背景：主动推送不经过 AstrBot 响应管线，插件因此按会话的
> `platform_settings.forward_threshold` 自行处理长文本 —— 超过阈值时折叠成 QQ「合并转发」。

| 编号 | 用例 | 前置 | 步骤 | 预期 |
|---|---|---|---|---|
| F1 | 短文本仍是普通文本 | AstrBot 平台设置 `forward_threshold=1500`；`page_size=10` | `/picadebug` 触发推送 | 排行榜以**普通文本**消息送达（不折叠） |
| F2 | 超阈值自动折叠 | 把 `forward_threshold` 调到 `100`（低于榜单文本长度） | `/picadebug` 触发推送 | 排行榜以 **QQ「合并转发」卡片**送达，点开内容完整、未截断 |
| F3 | 折叠阈值取自会话配置 | 同 F2，恢复 `forward_threshold=1500` | 再 `/picadebug` | 又变回普通文本（证明阈值确实读的是平台配置） |
| F4 | 长榜单实测 | 保持 `forward_threshold=1500`，`page_size=30` | `/picadebug` 触发推送 | 文本超过 1500 字 → 折叠成合并转发并成功送达 |
| F5 | 取不到账号时退化并告警 | 同 F2，人为让 bot 账号信息接口不可用 | 触发推送 | 日志出现 `获取机器人账号信息失败，长消息将不折叠`，并按普通文本发送，不抛异常 |
| F6 | 折叠发送失败有告警 | 同 F2，人为让转发消息发送失败 | 触发推送 | 日志出现 `合并转发发送失败` 的 warning（用户此时收不到内容，必须可见） |
| F7 | 推送里的短提示不折叠 | 同 F2 | 触发推送并观察 | 榜单折叠为卡片，而「正在下载封面…」等短提示仍是普通文本（同一条推送里两种形态共存） |

---

## G. 回归（确认原有功能未受影响）

| 编号 | 用例 | 步骤 | 预期 |
|---|---|---|---|
| G1 | `/picarank` 默认行为 | 关闭 `rank_cover_pdf`、`rank_new_highlight=false` | 输出与 v1.4.1 完全一致（仅排行榜文本） |
| G2 | 帮助与命令列表 | 发 `/pica`、`/picahelp` | 帮助里新增 `/picasub` 一行；其余命令不变（`/picadebug` 有意不列入公开帮助） |
| G3 | 单章节下载 | `/picadl <ID> 1` | 按 `pack_format` 正常打包发送；无重复文件 |
| G4 | 整本下载 | `/picadl <ID>` | 进度提示按百分比出现；完成后只收到**一份**文件（v1.4.1 的防重发语义仍生效） |
| G5 | 详情/搜索/收藏/签到 | `/picainfo`、`/picasearch`、`/picamyfav`、`/picapunch` | 行为不变 |
| G6 | `/picaclean` 不误删持久数据 | 先 `/picarank` 生成快照与订阅，再 `/picaclean` | `rank_history/`、`rank_subscriptions.json` 仍在；只清 `cache/` 与 `packs/` |
| G7 | 插件卸载/重载 | 重载插件 | 日志出现后台任务取消相关行为无异常；重载后订阅推送继续工作（不会重复推送） |
| G8 | 无事件循环加载 | 直接 `python -c "import main"` 之类 | 只记 warning，不抛异常 |

---

## H. 自动化测试（每次改动都跑）

```bash
pip install -r requirements.txt
pip install ruff pytest
ruff check main.py core tests conftest.py
pytest -q
```

预期：`ruff` 无输出（All checks passed）、`pytest` 全部通过（当前 208 例）。
