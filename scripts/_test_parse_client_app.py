"""Быстрый тест parse_client_app без запуска всего пакета приложения."""

import importlib.util
import pathlib

# Загружаем модуль напрямую, минуя __init__.py пакета
_spec = importlib.util.spec_from_file_location(
    "client_detect",
    pathlib.Path(__file__).parent.parent / "app" / "utils" / "client_detect.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
parse_client_app = _mod.parse_client_app

CASES = [
    ("Happ/1.2 (iPhone)", "Happ"),
    ("v2rayNG/1.9.5", "v2rayNG"),
    ("Streisand", "Streisand"),
    ("", "Unknown"),
    (None, "Unknown"),
    ("  Hiddify/2 ", "Hiddify"),
    ("Shadowrocket/2.2.1 (iOS)", "Shadowrocket"),
]

for ua, expected in CASES:
    result = parse_client_app(ua)
    assert result == expected, f"FAIL: parse_client_app({ua!r}) == {result!r}, expected {expected!r}"
    print(f"PASS: parse_client_app({ua!r}) -> {result!r}")

print("ALL PASS")
