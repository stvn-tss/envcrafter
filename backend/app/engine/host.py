"""Memory and disk left on the Docker host, read from inside the API container.

Containers share the host's kernel (Docker Desktop's Linux VM, or the Linux host itself):
/proc/meminfo describes the memory every environment competes for, and the workspaces
volume lives on Docker's data disk, where images and volumes go too.
"""

import platform
import shutil
from pathlib import Path

from app.engine.base import HostResources

_MEMINFO = Path("/proc/meminfo")
_MIB = 1024 * 1024


def read_host_resources(
    disk_path: Path, meminfo: Path = _MEMINFO, kernel_release: str | None = None
) -> HostResources:
    """Blocking: call through asyncio.to_thread(). Never raises."""
    fields: dict[str, int] = {}
    try:
        for line in meminfo.read_text(encoding="ascii", errors="replace").splitlines()[:64]:
            key, _, rest = line.partition(":")
            value = rest.split()
            if value and value[0].isdigit():
                fields[key.strip()] = int(value[0]) // 1024  # kB -> MiB
    except OSError:
        fields = {}
    # The workspaces directory may not exist yet: measure its closest existing parent.
    path = disk_path
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        disk: int | None = shutil.disk_usage(path).free // _MIB
    except OSError:
        disk = None
    release = (kernel_release if kernel_release is not None else platform.release()).lower()
    return HostResources(
        memory_total_mb=fields.get("MemTotal"),
        memory_available_mb=fields.get("MemAvailable"),
        disk_free_mb=disk,
        disk_is_virtual="microsoft" in release or "linuxkit" in release,
    )
