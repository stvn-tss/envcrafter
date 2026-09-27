"""Compose `configs` stay refused, and the policy says why.

Compose copies a config into its container through the archive endpoint
(`PUT /containers/{id}/archive`). The API's socket proxy keeps that endpoint closed: it
would also let the API read or write any file in any container. The prototype of roadmap
decision 27 hit exactly that on the real control plane (403 from the proxy), and the
end-to-end suite checks that the endpoint stays closed.
"""

from typing import Any

import pytest

from app.policy.compose_policy import ComposePolicyError, PolicyContext, validate_compose
from app.policy.images import ImageAllowlist

CONFIGURED: dict[str, Any] = {
    "services": {
        "cache": {
            "image": "docker.io/library/redis:8.10.2-alpine",
            "command": ["redis-server", "/usr/local/etc/redis/redis.conf"],
            "configs": [{"source": "redis", "target": "/usr/local/etc/redis/redis.conf"}],
        }
    },
    "configs": {"redis": {"content": 'port 6380\nsave ""\n'}},
}


def test_configs_are_refused_with_the_reason(allowlist: ImageAllowlist) -> None:
    with pytest.raises(ComposePolicyError) as exc_info:
        validate_compose(CONFIGURED, PolicyContext(allowlist=allowlist, allow_egress=False))
    message = str(exc_info.value)
    assert "configs are not supported" in message
    assert "archive endpoint" in message
