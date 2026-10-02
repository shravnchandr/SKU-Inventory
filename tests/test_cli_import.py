"""`main.py import` reports an unreadable or wrong file in plain English
(the parser's own message) rather than crashing with a traceback."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("content", [None, b"not a spreadsheet"])
def test_bad_file_gives_a_plain_message(tmp_path, content):
    path = tmp_path / "x.xls"
    if content is not None:
        path.write_bytes(content)
    r = subprocess.run(
        [sys.executable, "main.py", "--db", str(tmp_path / "t.db"), "import", str(path)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert r.returncode == 0
    assert "Not imported: Couldn't open this file as an Excel spreadsheet" in r.stdout
    assert "Traceback" not in r.stderr
