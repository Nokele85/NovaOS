from tools.nova_daily_inbox import reminder


def test_reminders_have_stable_distinct_names_and_no_goal_instruction():
    morning, text = reminder("morning", "2026-10-12")
    evening, other = reminder("evening", "2026-10-12")
    assert morning != evening
    assert reminder("morning", "2026-10-12") == (morning, text)
    assert "Guten Morgen" in text and "Guten Abend" in other
    assert "einmaligen reach_out-Auftrag" in text
    assert "kein wiederkehrendes Ziel" in text


def test_successful_upload_is_not_repeated_and_failure_is_retryable(monkeypatch, tmp_path):
    import sys
    from datetime import datetime
    from types import SimpleNamespace
    import httpx
    import tools.nova_daily_inbox as scheduler

    class FixedTime:
        @staticmethod
        def now(zone):
            return datetime(2026, 10, 12, 9, 10, tzinfo=zone)

    calls = []
    failing = [True]

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return httpx.Response(503 if failing[0] else 200,
                                  json={"noticed": True}, request=httpx.Request("POST", url))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["scheduler", "morning"])
    monkeypatch.setattr(scheduler, "datetime", FixedTime)
    monkeypatch.setattr(scheduler.httpx, "Client", Client)
    monkeypatch.setitem(sys.modules, "yurios.world.config",
                        SimpleNamespace(Config=lambda: SimpleNamespace(owner_token="test-only")))
    import pytest
    with pytest.raises(httpx.HTTPStatusError):
        scheduler.main()
    failing[0] = False
    scheduler.main()
    scheduler.main()
    assert len(calls) == 2
    assert "/characters/nova/mind/workspace/inbox" in calls[1][0]


def test_no_late_greeting_after_restart(monkeypatch, tmp_path):
    import sys
    from datetime import datetime
    import tools.nova_daily_inbox as scheduler

    class LateTime:
        @staticmethod
        def now(zone):
            return datetime(2026, 10, 12, 12, 0, tzinfo=zone)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["scheduler", "morning"])
    monkeypatch.setattr(scheduler, "datetime", LateTime)
    scheduler.main()
    assert not (tmp_path / ".yurios").exists()
