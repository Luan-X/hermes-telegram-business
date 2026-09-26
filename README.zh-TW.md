# hermes-telegram-business

[English](README.md) · [简体中文](README.zh-CN.md)

[Hermes Agent](https://github.com/NousResearch/hermes-agent) 的 Telegram Business 秘書外掛。

它保留原有的「主人核准後發送」擬稿流程，並為 Business 來信增加風險控制層。

## 新增風險能力

### 雙層訊息篩查

每則文字訊息或媒體說明文字都會在產生擬稿前經過檢查：

1. **關鍵字規則**識別詐騙、釣魚、付款施壓、廣告和垃圾訊息訊號。
2. **宿主 LLM** 將模糊內容分類為 `scam`、`phishing`、`advertising`、`spam`、`other` 或 `benign`。
3. **本地策略**合併兩類結果。硬規則不能被 LLM 結果覆蓋；篩查失敗時不產生擬稿。

### 告警與處置

發現可疑訊息後，主人會收到告警，內容包括類別、嚴重性、風險分數、信心度、原因和訊息摘錄。該對話的待處理擬稿會失效，後續擬稿暫停，直到主人作出決定。

高信心度、高嚴重性的結果可以自動套用本地臨時封鎖，預設 24 小時。告警提供四個僅限主人使用的操作：

| 操作 | 效果 |
|---|---|
| **Resume** | 清除風險暫停，繼續篩查新訊息 |
| **Block 24h** | 套用本地臨時封鎖 |
| **Block** | 套用本地永久封鎖 |
| **Mark safe** | 加入本地允許清單；硬性的詐騙/釣魚規則仍然生效 |

封鎖只在本外掛內生效：它會阻止該對話的擬稿和 Business 發送，不會改變 Telegram 帳號層面的聯絡人封鎖狀態。

### 對話模式

| 模式 | 含義 |
|---|---|
| `active` | 訊息會被篩查，並可進入擬稿流程 |
| `risk_hold` | 可疑內容觸發風險暫停，等待主人審核 |
| `temp_block` | 在到期前保持本地臨時封鎖 |
| `blocked` | 本地永久封鎖 |
| `allowlisted` | 放寬廣告和垃圾訊息閾值；硬性風險規則仍然生效 |

主人指令：

| 指令 | 效果 |
|---|---|
| `/biz risk list` | 列出風險暫停和本地封鎖的對話 |
| `/biz block <chat_id>` | 永久本地封鎖對話 |
| `/biz tempblock <chat_id> <minutes>` | 按時長本地封鎖對話 |
| `/biz unblock <chat_id>` | 解除臨時或永久本地封鎖 |
| `/biz allow <chat_id>` | 將對話加入本地允許清單 |
| `/biz unallow <chat_id>` | 從本地允許清單移除對話 |
| `/biz pause` / `/biz resume` | 暫停或恢復所有活動連線的擬稿 |
| `/biz off <chat_id>` / `/biz on <chat_id>` | 暫停或恢復單一對話的擬稿 |

## 原有擬稿流程

原有流程仍由主人控制：

1. Telegram Business 客戶訊息會以 LLM 擬稿形式發給主人。
2. 主人選擇 **Send**、**Edit** 或 **Discard**。
3. 沒有主人操作時不會向客戶發送訊息，不存在自動發送。

擬稿會合併連續輸入，預設 24 小時後過期；只有 Telegram `can_reply` 權限開啟時才會顯示 Send 按鈕。

## 安裝

以命名 profile 為例：

```bash
PROFILE=dajichat
PROFILE_HOME="$HOME/.hermes/profiles/$PROFILE"
git clone https://github.com/NousResearch/hermes-telegram-business \
    "$PROFILE_HOME/plugins/telegram-business"
hermes -p "$PROFILE" plugins enable telegram-business
hermes gateway restart
```

在 [@BotFather](https://t.me/BotFather) 開啟 Telegram Business Mode，在 **Telegram → 設定 → Business → Chatbots** 新增機器人，然後向機器人發送 `/biz` 檢查連線狀態。

## 設定

可在 `$HERMES_HOME/config.yaml` 中設定：

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
      data_retention_days: 30
      debounce_seconds: 8
      draft_ttl_hours: 24
```

篩查和擬稿使用 Hermes 目前模型的宿主 `ctx.llm` 介面，不需要外掛專用 API 金鑰。

## 狀態與限制

外掛使用 `$HERMES_HOME/telegram-business/state.db` 儲存 Business 連線、擬稿、對話控制和風險事件，不修改 Hermes 核心狀態。狀態目錄權限為 `0700`，資料庫權限為 `0600`；外掛啟動或處理更新時，會清理超過 `data_retention_days`（預設 30 天）的訊息記錄、風險事件和已結束連線中繼資料。請保護 profile 目錄及其備份。

v1 會跳過沒有說明文字的媒體訊息；擬稿上下文不包含完整對話歷史；本地封鎖不會改變 Telegram 帳號層面的聯絡人狀態。

## 測試

```bash
python3 -m pytest
```

測試涵蓋篩查、風險暫停、封鎖/解封、允許清單、連線生命週期、擬稿審批、編輯捕獲和僅限主人使用的回呼，不連線真實 Telegram。

## 授權條款

MIT
