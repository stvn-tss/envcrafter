"""Hardened YAML loading for every document EnvCrafter reads (manifests, compose files).

`yaml.safe_load` already refuses to build arbitrary Python objects. On top of it:
* aliases are rejected: they enable "billion laughs" expansion bombs and let a
  reviewer miss what a key really contains (`<<: *defaults` merges);
* duplicate keys are rejected: PyYAML silently keeps the last one, so a reviewer
  could approve `privileged: false` while a later `privileged: true` wins;
* documents are size-capped before parsing.
"""

from typing import Any

import yaml
from yaml.nodes import MappingNode

MAX_DOCUMENT_BYTES = 64 * 1024


class StrictYAMLError(ValueError):
    pass


class _StrictSafeLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            line = self.peek_event().start_mark.line + 1  # type: ignore[no-untyped-call]
            raise StrictYAMLError(f"YAML aliases are not allowed (line {line})")
        return super().compose_node(parent, index)

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:  # unhashable key: let PyYAML raise its own error
                break
            if duplicate:
                line = key_node.start_mark.line + 1
                raise StrictYAMLError(f"duplicate key {key!r} (line {line})")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_yaml(text: str, *, max_bytes: int = MAX_DOCUMENT_BYTES) -> Any:
    """Parse a single YAML document with the restrictions above."""
    if len(text.encode("utf-8")) > max_bytes:
        raise StrictYAMLError(f"document exceeds {max_bytes} bytes")
    loader = _StrictSafeLoader(text)
    try:
        return loader.get_single_data()
    except yaml.YAMLError as exc:
        raise StrictYAMLError(f"invalid YAML: {exc}") from None
    finally:
        loader.dispose()
