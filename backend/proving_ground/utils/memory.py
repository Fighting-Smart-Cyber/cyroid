"""Memory budgeting for emulated VMs inside a range.

Three things share a range's memory cap, and only one of them is the guest:

    guest RAM  +  QEMU's own overhead  +  the range's docker daemon

Sizing the VM container to the guest exactly put an OOM-killed VM into a
restart loop (46 restarts, ~40s apart, console unreachable). The overhead is
measured, not guessed: a 4 GiB guest settles ~0.9 GiB above its allocation at
a 5 GiB limit and ~1.2 GiB at a 6 GiB limit, peaking ~1.3 GiB during boot, so
it expands somewhat into whatever is available. A 1 GiB allowance still
OOM-killed once on the way up; 2 GiB survived a boot with no restarts.

It is near-constant across guest sizes rather than proportional -- a
percentage would shrink it exactly where it is already tightest.

A fresh install adds a fourth claim on the cap, and it is transient: the
install ISO is mounted into the VM container and lands in that container's
page cache. dockur admits a guest by comparing RAM_SIZE against the cgroup's
free memory -- memory.max minus memory.current -- and page cache counts toward
memory.current even though it is reclaimable, so an extracted ISO makes the
container look full and dockur silently hands the guest a fraction of its
configured RAM instead of failing. Measured on a Windows 11 install: a 7.74 GiB
ISO left 7.96 GiB of page cache (7.95 GiB of it inactive_file, i.e. reclaimable)
in a 10 GiB container, and an 8192 MiB guest was cut to 1931 MiB with only a
warning in the container log.

The kernel would have evicted that cache rather than OOM -- the limit was
adequate, dockur's admission check just could not see it. So the ISO allowance
raises the VM container's cap to get the guest admitted; it is not memory the
guest needs resident, and it deliberately does not change what
check_guest_fits_range considers a fitting guest.
"""

import re
from typing import Optional

# See module docstring: measured, and deliberately not a percentage.
QEMU_OVERHEAD_MB = 2048

# The range's own dockerd/containerd. Measured at ~270 MiB idle in a range
# with its VM stopped; 512 leaves room for image pulls and short-lived helpers.
DIND_DAEMON_RESERVE_MB = 512

# Slack on top of the install ISO's own size. The cache tracked the ISO closely
# (7.96 GiB for a 7.74 GiB image), so this covers the extraction overshoot
# rather than being a guess at the whole figure.
ISO_CACHE_MARGIN_MB = 512

_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt])b?\s*$", re.IGNORECASE)
_UNIT_MB = {"k": 1 / 1024, "m": 1, "g": 1024, "t": 1024 * 1024}


def parse_memory_to_mb(value: "str | int | None") -> Optional[int]:
    """Parse a Docker-style size string ("8g", "512m") into MiB.

    An int is taken as MiB already -- that is how ram_mb is stored.

    A unitless string is refused rather than guessed at. Docker reads "2048"
    as bytes, which is 0 MiB once rounded, and a zero cap would silently switch
    the budget check off instead of failing where someone can see it. Callers
    treat None as "cap unknown" and skip the check, so guessing here would be
    the dangerous option.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    m = _SIZE.match(str(value))
    if not m:
        return None
    number, unit = m.group(1), m.group(2).lower()
    return int(float(number) * _UNIT_MB[unit])


def vm_container_memory_mb(
    guest_ram_mb: int,
    *,
    install_iso_mb: Optional[int] = None,
    range_cap_mb: Optional[int] = None,
) -> int:
    """Container limit for an emulated VM: the guest's RAM plus the emulator's.

    install_iso_mb is the size of an ISO being installed from, if this VM is
    booting one. It buys headroom for that ISO's page cache so dockur admits
    the guest at its configured RAM (see the module docstring); a VM booting a
    disk it already has passes None and is sized exactly as before.

    range_cap_mb, when known, clamps the result to what the range can actually
    back. Handing the VM container a cap above the range's own would leave the
    range cgroup to do the reclaiming, and it may pick the range's dockerd
    rather than QEMU -- the OOM-restart-loop this module exists to avoid, moved
    up a level rather than fixed.
    """
    limit = guest_ram_mb + QEMU_OVERHEAD_MB
    if install_iso_mb:
        limit += install_iso_mb + ISO_CACHE_MARGIN_MB
    if range_cap_mb:
        limit = min(limit, max(range_cap_mb - DIND_DAEMON_RESERVE_MB, guest_ram_mb))
    return limit


def max_guest_ram_mb(range_cap_mb: int) -> int:
    """Largest guest that fits in a range once QEMU and dockerd are paid for."""
    return range_cap_mb - QEMU_OVERHEAD_MB - DIND_DAEMON_RESERVE_MB


def check_guest_fits_range(guest_ram_mb: int, range_cap_mb: Optional[int]) -> Optional[str]:
    """Return an explanation if this guest cannot fit, else None.

    Returns a message rather than raising so both the API (400) and the
    pre-deployment validator (ValidationResult) can present it their own way.
    """
    if not range_cap_mb or not guest_ram_mb:
        return None
    needed = vm_container_memory_mb(guest_ram_mb) + DIND_DAEMON_RESERVE_MB
    if needed <= range_cap_mb:
        return None
    allowed = max_guest_ram_mb(range_cap_mb)
    if allowed <= 0:
        return (
            f"The range's {range_cap_mb} MiB cap cannot host an emulated VM at all: "
            f"QEMU needs {QEMU_OVERHEAD_MB} MiB and the range's docker daemon "
            f"{DIND_DAEMON_RESERVE_MB} MiB before the guest gets anything."
        )
    return (
        f"{guest_ram_mb} MiB of guest RAM does not fit in the range's {range_cap_mb} MiB "
        f"cap. QEMU needs about {QEMU_OVERHEAD_MB} MiB on top of the guest and the "
        f"range's docker daemon another {DIND_DAEMON_RESERVE_MB} MiB, so the most this "
        f"range can give a VM is {allowed} MiB. Sizing the guest to the whole cap gets "
        f"it OOM-killed by the cgroup and restart-looped, with the console unreachable."
    )
