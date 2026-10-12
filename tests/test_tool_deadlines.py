"""SPEC §7.3: slow SD loading changes only the owner's named tool deadline."""
from types import SimpleNamespace

import pytest

from yurios.world.tools.guard import tool_timeout


def test_exact_tool_override_leaves_other_deadlines_alone():
    override = "device_soundscape=90,other=20"
    assert tool_timeout("device_soundscape", 10, override) == 90
    assert tool_timeout("device_sound_library", 10, override) == 10
    assert tool_timeout("device_soundscape_fake", 10, override) == 10


@pytest.mark.parametrize("value", ["wrong", "nan", "inf", "0", "-1", "301"])
def test_bad_override_cannot_disable_the_deadline(value):
    assert tool_timeout("device_soundscape", 10, "device_soundscape=" + value) == 10


def test_mind_uses_the_same_resolver():
    from yurios.mind.hands import Hands
    stub = SimpleNamespace(cfg=SimpleNamespace(tool_timeout_s=10,
                           tool_timeout_overrides="device_soundscape=90"))
    assert Hands.timeout_for(stub, "device_soundscape") == 90
    assert Hands.timeout_for(stub, "read_note") == 10
