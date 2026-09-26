# hermes-telegram-business

[简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md)

Telegram Business secretary plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

It keeps the original owner-approved drafting flow and adds a risk-control layer for incoming Business messages.

## Added Risk Controls

### Two-stage screening

Every text or caption is checked before a draft is created:

1. **Keyword rules** detect known scam, phishing, payment-pressure, advertising, and spam signals.
2. **The host LLM** classifies softer cases as `scam`, `phishing`, `advertising`, `spam`, `other`, or `benign`.
3. **Local policy** combines both results. Hard rules cannot be overridden by an LLM response. If screening fails, no draft is created.

### Alerts and enforcement

When a message is suspicious, the owner receives an alert with the category, severity, score, confidence, reasons, and message excerpt. Pending drafts for that chat are invalidated and future drafting is paused until the owner decides.

High-confidence, high-severity findings can apply a temporary local block automatically (24 hours by default). The alert provides four owner-only actions:

| Action | Effect |
|---|---|
| **Resume** | Clear the risk hold and continue screening new messages |
| **Block 24h** | Apply a temporary local block |
| **Block** | Apply a permanent local block |
| **Mark safe** | Add the chat to the local allowlist; hard scam/phishing rules still apply |

The block is local to this plugin: it stops drafting and Business sends for the chat. It does not block the contact at Telegram account level.

### Chat modes

| Mode | Meaning |
|---|---|
| `active` | Messages are screened and eligible for drafting |
| `risk_hold` | Suspicious content stopped drafting pending owner review |
| `temp_block` | Local block until the configured expiry |
| `blocked` | Permanent local block |
| `allowlisted` | Advertising and spam thresholds are relaxed; hard risk rules remain active |

Owner commands:

| Command | Effect |
|---|---|
| `/biz risk list` | List risk-held and locally blocked chats |
| `/biz block <chat_id>` | Permanently block a chat locally |
| `/biz tempblock <chat_id> <minutes>` | Block a chat locally for a duration |
| `/biz unblock <chat_id>` | Release a temporary or permanent local block |
| `/biz allow <chat_id>` | Add a chat to the local allowlist |
| `/biz unallow <chat_id>` | Remove a chat from the local allowlist |
| `/biz pause` / `/biz resume` | Pause or resume drafting for all active connections |
| `/biz off <chat_id>` / `/biz on <chat_id>` | Pause or resume drafting for one chat |

## Original Drafting Flow

The existing workflow remains owner-controlled:

1. A Telegram Business customer message is sent to the owner as an LLM draft.
2. The owner chooses **Send**, **Edit**, or **Discard**.
3. Nothing is sent to the customer without an owner action. There is no auto-send.

Drafts coalesce typing bursts, expire after 24 hours by default, and require the Telegram `can_reply` permission for the Send button.

## Install

For a named profile:

```bash
PROFILE=dajichat
PROFILE_HOME="$HOME/.hermes/profiles/$PROFILE"
git clone https://github.com/NousResearch/hermes-telegram-business \
    "$PROFILE_HOME/plugins/telegram-business"
hermes -p "$PROFILE" plugins enable telegram-business
hermes gateway restart
```

Enable Telegram Business Mode in [@BotFather](https://t.me/BotFather), add the bot under **Telegram → Settings → Business → Chatbots**, then send `/biz` to the bot to check the connection.

## Configuration

Optional profile config in `$HERMES_HOME/config.yaml`:

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

Screening and drafting use the active Hermes model through the host-owned `ctx.llm` surface; no plugin-specific API key is required.

## State and Limits

The plugin owns `$HERMES_HOME/telegram-business/state.db`. It stores Business connections, drafts, chat controls, and risk events without changing Hermes core state.

Media without a caption is skipped in v1. Conversation history is not added to the draft context, and local blocks do not change Telegram account-level contact status.

## Tests

```bash
python3 -m pytest
```

The tests cover screening, risk holds, block/unblock and allowlist modes, connection lifecycle, draft approval, edit capture, and owner-only callbacks. They do not contact Telegram.

## License

MIT
