from cards import Card, Cards, approval, clarify, confirm, confirm_text


def test_an_approval_card_has_a_button_per_choice_carrying_the_session():
    blocks = approval("rm &lt;dir&gt;", [("Approve", "once", "primary"), ("Deny", "deny", "")], "agent:main:sabha:5")
    assert blocks[0]["text"] == {"type": "mrkdwn", "text": "rm &lt;dir&gt;"}
    assert blocks[1]["elements"] == [
        {"type": "button", "text": {"type": "plain_text", "text": "Approve"}, "action_id": "hermes_approve_once",
         "value": "agent:main:sabha:5", "style": "primary"},
        {"type": "button", "text": {"type": "plain_text", "text": "Deny"}, "action_id": "hermes_deny",
         "value": "agent:main:sabha:5"}]


def test_a_clarify_card_fits_sabha_s_ten_controls_with_other_last():
    blocks = clarify("Which <env>?", [""] + [f"env {i}" for i in range(12)], "c1")
    assert blocks[0]["text"]["text"] == "❓ Which &lt;env&gt;?"
    buttons = blocks[1]["elements"]
    assert len(buttons) == 10
    assert (buttons[0]["text"]["text"], buttons[0]["value"]) == ("Option 1", "c1|0")
    assert (buttons[-1]["action_id"], buttons[-1]["value"]) == ("hermes_clarify_other", "c1|other")


def test_a_clarify_card_is_found_by_its_clarify_id_once():
    store = Cards()
    store.add("70", Card("approval", "x", "5"))
    store.add("71", Card("clarify", "q", "5", clarify_id="c1"))
    assert store.pop_clarify("c1")[0] == "71"
    assert store.pop_clarify("c1") is None and store.get("70")


def test_a_confirm_card_offers_once_always_and_cancel_for_its_confirm_id():
    blocks = confirm(confirm_text("/new", "Drops <history>"), "7")
    assert blocks[0]["text"]["text"] == "Drops &lt;history&gt;"
    assert [(b["action_id"], b["value"]) for b in blocks[1]["elements"]] == [
        ("hermes_confirm_once", "7"), ("hermes_confirm_always", "7"), ("hermes_confirm_cancel", "7")]
