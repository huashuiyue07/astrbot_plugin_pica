# 更新日志

## v1.4.1

### 修复：整本下载完成后重复发送给用户两份文件

**现象**：`/picadl <ID>` 整本下载完成后，用户会收到两份完全相同的打包文件。

**根因**：`_send_with_retry()` 把一个**客户端侧超时**当成了「未送达」并重发，而两者不等价。

1. AstrBot 的 aiocqhttp 适配器把 OneBot API 调用超时**硬编码为 180s**
   （`aiocqhttp_platform_adapter.py` 中 `CQHttp(api_timeout_sec=180)`）。
2. 发送打包好的大文件时，napcat 需要先把文件上传到 QQ 服务器再投递，
   耗时随体积增长、经常超过 180s。
3. `ResultStore.fetch()` 超时后抛出 `NetworkError('WebSocket API call timeout')`
   —— 但这个超时**只表示客户端不再等待**，napcat 侧的传输并未中断，
   文件最终仍然投递成功。
4. 旧代码 `except Exception` 捕获后无条件重试，于是同一份文件被投递了两次。

线上日志实证（已脱敏，QQ 号 / 昵称 / 本子 ID 均隐去）：

```
[23:31:04] <user>/<qq>: picadl <comic_id>
[23:31:04] Prepare to send - 📚 开始整本下载 [...]，完成后会自动通知你
[23:35:01] [astrbot_plugin_pica] [WARN] [main:648]: 发送消息失败(第1次): WebSocket API call timeout
```

**修复**：

- `_send_with_retry()` 对**含上传类组件**（图片/文件/语音/视频）的消息链
  **只尝试一次，绝不重发**；纯文本消息的失败重试逻辑保持不变。
- 新增 `send_api_timeout` 配置项（默认 `900` 秒）：发送上传类消息前，
  把 OneBot API 调用超时抬到不低于该值，从源头避免大文件上传撞上 180s 的墙。
  该调整**只增不减**，因此是幂等的，并发发送不会互相把超时改小。
- 发送结果细分为三态：已送达 / 结果不确定（上传超时）/ 确认失败。
  超时不再谎报「发送失败」、也不再重发，只在最后统一提示用户
  「无法确认是否送达，请先确认是否收到」。

### 其他

- 新增 `CHANGELOG.md`（此前缺少更新日志文件）。
- 新增 `send_api_timeout` 配置项与对应的 README 说明。
- 补充回归测试：`tests/test_send_dedup.py` 覆盖「上传类消息不重发」与
  「纯文本消息仍重试」两条路径。

## v1.4.0

- 整体优化与关键 Bug 修复。
- 整本下载进度提示改为按百分比（`progress_step_pct`，默认每 10%）。
- 整本下载按大小分批发送（`send_batch_mb`），避免单文件过大被 QQ 拒收。
- PDF 打包兼容 WEBP 图片（pica 部分图片为 WEBP 但扩展名为 .jpg）。
- 签到接口改用 POST（原 GET 返回 405）。

更早的变更参见 git 提交历史。
