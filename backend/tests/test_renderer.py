from app.core.config import Settings
from app.models.stack import StackBlueprint
from app.policy.images import ImageAllowlist
from app.services.template_catalog import TemplateCatalog
from app.workspace.renderer import render_compose


def _blueprint(allowlist: ImageAllowlist, template_id: str) -> StackBlueprint:
    template = TemplateCatalog.load(Settings().templates_dir, allowlist).get(template_id)
    assert template is not None
    return StackBlueprint(
        title=template.manifest.name,
        compose=template.compose,
        expose=tuple(template.manifest.expose),
        secrets=tuple(template.manifest.secrets),
    )


def test_isolated_template_has_no_egress(allowlist: ImageAllowlist) -> None:
    document = render_compose(_blueprint(allowlist, "dvwa"), project="lab", domain="localhost")

    assert set(document["networks"]) == {"internal", "edge"}
    assert document["networks"]["internal"]["internal"] is True
    assert document["networks"]["edge"]["internal"] is True
    # Only the exposed service shares a network with Traefik.
    assert document["services"]["dvwa"]["networks"] == ["internal", "edge"]
    assert document["services"]["mariadb"]["networks"] == ["internal"]
    assert "traefik.enable" not in document["services"]["mariadb"]["labels"]


def test_every_service_is_labelled_and_hardened(allowlist: ImageAllowlist) -> None:
    document = render_compose(_blueprint(allowlist, "glpi"), project="demo", domain="localhost")

    for service in document["services"].values():
        assert service["labels"]["envcrafter.managed"] == "true"
        assert service["labels"]["envcrafter.project"] == "demo"
        assert service["security_opt"] == ["no-new-privileges:true"]
    assert document["name"] == "ec-demo"


def test_secondary_web_uis_get_their_own_host(allowlist: ImageAllowlist) -> None:
    document = render_compose(
        _blueprint(allowlist, "media-stack"), project="tv", domain="localhost"
    )
    labels = document["services"]["sonarr"]["labels"]

    assert labels["traefik.http.routers.ec-tv-sonarr.rule"] == "Host(`sonarr.tv.localhost`)"
    assert labels["traefik.docker.network"] == "ec-tv-edge"
    jellyfin = document["services"]["jellyfin"]["labels"]
    assert jellyfin["traefik.http.routers.ec-tv-jellyfin.rule"] == "Host(`tv.localhost`)"
    assert document["networks"]["egress"] == {
        "name": "ec-tv-egress",
        "labels": {"envcrafter.managed": "true", "envcrafter.project": "tv"},
    }
