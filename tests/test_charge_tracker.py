"""Tests for ChargeTracker, the battery-trend charging inference.

ChargeTracker is a pure helper with no Home Assistant or Navimow hardware
dependency, so these run standalone. The integration package is stubbed so the
coordinator module imports without pulling the HA runtime.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PKG = "custom_components.navimow"


def _load_coordinator():
    # Real Home Assistant is available in the test env; only the package chain
    # (so `from .const import ...` resolves without running __init__.py, which
    # needs the SDK) and the Navimow SDK need stubbing. Stay isolated: do not
    # touch the real `homeassistant` modules — other tests rely on them.
    for name in ("custom_components", _PKG):
        if name not in sys.modules:
            module = types.ModuleType(name)
            module.__path__ = [str(_ROOT / name.replace(".", "/"))]
            sys.modules[name] = module

    class _Anything:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, *a, **k):
            pass

    def _stub(modname: str, **attrs):
        mod = types.ModuleType(modname)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules.setdefault(modname, mod)

    _stub("mower_sdk")
    _stub("mower_sdk.api", MowerAPI=_Anything)
    _stub(
        "mower_sdk.models",
        Device=_Anything,
        DeviceAttributesMessage=_Anything,
        DeviceStateMessage=_Anything,
        DeviceStatus=_Anything,
    )
    _stub("mower_sdk.sdk", NavimowSDK=_Anything)

    spec = importlib.util.spec_from_file_location(
        f"{_PKG}.coordinator",
        _ROOT / "custom_components" / "navimow" / "coordinator.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


coordinator = _load_coordinator()
ChargeTracker = coordinator.ChargeTracker
STALL = coordinator.CHARGE_STALL_TIMEOUT


def test_unknown_until_first_trend():
    tracker = ChargeTracker(STALL)
    assert tracker.charging is None
    # Single reading establishes a baseline but no trend yet.
    assert tracker.observe(50, now=0.0) is None
    assert tracker.charging is None


def test_rising_then_falling():
    tracker = ChargeTracker(STALL)
    tracker.observe(50, now=0.0)
    assert tracker.observe(51, now=10.0) is True
    assert tracker.observe(60, now=20.0) is True
    assert tracker.observe(59, now=30.0) is False
    assert tracker.charging is False


def test_flat_holds_until_stall_timeout():
    tracker = ChargeTracker(STALL)
    tracker.observe(80, now=0.0)
    assert tracker.observe(81, now=10.0) is True
    # Flat readings within the stall window keep the charging state.
    assert tracker.observe(81, now=10.0 + STALL) is True
    # Past the stall window while not full -> treated as no longer charging.
    assert tracker.observe(81, now=11.0 + STALL) is False


def test_full_battery_stays_on_when_flat():
    tracker = ChargeTracker(STALL)
    tracker.observe(99, now=0.0)
    assert tracker.observe(100, now=10.0) is True
    # At 100% the stall timeout does not flip it off (full / trickle on dock).
    assert tracker.observe(100, now=20.0 + 10 * STALL) is True


def test_none_reading_is_ignored():
    tracker = ChargeTracker(STALL)
    tracker.observe(50, now=0.0)
    tracker.observe(51, now=10.0)
    assert tracker.charging is True
    # A missing battery value must not disturb the inferred state.
    assert tracker.observe(None, now=20.0) is True
    assert tracker.charging is True


def test_resumes_charging_after_stall():
    tracker = ChargeTracker(STALL)
    tracker.observe(70, now=0.0)
    tracker.observe(71, now=10.0)
    # Stall it out.
    assert tracker.observe(71, now=20.0 + STALL) is False
    # A fresh rise re-arms charging.
    assert tracker.observe(72, now=30.0 + STALL) is True


@pytest.mark.parametrize(
    "readings,expected",
    [
        ([(50, 0.0), (50, 5.0)], None),          # flat from unknown -> unknown
        ([(50, 0.0), (40, 5.0)], False),         # immediate drop
        ([(50, 0.0), (55, 5.0), (55, 6.0)], True),  # rise then brief flat
    ],
)
def test_table(readings, expected):
    tracker = ChargeTracker(STALL)
    result = None
    for battery, now in readings:
        result = tracker.observe(battery, now=now)
    assert result is expected
