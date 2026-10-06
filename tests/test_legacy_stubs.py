"""The moved evaluation commands are stubs in the core CLI (see legacy/README.md)."""

import pytest

from agr import cli


@pytest.mark.parametrize("command", ["eval", "benchmark", "audit-pack", "publish-evaluation"])
def test_moved_commands_print_the_pointer_and_exit_2(command, capsys):
    # Old flags are tolerated so the pointer is shown instead of an argparse error.
    rc = cli.main([command, "--gold", "x", "--provider", "openai", "--whatever"])
    err = capsys.readouterr().err
    assert rc == 2
    assert f"python -m legacy {command}" in err
    assert "repository checkout" in err


def test_other_commands_still_reject_unknown_flags():
    with pytest.raises(SystemExit) as exc:
        cli.main(["runs", "--bogus"])
    assert exc.value.code == 2
