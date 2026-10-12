from tools.nova_daily_inbox import reminder


def test_reminders_have_stable_distinct_names_and_no_goal_instruction():
    morning, text = reminder("morning", "2026-10-12")
    evening, other = reminder("evening", "2026-10-12")
    assert morning != evening
    assert reminder("morning", "2026-10-12") == (morning, text)
    assert "Guten Morgen" in text and "Guten Abend" in other
    assert "kein neues oder wiederkehrendes Ziel" in text
