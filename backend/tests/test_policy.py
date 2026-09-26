"""Negative tests for the compose policy: every rule must reject what it targets."""

from typing import Any

import pytest

from app.core.config import Settings
from app.models.template import RESERVED_SECRET_PREFIX
from app.policy.compose_policy import (
    BUILTIN_VARIABLES,
    ComposePolicyError,
    PolicyContext,
    validate_compose,
)
from app.policy.images import ImageAllowlist, ImageAllowlistError, parse_image_ref
from app.policy.yaml_loader import StrictYAMLError, load_yaml
from app.services.template_catalog import TemplateCatalog

DB = "mariadb:11.4.13"


def _context(allowlist: ImageAllowlist, **overrides: Any) -> PolicyContext:
    return PolicyContext(
        allowlist=allowlist,
        allow_egress=overrides.get("allow_egress", False),
        secret_names=frozenset(overrides.get("secrets", {"DB_PASSWORD"})),
    )


def _stack(service: dict[str, Any], **top_level: Any) -> dict[str, Any]:
    return {"services": {"app": {"image": DB, **service}}, **top_level}


def _violations(allowlist: ImageAllowlist, document: dict[str, Any], **ctx: Any) -> str:
    with pytest.raises(ComposePolicyError) as exc_info:
        validate_compose(document, _context(allowlist, **ctx))
    return str(exc_info.value)


def test_every_shipped_template_passes_the_policy(allowlist: ImageAllowlist) -> None:
    catalog = TemplateCatalog.load(Settings().templates_dir, allowlist)
    assert len(catalog.all()) >= 6


def test_minimal_stack_is_accepted(allowlist: ImageAllowlist) -> None:
    document = _stack(
        {"environment": {"MARIADB_PASSWORD": "${DB_PASSWORD}"}, "volumes": ["data:/var/lib/mysql"]},
        volumes={"data": None},
    )
    spec = validate_compose(document, _context(allowlist))
    assert list(spec.services) == ["app"]


@pytest.mark.parametrize(
    ("service", "expected"),
    [
        ({"privileged": True}, "privileged containers"),
        ({"cap_add": ["SYS_ADMIN"]}, "adding Linux capabilities"),
        ({"devices": ["/dev/kvm:/dev/kvm"]}, "host devices"),
        ({"network_mode": "host"}, "EnvCrafter-managed networks"),
        ({"pid": "host"}, "PID namespace"),
        ({"ipc": "host"}, "IPC namespace"),
        ({"userns_mode": "host"}, "user namespace"),
        ({"ports": ["0.0.0.0:3306:3306"]}, "host ports are never published"),
        ({"security_opt": ["seccomp:unconfined"]}, "only no-new-privileges"),
        ({"security_opt": ["apparmor=unconfined"]}, "only no-new-privileges"),
        ({"volumes": ["/var/run/docker.sock:/var/run/docker.sock"]}, "engine socket"),
        ({"volumes": ["/:/host"]}, "inside the project workspace"),
        ({"volumes": ["/etc:/data:ro"]}, "inside the project workspace"),
        ({"volumes": ["./data/../../etc:/data"]}, "inside the project workspace"),
        ({"volumes": ["undeclared:/data"]}, "is not declared"),
        ({"volumes": [{"type": "bind", "source": "/root", "target": "/r"}]}, "inside the project"),
        ({"image": "mariadb:latest"}, "not on the image allow-list"),
        ({"image": "evil/miner:1.0"}, "not on the image allow-list"),
        ({"image": "${IMAGE}"}, "interpolation is not allowed in this field"),
        ({"environment": {"X": "${HOME}"}}, "unknown variable ${HOME}"),
        ({"environment": {"X": "${DB_PASSWORD:-fallback}"}}, "modifiers"),
        ({"environment": {"X": "cost: $5"}}, "malformed variable interpolation"),
        ({"networks": ["egress"]}, "outbound Internet access is not allowed"),
        ({"depends_on": ["ghost"]}, "unknown service"),
        ({"labels": {"traefik.http.routers.x.rule": "Host(`bank.localhost`)"}}, "reserved"),
        ({"entrypoint": ["/bin/sh"]}, "unsupported key"),
        ({"build": "."}, "building images is not allowed"),
    ],
)
def test_service_level_rules(
    allowlist: ImageAllowlist, service: dict[str, Any], expected: str
) -> None:
    assert expected in _violations(allowlist, _stack(service))


@pytest.mark.parametrize(
    ("top_level", "expected"),
    [
        ({"networks": {"default": {"external": True}}}, "networks are generated"),
        ({"name": "someone-else"}, "project name is generated"),
        ({"x-anything": {}}, "extension fields"),
        (
            {"volumes": {"data": {"driver_opts": {"type": "none", "o": "bind", "device": "/"}}}},
            "volume options are not configurable",
        ),
    ],
)
def test_top_level_rules(
    allowlist: ImageAllowlist, top_level: dict[str, Any], expected: str
) -> None:
    assert expected in _violations(allowlist, _stack({}, **top_level))


def test_vulnerable_images_never_get_egress(allowlist: ImageAllowlist) -> None:
    document = {
        "services": {"shop": {"image": "bkimminich/juice-shop:v20.2.0", "networks": ["egress"]}}
    }
    assert "never get Internet access" in _violations(allowlist, document, allow_egress=True)


def test_yaml_loader_rejects_aliases_and_duplicates() -> None:
    with pytest.raises(StrictYAMLError, match="aliases"):
        load_yaml("base: &b {privileged: false}\nservice:\n  <<: *b\n")
    with pytest.raises(StrictYAMLError, match="duplicate key"):
        load_yaml("privileged: false\nprivileged: true\n")
    with pytest.raises(StrictYAMLError, match="exceeds"):
        load_yaml("a: " + "x" * 70_000)


@pytest.mark.parametrize(
    ("reference", "canonical"),
    [
        ("mariadb:11.4.13", "docker.io/library/mariadb:11.4.13"),
        ("glpi/glpi:11.0.9", "docker.io/glpi/glpi:11.0.9"),
        ("index.docker.io/library/mariadb:11.4.13", "docker.io/library/mariadb:11.4.13"),
        ("lscr.io/linuxserver/sonarr:4.0.20", "lscr.io/linuxserver/sonarr:4.0.20"),
    ],
)
def test_image_references_are_normalized(reference: str, canonical: str) -> None:
    assert str(parse_image_ref(reference)) == canonical


def test_allowlist_rejects_floating_tags(tmp_path: Any) -> None:
    path = tmp_path / "allowlist.yaml"
    path.write_text(
        "images:\n  - ref: docker.io/library/mariadb:latest\n    title: t\n    description: d\n",
        encoding="utf-8",
    )
    with pytest.raises(ImageAllowlistError, match="pinned"):
        ImageAllowlist.load(path)


def test_every_builtin_variable_uses_the_reserved_prefix() -> None:
    """Secrets can never take a built-in name as long as builtins keep the prefix."""
    assert all(name.startswith(RESERVED_SECRET_PREFIX) for name in BUILTIN_VARIABLES)
