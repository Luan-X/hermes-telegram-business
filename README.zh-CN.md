# hermes-telegram-business

[English](README.md) · [繁體中文](README.zh-TW.md)

[Hermes Agent](https://github.com/NousResearch/hermes-agent) 的 Telegram Business 秘书插件。

它保留原有的“主人审批后发送”拟稿流程，并为 Business 来信增加风险控制层。

## 新增风险能力

### 双层消息筛查

每条文本消息或媒体说明文字都会在生成拟稿前经过检查：

1. **关键词规则**识别诈骗、钓鱼、付款施压、广告和垃圾消息信号。
2. **宿主 LLM** 将模糊内容分类为 `scam`、`phishing`、`advertising`、`spam`、`other` 或 `benign`。
3. **本地策略**合并两类结果。硬规则不能被 LLM 结果覆盖；筛查失败时不生成拟稿。

### 告警与处置

发现可疑消息后，主人会收到告警，内容包括类别、严重性、风险分数、置信度、原因和消息摘录。该会话的待处理拟稿会失效，后续拟稿暂停，直到主人作出决定。

高置信度、高严重性的结果可以自动应用本地临时拉黑，默认 24 小时。告警提供四个仅限主人使用的操作：

| 操作 | 效果 |
|---|---|
| **Resume** | 清除风险暂停，继续筛查新消息 |
| **Block 24h** | 应用本地临时拉黑 |
| **Block** | 应用本地永久拉黑 |
| **Mark safe** | 加入本地白名单；硬性的诈骗/钓鱼规则仍然生效 |

拉黑只在本插件内生效：它会阻止该会话的拟稿和 Business 发送，不会改变 Telegram 账号层面的联系人封禁状态。

### 会话模式

| 模式 | 含义 |
|---|---|
| `active` | 消息会被筛查，并可进入拟稿流程 |
| `risk_hold` | 可疑内容触发风险暂停，等待主人审核 |
| `temp_block` | 在到期前保持本地临时拉黑 |
| `blocked` | 本地永久拉黑 |
| `allowlisted` | 放宽广告和垃圾消息阈值；硬性风险规则仍然生效 |

主人命令：

| 命令 | 效果 |
|---|---|
| `/biz risk list` | 列出风险暂停和本地拉黑的会话 |
| `/biz block <chat_id>` | 永久本地拉黑会话 |
| `/biz tempblock <chat_id> <minutes>` | 按时长本地拉黑会话 |
| `/biz unblock <chat_id>` | 解除临时或永久本地拉黑 |
| `/biz allow <chat_id>` | 将会话加入本地白名单 |
| `/biz unallow <chat_id>` | 从本地白名单移除会话 |
| `/biz pause` / `/biz resume` | 暂停或恢复所有活动连接的拟稿 |
| `/biz off <chat_id>` / `/biz on <chat_id>` | 暂停或恢复单个会话的拟稿 |

## 原有拟稿流程

原有流程仍由主人控制：

1. Telegram Business 客户消息会以 LLM 拟稿形式发给主人。
2. 主人选择 **Send**、**Edit** 或 **Discard**。
3. 没有主人操作时不会向客户发送消息，不存在自动发送。

拟稿会合并连续输入，默认 24 小时后过期；只有 Telegram `can_reply` 权限开启时才会显示 Send 按钮。

## 安装

以命名 profile 为例：

```bash
PROFILE=dajichat
PROFILE_HOME="$HOME/.hermes/profiles/$PROFILE"
git clone https://github.com/NousResearch/hermes-telegram-business \
    "$PROFILE_HOME/plugins/telegram-business"
hermes -p "$PROFILE" plugins enable telegram-business
hermes gateway restart
```

在 [@BotFather](https://t.me/BotFather) 开启 Telegram Business Mode，在 **Telegram → 设置 → Business → Chatbots** 添加机器人，然后向机器人发送 `/biz` 检查连接状态。

## 配置

可在 `$HERMES_HOME/config.yaml` 中配置：

```yaml
plugins:
  entries:
    telegram-business:
      screening_enabled: true
      screening_llm_enabled: true
      risk_threshold: 0.65
      risk_confidence_threshold: 0.60
      auto_temp_block: true
      temp_block_minutes: 1440
      debounce_seconds: 8
      draft_ttl_hours: 24
```

筛查和拟稿使用 Hermes 当前模型的宿主 `ctx.llm` 接口，不需要插件专用 API 密钥。

## 状态与限制

插件使用 `$HERMES_HOME/telegram-business/state.db` 保存 Business 连接、拟稿、会话控制和风险事件，不修改 Hermes 核心状态。

v1 会跳过没有说明文字的媒体消息；拟稿上下文不包含完整会话历史；本地拉黑不会改变 Telegram 账号层面的联系人状态。

## 测试

```bash
python3 -m pytest
```

测试覆盖筛查、风险暂停、拉黑/解封、白名单、连接生命周期、拟稿审批、编辑捕获和仅限主人使用的回调，不连接真实 Telegram。

## 许可证

MIT
