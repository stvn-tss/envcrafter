"""Check the template catalog without starting the server.

    uv run python -m app.tools.template_lint [--templates DIR] [--write-schema]

Each template is checked on its own with the rules the application applies at startup
(manifest schema, compose policy, manifest/compose agreement, logo), so one broken
template does not hide the others. `templates/manifest.schema.json` gives editors
completion and validation for `manifest.yaml`; `--write-schema` regenerates it from the
TemplateManifest model, and the check fails while it is out of date.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.models.template import TemplateManifest
from app.policy.images import ImageAllowlist, ImageAllowlistError
from app.services.template_catalog import TemplateCatalogError, load_template

SCHEMA_FILE = "manifest.schema.json"
_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def manifest_schema() -> dict[str, Any]:
    return {"$schema": _DIALECT, **TemplateManifest.model_json_schema()}


def lint(templates_dir: Path, allowlist: ImageAllowlist) -> tuple[list[str], list[str]]:
    """(ids of the valid templates, problems found)."""
    passed: list[str] = []
    problems: list[str] = []
    seen: set[str] = set()
    manifests = sorted(templates_dir.glob("*/*/manifest.yaml"))
    if not manifests:
        problems.append(f"{templates_dir}: no <category>/<id>/manifest.yaml found")
    for manifest in manifests:
        try:
            template = load_template(manifest, allowlist)
        except TemplateCatalogError as exc:
            problems.append(str(exc))
            continue
        template_id = template.manifest.id
        if template_id in seen:
            problems.append(f"{manifest.parent}: duplicate template id '{template_id}'")
            continue
        seen.add(template_id)
        passed.append(template_id)
    schema = templates_dir / SCHEMA_FILE
    try:
        current = json.loads(schema.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = None
    if current != manifest_schema():
        problems.append(f"{schema}: missing or out of date, run with --write-schema")
    return passed, problems


def main(argv: list[str] | None = None) -> int:
    settings = Settings()
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.template_lint", description="Check the EnvCrafter templates."
    )
    parser.add_argument("--templates", type=Path, default=settings.templates_dir)
    parser.add_argument(
        "--write-schema", action="store_true", help=f"regenerate {SCHEMA_FILE} first"
    )
    args = parser.parse_args(argv)
    if args.write_schema:
        text = json.dumps(manifest_schema(), indent=2) + "\n"
        (args.templates / SCHEMA_FILE).write_text(text, encoding="utf-8", newline="\n")
    try:
        allowlist = ImageAllowlist.load(settings.image_allowlist)
    except ImageAllowlistError as exc:
        print(f"error  {exc}", file=sys.stderr)
        return 1
    passed, problems = lint(args.templates, allowlist)
    for template_id in passed:
        print(f"ok     {template_id}")
    for problem in problems:
        print(f"error  {problem}", file=sys.stderr)
    print(f"{len(passed)} template(s) valid, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
