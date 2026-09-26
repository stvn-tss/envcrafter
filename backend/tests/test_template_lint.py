"""The template lint command: the startup checks, template by template, plus the schema."""

import json
import shutil
from pathlib import Path

import pytest

from app.core.config import Settings
from app.policy.images import ImageAllowlist
from app.tools.template_lint import SCHEMA_FILE, lint, main, manifest_schema

REAL_TEMPLATES = Settings().templates_dir
ALL = {"glpi", "zabbix", "audiobookshelf", "media-stack", "dvwa", "owasp-juice-shop"}
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
    assert "6 template(s) valid, 0 problem(s)" in capsys.readouterr().out
