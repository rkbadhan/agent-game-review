"""The core package must never depend on ``legacy/`` (see legacy/README.md)."""

import ast
import pathlib

AGR = pathlib.Path(__file__).resolve().parent.parent / "agr"


def test_agr_never_imports_legacy():
    offenders = []
    for path in sorted(AGR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            if any(n == "legacy" or n.startswith("legacy.") for n in names):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"agr must not import legacy: {offenders}"
