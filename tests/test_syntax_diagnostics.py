from __future__ import annotations

from collections.abc import Iterator

from repoforge.application.syntax_diagnostics import SyntaxDiagnosticAnalyzer
from repoforge.domain.syntax_diagnostics import SyntaxDiagnosticState, SyntaxSeverity


def test_valid_python_reports_ok() -> None:
    result = SyntaxDiagnosticAnalyzer().analyze({"src/value.py": b"def value():\n    return 1\n"})

    assert result.state is SyntaxDiagnosticState.OK
    assert result.parse_ok is True
    assert result.analyzed_paths == ("src/value.py",)
    assert result.unknown_paths == ()
    assert result.diagnostics == ()
    assert result.truncated is False


def test_malformed_python_reports_bounded_line_error_without_source_echo() -> None:
    source = b"def private_secret(:\n"
    result = SyntaxDiagnosticAnalyzer().analyze({"src/broken.py": source})

    assert result.state is SyntaxDiagnosticState.ERROR
    assert result.parse_ok is False
    assert result.analyzed_paths == ("src/broken.py",)
    assert result.unknown_paths == ()
    assert result.diagnostics
    first = result.diagnostics[0]
    assert first.path == "src/broken.py"
    assert first.line == 1
    assert first.severity is SyntaxSeverity.ERROR
    assert "private_secret" not in first.message
    assert source.decode("utf-8") not in repr(result)


def test_unsupported_grammar_reports_unknown_not_success() -> None:
    result = SyntaxDiagnosticAnalyzer().analyze({"README.md": b"# Heading\n"})

    assert result.state is SyntaxDiagnosticState.UNKNOWN
    assert result.parse_ok is None
    assert result.analyzed_paths == ()
    assert result.unknown_paths == ("README.md",)
    assert result.diagnostics == ()


def test_invalid_utf8_reports_unknown_without_source_echo() -> None:
    result = SyntaxDiagnosticAnalyzer().analyze({"src/binary.py": b"\xff\xfe\x00"})

    assert result.state is SyntaxDiagnosticState.UNKNOWN
    assert result.parse_ok is None
    assert result.unknown_paths == ("src/binary.py",)
    assert result.diagnostics == ()
    assert "\\xff" not in repr(result)


def test_error_takes_precedence_over_unknown_in_mixed_input() -> None:
    result = SyntaxDiagnosticAnalyzer().analyze(
        {
            "README.md": b"# Unsupported\n",
            "src/broken.py": b"def broken(:\n",
        }
    )

    assert result.state is SyntaxDiagnosticState.ERROR
    assert result.parse_ok is False
    assert result.analyzed_paths == ("src/broken.py",)
    assert result.unknown_paths == ("README.md",)
    assert result.diagnostics


def test_deleted_paths_are_ignored_and_deletion_only_is_ok() -> None:
    result = SyntaxDiagnosticAnalyzer().analyze({"src/deleted.py": None, "README.md": None})

    assert result.state is SyntaxDiagnosticState.OK
    assert result.parse_ok is True
    assert result.analyzed_paths == ()
    assert result.unknown_paths == ()
    assert result.diagnostics == ()


def test_diagnostics_are_capped_and_truncation_is_explicit() -> None:
    files = {f"src/broken_{index:03d}.py": b"def broken(:\n" for index in range(105)}

    result = SyntaxDiagnosticAnalyzer().analyze(files)

    assert result.state is SyntaxDiagnosticState.ERROR
    assert result.parse_ok is False
    assert len(result.diagnostics) == 100
    assert result.truncated is True
    assert tuple(item.path for item in result.diagnostics) == tuple(
        sorted(item.path for item in result.diagnostics)
    )


def test_observed_parse_budget_overrun_reports_unknown() -> None:
    ticks: Iterator[float] = iter((0.0, 0.2))
    analyzer = SyntaxDiagnosticAnalyzer(
        file_budget_seconds=0.1,
        monotonic=lambda: next(ticks),
    )

    result = analyzer.analyze({"src/value.py": b"value = 1\n"})

    assert result.state is SyntaxDiagnosticState.UNKNOWN
    assert result.parse_ok is None
    assert result.analyzed_paths == ()
    assert result.unknown_paths == ("src/value.py",)
    assert result.diagnostics == ()
