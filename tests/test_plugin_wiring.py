"""Wiring tests: register(ctx) queues a factory; the factory adds handlers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import telegram_business_plugin as plugin


class _FakeCtx:
    def __init__(self):
        self.factories = []
        self.llm = MagicMock()

    def register_telegram_handler(self, factory):
        assert callable(factory)
        self.factories.append(factory)


def test_register_queues_one_factory():
    ctx = _FakeCtx()
    plugin.register(ctx)
    assert len(ctx.factories) == 1


def test_factory_wires_handlers(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx = _FakeCtx()
    plugin.register(ctx)
    factory = ctx.factories[0]

    application = MagicMock()
    adapter = SimpleNamespace(_bot=MagicMock(), name="telegram")

    factory(application, adapter)

    # 6 handlers: BusinessConnection, 2x business message, bd: callback,
    # /biz command, owner-DM edit capture.
    assert application.add_handler.call_count == 6

    # The edit-capture handler must be registered in group -1 so it runs
    # before (and can yield to) the core adapter's text handler.
    groups = [
        kwargs.get("group")
        for args, kwargs in application.add_handler.call_args_list
    ]
    assert -1 in groups

    # Plugin state DB created under HERMES_HOME, not the core state.db.
    assert (tmp_path / "telegram-business" / "state.db").exists()
