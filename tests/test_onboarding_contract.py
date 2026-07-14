from repoforge.interfaces.cli.contract import build_cli_release_contract


def test_cli_release_contract_includes_interactive_ui_options() -> None:
    commands = build_cli_release_contract()["commands"]
    assert isinstance(commands, dict)
    onboard = commands["onboard"]
    assert isinstance(onboard, dict)
    options = onboard["options"]
    assert isinstance(options, list)
    assert "--ui" in options
    assert "--defaults" in options
