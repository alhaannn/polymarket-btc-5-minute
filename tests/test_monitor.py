from polybot.monitor import TelegramAlerter


def test_telegram_alerter_disabled_without_keys():
    alerter = TelegramAlerter(bot_token="", chat_id="")
    assert not alerter.enabled
    # Calling methods should safely return False without network calls
    assert not alerter.send_trade_alert("slug-1", "Up", 0.90, 5.0, 4.5, "edge")
    assert not alerter.send_settlement_alert("slug-1", "Up", "Up", 5.0, 0.5, 0.5)
    assert not alerter.send_risk_alert("Daily loss limit")
    assert not alerter.send_crash_alert("Network drop")
    assert not alerter.send_daily_summary(10, 8, 80.0, 4.5)


def test_telegram_alerter_mocked():
    alerter = TelegramAlerter(bot_token="fake_token", chat_id="123456")
    assert alerter.enabled

    # Mock send_message to test formatting without hitting Telegram
    sent = []
    alerter.send_message = lambda text: sent.append(text) or True

    alerter.send_trade_alert("btc-5m-1", "Up", 0.92, 10.0, 9.2, "edge", 0.03)
    assert len(sent) == 1
    assert "TRADE ENTERED" in sent[0]
    assert "btc-5m-1" in sent[0]
    assert "Up" in sent[0]

    alerter.send_settlement_alert("btc-5m-1", "Up", "Up", 10.0, 0.8, 0.8)
    assert len(sent) == 2
    assert "POSITION SETTLED" in sent[1]

    alerter.send_risk_alert("Max trades per day reached")
    assert len(sent) == 3
    assert "CIRCUIT BREAKER" in sent[2]
