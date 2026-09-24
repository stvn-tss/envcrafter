"""Image references and the image allow-list.

Every container EnvCrafter runs, from a template or from the LLM, must use an
image listed in `image_allowlist.yaml`, compared on its fully normalized
reference (`mariadb:11.4.13` == `docker.io/library/mariadb:11.4.13`). Entries are
pinned to an exact tag or digest: bumping a version is a reviewed change.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.policy.yaml_loader import load_yaml

# Tags that move over time: an allow-listed name must always mean the same bits.
_FLOATING_TAGS = frozenset(
    {
        "latest",
        "nightly",
        "dev",
        "develop",
        "edge",
        "main",
        "master",
        "stable",
        "snapshot",
        "trunk",
        "unstable",
        "rolling",
    }
)
_FLOATING_SUFFIXES = ("-latest", "-nightly", "-dev", "-edge", "-snapshot", "-trunk")

_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_REPOSITORY_RE = re.compile(rf"^{_COMPONENT}(?:/{_COMPONENT})*$")
_REGISTRY_RE = re.compile(r"^(?:localhost|[a-z0-9-]+(?:\.[a-z0-9-]+)+)(?::\d{1,5})?$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


class ImageRefError(ValueError):
    pass


@dataclass(frozen=True)
class ImageRef:
    registry: str
    repository: str
    tag: str | None
    digest: str | None

    @property
    def name(self) -> str:
        return f"{self.registry}/{self.repository}"

    @property
    def is_pinned(self) -> bool:
        if self.digest is not None:
            return True
        if self.tag is None:
            return False
        tag = self.tag.lower()
        return tag not in _FLOATING_TAGS and not tag.endswith(_FLOATING_SUFFIXES)

    def __str__(self) -> str:
        text = self.name
        if self.tag:
            text += f":{self.tag}"
        if self.digest:
            text += f"@{self.digest}"
        return text


def parse_image_ref(value: str) -> ImageRef:
    """Normalize an image reference the way the Docker engine resolves it."""
    if not value or len(value) > 512 or not value.isascii() or value != value.strip():
        raise ImageRefError("malformed image reference")

    name, at, digest = value.partition("@")
    if at and not _DIGEST_RE.match(digest):
        raise ImageRefError("malformed image digest")

    tag: str | None = None
    if name.rfind(":") > name.rfind("/"):
        name, _, tag = name.rpartition(":")
        if not _TAG_RE.match(tag):
            raise ImageRefError("malformed image tag")

    first, slash, rest = name.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first, rest
    else:
        registry, repository = "docker.io", name
    if registry in {"index.docker.io", "registry-1.docker.io"}:
        registry = "docker.io"
    if registry == "docker.io" and "/" not in repository:
        repository = f"library/{repository}"

    if not _REGISTRY_RE.match(registry) or not _REPOSITORY_RE.match(repository):
        raise ImageRefError("malformed image name")
    return ImageRef(registry=registry, repository=repository, tag=tag, digest=digest or None)


class _AllowlistEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str
    title: Annotated[str, StringConstraints(min_length=1, max_length=60)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=400)]
    web_port: Annotated[int, Field(ge=1, le=65535)] | None = None
    # Intentionally vulnerable software: never granted outbound Internet access.
    vulnerable: bool = False


class _AllowlistFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    images: list[_AllowlistEntry]


@dataclass(frozen=True)
class AllowedImage:
    ref: ImageRef
    title: str
    description: str
    web_port: int | None
    vulnerable: bool


class ImageAllowlistError(ValueError):
    pass


class ImageAllowlist:
    def __init__(self, images: list[AllowedImage]) -> None:
        self._by_ref = {str(image.ref): image for image in images}

    @classmethod
    def load(cls, path: Path) -> "ImageAllowlist":
        """Blocking disk I/O: call through `asyncio.to_thread()` from async code."""
        try:
            data = _AllowlistFile.model_validate(load_yaml(path.read_text(encoding="utf-8")))
        except ValidationError as exc:
            raise ImageAllowlistError(f"{path}: {exc}") from None

        images: list[AllowedImage] = []
        for entry in data.images:
            ref = parse_image_ref(entry.ref)
            if not ref.is_pinned:
                raise ImageAllowlistError(f"{entry.ref}: allow-listed images must be pinned")
            if str(ref) != entry.ref:
                raise ImageAllowlistError(f"{entry.ref}: write it in canonical form {ref}")
            images.append(
                AllowedImage(ref, entry.title, entry.description, entry.web_port, entry.vulnerable)
            )
        return cls(images)

    def find(self, ref: ImageRef) -> AllowedImage | None:
        # Exact canonical match: tag AND digest must be exactly those reviewed.
        return self._by_ref.get(str(ref))

    def all(self) -> list[AllowedImage]:
        return list(self._by_ref.values())
