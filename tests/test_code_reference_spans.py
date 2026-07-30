"""Reference facts must locate a symbol precisely enough to rewrite it.

Prerequisite for #216 (`rename_symbol` / `move_symbol`). A reference fact used to carry
only a line number, which forces any consumer that wants to rewrite the symbol to re-find
it by matching text on that line. That is the same "missed a reference / wrong target"
defect class #216 exists to remove, so the fact now carries the column too.

The two cases a line number cannot resolve are the point of this file: a symbol that is a
substring of another identifier, and a symbol occurring more than once on one line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoforge.adapters.code_intelligence import TreeSitterCodeIntelligenceProvider
from repoforge.adapters.code_intelligence.syntax import SyntaxCodeIntelligenceProvider
from repoforge.domain.code_intelligence import (
    CodeIntelligenceRequest,
    CodeIntelligenceSnapshot,
    CodeLanguage,
    CodeReferenceFact,
)
from repoforge.domain.errors import RepoForgeError


def _snapshot() -> CodeIntelligenceSnapshot:
    return CodeIntelligenceSnapshot(
        repo_id="demo",
        workspace_id="workspace-1",
        head_sha="a" * 40,
        workspace_fingerprint="b" * 64,
    )


def _write(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _request(root: Path, files: dict[str, str], changed: str) -> CodeIntelligenceRequest:
    for path, content in files.items():
        _write(root, path, content)
    return CodeIntelligenceRequest(
        workspace_root=root.resolve(),
        snapshot=_snapshot(),
        paths=tuple(files),
        changed_paths=(changed,),
    )


def _slice(root: Path, fact: CodeReferenceFact) -> str:
    """Read back exactly the bytes the fact points at."""

    line = (root / fact.source_path).read_text(encoding="utf-8").splitlines()[fact.line - 1]
    start, end = fact.span
    return line.encode("utf-8")[start:end].decode("utf-8")


# ------------------------------------------------------------------- the domain fact


def test_the_span_is_derived_from_the_symbol_and_cannot_disagree_with_it() -> None:
    fact = CodeReferenceFact(CodeLanguage.PYTHON, "src/a.py", "user", 3, 8)
    assert fact.span == (8, 12)
    assert fact.end_column == 12


def test_a_multibyte_symbol_measures_its_span_in_bytes() -> None:
    fact = CodeReferenceFact(CodeLanguage.PYTHON, "src/a.py", "café", 1, 0)
    # Five bytes, four characters: a consumer slicing bytes must be given bytes.
    assert fact.span == (0, 5)


def test_column_zero_is_valid_and_a_negative_column_is_refused() -> None:
    assert CodeReferenceFact(CodeLanguage.PYTHON, "src/a.py", "x", 1, 0).column == 0
    with pytest.raises(RepoForgeError):
        CodeReferenceFact(CodeLanguage.PYTHON, "src/a.py", "x", 1, -1)


# -------------------------------------------- what a line number could not tell apart


def test_tree_sitter_span_distinguishes_a_symbol_from_a_longer_identifier(
    tmp_path: Path,
) -> None:
    files = {
        "src/models.py": "user = 1\n",
        "src/app.py": ("from .models import user\n\nusername = 'x'\nprint(user, username)\n"),
    }
    result = TreeSitterCodeIntelligenceProvider().analyze(
        _request(tmp_path, files, "src/models.py")
    )

    facts = [
        fact
        for fact in result.references
        if fact.source_path == "src/app.py" and fact.symbol == "user"
    ]
    assert facts, "the reference to `user` should be found"
    # Every reported span reads back as exactly `user` -- never the `user` inside
    # `username`, which a text search on the same line would have matched.
    for fact in facts:
        assert _slice(tmp_path, fact) == "user"


def test_tree_sitter_reports_each_occurrence_on_one_line_at_its_own_column(
    tmp_path: Path,
) -> None:
    files = {
        "src/models.py": "user = 1\n",
        "src/app.py": "from .models import user\n\nresult = combine(user, user)\n",
    }
    result = TreeSitterCodeIntelligenceProvider().analyze(
        _request(tmp_path, files, "src/models.py")
    )

    line_three = sorted(
        fact.column
        for fact in result.references
        if fact.source_path == "src/app.py" and fact.symbol == "user" and fact.line == 3
    )
    assert len(line_three) == 2, "both occurrences on the line must be reported"
    assert line_three[0] != line_three[1], "and at distinct columns"
    source = files["src/app.py"].splitlines()[2]
    for column in line_three:
        assert source[column : column + 4] == "user"


def test_the_syntax_fallback_also_reports_every_occurrence_with_its_column(
    tmp_path: Path,
) -> None:
    """The regex fallback used to record one fact per line, silently dropping the rest."""

    files = {
        "src/button.js": "export const Button = 1;\n",
        "src/app.js": "import { Button } from './button';\nrender(Button, Button);\n",
    }
    result = SyntaxCodeIntelligenceProvider().analyze(_request(tmp_path, files, "src/button.js"))

    columns = sorted(
        fact.column
        for fact in result.references
        if fact.source_path == "src/app.js" and fact.symbol == "Button" and fact.line == 2
    )
    assert len(columns) == 2
    source = files["src/app.js"].splitlines()[1]
    for column in columns:
        assert source[column : column + 6] == "Button"


def test_every_python_reference_span_reads_back_as_its_own_symbol(tmp_path: Path) -> None:
    """A blunt invariant over a whole file: no span may point at the wrong bytes."""

    files = {
        "src/models.py": "user = 1\naccount = 2\n",
        "src/app.py": (
            "from .models import user, account\n"
            "\n"
            "username = 'x'\n"
            "accounts = []\n"
            "def run():\n"
            "    return (user, account, user, accounts, username)\n"
        ),
    }
    result = TreeSitterCodeIntelligenceProvider().analyze(
        _request(tmp_path, files, "src/models.py")
    )

    app_facts = [fact for fact in result.references if fact.source_path == "src/app.py"]
    assert app_facts
    for fact in app_facts:
        assert _slice(tmp_path, fact) == fact.symbol
