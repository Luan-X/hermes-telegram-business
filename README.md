# hermes-telegram-business

**Observe-with-approval Telegram Business Mode (secretary bot) for [Hermes Agent](https://github.com/NousResearch/hermes-agent).**

Connect your Telegram Business account to your Hermes bot and it becomes your secretary: every incoming customer message gets an LLM-drafted reply delivered to **your** DM with inline buttons — **nothing reaches the customer until you tap Send.**

```
Customer → your Business account (via Business Mode)
            ↓ debounce 8s, draft once
Bot → your DM
       💬 Carol wrote:
         > Hey, are you free Friday afternoon?

       📝 Suggested reply:
       Friday afternoon works — anywhere between 2 and 5pm?

       [✓ Send]   [✎ Edit]   [✕ Discard]
            ↓
Send    → delivered to the customer via your Business connection
Edit    → your next DM text becomes the outgoing message
Discard → dropped, the customer sees nothing
```

## Safety properties

- **No auto-send exists.** Every reply requires an owner button tap, even when `can_reply` is granted.
- **Owner-only buttons.** Callbacks are authorized against the connection's `owner_user_id`; anyone else gets `⛔`.
- **Drafts expire after 24h** (configurable). Stale buttons no-op.
- **Without `can_reply`, the Send button is hidden** — you're told to copy/paste manually instead of getting a button that silently fails.
- **Typing bursts coalesce** — new messages within the debounce window supersede the prior draft, one draft per coherent thought.
- **Risk holds stop drafting** — keyword rules and the host LLM can pause a chat and invalidate pending drafts.
- **Local block modes** — owners can temporarily block, permanently block, unblock, or allowlist a chat.

## Requirements

- Hermes Agent with plugin Telegram-handler support (`ctx.register_telegram_handler`, hermes-agent PR #59159 / v0.18+)
- A Telegram **Business** subscription on your personal account
- Your Hermes Telegram bot with **Business Mode** toggled on in [@BotFather](https://t.me/BotFather)
- python-telegram-bot ≥ 21.1 (already installed with the Hermes Telegram platform)

## Install

```bash
PROFILE_HOME="${HERMES_HOME:-$HOME/.hermes}"
git clone https://github.com/NousResearch/hermes-telegram-business \
    "$PROFILE_HOME/plugins/telegram-business"
hermes plugins enable telegram-business
```

Restart the gateway (`hermes gateway restart`). Then:

1. @BotFather → your bot → **Business Mode** → enable.
2. Telegram → **Settings → Business → Chatbots** → add your bot → pick which chats it can see, and whether it can **reply on your behalf** (needed for the Send button).
3. Message your bot `/biz` to confirm the connection is live.

## Owner controls

| Command | Effect |
|---|---|
| `/biz` | status dashboard |
| `/biz pause` / `/biz resume` | global drafting kill switch |
| `/biz off <chat_id>` / `/biz on <chat_id>` | per-customer-chat mute |
| `/biz block <chat_id>` / `/biz unblock <chat_id>` | local permanent block and release |
| `/biz tempblock <chat_id> <minutes>` | local timed block |
| `/biz allow <chat_id>` / `/biz unallow <chat_id>` | local allowlist controls |
| `/biz risk list` | show risk-held and blocked chats |

## Configuration

Optional block in `~/.hermes/config.yaml`:

```yaml
plugins:
  entries:
    telegram-business:
      debounce_seconds: 8        # coalesce typing bursts
      draft_ttl_hours: 24        # stale-draft expiry
      max_customer_text_chars: 4000
      screening_enabled: true
      screening_llm_enabled: true
      risk_threshold: 0.65
      risk_confidence_threshold: 0.60
      temp_block_minutes: 1440
      auto_temp_block: true
      owner_persona: >
        You are drafting replies for a freelance photographer.
        Friendly, brief, always suggest a concrete next step.
```

Drafting uses your active Hermes model through the host-owned plugin LLM surface (`ctx.llm`) — no separate API key.

## State

Plugin-owned SQLite at `$HERMES_HOME/telegram-business/state.db` (normally `~/.hermes/profiles/<profile>/telegram-business/state.db`; connections, drafts, per-chat controls, and risk events). Hermes' core state is never touched. Delete the file to reset.

## v1 limits

- **Text only** — customer media (photos, voice, documents) is skipped; captions do trigger drafts.
- **No conversation history** — each customer message is drafted in isolation. The Edit button absorbs the gap.
- **No persona learning from edits** — your overrides go to the customer but don't train future drafts.
- **Local blocking** — `block` prevents this plugin from drafting or sending for the chat. Telegram account-level blocking remains a separate client-side operation.

## Tests

```bash
python3 -m pytest
```

37 tests covering connection lifecycle, draft supersession, debounce coalescing, all three button paths, edit capture, owner-scoped callback authorization, and `/biz` subcommands. No network, no live Telegram.

## Credits

Based on the design and implementation from [hermes-agent#30055](https://github.com/NousResearch/hermes-agent/pull/30055), replatformed as a standalone plugin. Related earlier community proposals: [#26654](https://github.com/NousResearch/hermes-agent/pull/26654) by @evgyur (earliest submission), [#35342](https://github.com/NousResearch/hermes-agent/pull/35342) by @MilekhinAV, [#46728](https://github.com/NousResearch/hermes-agent/pull/46728) by @kxnkxv.

## License

MIT
