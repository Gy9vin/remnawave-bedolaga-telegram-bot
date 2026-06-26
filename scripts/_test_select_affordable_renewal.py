"""Standalone logic test for PricingEngine.select_affordable_renewal.

Run with: .venv/bin/python scripts/_test_select_affordable_renewal.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from types import SimpleNamespace

# Ensure repo root is on sys.path so 'app' package is found
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

# ---------------------------------------------------------------------------
# Minimal stubs so pricing_engine can be imported without a real DB / env
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Stub out the heavy deps BEFORE importing pricing_engine.
# We use importlib-style manual stubs so 'app' stays a real package
# (loaded from disk) while only the leaf deps are replaced.
# ---------------------------------------------------------------------------

# structlog
try:
    import structlog  # noqa: F401
except ImportError:
    _sl = types.ModuleType('structlog')
    _sl.get_logger = lambda *a, **k: SimpleNamespace(  # type: ignore[attr-defined]
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    sys.modules['structlog'] = _sl

# cryptography / pydantic-settings / sqlalchemy that config.py pulls in at import time
for _heavy in [
    'cryptography', 'cryptography.hazmat', 'cryptography.hazmat.primitives',
    'cryptography.hazmat.primitives.hashes',
    'pydantic_settings',
    'sqlalchemy', 'sqlalchemy.ext', 'sqlalchemy.ext.asyncio',
    'aiogram', 'aiogram.types',
    'redis', 'aiohttp',
    'app.database.crud.server_squad',
]:
    if _heavy not in sys.modules:
        sys.modules[_heavy] = types.ModuleType(_heavy)

# Stub get_server_squads_by_uuids used inside pricing_engine
sys.modules['app.database.crud.server_squad'].get_server_squads_by_uuids = (  # type: ignore[attr-defined]
    lambda *a, **k: None
)


def _make_settings():
    """Minimal settings object for the test."""
    ns = SimpleNamespace(
        get_available_renewal_periods=lambda: [30, 90, 180, 360],
        AVAILABLE_RENEWAL_PERIODS='30,90,180,360',
        CLASSIC_PERIOD_PRICES={},
        PRICE_PER_DEVICE=0,
    )
    return ns


# We import config carefully: stub its own imports first, then patch settings
import importlib

# Patch utils modules that config may import
for _mod_name, _attrs in [
    ('app.utils.pricing_utils', {'calculate_months_from_days': lambda days: days // 30}),
    ('app.utils.promo_offer', {'get_user_active_promo_discount_percent': lambda user: 0}),
]:
    _m = sys.modules.get(_mod_name) or types.ModuleType(_mod_name)
    for _k, _v in _attrs.items():
        setattr(_m, _k, _v)
    sys.modules[_mod_name] = _m

# Now try to import config; if it fails (missing DB deps), stub it entirely
try:
    from app.config import CLASSIC_PERIOD_PRICES, PERIOD_PRICES, settings  # noqa: F401
    # If settings doesn't have our getter, patch it
    if not hasattr(settings, 'get_available_renewal_periods'):
        settings.get_available_renewal_periods = lambda: [30, 90, 180, 360]  # type: ignore[attr-defined]
except Exception:
    # Full stub
    _cfg = types.ModuleType('app.config')
    _cfg.settings = _make_settings()  # type: ignore[attr-defined]
    _cfg.CLASSIC_PERIOD_PRICES = {}  # type: ignore[attr-defined]
    _cfg.PERIOD_PRICES = {}  # type: ignore[attr-defined]
    sys.modules['app.config'] = _cfg

# Also stub app.services so importlib can find it as a package
_svc_pkg = sys.modules.get('app.services') or types.ModuleType('app.services')
sys.modules.setdefault('app.services', _svc_pkg)

# ---------------------------------------------------------------------------
# Now import the engine directly from file to avoid __init__ chain issues
# ---------------------------------------------------------------------------
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    'app.services.pricing_engine',
    os.path.join(_repo_root, 'app', 'services', 'pricing_engine.py'),
)
_pe_mod = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules['app.services.pricing_engine'] = _pe_mod
_spec.loader.exec_module(_pe_mod)  # type: ignore[union-attr]

PricingEngine = _pe_mod.PricingEngine
RenewalPricing = _pe_mod.RenewalPricing

# ---------------------------------------------------------------------------
# Fake prices per period (kopeks)
# ---------------------------------------------------------------------------
FAKE_PRICES: dict[int, int] = {30: 20000, 90: 51000, 180: 96000, 360: 180000}


def make_fake_pricing(period: int) -> RenewalPricing:
    price = FAKE_PRICES[period]
    return RenewalPricing(
        base_price=price,
        servers_price=0,
        traffic_price=0,
        devices_price=0,
        promo_group_discount=0,
        promo_offer_discount=0,
        final_total=price,
        period_days=period,
        is_tariff_mode=False,
    )


async def patched_calculate_renewal_price(self, db, subscription, period_days, *, user=None):
    return make_fake_pricing(period_days)


async def run_tests():
    engine = PricingEngine()
    # Monkeypatch calculate_renewal_price
    engine.calculate_renewal_price = lambda db, sub, period, user=None: (  # type: ignore[method-assign]
        patched_calculate_renewal_price(engine, db, sub, period, user=user)
    )

    # Classic mode subscription (tariff_id=None)
    sub_classic = SimpleNamespace(tariff_id=None, tariff=None)
    db = None  # db not used in mocked path

    test_cases = [
        (51000, (90, 51000)),
        (30000, (30, 20000)),
        (10000, None),
        (200000, (360, 180000)),
    ]

    all_passed = True
    for balance, expected in test_cases:
        user = SimpleNamespace(balance_kopeks=balance)
        result = await engine.select_affordable_renewal(db, sub_classic, user)
        status = 'PASS' if result == expected else 'FAIL'
        if status == 'FAIL':
            all_passed = False
        print(f'[{status}] balance={balance} → expected={expected}, got={result}')

    if all_passed:
        print('\nAll tests PASSED.')
    else:
        print('\nSome tests FAILED.')
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(run_tests())
