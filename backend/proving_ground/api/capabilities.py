"""What this install can actually do -- the one place the UI asks before it draws.

The product runs on one of two substrates (`RANGE_SUBSTRATE`), and they do not offer the same
things. A Docker host has an image cache, a local registry and ranges built from Network and VM
rows. A Kubernetes install has none of those: no Docker socket, no image cache, and a range whose
truth is a namespace of KubeVirt workloads, Multus attachments and capability charts.

Before this endpoint the frontend had no way to ask, so it drew the Docker product on both and a
user on Kubernetes met a page of panels that could only fail -- "Failed to load cache data", a
registry reporting Unhealthy, a range page claiming "No networks configured" about a range with
two networks.

**The policy lives here, not in the frontend.** The UI asks what is available and renders that;
it does not re-derive which substrate implies which feature. That keeps one answer in one place
when MIG-3 deletes the Docker path and every `false` below becomes the only answer.

Public and unauthenticated, like `/system/info` and `/branding`: it describes the deployment, not
anything belonging to a user, and the login page needs it before anyone has a session.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from proving_ground.config import get_settings

router = APIRouter(prefix="/system", tags=["system"])

__all__ = ["Capabilities", "capabilities_for", "router"]

KUBERNETES = "kubernetes"


class Features(BaseModel):
    """Each flag answers one question: may the UI offer this at all on this install?"""

    # --- Era A only: everything below is built on the Docker socket or the DinD range container.
    image_cache: bool
    docker_registry: bool
    image_library: bool
    docker_status: bool
    # Ranges assembled in the UI from Network and VM rows: the wizard, Add Network, Add VM, and
    # the Networks / Virtual Machines sections of a range page.
    range_composition: bool
    legacy_range_console: bool

    # --- Era B only.
    # A range's workloads (KubeVirt VMs) and their consoles.
    workloads: bool
    # A range's web applications on the cluster ingress (PG-62).
    range_apps: bool

    # --- Both, listed so the UI never has to infer them from the substrate name.
    blueprints: bool
    training_events: bool
    content_library: bool
    catalog: bool


class Capabilities(BaseModel):
    substrate: str
    # Human-readable, for the one place the UI has to name the substrate to an operator.
    substrate_label: str
    features: Features


def capabilities_for(substrate: str) -> Capabilities:
    """The feature set a substrate offers. Pure, so the policy is testable without an app."""
    kubernetes = substrate == KUBERNETES
    return Capabilities(
        substrate=substrate,
        substrate_label="Kubernetes" if kubernetes else "Docker",
        features=Features(
            image_cache=not kubernetes,
            docker_registry=not kubernetes,
            image_library=not kubernetes,
            docker_status=not kubernetes,
            range_composition=not kubernetes,
            legacy_range_console=not kubernetes,
            workloads=kubernetes,
            range_apps=kubernetes,
            blueprints=True,
            training_events=True,
            content_library=True,
            catalog=True,
        ),
    )


@router.get("/capabilities", response_model=Capabilities)
async def get_capabilities() -> Capabilities:
    """What this install offers. The UI asks once at startup and draws accordingly."""
    return capabilities_for(get_settings().range_substrate)
