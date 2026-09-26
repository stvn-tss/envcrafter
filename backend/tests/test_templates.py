import shutil
import struct
import zlib
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.policy.images import ImageAllowlist
from app.services.template_catalog import MAX_LOGO_BYTES, TemplateCatalog, TemplateCatalogError
from tests.conftest import ClientFactory

REAL_TEMPLATES = Settings().templates_dir
SHOP = "security-lab/owasp-juice-shop"


def png_bytes() -> bytes:
    """A genuine 1x1 PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    pixels = zlib.compress(b"\x00\x00\x00\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


def catalog_with(
    tmp_path: Path, *, logo_name: str | None = "logo.png", logo: bytes | None = None
) -> Path:
    """A one-template catalog (Juice Shop) whose manifest declares `logo_name`."""
    root = tmp_path / "templates"
    target = root / SHOP
    shutil.copytree(REAL_TEMPLATES / SHOP, target)
    if logo_name is not None:
        manifest = yaml.safe_load((target / "manifest.yaml").read_text(encoding="utf-8"))
        manifest["logo"] = logo_name
        (target / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    if logo is not None and logo_name is not None:
        (target / logo_name).write_bytes(logo)
    return root


def test_vulnerable_flag_comes_from_the_allowlist(client: TestClient) -> None:
    templates = {t["id"]: t for t in client.get("/api/templates").json()["templates"]}
    assert templates["dvwa"]["vulnerable"] is True
    assert templates["owasp-juice-shop"]["vulnerable"] is True
    assert templates["glpi"]["vulnerable"] is False
    assert templates["media-stack"]["vulnerable"] is False


def test_every_template_declares_its_footprint_and_no_brand_logo(client: TestClient) -> None:
    for template in client.get("/api/templates").json()["templates"]:
        footprint = template["footprint"]
        assert set(footprint) == {"download_mb", "memory_mb", "first_start_seconds"}
        assert all(isinstance(value, int) and value > 0 for value in footprint.values())
        assert template["logo_url"] is None  # no third-party logo ships in the repository


def test_logo_is_served_from_memory_with_hardened_headers(
    make_client: ClientFactory, settings: Settings, tmp_path: Path
) -> None:
    settings.templates_dir = catalog_with(tmp_path, logo=png_bytes())
    client = make_client()
    [template] = client.get("/api/templates").json()["templates"]
    assert template["logo_url"] == "/api/templates/owasp-juice-shop/logo"

    response = client.get(template["logo_url"])
    assert response.status_code == 200
    assert response.content == png_bytes()
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["cache-control"] == "no-cache"

    cached = client.get(template["logo_url"], headers={"If-None-Match": response.headers["etag"]})
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["x-content-type-options"] == "nosniff"
    assert cached.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert cached.headers["cross-origin-resource-policy"] == "same-origin"


def test_logo_route_validates_the_template_id(client: TestClient) -> None:
    assert client.get("/api/templates/glpi/logo").status_code == 404  # no logo declared
    assert client.get("/api/templates/nope/logo").status_code == 404
    assert client.get("/api/templates/Bad_Id/logo").status_code == 422
    assert client.get("/api/templates/..%2F..%2Fetc/logo").status_code in {404, 422}


@pytest.mark.parametrize(
    ("logo_name", "content", "message"),
    [
        ("logo.png", b"GIF89a" + b"\x00" * 32, "does not match"),
        ("logo.webp", png_bytes(), "does not match"),
        ("logo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_LOGO_BYTES, "exceeds"),
        ("logo.png", None, "regular file"),  # declared but missing
    ],
    # Explicit ids: the "oversized" case's 64 KiB payload would otherwise be
    # escaped into the auto-generated node id, which Windows' pytest chokes on
    # (PYTEST_CURRENT_TEST exceeds the 32767-character environment value limit).
    ids=["wrong-signature", "wrong-extension", "oversized", "missing-file"],
)
def test_invalid_logos_stop_the_catalog(
    tmp_path: Path,
    allowlist: ImageAllowlist,
    logo_name: str,
    content: bytes | None,
    message: str,
) -> None:
    root = catalog_with(tmp_path, logo_name=logo_name, logo=content)
    with pytest.raises(TemplateCatalogError, match=message):
        TemplateCatalog.load(root, allowlist)


def test_svg_logos_are_refused(tmp_path: Path, allowlist: ImageAllowlist) -> None:
    # An SVG is a document that can carry scripts: the manifest schema refuses it.
    root = catalog_with(tmp_path, logo_name="logo.svg", logo=b"<svg onload='alert(1)'/>")
    with pytest.raises(TemplateCatalogError, match="logo"):
        TemplateCatalog.load(root, allowlist)


def test_symlinked_logos_are_refused(tmp_path: Path, allowlist: ImageAllowlist) -> None:
    root = catalog_with(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(png_bytes())
    try:
        (root / SHOP / "logo.png").symlink_to(outside)
    except OSError:
        pytest.skip("creating symlinks needs extra privileges on this platform")
    with pytest.raises(TemplateCatalogError, match="regular file"):
        TemplateCatalog.load(root, allowlist)


def test_footprint_is_required(tmp_path: Path, allowlist: ImageAllowlist) -> None:
    root = catalog_with(tmp_path, logo_name=None)
    path = root / SHOP / "manifest.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    del manifest["footprint"]
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    with pytest.raises(TemplateCatalogError, match="footprint"):
        TemplateCatalog.load(root, allowlist)


def test_every_web_interface_has_a_healthcheck(allowlist: ImageAllowlist) -> None:
    """Without one, `compose up --wait` reports a web UI as ready before it answers."""
    catalog = TemplateCatalog.load(REAL_TEMPLATES, allowlist)
    missing = [
        f"{template.manifest.id}/{exposed.service}"
        for template in catalog.all()
        for exposed in template.manifest.expose
        if template.compose.services[exposed.service].healthcheck is None
    ]
    assert missing == []


@pytest.mark.parametrize("name", ["EC_TZ", "EC_CUSTOM"])
def test_template_secrets_cannot_use_the_reserved_prefix(
    tmp_path: Path, allowlist: ImageAllowlist, name: str
) -> None:
    root = catalog_with(tmp_path, logo_name=None)
    path = root / SHOP / "manifest.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest["secrets"] = [name]
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    with pytest.raises(TemplateCatalogError, match="reserved"):
        TemplateCatalog.load(root, allowlist)
