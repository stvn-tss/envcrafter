"""Turns a validated blueprint into the Compose file that is actually deployed.

This is the only place where networks, names and Traefik routing are defined,
and it only uses values produced by trusted code (validated slugs, settings):
* every service joins `ec-<project>-internal` (`internal: true`: no Internet);
* `ec-<project>-egress` exists only if a service asked for it and was allowed;
* exposed services also join `ec-<project>-edge`, the only network Traefik
  shares with the project, so other projects stay unreachable;
* every object carries the `envcrafter.*` labels the engine and Traefik filter on.
"""

from typing import Any

from app.models.stack import StackBlueprint
from app.models.template import ExposedPort

MANAGED_LABEL = "envcrafter.managed"
PROJECT_LABEL = "envcrafter.project"


def compose_project_name(project: str) -> str:
    return f"ec-{project}"


def edge_network_name(project: str) -> str:
    return f"ec-{project}-edge"


def web_hostname(project: str, domain: str, service: str | None = None) -> str:
    """Main UI: <project>.<domain>; secondary UIs: <service>.<project>.<domain>."""
    return f"{service}.{project}.{domain}" if service else f"{project}.{domain}"


def web_hostnames(blueprint: StackBlueprint, project: str, domain: str) -> dict[str, str]:
    return {
        exposed.service: web_hostname(project, domain, None if index == 0 else exposed.service)
        for index, exposed in enumerate(blueprint.expose)
    }


def render_compose(blueprint: StackBlueprint, *, project: str, domain: str) -> dict[str, Any]:
    base_labels = {MANAGED_LABEL: "true", PROJECT_LABEL: project}
    exposed = {item.service: item for item in blueprint.expose}
    hostnames = web_hostnames(blueprint, project, domain)

    services: dict[str, Any] = {}
    for name, service in blueprint.compose.services.items():
        rendered = service.model_dump(exclude_none=True)
        service_networks = ["internal"]
        if "egress" in service.networks:
            service_networks.append("egress")
        if name in exposed:
            service_networks.append("edge")
        rendered["networks"] = service_networks
        rendered.setdefault("restart", "unless-stopped")
        rendered.setdefault("security_opt", ["no-new-privileges:true"])
        rendered["labels"] = {
            **(service.labels or {}),
            **base_labels,
            "envcrafter.service": name,
            **(_traefik_labels(project, exposed[name], hostnames[name]) if name in exposed else {}),
        }
        services[name] = rendered

    networks: dict[str, Any] = {
        "internal": {"name": f"ec-{project}-internal", "internal": True, "labels": base_labels}
    }
    if exposed:
        # Traefik is attached to this network by the engine after creation.
        networks["edge"] = {
            "name": edge_network_name(project),
            "internal": True,
            "labels": base_labels,
        }
    if blueprint.uses_egress:
        networks["egress"] = {"name": f"ec-{project}-egress", "labels": base_labels}

    document: dict[str, Any] = {
        "name": compose_project_name(project),
        "services": services,
        "networks": networks,
    }
    if blueprint.compose.volumes:
        document["volumes"] = {name: {"labels": base_labels} for name in blueprint.compose.volumes}
    return document


def _traefik_labels(project: str, exposed: ExposedPort, hostname: str) -> dict[str, str]:
    router = f"{compose_project_name(project)}-{exposed.service}"
    return {
        "traefik.enable": "true",
        "traefik.docker.network": edge_network_name(project),
        f"traefik.http.routers.{router}.rule": f"Host(`{hostname}`)",
        f"traefik.http.routers.{router}.entrypoints": "web",
        f"traefik.http.routers.{router}.service": router,
        f"traefik.http.services.{router}.loadbalancer.server.port": str(exposed.port),
    }
