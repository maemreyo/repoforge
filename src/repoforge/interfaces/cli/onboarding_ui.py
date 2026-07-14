"""Optional interactive terminal backends for guided onboarding."""

from __future__ import annotations

import getpass
import importlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from typing import Any, Protocol, TextIO


@dataclass(frozen=True, slots=True)
class ChoiceItem:
    value: str
    label: str
    description: str = ""
    selected: bool = False
    disabled: bool = False


ChoiceLike = ChoiceItem | str


class UiBackendUnavailable(RuntimeError):
    """Raised when an explicitly requested optional UI backend is unavailable."""


class OnboardingUI(Protocol):
    @property
    def interactive(self) -> bool: ...

    @property
    def backend_name(self) -> str: ...

    def show_json(self, event: object) -> None: ...

    def stage(self, *, index: int, total: int, title: str) -> None: ...

    def panel(self, *, title: str, lines: tuple[str, ...]) -> None: ...

    def table(
        self,
        *,
        title: str,
        headers: tuple[str, ...],
        rows: tuple[tuple[str, ...], ...],
    ) -> None: ...

    def code(self, *, title: str, text: str, lexer: str = "text") -> None: ...

    def choose(
        self,
        *,
        prompt: str,
        choices: tuple[ChoiceLike, ...],
        default: str | None = None,
    ) -> str: ...

    def select_many(self, *, prompt: str, choices: tuple[ChoiceItem, ...]) -> tuple[str, ...]: ...

    def ask(self, *, prompt: str, secret: bool = False, default: str | None = None) -> str: ...

    def confirm(self, *, prompt: str, default: bool = False) -> bool: ...


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _is_interactive(stdin: TextIO, stderr: TextIO) -> bool:
    return stdin.isatty() and stderr.isatty()


def _normalize_choice(choice: ChoiceLike) -> ChoiceItem:
    return choice if isinstance(choice, ChoiceItem) else ChoiceItem(choice, choice)


def _choice_text(choice: ChoiceLike) -> str:
    normalized = _normalize_choice(choice)
    if normalized.description:
        return f"{normalized.label} — {normalized.description}"
    return normalized.label


def _load_attribute(module_name: str, attribute: str) -> Any:
    module = importlib.import_module(module_name)
    return getattr(module, attribute)


class PlainOnboardingUI:
    def __init__(self, stdin: TextIO, stdout: TextIO, stderr: TextIO):
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = stderr

    @property
    def interactive(self) -> bool:
        return _is_interactive(self._stdin, self._stderr)

    @property
    def backend_name(self) -> str:
        return "plain"

    def show_json(self, event: object) -> None:
        print(
            json.dumps(event, indent=2, ensure_ascii=False, default=str),
            file=self._stdout,
        )

    def show(self, event: object) -> None:
        """Backward-compatible alias for the original terminal adapter."""
        self.show_json(event)

    def stage(self, *, index: int, total: int, title: str) -> None:
        print(f"\n[{index}/{total}] {title}", file=self._stderr)
        print("-" * max(16, len(title) + 8), file=self._stderr)

    def panel(self, *, title: str, lines: tuple[str, ...]) -> None:
        print(f"\n=== {title} ===", file=self._stderr)
        for line in lines:
            print(f"  {line}", file=self._stderr)

    def table(
        self,
        *,
        title: str,
        headers: tuple[str, ...],
        rows: tuple[tuple[str, ...], ...],
    ) -> None:
        self.panel(title=title, lines=())
        if not rows:
            print("  (none)", file=self._stderr)
            return
        widths = [len(header) for header in headers]
        for row in rows:
            for index, value in enumerate(row):
                if index < len(widths):
                    widths[index] = min(48, max(widths[index], len(value)))
        rendered_header = " | ".join(
            header.ljust(widths[index]) for index, header in enumerate(headers)
        )
        print(f"  {rendered_header}", file=self._stderr)
        separator = "-+-".join("-" * width for width in widths)
        print(f"  {separator}", file=self._stderr)
        for row in rows:
            rendered = " | ".join(
                value[: widths[index]].ljust(widths[index]) for index, value in enumerate(row)
            )
            print(f"  {rendered}", file=self._stderr)

    def code(self, *, title: str, text: str, lexer: str = "text") -> None:
        del lexer
        self.panel(title=title, lines=tuple(text.rstrip().splitlines()) or ("(empty)",))

    def choose(
        self,
        *,
        prompt: str,
        choices: tuple[ChoiceLike, ...],
        default: str | None = None,
    ) -> str:
        normalized = tuple(_normalize_choice(choice) for choice in choices)
        enabled = tuple(choice for choice in normalized if not choice.disabled)
        values = {choice.value: choice for choice in enabled}
        while True:
            for index, choice in enumerate(enabled, start=1):
                suffix = " (default)" if choice.value == default else ""
                print(f"  {index}. {_choice_text(choice)}{suffix}", file=self._stderr)
            print(prompt, file=self._stderr, end=": ", flush=True)
            raw = self._stdin.readline().strip()
            if not raw and default in values:
                return str(default)
            if raw in values:
                return raw
            if raw.isdigit() and 1 <= int(raw) <= len(enabled):
                return enabled[int(raw) - 1].value
            print("Choose one listed value or number.", file=self._stderr)

    def select_many(self, *, prompt: str, choices: tuple[ChoiceItem, ...]) -> tuple[str, ...]:
        enabled = tuple(choice for choice in choices if not choice.disabled)
        defaults = tuple(choice.value for choice in enabled if choice.selected)
        if not enabled:
            return ()
        values = {choice.value for choice in enabled}
        while True:
            for index, choice in enumerate(enabled, start=1):
                marker = "x" if choice.selected else " "
                print(f"  {index}. [{marker}] {_choice_text(choice)}", file=self._stderr)
            print(
                f"{prompt} [comma-separated numbers; Enter=defaults, all, none]",
                file=self._stderr,
                end=": ",
                flush=True,
            )
            raw = self._stdin.readline().strip()
            command = raw.lower()
            if not raw:
                return defaults
            if command in {"all", "a"}:
                return tuple(choice.value for choice in enabled)
            if command in {"none", "n"}:
                return ()
            selected: list[str] = []
            valid = True
            for token in (item.strip() for item in raw.split(",")):
                if token.isdigit() and 1 <= int(token) <= len(enabled):
                    selected.append(enabled[int(token) - 1].value)
                elif token in values:
                    selected.append(token)
                else:
                    valid = False
                    break
            if valid:
                selected_set = set(selected)
                return tuple(choice.value for choice in enabled if choice.value in selected_set)
            print("Select only listed values or numbers.", file=self._stderr)

    def ask(self, *, prompt: str, secret: bool = False, default: str | None = None) -> str:
        suffix = f" [{default}]" if default is not None else ""
        if secret:
            value = getpass.getpass(prompt + suffix + ": ", stream=self._stderr).strip()
        else:
            print(prompt + suffix, file=self._stderr, end=": ", flush=True)
            value = self._stdin.readline().strip()
        return value if value else (default or "")

    def confirm(self, *, prompt: str, default: bool = False) -> bool:
        suffix = "Y/n" if default else "y/N"
        while True:
            print(f"{prompt} [{suffix}]", file=self._stderr, end=": ", flush=True)
            value = self._stdin.readline().strip().lower()
            if not value:
                return default
            if value in {"y", "yes"}:
                return True
            if value in {"n", "no"}:
                return False
            print("Answer yes or no.", file=self._stderr)


class RichOnboardingUI(PlainOnboardingUI):
    def __init__(
        self,
        stdin: TextIO,
        stdout: TextIO,
        stderr: TextIO,
        *,
        enable_inquirer: bool = True,
    ) -> None:
        super().__init__(stdin, stdout, stderr)
        console_type = _load_attribute("rich.console", "Console")
        self._console: Any = console_type(
            file=stderr,
            force_terminal=stderr.isatty(),
            color_system="auto",
        )
        self._inquirer_enabled = (
            enable_inquirer
            and _module_available("InquirerPy")
            and stdin is sys.stdin
            and stderr is sys.stderr
        )

    @property
    def backend_name(self) -> str:
        return "rich+inquirer" if self._inquirer_enabled else "rich"

    def stage(self, *, index: int, total: int, title: str) -> None:
        self._console.rule(f"[bold][{index}/{total}] {title}[/bold]")

    def panel(self, *, title: str, lines: tuple[str, ...]) -> None:
        panel_type = _load_attribute("rich.panel", "Panel")
        self._console.print(panel_type("\n".join(lines) if lines else "(none)", title=title))

    def table(
        self,
        *,
        title: str,
        headers: tuple[str, ...],
        rows: tuple[tuple[str, ...], ...],
    ) -> None:
        table_type = _load_attribute("rich.table", "Table")
        table = table_type(*headers, title=title, expand=True)
        for row in rows:
            table.add_row(*row)
        if rows:
            self._console.print(table)
        else:
            self.panel(title=title, lines=("(none)",))

    def code(self, *, title: str, text: str, lexer: str = "text") -> None:
        panel_type = _load_attribute("rich.panel", "Panel")
        syntax_type = _load_attribute("rich.syntax", "Syntax")
        syntax = syntax_type(
            text or "(empty)\n",
            lexer,
            theme="ansi_dark",
            word_wrap=True,
        )
        self._console.print(panel_type(syntax, title=title))

    @staticmethod
    def _inquirer_choices(choices: tuple[ChoiceLike, ...]) -> list[object]:
        choice_type = _load_attribute("InquirerPy.base.control", "Choice")
        normalized = tuple(_normalize_choice(choice) for choice in choices)
        return [
            choice_type(
                value=choice.value,
                name=_choice_text(choice),
                enabled=choice.selected,
            )
            for choice in normalized
            if not choice.disabled
        ]

    @staticmethod
    def _inquirer() -> Any:
        return _load_attribute("InquirerPy", "inquirer")

    def choose(
        self,
        *,
        prompt: str,
        choices: tuple[ChoiceLike, ...],
        default: str | None = None,
    ) -> str:
        if self._inquirer_enabled:
            result = (
                self._inquirer()
                .select(
                    message=prompt,
                    choices=self._inquirer_choices(choices),
                    default=default,
                    cycle=False,
                )
                .execute()
            )
            return str(result)
        prompt_type = _load_attribute("rich.prompt", "Prompt")
        normalized = tuple(_normalize_choice(choice) for choice in choices)
        values = [choice.value for choice in normalized if not choice.disabled]
        return str(
            prompt_type.ask(
                prompt,
                choices=values,
                default=default if default is not None else ...,
                console=self._console,
                stream=self._stdin,
            )
        )

    def select_many(self, *, prompt: str, choices: tuple[ChoiceItem, ...]) -> tuple[str, ...]:
        if self._inquirer_enabled:
            result = (
                self._inquirer()
                .checkbox(
                    message=prompt,
                    choices=self._inquirer_choices(choices),
                    cycle=False,
                    instruction="(Space toggle, Ctrl+A select all, Ctrl+R invert, Enter confirm)",
                )
                .execute()
            )
            selected = {str(item) for item in result}
            return tuple(choice.value for choice in choices if choice.value in selected)
        return super().select_many(prompt=prompt, choices=choices)

    def ask(self, *, prompt: str, secret: bool = False, default: str | None = None) -> str:
        if self._inquirer_enabled:
            inquirer = self._inquirer()
            factory = inquirer.secret if secret else inquirer.text
            result = factory(message=prompt, default=default or "").execute()
            return str(result).strip()
        prompt_type = _load_attribute("rich.prompt", "Prompt")
        return str(
            prompt_type.ask(
                prompt,
                password=secret,
                default=default if default is not None else ...,
                console=self._console,
                stream=self._stdin,
            )
        ).strip()

    def confirm(self, *, prompt: str, default: bool = False) -> bool:
        if self._inquirer_enabled:
            return bool(self._inquirer().confirm(message=prompt, default=default).execute())
        confirm_type = _load_attribute("rich.prompt", "Confirm")
        return bool(
            confirm_type.ask(
                prompt,
                default=default,
                console=self._console,
                stream=self._stdin,
            )
        )


def build_onboarding_ui(
    mode: str,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> OnboardingUI:
    if mode not in {"auto", "rich", "plain"}:
        raise ValueError(f"Unsupported UI mode: {mode}")
    if mode == "plain" or not _is_interactive(stdin, stderr):
        return PlainOnboardingUI(stdin, stdout, stderr)
    rich_available = _module_available("rich")
    if mode == "rich" and not rich_available:
        raise UiBackendUnavailable(
            "Rich UI is unavailable in this installation. Reinstall RepoForge from the locked "
            "package or use `--ui plain`."
        )
    if rich_available:
        return RichOnboardingUI(stdin, stdout, stderr)
    return PlainOnboardingUI(stdin, stdout, stderr)
