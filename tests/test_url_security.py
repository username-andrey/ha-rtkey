"""Offline smoke tests for destination validation (requires Home Assistant deps)."""
from custom_components.rtkey.api import _trusted_host


def test_auth_hosts():
    assert _trusted_host('https://keyapis.key.rt.ru/path', service=True)
    assert _trusted_host('https://household.key.rt.ru/path', service=True)
    assert not _trusted_host('https://evil.example/path', service=True)
    assert not _trusted_host('https://keyapis.key.rt.ru.evil.example/path', service=True)


def test_media_hosts():
    assert _trusted_host('https://camera.rt.ru/stream')
    assert not _trusted_host('http://camera.rt.ru/stream')
    assert not _trusted_host('https://camera.rt.ru:444/stream')
    assert not _trusted_host('https://127.0.0.1/stream')
    assert not _trusted_host('https://user:pass@camera.rt.ru/stream')
