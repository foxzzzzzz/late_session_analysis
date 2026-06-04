"""市场状态防抖测试"""
from screening import market_regime


def test_debounce_switches_after_two_consecutive_opposite_days(monkeypatch):
    """bull/bear 反向连续两天应完成切换，而不是永久维持旧状态"""
    states = [{"regime": "bull", "consecutive_days": 3, "last_change": "2026-05-01"}]
    saved = []

    def fake_load():
        return states[-1]

    def fake_save(state):
        saved.append(state)
        states.append(state)

    monkeypatch.setattr(market_regime, "_load_previous_regime", fake_load)
    monkeypatch.setattr(market_regime, "_save_regime", fake_save)

    first = market_regime._debounce("bear")
    second = market_regime._debounce("bear")

    assert first == "bull"
    assert second == "bear"
    assert saved[-1]["regime"] == "bear"
