"""Seed a first Kubernetes-substrate blueprint -- `python -m proving_ground.tools.seed_k8s_blueprint`.

A v2 (Era B) blueprint cannot be authored in the UI yet, and the catalog holds only Era A ones,
so a fresh in-cluster install has nothing a range could be made from. This gives it one: a
Linux VM (cirros, by digest, small enough to import in a minute) on two range networks, plus an
unmodified upstream chart with seed and verify hooks -- the same shape the seam harness proves on
pg-devtest. Idempotent: re-running updates the blueprint in place rather than adding another.
"""

from __future__ import annotations

import logging
import sys

from proving_ground.capability.blueprint import read_blueprint
from proving_ground.database import get_session_local
from proving_ground.models.blueprint import RangeBlueprint
from proving_ground.models.user import User, UserRole

logger = logging.getLogger(__name__)

NAME = "Kubernetes sample: one VM, two networks, one capability"

BUSYBOX = "docker.io/library/busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
# quay.io/kubevirt/cirros-container-disk-demo:v1.9.0
CIRROS = "quay.io/kubevirt/cirros-container-disk-demo@sha256:ebdb8d8b9b480f6ee7664ed3fdde8428767664f507d98f94090edeff04d7ebf2"

CONFIG = {
    "schemaVersion": 2,
    "networks": [
        {"name": "dmz", "subnet": "172.30.10.0/24", "gateway": "172.30.10.1"},
        {"name": "internal", "subnet": "172.30.20.0/24"},
    ],
    "workloads": [
        {
            "name": "web",
            "os": {"family": "linux", "version": "cirros-0.6"},
            "cpus": 1,
            "memoryMb": 256,
            "bootImage": CIRROS,
            "disks": [{"name": "root", "sizeGb": 1, "boot": True}],
            "interfaces": [
                {"network": "dmz", "ip": "172.30.10.5", "primary": True},
                {"network": "internal", "ip": "172.30.20.5"},
            ],
        }
    ],
    "capabilities": [
        {
            "name": "podinfo",
            "version": "1.0.0",
            # shared, not per-learner: an instructor's sandbox has no assigned learner, and a
            # per-learner capability on such a range is refused at deploy time by design.
            "scope": "shared",
            "chart": {
                "name": "podinfo",
                "version": "6.7.1",
                "repository": "https://stefanprodan.github.io/podinfo",
            },
            "values": {"replicaCount": 1},
            # podinfo's own UI, reachable at /apps/<range>/podinfo/ once deployed (PG-62).
            "web": {"service": "podinfo", "port": 9898},
            "hooks": {
                "seed": {
                    "image": BUSYBOX,
                    "command": ["/bin/sh", "-c", "echo seeding; echo '{\"seeded\": true}'"],
                },
                "verify": {
                    "image": BUSYBOX,
                    "command": [
                        "/bin/sh",
                        "-c",
                        'echo \'{"passed": true, "convoy_id": "C-17"}\'',
                    ],
                },
            },
        }
    ],
}


def main() -> int:
    spec = read_blueprint(CONFIG)  # refuses here, not at the first deploy, if the sample is wrong
    assert spec.deployable_on_kubernetes

    db = get_session_local()()
    try:
        admin = (
            db.query(User)
            .filter(User.role == UserRole.ADMIN)
            .order_by(User.created_at.asc())
            .first()
        )
        if admin is None:
            print(
                "no admin user yet: register the first account in the UI, then re-run",
                file=sys.stderr,
            )
            return 1

        existing = db.query(RangeBlueprint).filter(RangeBlueprint.name == NAME).first()
        if existing is None:
            db.add(
                RangeBlueprint(
                    name=NAME,
                    description=(
                        "A cirros VM on two range networks and the upstream podinfo chart, for "
                        "proving the Kubernetes substrate end to end. Deploy an instance, watch "
                        "the VM come up, stop, start, tear down."
                    ),
                    version=1,
                    config=CONFIG,
                    created_by=admin.id,
                )
            )
            action = "created"
        else:
            existing.config = CONFIG
            existing.version = (existing.version or 1) + 1
            action = f"updated to v{existing.version}"
        db.commit()
        print(f"{action}: {NAME!r} (owner {admin.username})")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
