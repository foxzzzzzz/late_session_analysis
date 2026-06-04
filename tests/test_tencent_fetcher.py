"""Tencent fetcher cache tests."""
from data_provider.base import RealtimeQuote
from data_provider.tencent_fetcher import TencentFetcher


def make_quote(code="000001"):
    return RealtimeQuote(
        code=code,
        name="测试",
        price=10.0,
        change_pct=1.0,
        turnover=100_000_000,
        turnover_rate=2.0,
        volume=1_000_000,
        high=10.2,
        low=9.8,
        open=9.9,
        pre_close=9.8,
    )


def test_fetch_codes_uses_ttl_cache(monkeypatch):
    fetcher = TencentFetcher()
    fetcher.fetch_codes_ttl_seconds = 30
    calls = {"count": 0}

    def fake_fetch_batch(codes):
        calls["count"] += 1
        return [make_quote(codes[0])]

    monkeypatch.setattr(fetcher, "_fetch_batch", fake_fetch_batch)

    first = fetcher.fetch_codes(["000001"])
    second = fetcher.fetch_codes(["000001"])

    assert calls["count"] == 1
    assert first[0].code == "000001"
    assert second[0].code == "000001"
    assert first[0] is not second[0]


def test_fetch_codes_cache_disabled(monkeypatch):
    fetcher = TencentFetcher()
    fetcher.fetch_codes_ttl_seconds = 0
    calls = {"count": 0}

    def fake_fetch_batch(codes):
        calls["count"] += 1
        return [make_quote(codes[0])]

    monkeypatch.setattr(fetcher, "_fetch_batch", fake_fetch_batch)

    fetcher.fetch_codes(["000001"])
    fetcher.fetch_codes(["000001"])

    assert calls["count"] == 2
