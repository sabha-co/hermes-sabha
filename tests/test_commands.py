from commands import MAX_COMMANDS, command_text, declarations


def test_sabha_s_own_names_and_repeats_are_skipped_and_the_list_stops_at_sabha_s_cap():
    slashes = [("hermes", "Talk to Hermes", "[subcommand]"), ("help", "Show commands", ""),
               ("new", "x" * 140, ""), ("new", "Alias", "")]
    slashes += [(f"skill{i}", "", "") for i in range(40)]
    declared = declarations(slashes)
    assert [d["name"] for d in declared[:3]] == ["hermes", "new", "skill0"]
    assert len(declared) == MAX_COMMANDS
    assert len(declared[1]["description"]) == 100
    assert declared[2]["description"] == "/skill0"


def test_a_command_is_its_own_text_and_hermes_names_another_or_asks():
    subcommands = {"new": "/new", "reset": "/new"}
    assert command_text({"command": "/model", "text": " gpt "}, subcommands) == "/model gpt"
    assert command_text({"command": "/hermes", "text": "reset now"}, subcommands) == "/new now"
    assert command_text({"command": "/hermes", "text": "what changed?"}, subcommands) == "what changed?"
    assert command_text({"command": "/hermes", "text": ""}, subcommands) == "/help"
