import re

from cli import SetupError, settings
import pytest

KEY = "sabha_app_" + "0123456789abcdef" * 2


def test_the_values_from_the_app_s_page_are_saved_as_given():
    assert settings("https://chat.example.test/api/v1", KEY, "wss://chat.example.test/cable", "1") == {
        "SABHA_API_URL": "https://chat.example.test/api/v1",
        "SABHA_WEBSOCKET_URL": "wss://chat.example.test/cable",
        "SABHA_APP_KEY": KEY,
        "SABHA_ALLOWED_USERS": "1",
    }


@pytest.mark.parametrize("url, api_url, websocket_url", [
    ("https://chat.example.test", "https://chat.example.test/api/v1", "wss://chat.example.test/cable"),
    (" https://chat.example.test/api/v1/ ", "https://chat.example.test/api/v1", "wss://chat.example.test/cable"),
    ("http://localhost:3000/?x=1#y", "http://localhost:3000/api/v1", "ws://localhost:3000/cable"),
    ("https://sabha.co/1000006/api/v1", "https://sabha.co/1000006/api/v1", "wss://sabha.co/cable?wid=1000006"),
])
def test_the_workspace_or_its_api_url_will_do_and_the_websocket_follows_from_it(url, api_url, websocket_url):
    values = settings(url, KEY)
    assert (values["SABHA_API_URL"], values["SABHA_WEBSOCKET_URL"]) == (api_url, websocket_url)
    assert "SABHA_ALLOWED_USERS" not in values


@pytest.mark.parametrize("url, key, cable, allowed, says", [
    ("", KEY, None, None, "Pass --url"),
    ("ftp://chat.example.test", KEY, None, None, "Not an http(s) URL"),
    ("https://chat.example.test", "sabha_app_…", None, None, "should look like sabha_app_"),
    ("https://chat.example.test", KEY, "https://chat.example.test/cable", None, "Not a ws(s) URL"),
    ("https://chat.example.test", KEY, None, "ashwin", "comma-separated"),
])
def test_what_won_t_work_is_refused_before_anything_is_saved(url, key, cable, allowed, says):
    with pytest.raises(SetupError, match=re.escape(says)):
        settings(url, key, cable, allowed)


def test_spaces_in_the_allowlist_are_dropped():
    assert settings("https://chat.example.test", KEY, allowed_users="1, 42")["SABHA_ALLOWED_USERS"] == "1,42"
