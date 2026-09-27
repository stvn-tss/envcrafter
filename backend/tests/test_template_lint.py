"""The template lint command: the startup checks, template by template, plus the schema."""

import json
import re
import shutil
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.template import is_valid_secret_name
from app.policy.images import ImageAllowlist
from app.tools.template_lint import SCHEMA_FILE, lint, main, manifest_schema

REAL_TEMPLATES = Settings().templates_dir
ALL = {
    "glpi",
    "zabbix",
    "uptime-kuma",
    "audiobookshelf",
    "media-stack",
    "dvwa",
    "owasp-juice-shop",
    "forgejo",
    "mailpit",
}
EVIL = "  evil:\n    image: docker.io/library/alpine:3.20\n    privileged: true\n"


def test_the_real_catalog_is_clean(allowlist: ImageAllowlist) -> None:
    passed, problems = lint(REAL_TEMPLATES, allowlist)
    assert problems == []
    assert set(passed) == ALL


def test_one_broken_template_does_not_hide_the_others(
    tmp_path: Path, allowlist: ImageAllowlist
) -> None:
    root = tmp_path / "templates"
    shutil.copytree(REAL_TEMPLATES, root)
    compose = root / "security-lab" / "dvwa" / "compose.yaml"
    text = compose.read_text(encoding="utf-8")
    compose.write_text(text.replace("services:\n", "services:\n" + EVIL, 1), encoding="utf-8")

    passed, problems = lint(root, allowlist)

    assert set(passed) == ALL - {"dvwa"}
    assert len(problems) == 1
    assert "dvwa" in problems[0]


def test_the_committed_schema_is_up_to_date() -> None:
    committed = json.loads((REAL_TEMPLATES / SCHEMA_FILE).read_text(encoding="utf-8"))
    assert committed == manifest_schema()


def test_the_schema_reserves_the_builtin_prefix() -> None:
    items = manifest_schema()["properties"]["secrets"]["items"]
    assert items["pattern"].startswith("^(?!EC_)")


@pytest.mark.parametrize(
    "name",
    [
        "DB_PASSWORD",
        "EC_TZ",
        "EC_",
        "ECHO_KEY",
        "AB",
        "db_password",
        "A" * 64,
        "A" * 65,
        "DB_PASSWORD\n",
    ],
)
def test_the_schema_pattern_agrees_with_the_validator(name: str) -> None:
    pattern = manifest_schema()["properties"]["secrets"]["items"]["pattern"]
    assert bool(re.fullmatch(pattern, name)) == is_valid_secret_name(name), name


def test_a_file_that_is_not_utf8_is_reported_not_raised(
    tmp_path: Path, allowlist: ImageAllowlist
) -> None:
    root = tmp_path / "templates"
    shutil.copytree(REAL_TEMPLATES, root)
    manifest = root / "security-lab" / "dvwa" / "manifest.yaml"
    # A Latin-1 "é" (0xE9) is not valid UTF-8.
    manifest.write_bytes(manifest.read_bytes() + b"# caf" + bytes([0xE9]) + b"\n")

    passed, problems = lint(root, allowlist)

    assert set(passed) == ALL - {"dvwa"}
    assert len(problems) == 1
    assert "not valid UTF-8" in problems[0]


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--templates", str(REAL_TEMPLATES)]) == 0
    empty = tmp_path / "none"
    empty.mkdir()
    assert main(["--templates", str(empty)]) == 1
    copy = tmp_path / "copy"
    shutil.copytree(REAL_TEMPLATES, copy)
    (copy / SCHEMA_FILE).unlink()
    assert main(["--templates", str(copy)]) == 1
    assert main(["--templates", str(copy), "--write-schema"]) == 0
    assert f"{len(ALL)} template(s) valid, 0 problem(s)" in capsys.readouterr().out
