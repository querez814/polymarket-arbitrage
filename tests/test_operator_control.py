from scripts.operator_control import main


def test_operator_helper_rejects_non_loopback_target_before_sending_token(
    monkeypatch, capsys
):
    monkeypatch.setenv("NIGHTWATCH_OPERATOR_TOKEN", "secret-token")

    result = main(["status", "--base-url", "https://example.test"])

    assert result == 2
    assert "loopback" in capsys.readouterr().out
