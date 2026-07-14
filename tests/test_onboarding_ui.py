from __future__ import annotations

import io

import pytest

import repoforge.interfaces.cli.onboarding_ui as onboarding_ui_module
from repoforge.interfaces.cli.onboarding_ui import (
    ChoiceItem,
    PlainOnboardingUI,
    RichOnboardingUI,
    UiBackendUnavailable,
    build_onboarding_ui,
)


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def test_auto_uses_plain_without_tty_and_does_not_probe_optional_backends(
    monkeypatch,
) -> None:
    def fail_probe(name: str) -> bool:
        raise AssertionError(f"optional backend probed: {name}")

    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding_ui._module_available", fail_probe
    )
    ui = build_onboarding_ui("auto", stdin=Pipe(), stdout=Pipe(), stderr=Pipe())
    assert isinstance(ui, PlainOnboardingUI)
    assert not ui.interactive


def test_plain_select_many_supports_defaults_all_and_numeric_selection() -> None:
    choices = (
        ChoiceItem("a", "Alpha", selected=True),
        ChoiceItem("b", "Beta"),
        ChoiceItem("c", "Gamma", selected=True),
    )
    assert PlainOnboardingUI(TTY("\n"), TTY(), TTY()).select_many(
        prompt="pick", choices=choices
    ) == ("a", "c")
    assert PlainOnboardingUI(TTY("all\n"), TTY(), TTY()).select_many(
        prompt="pick", choices=choices
    ) == ("a", "b", "c")
    assert PlainOnboardingUI(TTY("2,3\n"), TTY(), TTY()).select_many(
        prompt="pick", choices=choices
    ) == ("b", "c")


def test_explicit_rich_reports_install_action_when_rich_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding_ui._module_available",
        lambda name: False,
    )
    with pytest.raises(UiBackendUnavailable, match="--with rich --with InquirerPy"):
        build_onboarding_ui("rich", stdin=TTY(), stdout=TTY(), stderr=TTY())


def test_rich_backend_renders_panels_tables_and_diff_without_inquirer() -> None:
    stdout = TTY()
    stderr = TTY()
    ui = RichOnboardingUI(
        stdin=TTY("yes\n"),
        stdout=stdout,
        stderr=stderr,
        enable_inquirer=False,
    )
    ui.stage(index=1, total=6, title="Discovery")
    ui.panel(title="Summary", lines=("one", "two"))
    ui.table(title="Repos", headers=("ID", "Path"), rows=(("demo", "/repo"),))
    ui.code(title="Config diff", text="+added\n", lexer="diff")
    rendered = stderr.getvalue()
    assert "Discovery" in rendered
    assert "Summary" in rendered
    assert "demo" in rendered
    assert "+added" in rendered


def test_auto_uses_rich_on_tty_when_available(monkeypatch) -> None:
    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding_ui._module_available",
        lambda name: name == "rich",
    )
    ui = build_onboarding_ui("auto", stdin=TTY(), stdout=TTY(), stderr=TTY())
    assert isinstance(ui, RichOnboardingUI)


def test_plain_select_many_preserves_case_sensitive_choice_values() -> None:
    choices = (ChoiceItem("Repo-One", "Repository One"),)
    selected = PlainOnboardingUI(TTY("Repo-One\n"), TTY(), TTY()).select_many(
        prompt="pick", choices=choices
    )
    assert selected == ("Repo-One",)


def test_inquirer_backend_uses_arrow_select_and_preselected_checkboxes(monkeypatch) -> None:
    class FakeChoice:
        def __init__(self, value, *, name, enabled=False):
            self.value = value
            self.name = name
            self.enabled = enabled

    class FakePrompt:
        def __init__(self, result):
            self._result = result

        def execute(self):
            return self._result

    class FakeInquirer:
        def __init__(self):
            self.select_call = None
            self.checkbox_call = None

        def select(self, **kwargs):
            self.select_call = kwargs
            return FakePrompt("b")

        def checkbox(self, **kwargs):
            self.checkbox_call = kwargs
            return FakePrompt(["a"])

    fake_inquirer = FakeInquirer()
    monkeypatch.setattr(
        onboarding_ui_module,
        "_load_attribute",
        lambda module, attribute: FakeChoice
        if (module, attribute) == ("InquirerPy.base.control", "Choice")
        else None,
    )
    monkeypatch.setattr(
        RichOnboardingUI, "_inquirer", staticmethod(lambda: fake_inquirer)
    )
    ui = object.__new__(RichOnboardingUI)
    PlainOnboardingUI.__init__(ui, TTY(), TTY(), TTY())
    ui._console = None
    ui._inquirer_enabled = True

    assert ui.choose(
        prompt="pick",
        choices=(ChoiceItem("a", "Alpha"), ChoiceItem("b", "Beta")),
        default="b",
    ) == "b"
    assert fake_inquirer.select_call["cycle"] is False
    assert fake_inquirer.select_call["default"] == "b"

    selected = ui.select_many(
        prompt="choose",
        choices=(
            ChoiceItem("a", "Alpha", selected=True),
            ChoiceItem("b", "Beta"),
        ),
    )
    assert selected == ("a",)
    checkbox_choices = fake_inquirer.checkbox_call["choices"]
    assert [choice.enabled for choice in checkbox_choices] == [True, False]
    assert "Ctrl+A" in fake_inquirer.checkbox_call["instruction"]
