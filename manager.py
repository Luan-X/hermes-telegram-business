"""Telegram Business Mode (Secretary Bots) — owner-approved drafting.

When a Telegram Business account owner connects this bot via BotFather's
Business Mode, the bot starts receiving messages addressed to the owner
in chats the owner has whitelisted. This module turns those incoming
customer messages into drafted replies that the owner approves before
they go out — no auto-send, ever.

User flow (the simplest path that works end-to-end):

  1. Owner enables Business Mode in BotFather and connects this bot.
     → bot receives ``BusinessConnection`` update, persists it, DMs the
     owner an onboarding message with a quick how-it-works summary.

  2. Customer messages the owner in a chat covered by the connection.
     → bot debounces ``debounce_seconds`` (default 8s) to coalesce
     typing bursts, then drafts a single reply using the most-recent
     customer text.
     → bot DMs the draft to the *owner's* chat with this bot, with
     inline buttons: [✓ Send]  [✎ Edit]  [✕ Discard].

  3. Owner taps:
       Send    → bot sends the draft to the customer chat using
                 ``business_connection_id``. Owner gets a "✓ Sent" confirmation.
       Edit    → bot replies "send me the text you want delivered" and
                 captures the owner's next text DM as the outgoing reply.
       Discard → draft dropped, nothing goes to the customer.

The owner is always in control: ``can_reply`` from Telegram is required
for the Send button to appear, ``/biz pause`` globally suspends drafting,
and per-chat pauses live in ``telegram_business_connections.paused_chats``.

State lives in two SQLite tables in the plugin's own database file
(``<HERMES_HOME>/telegram-business/state.db``) — see ``state.py`` for the
schema. Hermes' core state.db is never touched.

This module is glued onto the Hermes Telegram adapter by ``__init__.py``
via ``ctx.register_telegram_handler`` — the adapter invokes the plugin's
factory at connect() time and the factory wires PTB update handlers plus
an inline-button callback prefix (``bd:`` for "business draft"). The host
adapter owns all Telegram I/O — this module just calls back into it for
sending.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any, Awaitable, Callable, Dict, List, Optional

try:
    from .screening import ScreeningResult, combine_results, content_hash, scan_keywords
except ImportError:  # pragma: no cover - bare-module plugin loading
    from screening import ScreeningResult, combine_results, content_hash, scan_keywords

logger = logging.getLogger(__name__)


# Callback-data prefixes for the inline buttons. Keep them short — Telegram
# caps callback_data at 64 bytes.
CALLBACK_PREFIX = "bd:"
RISK_CALLBACK_PREFIX = "br:"


# Choice values rendered on the inline keyboard.
CHOICE_SEND = "send"
CHOICE_EDIT = "edit"
CHOICE_DISCARD = "discard"

RISK_RESUME = "resume"
RISK_TEMP_BLOCK = "temp"
RISK_BLOCK = "block"
RISK_ALLOW = "allow"


# Onboarding text the bot DMs the owner the first time a BusinessConnection
# arrives. Plain text (Telegram MarkdownV2 escaping is fragile, and this
# message is content-stable enough to hand-author).
ONBOARDING_MESSAGE = (
    "🤝 You've connected me as your Telegram Business assistant.\n\n"
    "Here's how it works:\n"
    "• When someone messages you in a chat I have access to, I'll draft "
    "a reply and send it to you here.\n"
    "• You'll see [✓ Send] [✎ Edit] [✕ Discard] buttons. Nothing goes "
    "to your contact until you tap Send.\n"
    "• Tap Edit to override the draft with your own wording.\n"
    "• Tap Discard to drop the draft entirely.\n\n"
    "Useful commands:\n"
    "  /biz             — show status and active connections\n"
    "  /biz pause       — pause drafting (you still see the messages)\n"
    "  /biz resume      — resume drafting\n"
    "  /biz off         — disable drafting for one chat (reply to that customer's draft)\n\n"
    "Risk controls: /biz risk list · /biz block <chat_id> · /biz unblock <chat_id>\n\n"
    "I never auto-send. Every reply is yours to approve."
)


# ---------------------------------------------------------------------------
# Type aliases for the adapter callbacks the manager depends on.
# Defined as Callables so this module stays import-light and decoupled
# from the rest of the telegram.py module.
# ---------------------------------------------------------------------------

# Generates a draft reply text for a given customer message.
# Returns the draft text, or raises on failure.
DraftGenerator = Callable[[str, str], Awaitable[str]]  # (customer_text, customer_chat_id) -> draft

# Sends a message to a chat. For owner DMs, business_connection_id is None.
# For customer chats reached via business mode, business_connection_id is set.
SendMessage = Callable[..., Awaitable[Any]]  # delegates to bot.send_message kwargs


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class BusinessModeManager:
    """Owns Business Mode state and orchestrates drafting + approval.

    One instance per Telegram adapter. The adapter wires up handlers in
    ``connect()`` and calls into this manager for every business update.
    """

    def __init__(
        self,
        *,
        session_db: Any,
        send_message: SendMessage,
        draft_generator: DraftGenerator,
        debounce_seconds: float = 8.0,
        draft_ttl_hours: float = 24.0,
        max_customer_text_chars: int = 4000,
        risk_classifier: Optional[Callable[[str], Awaitable[Any]]] = None,
        screening_enabled: bool = True,
        risk_threshold: float = 0.65,
        risk_confidence_threshold: float = 0.60,
        temp_block_seconds: float = 24.0 * 3600.0,
        auto_temp_block: bool = True,
        data_retention_days: float = 30.0,
    ) -> None:
        self._db = session_db
        self._send = send_message
        self._draft_generator = draft_generator
        self._debounce_seconds = max(0.0, float(debounce_seconds))
        self._draft_ttl_seconds = max(60.0, float(draft_ttl_hours) * 3600.0)
        self._max_customer_text_chars = int(max_customer_text_chars)
        self._risk_classifier = risk_classifier
        self._screening_enabled = bool(screening_enabled)
        self._risk_threshold = max(0.0, min(1.0, float(risk_threshold)))
        self._risk_confidence_threshold = max(0.0, min(1.0, float(risk_confidence_threshold)))
        self._temp_block_seconds = max(60.0, float(temp_block_seconds))
        self._auto_temp_block = bool(auto_temp_block)
        self._data_retention_seconds = max(1.0, float(data_retention_days) * 86400.0)

        # Keep unresolved records for active workflows, while bounding the
        # lifetime of completed message-bearing records in the local DB.
        self._db.expire_telegram_business_drafts()
        self._db.purge_telegram_business_data(
            older_than_seconds=self._data_retention_seconds,
        )

        # In-flight debounce tasks, keyed by (connection_id, customer_chat_id).
        # New customer messages reset the timer so a typing burst yields one draft.
        self._debounce_tasks: Dict[str, asyncio.Task] = {}
        self._debounce_buffers: Dict[str, Dict[str, Any]] = {}

        # Owner DMs that are in "next message = edited reply" mode.
        # Keyed by owner_chat_id → draft_id awaiting the override text.
        self._edit_capture: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # BusinessConnection updates (established / edited / ended).
    # ------------------------------------------------------------------

    async def handle_connection_update(self, business_connection: Any) -> None:
        """Persist or remove a connection row.

        ``business_connection`` is a PTB ``BusinessConnection`` object.
        On Telegram Bot API 9.0+, ``rights`` is an object with ``can_reply``
        among other flags; on older versions a ``can_reply`` bool sits on
        the connection itself.  We tolerate both shapes.
        """
        conn_id = getattr(business_connection, "id", None)
        user = getattr(business_connection, "user", None)
        owner_user_id = getattr(user, "id", None)
        owner_chat_id = getattr(business_connection, "user_chat_id", None)
        is_enabled = bool(getattr(business_connection, "is_enabled", False))
        if conn_id is None or owner_user_id is None or owner_chat_id is None:
            logger.warning("BusinessConnection update missing required fields")
            return

        # ``can_reply`` location moved in API 9.0 from connection root to
        # ``rights.can_reply``.  Probe rights first.
        can_reply = False
        rights = getattr(business_connection, "rights", None)
        if rights is not None:
            can_reply = bool(getattr(rights, "can_reply", False))
        else:
            can_reply = bool(getattr(business_connection, "can_reply", False))

        previous = self._db.get_telegram_business_connection(str(conn_id))
        self._db.upsert_telegram_business_connection(
            connection_id=str(conn_id),
            owner_user_id=str(owner_user_id),
            owner_chat_id=str(owner_chat_id),
            can_reply=can_reply,
            is_enabled=is_enabled,
        )

        # First-time onboarding DM.
        if previous is None and is_enabled:
            try:
                await self._send(
                    chat_id=int(owner_chat_id),
                    text=ONBOARDING_MESSAGE,
                    disable_notification=False,
                )
            except Exception as exc:
                logger.warning("Failed to deliver business-mode onboarding DM (%s)", type(exc).__name__)

        # Connection ended → DM the owner a confirmation.
        if previous is not None and previous.get("is_enabled") and not is_enabled:
            try:
                await self._send(
                    chat_id=int(owner_chat_id),
                    text="🔌 Business connection ended. I won't draft any more replies.",
                    disable_notification=True,
                )
            except Exception as exc:
                logger.debug("Disconnection notice send failed (%s)", type(exc).__name__)

        # can_reply changed → tell the owner.
        if previous is not None and previous.get("is_enabled") and is_enabled:
            if previous.get("can_reply") != can_reply:
                msg = (
                    "✅ Send-on-your-behalf permission enabled — Send buttons are now live."
                    if can_reply else
                    "⚠️ Send-on-your-behalf permission is OFF in your Telegram Business "
                    "settings. I'll still draft replies but you'll need to copy and "
                    "send them yourself."
                )
                try:
                    await self._send(
                        chat_id=int(owner_chat_id), text=msg, disable_notification=True,
                    )
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Screening, local enforcement, and owner alerts.
    # ------------------------------------------------------------------

    def _chat_control(self, conn_id: str, customer_chat_id: str) -> Optional[Dict[str, Any]]:
        control = self._db.get_telegram_business_chat_control(conn_id, customer_chat_id)
        if (
            control
            and control.get("enforcement_state") == "temp_block"
            and control.get("blocked_until") is not None
            and float(control["blocked_until"]) <= time.time()
        ):
            self._db.clear_telegram_business_chat_control(
                conn_id, customer_chat_id, actor="temp_block_expired"
            )
            if control.get("last_risk_event_id"):
                self._db.resolve_telegram_business_risk_event(control["last_risk_event_id"])
            control = self._db.get_telegram_business_chat_control(conn_id, customer_chat_id)
        return control

    def _chat_suppressed(
        self, conn: Dict[str, Any], customer_chat_id: str
    ) -> bool:
        if str(customer_chat_id) in (conn.get("paused_chats") or []):
            return True
        control = self._chat_control(conn["connection_id"], customer_chat_id)
        if not control:
            return False
        return control.get("screening_state") in {"risk_hold", "manual_pause"} or control.get(
            "enforcement_state"
        ) in {"temp_block", "blocked"}

    def _cancel_chat_buffer(self, key: str) -> None:
        prior = self._debounce_tasks.pop(key, None)
        if prior is not None and not prior.done():
            prior.cancel()
        self._debounce_buffers.pop(key, None)

    async def _apply_screening_result(
        self,
        *,
        buf: Dict[str, Any],
        result: ScreeningResult,
        alert_if_suppressed: bool = True,
    ) -> None:
        """Persist a finding and apply the local enforcement policy."""
        if result.action == "allow":
            return

        high_confidence = (
            result.action == "hold"
            and result.severity == "high"
            and result.confidence >= self._risk_confidence_threshold
            and self._auto_temp_block
        )
        enforcement = "temp_block" if high_confidence else "none"
        decision = "temp_block" if enforcement == "temp_block" else "risk_hold"
        prior = self._chat_control(buf["conn_id"], buf["customer_chat_id"])
        prior_suppressed = bool(
            prior
            and (
                prior.get("screening_state") in {"risk_hold", "manual_pause"}
                or prior.get("enforcement_state") in {"temp_block", "blocked"}
            )
        )
        event_id = self._db.create_telegram_business_risk_event(
            connection_id=buf["conn_id"],
            customer_chat_id=buf["customer_chat_id"],
            customer_msg_id=buf.get("customer_msg_id") or None,
            category=result.category,
            severity=result.severity,
            risk_score=result.risk_score,
            confidence=result.confidence,
            rule_hits=list(result.rule_hits),
            result_json=result.as_dict(),
            message_excerpt=buf["customer_text"],
            content_hash=content_hash(buf["customer_text"]),
            decision=decision,
        )

        if result.action in {"hold", "review"}:
            blocked_until = time.time() + self._temp_block_seconds if enforcement == "temp_block" else None
            self._db.set_telegram_business_chat_control(
                buf["conn_id"],
                buf["customer_chat_id"],
                screening_state="risk_hold",
                enforcement_state=enforcement,
                blocked_until=blocked_until,
                reason_code=result.category,
                source=result.source,
                last_risk_event_id=event_id,
            )
            self._db.block_pending_telegram_business_drafts(
                buf["conn_id"], buf["customer_chat_id"]
            )

        if alert_if_suppressed and (not prior_suppressed or result.action == "review"):
            try:
                await self._send(
                    chat_id=int(buf["owner_chat_id"]),
                    text=self._render_risk_alert(
                        customer_name=buf["customer_name"],
                        customer_chat_id=buf["customer_chat_id"],
                        result=result,
                        excerpt=buf["customer_text"],
                        enforcement=enforcement,
                    ),
                    reply_markup=self._build_risk_keyboard(event_id),
                    disable_notification=False,
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                logger.debug("Risk alert send failed (%s)", type(exc).__name__)

    async def _classify_message(
        self, text: str, rules: ScreeningResult, *, allowlisted: bool = False
    ) -> ScreeningResult:
        if not self._screening_enabled or self._risk_classifier is None:
            result = rules
        else:
            raw = await self._risk_classifier(text)
            result = combine_results(
                rules,
                raw,
                risk_threshold=self._risk_threshold,
                confidence_threshold=self._risk_confidence_threshold,
            )
        hard_hits = {
            "phishing_login", "secret_request", "urgent_payment", "guaranteed_return"
        }
        if allowlisted and result.category in {"advertising", "spam"} and not (
            set(result.rule_hits) & hard_hits
        ):
            return replace(result, action="allow")
        return result

    @staticmethod
    def _render_risk_alert(
        *,
        customer_name: str,
        customer_chat_id: str,
        result: ScreeningResult,
        excerpt: str,
        enforcement: str,
    ) -> str:
        if enforcement == "temp_block":
            state = "Temporary local block applied."
        elif result.action in {"hold", "review"}:
            state = "Drafting paused for this conversation."
        else:
            state = "This message was held for review; no draft was created."
        reasons = "; ".join(result.reasons[:3]) or "The content matched risk signals."
        return (
            "⚠️ Business message risk alert\n\n"
            f"Customer: {customer_name} ({customer_chat_id})\n"
            f"Category: {result.category} · {result.severity}\n"
            f"Score: {result.risk_score:.2f} · confidence: {result.confidence:.2f}\n"
            f"Reasons: {reasons}\n\n"
            f"Message excerpt:\n{_quote_block(excerpt, max_len=600)}\n\n"
            f"{state}"
        )

    @staticmethod
    def _build_risk_keyboard(event_id: int):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "Resume", callback_data=f"{RISK_CALLBACK_PREFIX}{RISK_RESUME}:{event_id}"
                ),
                InlineKeyboardButton(
                    "Block 24h", callback_data=f"{RISK_CALLBACK_PREFIX}{RISK_TEMP_BLOCK}:{event_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "Block", callback_data=f"{RISK_CALLBACK_PREFIX}{RISK_BLOCK}:{event_id}"
                ),
                InlineKeyboardButton(
                    "Mark safe", callback_data=f"{RISK_CALLBACK_PREFIX}{RISK_ALLOW}:{event_id}"
                ),
            ],
        ])

    # ------------------------------------------------------------------
    # business_message updates (customer talks to owner).
    # ------------------------------------------------------------------

    async def handle_business_message(self, message: Any) -> None:
        """Schedule a debounced draft for an incoming customer message.

        ``message`` is a PTB ``Message`` from a business_message update.
        It has ``business_connection_id``, ``chat`` (the customer chat),
        ``from_user`` (the customer), and ``text``/``caption``.
        """
        self._db.expire_telegram_business_drafts()
        conn_id = getattr(message, "business_connection_id", None)
        if not conn_id:
            return

        conn = self._db.get_telegram_business_connection(str(conn_id))
        if not conn:
            logger.debug("Received business_message for unknown connection")
            return
        if not conn.get("is_enabled"):
            return
        if not conn.get("auto_draft", True):
            logger.debug("Business connection has auto_draft=False; skipping draft")
            return

        chat = getattr(message, "chat", None)
        customer_chat_id = getattr(chat, "id", None)
        if customer_chat_id is None:
            return
        if self._chat_suppressed(conn, str(customer_chat_id)):
            logger.debug("Customer chat is paused; skipping draft")
            return

        # Pull text. Captions on media count too — they're often the only
        # part the customer typed.
        text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
        if not text:
            # Pure media without caption — out of scope for v1 (no vision pipeline).
            return
        if len(text) > self._max_customer_text_chars:
            text = text[: self._max_customer_text_chars]

        key = f"{conn_id}:{customer_chat_id}"

        # High-confidence rules stop the conversation before the debounce
        # timer can produce a draft.  Softer findings travel with the buffer
        # and are combined with the LLM result at fire time.
        rules = scan_keywords(text) if self._screening_enabled else ScreeningResult()
        if rules.action == "hold":
            self._cancel_chat_buffer(key)
            await self._apply_screening_result(
                buf={
                    "conn_id": str(conn_id),
                    "owner_chat_id": str(conn.get("owner_chat_id")),
                    "customer_chat_id": str(customer_chat_id),
                    "customer_msg_id": str(getattr(message, "message_id", "") or ""),
                    "customer_text": text,
                    "customer_name": _customer_display_name(message),
                },
                result=rules,
            )
            return

        # Cancel any in-flight debounce for this (connection, customer chat) so
        # rapid bursts coalesce into a single draft against the latest text.
        prior = self._debounce_tasks.pop(key, None)
        if prior is not None and not prior.done():
            prior.cancel()

        # Buffer the latest message info — the timer will read this at fire time.
        self._debounce_buffers[key] = {
            "conn_id": str(conn_id),
            "owner_chat_id": str(conn.get("owner_chat_id")),
            "customer_chat_id": str(customer_chat_id),
            "customer_msg_id": str(getattr(message, "message_id", "") or ""),
            "customer_text": text,
            "customer_name": _customer_display_name(message),
            "screening_rules": rules,
        }

        # Schedule the actual draft.  If debounce is zero (test mode), fire
        # immediately so the test doesn't have to wait.
        if self._debounce_seconds <= 0:
            await self._run_draft(key)
        else:
            self._debounce_tasks[key] = asyncio.create_task(
                self._debounce_then_draft(key)
            )

    async def _debounce_then_draft(self, key: str) -> None:
        try:
            await asyncio.sleep(self._debounce_seconds)
        except asyncio.CancelledError:
            return
        try:
            await self._run_draft(key)
        finally:
            self._debounce_tasks.pop(key, None)

    async def _run_draft(self, key: str) -> None:
        buf = self._debounce_buffers.pop(key, None)
        if not buf:
            return

        # Re-check the connection state — owner may have hit /biz pause
        # during the debounce window.
        conn = self._db.get_telegram_business_connection(buf["conn_id"])
        if not conn or not conn.get("is_enabled") or not conn.get("auto_draft", True):
            return
        if self._chat_suppressed(conn, buf["customer_chat_id"]):
            return

        control = self._chat_control(buf["conn_id"], buf["customer_chat_id"])
        allowlisted = bool(control and control.get("enforcement_state") == "allowlisted")
        try:
            screening = await self._classify_message(
                buf["customer_text"],
                buf.get("screening_rules") or scan_keywords(buf["customer_text"]),
                allowlisted=allowlisted,
            )
        except Exception as exc:
            logger.warning("Business-mode message screening failed (%s)", type(exc).__name__)
            try:
                await self._send(
                    chat_id=int(buf["owner_chat_id"]),
                    text=(
                        "⚠️ I couldn't screen a message from "
                        f"{buf['customer_name']}. No draft was created.\n\n"
                        f"Their message was:\n\n{_quote_block(buf['customer_text'])}"
                    ),
                    disable_notification=True,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return

        # Re-read the durable state after the classifier call.  An owner may
        # have blocked the chat while the model was running.
        conn = self._db.get_telegram_business_connection(buf["conn_id"])
        if not conn or not conn.get("is_enabled") or not conn.get("auto_draft", True):
            return
        if self._chat_suppressed(conn, buf["customer_chat_id"]):
            return
        if screening.action != "allow":
            await self._apply_screening_result(buf=buf, result=screening)
            return

        try:
            draft_text = await self._draft_generator(
                buf["customer_text"], buf["customer_chat_id"]
            )
        except Exception as exc:
            logger.error("Business-mode draft generator failed (%s)", type(exc).__name__)
            try:
                await self._send(
                    chat_id=int(buf["owner_chat_id"]),
                    text=(
                        "⚠️ I couldn't draft a reply to "
                        f"{buf['customer_name']}. No draft was created.\n\n"
                        f"Their message was:\n\n{buf['customer_text']}"
                    ),
                    disable_notification=True,
                )
            except Exception:
                pass
            return

        draft_text = (draft_text or "").strip()
        if not draft_text:
            logger.debug("Empty draft — skipping")
            return

        # The owner or another update may have changed the chat state while
        # the draft model was running.  Never persist a draft after a hold.
        conn = self._db.get_telegram_business_connection(buf["conn_id"])
        if not conn or not conn.get("is_enabled") or not conn.get("auto_draft", True):
            return
        if self._chat_suppressed(conn, buf["customer_chat_id"]):
            return

        draft_id = self._db.create_telegram_business_draft(
            connection_id=buf["conn_id"],
            owner_chat_id=buf["owner_chat_id"],
            customer_chat_id=buf["customer_chat_id"],
            customer_msg_id=buf["customer_msg_id"] or None,
            customer_text=buf["customer_text"],
            draft_text=draft_text,
            ttl_seconds=self._draft_ttl_seconds,
        )
        if not draft_id:
            return

        owner_message = self._render_draft_owner_message(
            customer_name=buf["customer_name"],
            customer_text=buf["customer_text"],
            draft_text=draft_text,
        )
        keyboard = self._build_draft_keyboard(draft_id, can_reply=bool(conn.get("can_reply")))

        try:
            sent = await self._send(
                chat_id=int(buf["owner_chat_id"]),
                text=owner_message,
                reply_markup=keyboard,
                disable_notification=False,
            )
        except Exception as exc:
            logger.warning("Failed to deliver business-mode draft to owner (%s)", type(exc).__name__)
            # Mark the draft expired so we don't leave an unactionable row.
            self._db.resolve_telegram_business_draft(draft_id, status="expired")
            return

        owner_msg_id = getattr(sent, "message_id", None)
        if owner_msg_id is not None:
            self._db.set_telegram_business_draft_owner_message(draft_id, str(owner_msg_id))

    # ------------------------------------------------------------------
    # Inline-button callback dispatch (bd:choice:draft_id)
    # ------------------------------------------------------------------

    async def _handle_risk_callback(
        self,
        *,
        data: str,
        caller_user_id: Optional[str],
        answer: Callable[..., Awaitable[Any]],
        edit_message_text: Callable[..., Awaitable[Any]],
    ) -> bool:
        parts = data.split(":", 2)
        if len(parts) != 3:
            await answer(text="Invalid risk action.")
            return True
        action = parts[1]
        try:
            event_id = int(parts[2])
        except ValueError:
            await answer(text="Invalid risk action.")
            return True

        event = self._db.get_telegram_business_risk_event(event_id)
        if event is None:
            await answer(text="That risk event has expired.")
            return True
        if event.get("resolved_at") is not None:
            await answer(text="That risk event has already been resolved.")
            return True
        conn = self._db.get_telegram_business_connection(event["connection_id"])
        if not conn or str(caller_user_id) != str(conn.get("owner_user_id")):
            await answer(text="⛔ Only the connected account owner can manage this chat.")
            return True

        connection_id = event["connection_id"]
        customer_chat_id = event["customer_chat_id"]
        if action == RISK_RESUME:
            self._db.clear_telegram_business_chat_control(
                connection_id, customer_chat_id, actor="owner_callback"
            )
            message = "▶ Drafting resumed for this chat. New messages will be screened."
        elif action == RISK_TEMP_BLOCK:
            self._db.set_telegram_business_chat_control(
                connection_id,
                customer_chat_id,
                screening_state="risk_hold",
                enforcement_state="temp_block",
                blocked_until=time.time() + self._temp_block_seconds,
                reason_code=event.get("category") or "risk",
                source="owner_callback",
                last_risk_event_id=event_id,
            )
            self._db.block_pending_telegram_business_drafts(connection_id, customer_chat_id)
            message = "⏱ Temporary local block applied."
        elif action == RISK_BLOCK:
            self._db.set_telegram_business_chat_control(
                connection_id,
                customer_chat_id,
                screening_state="risk_hold",
                enforcement_state="blocked",
                blocked_until=None,
                reason_code=event.get("category") or "risk",
                source="owner_callback",
                last_risk_event_id=event_id,
            )
            self._db.block_pending_telegram_business_drafts(connection_id, customer_chat_id)
            message = "⛔ Permanent local block applied."
        elif action == RISK_ALLOW:
            self._db.set_telegram_business_chat_control(
                connection_id,
                customer_chat_id,
                screening_state="active",
                enforcement_state="allowlisted",
                blocked_until=None,
                reason_code="owner_allowlist",
                source="owner_callback",
                last_risk_event_id=event_id,
            )
            message = "✅ Chat marked safe. Advertising and spam thresholds are relaxed; hard risk rules still apply."
        else:
            await answer(text="Unknown risk action.")
            return True

        self._db.set_telegram_business_risk_event_actor(
            event_id, f"owner:{conn.get('owner_user_id')}"
        )
        self._db.resolve_telegram_business_risk_event(event_id)
        await answer(text=message[:200])
        try:
            await edit_message_text(
                text=f"{message}\n\nChat: {customer_chat_id}",
                reply_markup=None,
            )
        except Exception:
            pass
        return True

    async def handle_callback(
        self,
        *,
        data: str,
        caller_user_id: Optional[str],
        answer: Callable[..., Awaitable[Any]],
        edit_message_text: Callable[..., Awaitable[Any]],
    ) -> bool:
        """Handle a callback_query whose data starts with ``bd:``.

        Returns True if the callback was dispatched (caller should stop
        further handling), False if it wasn't ours.
        """
        self._db.expire_telegram_business_drafts()
        if data.startswith(RISK_CALLBACK_PREFIX):
            return await self._handle_risk_callback(
                data=data,
                caller_user_id=caller_user_id,
                answer=answer,
                edit_message_text=edit_message_text,
            )
        if not data.startswith(CALLBACK_PREFIX):
            return False

        parts = data.split(":", 2)
        if len(parts) != 3:
            await answer(text="Invalid draft action.")
            return True
        choice = parts[1]
        try:
            draft_id = int(parts[2])
        except ValueError:
            await answer(text="Invalid draft action.")
            return True

        draft = self._db.get_telegram_business_draft(draft_id)
        if draft is None:
            await answer(text="That draft has expired.")
            return True
        if draft.get("status") != "pending":
            await answer(text="That draft has already been resolved.")
            return True
        if float(draft.get("expires_at") or 0) <= time.time():
            self._db.resolve_telegram_business_draft(draft_id, status="expired")
            await answer(text="That draft has expired.")
            return True

        # Only the owner of this connection may act on the buttons. Reject
        # whenever the caller's identity doesn't match the connection owner —
        # including when caller_user_id is missing/falsy. The previous
        # `caller_user_id and ...` form skipped this check entirely for a
        # falsy caller_user_id, which is a fail-open authorization bug: it
        # would let anyone act on the draft instead of rejecting them.
        conn = self._db.get_telegram_business_connection(draft["connection_id"])
        if not conn:
            await answer(text="Connection no longer exists.")
            return True
        if str(caller_user_id) != str(conn.get("owner_user_id")):
            await answer(text="⛔ Only the connected account owner can use these buttons.")
            return True

        if self._chat_suppressed(conn, draft["customer_chat_id"]):
            await answer(text="This chat is blocked or paused. Resume it before sending.")
            return True

        if choice == CHOICE_DISCARD:
            self._db.resolve_telegram_business_draft(draft_id, status="discarded")
            await answer(text="✕ Discarded")
            try:
                await edit_message_text(
                    text=self._render_resolved_message(draft, status="discarded"),
                    reply_markup=None,
                )
            except Exception:
                pass
            return True

        if choice == CHOICE_EDIT:
            if not self._db.mark_telegram_business_draft_awaiting_edit(draft_id):
                await answer(text="That draft has expired or was already resolved.")
                return True
            draft = self._db.get_telegram_business_draft(draft_id) or draft
            self._edit_capture[str(conn["owner_chat_id"])] = draft_id
            await answer(text="✎ Send me the text to deliver")
            try:
                await edit_message_text(
                    text=(
                        self._render_resolved_message(draft, status="awaiting_edit")
                        + "\n\n✎ Reply to this DM with the text you want delivered."
                    ),
                    reply_markup=None,
                )
            except Exception:
                pass
            return True

        if choice == CHOICE_SEND:
            if not conn.get("can_reply"):
                await answer(
                    text=(
                        "⚠️ Send-on-your-behalf is OFF — enable it in Telegram → "
                        "Business → Chatbots, then try again."
                    )
                )
                return True
            claimed = self._db.claim_telegram_business_draft_for_send(draft_id)
            if claimed is None:
                latest = self._db.get_telegram_business_draft(draft_id)
                await answer(
                    text=("That draft has expired." if not latest or latest.get("status") == "expired"
                          else "That draft is already being sent or was resolved.")
                )
                return True
            draft = claimed
            try:
                await self._send(
                    chat_id=int(draft["customer_chat_id"]),
                    text=draft["draft_text"],
                    business_connection_id=draft["connection_id"],
                )
            except Exception as exc:
                logger.warning("Business send failed (%s)", type(exc).__name__)
                self._db.release_telegram_business_draft_send(draft_id)
                await answer(text="⚠️ Send failed. Telegram did not accept the message.")
                return True

            self._db.resolve_telegram_business_draft(
                draft_id, status="sent", final_sent_text=draft["draft_text"],
            )
            await answer(text="✓ Sent")
            try:
                await edit_message_text(
                    text=self._render_resolved_message(draft, status="sent"),
                    reply_markup=None,
                )
            except Exception:
                pass
            return True

        await answer(text="Unknown action.")
        return True

    # ------------------------------------------------------------------
    # Owner-side "next message after Edit = the actual reply text" capture.
    # ------------------------------------------------------------------

    async def maybe_handle_edit_capture(
        self,
        *,
        owner_chat_id: str,
        text: str,
    ) -> bool:
        """If the owner just tapped Edit, treat their next DM as the override.

        Returns True if the message was consumed by the edit-capture flow
        (so the caller shouldn't dispatch it to the normal command path).
        """
        draft_id = self._edit_capture.pop(str(owner_chat_id), None)
        if draft_id is None:
            return False

        override = (text or "").strip()
        if not override:
            # Empty edit attempt → restore capture and let the user retry.
            self._edit_capture[str(owner_chat_id)] = draft_id
            try:
                await self._send(
                    chat_id=int(owner_chat_id),
                    text="✎ Edit cancelled (empty text). Tap Edit again if you want to try.",
                    disable_notification=True,
                )
            except Exception:
                pass
            return True

        draft = self._db.get_telegram_business_draft(draft_id)
        if not draft or draft.get("status") not in {"pending", "awaiting_edit"}:
            try:
                await self._send(
                    chat_id=int(owner_chat_id),
                    text="That draft has expired or was already resolved.",
                    disable_notification=True,
                )
            except Exception:
                pass
            return True
        if float(draft.get("expires_at") or 0) <= time.time():
            self._db.resolve_telegram_business_draft(draft_id, status="expired")
            try:
                await self._send(
                    chat_id=int(owner_chat_id),
                    text="That draft has expired.",
                    disable_notification=True,
                )
            except Exception:
                pass
            return True

        conn = self._db.get_telegram_business_connection(draft["connection_id"])
        if not conn:
            return True
        if self._chat_suppressed(conn, draft["customer_chat_id"]):
            try:
                await self._send(
                    chat_id=int(owner_chat_id),
                    text="This chat is blocked or paused. Resume it before sending an edited reply.",
                    disable_notification=False,
                )
            except Exception:
                pass
            return True
        if not conn.get("can_reply"):
            try:
                await self._send(
                    chat_id=int(owner_chat_id),
                    text=(
                        "⚠️ Send-on-your-behalf is OFF in Telegram → Business → "
                        "Chatbots. I can't deliver this — copy the text and send it "
                        "manually."
                    ),
                    disable_notification=False,
                )
            except Exception:
                pass
            return True

        try:
            await self._send(
                chat_id=int(draft["customer_chat_id"]),
                text=override,
                business_connection_id=draft["connection_id"],
            )
        except Exception as exc:
            logger.warning("Business edit-send failed (%s)", type(exc).__name__)
            try:
                await self._send(
                    chat_id=int(owner_chat_id),
                    text="⚠️ Send failed. Telegram did not accept the message.",
                    disable_notification=False,
                )
            except Exception:
                pass
            return True

        self._db.resolve_telegram_business_draft(
            draft_id, status="edited", final_sent_text=override,
        )
        try:
            await self._send(
                chat_id=int(owner_chat_id),
                text=f"✓ Sent (edited):\n\n{override}",
                disable_notification=True,
            )
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------
    # /biz slash command (owner-only).
    # ------------------------------------------------------------------

    async def handle_biz_command(
        self,
        *,
        owner_user_id: str,
        owner_chat_id: str,
        args: List[str],
    ) -> str:
        """Process /biz subcommands.  Returns text the adapter should send back.

        Supported:
          /biz                 — status dashboard
          /biz pause           — set auto_draft=False on all this user's connections
          /biz resume          — set auto_draft=True on all this user's connections
          /biz off <chat_id>   — add a customer chat to the paused list
          /biz on  <chat_id>   — remove a customer chat from the paused list
        """
        connections = self._db.list_telegram_business_connections(
            owner_user_id=str(owner_user_id), enabled_only=False,
        )
        if connections and not any(
            str(connection.get("owner_chat_id")) == str(owner_chat_id)
            for connection in connections
        ):
            return "⚠️ /biz commands are available only in the connected owner's private chat."
        active = [c for c in connections if c.get("is_enabled")]

        if not args:
            return self._render_status(connections=connections, owner_chat_id=owner_chat_id)

        sub = args[0].lower()
        if sub == "risk":
            if len(args) == 1 or args[1].lower() == "list":
                return self._render_risk_status(
                    connections=connections,
                    controls=self._db.list_telegram_business_chat_controls(
                        connection_ids=[c["connection_id"] for c in connections]
                    ),
                )
            if len(args) >= 3:
                nested = args[1].lower()
                mapped = {"resume": "unblock"}.get(nested, nested)
                if mapped in {"block", "unblock", "tempblock", "allow", "unallow"}:
                    return await self.handle_biz_command(
                        owner_user_id=owner_user_id,
                        owner_chat_id=owner_chat_id,
                        args=[mapped, *args[2:]],
                    )
            return (
                "Usage:\n"
                "  /biz risk list\n"
                "  /biz risk resume <chat_id>\n"
                "  /biz risk block <chat_id>\n"
                "  /biz risk tempblock <chat_id> <minutes>\n"
                "  /biz risk allow <chat_id>\n"
                "  /biz risk unallow <chat_id>\n"
            )

        if sub in {"block", "tempblock", "unblock", "allow", "unallow"}:
            chat_arg = args[1].strip() if len(args) >= 2 else ""
            if not chat_arg:
                return f"Usage: /biz {sub} <chat_id>"
            if not active:
                return "You don't have any active business connections."
            seconds = self._temp_block_seconds
            if sub == "tempblock":
                try:
                    minutes = max(1, min(60 * 24 * 30, int(args[2])))
                except (IndexError, ValueError):
                    return "Usage: /biz tempblock <chat_id> <minutes>"
                seconds = minutes * 60.0
            changed = 0
            for conn in active:
                conn_id = conn["connection_id"]
                existing = self._db.get_telegram_business_chat_control(conn_id, chat_arg)
                if sub == "unblock":
                    if existing and existing.get("enforcement_state") in {"blocked", "temp_block"}:
                        self._db.clear_telegram_business_chat_control(
                            conn_id, chat_arg, actor="owner_command"
                        )
                        if existing.get("last_risk_event_id"):
                            self._db.resolve_telegram_business_risk_event(
                                existing["last_risk_event_id"]
                            )
                        changed += 1
                elif sub == "allow":
                    self._db.set_telegram_business_chat_control(
                        conn_id, chat_arg, screening_state="active",
                        enforcement_state="allowlisted", blocked_until=None,
                        reason_code="owner_allowlist", source="owner_command",
                    )
                    changed += 1
                elif sub == "unallow":
                    if existing and existing.get("enforcement_state") == "allowlisted":
                        self._db.clear_telegram_business_chat_control(
                            conn_id, chat_arg, actor="owner_command"
                        )
                        changed += 1
                else:
                    enforcement = "blocked" if sub == "block" else "temp_block"
                    self._db.set_telegram_business_chat_control(
                        conn_id, chat_arg, screening_state="risk_hold",
                        enforcement_state=enforcement,
                        blocked_until=(time.time() + seconds if enforcement == "temp_block" else None),
                        reason_code="owner_command", source="owner_command",
                    )
                    self._db.block_pending_telegram_business_drafts(conn_id, chat_arg)
                    changed += 1
            if sub == "unblock":
                return f"▶ Unblocked chat {chat_arg}. New messages will be screened."
            if sub == "allow":
                return f"✅ Chat {chat_arg} added to the local allowlist."
            if sub == "unallow":
                return f"✅ Chat {chat_arg} removed from the local allowlist."
            if sub == "tempblock":
                return f"⏱ Chat {chat_arg} locally blocked for {int(seconds / 60)} minute(s)."
            return f"⛔ Chat {chat_arg} permanently blocked locally."

        if sub in {"pause", "resume"}:
            target = (sub == "resume")
            if not active:
                return "You don't have any active business connections."
            for conn in active:
                self._db.set_telegram_business_auto_draft(
                    conn["connection_id"], auto_draft=target,
                )
            verb = "▶ Resumed drafting." if target else "⏸ Paused drafting."
            return f"{verb}  ({len(active)} connection{'s' if len(active) != 1 else ''})"

        if sub in {"off", "on"} and len(args) >= 2:
            chat_arg = args[1].strip()
            if not active:
                return "You don't have any active business connections."
            updated = 0
            for conn in active:
                paused = list(conn.get("paused_chats") or [])
                if sub == "off" and chat_arg not in paused:
                    paused.append(chat_arg)
                    self._db.set_telegram_business_paused_chats(
                        conn["connection_id"], paused,
                    )
                    updated += 1
                elif sub == "on" and chat_arg in paused:
                    paused.remove(chat_arg)
                    self._db.set_telegram_business_paused_chats(
                        conn["connection_id"], paused,
                    )
                    updated += 1
            if updated:
                verb = "muted in" if sub == "off" else "re-enabled for"
                return f"✓ Chat {chat_arg} {verb} {updated} connection(s)."
            return f"No change — {chat_arg} was already in that state."

        return (
            "Usage:\n"
            "  /biz             — show status\n"
            "  /biz pause       — pause drafting (still see messages)\n"
            "  /biz resume      — resume drafting\n"
            "  /biz off <id>    — mute drafting for one customer chat\n"
            "  /biz on  <id>    — re-enable drafting for one customer chat\n"
            "  /biz block <id>  — permanently block a chat locally\n"
            "  /biz unblock <id> — unblock a chat locally\n"
            "  /biz tempblock <id> <minutes>\n"
            "  /biz allow <id>  — add a chat to the local allowlist\n"
            "  /biz risk list   — show risk-held and blocked chats"
        )

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _render_draft_owner_message(
        *, customer_name: str, customer_text: str, draft_text: str
    ) -> str:
        """Build the plain-text owner-DM body that carries one draft."""
        return (
            f"💬 {customer_name} wrote:\n"
            f"{_quote_block(customer_text)}\n\n"
            f"📝 Suggested reply:\n"
            f"{draft_text}"
        )

    @staticmethod
    def _render_resolved_message(draft: Dict[str, Any], *, status: str) -> str:
        """Re-render the owner DM after a button decision."""
        header = {
            "sent": "✓ Sent",
            "edited": "✓ Sent (edited)",
            "discarded": "✕ Discarded",
            "expired": "⏰ Expired",
            "risk_blocked": "⚠️ Blocked by risk screening",
            "awaiting_edit": "✎ Edit",
        }.get(status, status)
        return (
            f"{header}\n\n"
            f"💬 Customer wrote:\n{_quote_block(draft.get('customer_text', ''))}\n\n"
            f"📝 Draft was:\n{draft.get('draft_text', '')}"
        )

    @staticmethod
    def _render_status(*, connections: List[Dict[str, Any]], owner_chat_id: str) -> str:
        if not connections:
            return (
                "You haven't connected this bot to any Telegram Business account yet.\n\n"
                "Open Telegram → Settings → Business → Chatbots, paste my @username, "
                "and pick which chats I can see."
            )
        lines = ["📋 Business Mode status\n"]
        for c in connections:
            state = "🟢 active" if c.get("is_enabled") else "⚪ ended"
            auto = "drafting ON" if c.get("auto_draft") else "drafting PAUSED"
            send_perm = "send ON" if c.get("can_reply") else "send OFF"
            paused = c.get("paused_chats") or []
            paused_note = f" — muted chats: {', '.join(paused)}" if paused else ""
            lines.append(
                f"• {state}  ·  {auto}  ·  {send_perm}{paused_note}"
            )
        lines.append("")
        lines.append(
            "Commands: /biz pause · /biz resume · /biz off <chat_id> · /biz on <chat_id>"
        )
        return "\n".join(lines)

    @staticmethod
    def _render_risk_status(
        *, connections: List[Dict[str, Any]], controls: List[Dict[str, Any]]
    ) -> str:
        active_ids = {c["connection_id"] for c in connections if c.get("is_enabled")}
        visible = [
            row for row in controls
            if row["connection_id"] in active_ids
            and (
                row.get("screening_state") != "active"
                or row.get("enforcement_state") not in {"none", "allowlisted"}
            )
        ]
        if not visible:
            return "✅ No risk-held or locally blocked chats."
        lines = ["📋 Risk chat status\n"]
        for row in visible:
            blocked_until = row.get("blocked_until")
            expiry = f" until {int(blocked_until)}" if blocked_until else ""
            lines.append(
                f"• chat {row['customer_chat_id']} · "
                f"{row.get('screening_state')} · {row.get('enforcement_state')}{expiry} · "
                f"reason: {row.get('reason_code') or 'unknown'}"
            )
        lines.append("")
        lines.append("Use /biz unblock <chat_id> or /biz allow <chat_id> to restore handling.")
        return "\n".join(lines)

    @staticmethod
    def _build_draft_keyboard(draft_id: int, *, can_reply: bool):
        """Construct the inline keyboard for one draft.

        Imported lazily so the module is importable when python-telegram-bot
        is missing (matches the lazy-deps pattern used elsewhere in the
        adapter).
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        row = []
        if can_reply:
            row.append(InlineKeyboardButton(
                "✓ Send", callback_data=f"{CALLBACK_PREFIX}{CHOICE_SEND}:{draft_id}",
            ))
        row.append(InlineKeyboardButton(
            "✎ Edit", callback_data=f"{CALLBACK_PREFIX}{CHOICE_EDIT}:{draft_id}",
        ))
        row.append(InlineKeyboardButton(
            "✕ Discard", callback_data=f"{CALLBACK_PREFIX}{CHOICE_DISCARD}:{draft_id}",
        ))
        return InlineKeyboardMarkup([row])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _customer_display_name(message: Any) -> str:
    """Best-effort human name for the customer who sent ``message``."""
    user = getattr(message, "from_user", None)
    if user is not None:
        for attr in ("full_name", "first_name", "username"):
            val = getattr(user, attr, None)
            if val:
                return str(val)
    chat = getattr(message, "chat", None)
    if chat is not None:
        for attr in ("full_name", "title", "username"):
            val = getattr(chat, attr, None)
            if val:
                return str(val)
    return "Customer"


def _quote_block(text: str, *, max_len: int = 600) -> str:
    """Render a customer message as a quoted block for the owner DM."""
    if not text:
        return "  (no text)"
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return "\n".join(f"  > {line}" for line in text.splitlines())
