"""Risk screening, local block modes, and recovery tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from telegram_business_plugin.manager import BusinessModeManager
from telegram_business_plugin.screening import combine_results, scan_keywords
from telegram_business_plugin.state import BusinessStateDB


@pytest.fixture()
def db(tmp_path):
    database = BusinessStateDB(tmp_path / "risk.db")
    yield database
    database.close()


def _connection():
    return SimpleNamespace(
        id="conn-risk",
        user=SimpleNamespace(id=42, full_name="Owner"),
        user_chat_id=100,
        is_enabled=True,
        rights=SimpleNamespace(can_reply=True),
    )


def _message(text, *, chat_id=200, message_id=1):
    return SimpleNamespace(
        business_connection_id="conn-risk",
        chat=SimpleNamespace(id=chat_id, type="private"),
        from_user=SimpleNamespace(full_name="Customer"),
        text=text,
        caption=None,
        message_id=message_id,
    )


class _Sender:
    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(message_id=len(self.calls))


def test_keyword_rules_detect_secret_request():
    result = scan_keywords("Please send your OTP verification code now")
    assert result.category == "scam"
    assert result.action == "hold"
    assert "secret_request" in result.rule_hits


def test_llm_result_cannot_override_hard_rule():
    rules = scan_keywords("Please send your OTP verification code now")
    result = combine_results(
        rules,
        {
            "category": "benign",
            "severity": "low",
            "risk_score": 0.0,
            "confidence": 1.0,
        },
    )
    assert result.category == "scam"
    assert result.action == "hold"


@pytest.mark.asyncio
async def test_risk_hold_blocks_pending_draft_and_unblock_restores(db):
    db.upsert_telegram_business_connection(
        connection_id="conn-risk", owner_user_id="42", owner_chat_id="100",
        can_reply=True, is_enabled=True,
    )
    sender = _Sender()

    async def draft(text, chat_id):
        return "safe draft"

    async def classifier(text):
        return {
            "category": "phishing",
            "severity": "high",
            "risk_score": 0.95,
            "confidence": 0.95,
            "reasons": ["fake login"],
        }

    manager = BusinessModeManager(
        session_db=db,
        send_message=sender,
        draft_generator=draft,
        debounce_seconds=0,
        risk_classifier=classifier,
    )
    draft_id = db.create_telegram_business_draft(
        connection_id="conn-risk", owner_chat_id="100", customer_chat_id="200",
        customer_msg_id="old", customer_text="hello", draft_text="safe draft",
    )
    await manager.handle_business_message(_message("hello", message_id=2))
    assert db.get_telegram_business_draft(draft_id)["status"] == "risk_blocked"
    control = db.get_telegram_business_chat_control("conn-risk", "200")
    assert control["enforcement_state"] == "temp_block"
    assert sender.calls and "risk alert" in sender.calls[-1]["text"].lower()

    reply = await manager.handle_biz_command(
        owner_user_id="42", owner_chat_id="100", args=["unblock", "200"]
    )
    assert "Unblocked" in reply
    assert db.get_telegram_business_chat_control("conn-risk", "200")["enforcement_state"] == "none"


@pytest.mark.asyncio
async def test_risk_callback_is_owner_scoped(db):
    db.upsert_telegram_business_connection(
        connection_id="conn-risk", owner_user_id="42", owner_chat_id="100",
        can_reply=True, is_enabled=True,
    )
    event_id = db.create_telegram_business_risk_event(
        connection_id="conn-risk", customer_chat_id="200", customer_msg_id="1",
        category="scam", severity="high", risk_score=0.9, confidence=0.9,
        rule_hits=["secret_request"], result_json={}, message_excerpt="otp",
        content_hash="hash", decision="temp_block",
    )
    sender = _Sender()
    manager = BusinessModeManager(
        session_db=db, send_message=sender,
        draft_generator=lambda *_: None, debounce_seconds=0,
    )
    answers = []

    async def answer(**kwargs):
        answers.append(kwargs)

    async def edit(**kwargs):
        return None

    await manager.handle_callback(
        data=f"br:block:{event_id}", caller_user_id="99",
        answer=answer, edit_message_text=edit,
    )
    assert "Only the connected account owner" in answers[-1]["text"]
    assert db.get_telegram_business_chat_control("conn-risk", "200") is None
