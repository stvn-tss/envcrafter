"""Aggregates `docker compose --progress json pull` events into user-facing progress.

Compose v5 prints one JSON object per line (format checked on Compose v5.5.1):
  {"id": "Image docker.io/library/mariadb:11.4.13", "status": "Working", "text": "Pulling"}
  {"id": "68fe9bff2ad4", "parent_id": "Image ...", "text": "Downloading",
   "current": 1048576, "total": 2211514, "percent": 47}
  {"id": "Image ...", "status": "Done", "text": "Pulled"}
  {"id": "Image ...", "status": "Error", "text": "Error", "details": "..."}
  {"error": true, "message": "..."}
The output is data: anything unexpected is ignored, and upstream error details only
reach the server logs.
"""

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_IMAGE_PREFIX = "Image "
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,511}$")
_LAYER_DONE = frozenset({"Download complete", "Pull complete", "Already exists"})
_MAX_LAYER_ID = 128


def _count(value: object) -> int | None:
    """A non-negative JSON integer (JSON booleans are not counts)."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def format_bytes(count: int) -> str:
    """Decimal units, like Docker: 1_048_576 -> "1.0 MB"."""
    if count < 1000:
        return f"{count} B"
    value = float(count)
    for unit in ("kB", "MB"):
        value /= 1000
        if value < 1000:
            return f"{value:.1f} {unit}"
    return f"{value / 1000:.1f} GB"


@dataclass
class _Layer:
    current: int = 0
    total: int = 0
    done: bool = False

    def received(self) -> int:
        return self.total if self.done and self.total else self.current


@dataclass
class _Image:
    state: str = "pulling"  # pulling | done | failed
    announced: bool = False
    layers: dict[str, _Layer] = field(default_factory=dict)

    def fraction(self) -> float:
        if self.state == "done":
            return 1.0
        sized = [layer for layer in self.layers.values() if layer.total > 0]
        total = sum(layer.total for layer in sized)
        if total == 0:
            return 0.0
        return min(sum(layer.received() for layer in sized) / total, 1.0)

    def received(self) -> int:
        return sum(layer.received() for layer in self.layers.values())


class PullProgressTracker:
    def __init__(self) -> None:
        self._images: dict[str, _Image] = {}
        self._percent = 0

    def feed(self, event: object) -> list[str]:
        """Apply one decoded JSON event; return the log lines it produces."""
        if not isinstance(event, dict):
            return []
        if event.get("error") is True:
            logger.warning("Image pull error: %s", str(event.get("message", ""))[:500])
            return []
        ident, text = event.get("id"), event.get("text")
        if not isinstance(ident, str) or not isinstance(text, str):
            return []
        if ident.startswith(_IMAGE_PREFIX):
            return self._image_event(ident.removeprefix(_IMAGE_PREFIX), event, text)
        parent = event.get("parent_id")
        if isinstance(parent, str) and parent.startswith(_IMAGE_PREFIX):
            self._layer_event(parent.removeprefix(_IMAGE_PREFIX), ident, text, event)
        return []

    def snapshot(self) -> tuple[int, str]:
        """(percent, message). The percent never goes backwards."""
        if not self._images:
            return self._percent, "Waiting for the registry"
        total = len(self._images)
        value = int(100 * sum(image.fraction() for image in self._images.values()) / total)
        self._percent = max(self._percent, min(value, 100))
        done = sum(1 for image in self._images.values() if image.state == "done")
        received = sum(image.received() for image in self._images.values())
        message = f"{done} of {total} images"
        if received:
            message += f" · {format_bytes(received)} downloaded"
        return self._percent, message

    def _image_event(self, ref: str, event: dict[object, object], text: str) -> list[str]:
        if not _REF_RE.match(ref):
            return []
        image = self._images.setdefault(ref, _Image())
        if event.get("status") == "Error":
            image.state = "failed"
            logger.warning("Could not pull %s: %s", ref, str(event.get("details", ""))[:500])
            return [f"Could not pull {ref}"]
        if text == "Pulled" and image.state != "done":
            image.state = "done"
            return [f"Pulled {ref}"]
        if text == "Pulling" and not image.announced:
            image.announced = True
            return [f"Pulling {ref}"]
        return []

    def _layer_event(self, ref: str, layer_id: str, text: str, event: dict[object, object]) -> None:
        if not _REF_RE.match(ref) or len(layer_id) > _MAX_LAYER_ID:
            return
        layer = self._images.setdefault(ref, _Image()).layers.setdefault(layer_id, _Layer())
        if text == "Downloading":
            total, current = _count(event.get("total")), _count(event.get("current"))
            if total:
                layer.total = total
            if current is not None:
                layer.current = current
        elif text in _LAYER_DONE:
            layer.done = True
