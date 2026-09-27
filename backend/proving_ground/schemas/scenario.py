# backend/proving_ground/schemas/scenario.py
"""The request and response shapes for applying a training scenario to a range.

A scenario is a YAML file on the data volume, not a database row. Its id is the
file's stem, or the `seed_id` the file declares -- "ransomware-attack", never a
UUID. This module used to declare `scenario_id` as a UUID, so every request the
scenario picker sent was rejected as malformed before the handler ran: Apply
Scenario could not succeed on either substrate. The id is text here because
that is what `services.scenario_filesystem.get_scenario` is asked for.

The scenario list and detail responses do not live here. `api/scenarios.py`
owns them, reading the same filesystem, and the models that used to sit in this
file described scenarios as database rows with UUID primary keys and generated
timestamps -- shapes nothing produced and nothing consumed.
"""
from typing import Dict
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# The same ceiling `api/scenarios.py` puts on an id, for the same reason: the id
# names a single file under the scenarios directory.
MAX_SCENARIO_ID = 128


class ApplyScenarioRequest(BaseModel):
    """Apply one scenario to one range, with each of its roles bound to a machine."""

    scenario_id: str = Field(..., min_length=1, max_length=MAX_SCENARIO_ID)
    role_mapping: Dict[str, str]  # {"domain-controller": "vm-uuid-1", "workstation": "vm-uuid-2"}

    @field_validator("role_mapping")
    @classmethod
    def _each_role_names_a_machine(cls, mapping: Dict[str, str]) -> Dict[str, str]:
        """Refuse a mapping the handler would choke on rather than reject.

        Every value is fed to `UUID()` when the handler checks the machines
        belong to this range, so a value that is not one raises ValueError
        there and reaches the caller as a 500 naming nothing. Refused here it
        is a 422 that names the role.

        Machine ids are the Era A range's VM rows. When the Kubernetes path
        gains a way to bind a role to a blueprint workload, this constraint --
        not the handler's -- is the thing to widen.
        """
        for role, machine_id in mapping.items():
            if not role or not role.strip():
                raise ValueError("a role name cannot be empty")
            try:
                UUID(machine_id)
            except ValueError as exc:
                raise ValueError(
                    f"role '{role}' must be mapped to a machine id, got {machine_id!r}"
                ) from exc
        return mapping


class ApplyScenarioResponse(BaseModel):
    """What the range gained: one MSEL and the injects its events became."""

    msel_id: UUID
    inject_count: int
    status: str = "applied"
