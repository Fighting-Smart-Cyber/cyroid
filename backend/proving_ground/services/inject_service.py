# backend/proving_ground/services/inject_service.py
"""Execute an MSEL inject's actions against a range's machines -- MSEL-055.

An inject fires in the middle of a live exercise and its status is the record of what the
exercise actually did. Two of the things this file used to claim were not true.

`place_file` logged "Would place ..." and returned ``{"placed": True}``, so an inject whose only
action was a file placement closed COMPLETED with a green tick over a machine that had received
nothing. A controller could not tell a real placement from a simulated one, and the record of the
exercise was wrong in a way nobody would notice until it mattered.

`run_command` resolves its target through ``VM.container_id``. A range machine on the Kubernetes
substrate is a KubeVirt virtual machine and has no container id -- and its range has no VM rows
at all -- so on that substrate every inject reported every one of its targets missing.

**Why this refuses on Kubernetes rather than routing through the capability runtime.** The one
escape hatch is ``CapabilityRuntime.exec``, and it reaches a *capability's* pods by label
selector -- not a learner's machine, whose guest sits behind a virt-launcher pod that nothing
here speaks to. There is no guest-agent path in this codebase to borrow. Sending an inject
through ``exec`` anyway would also rebuild the original defect somewhere new, because
``exec_in_pod`` reports exit code 0 whatever the command did, so a failed inject would once
again close COMPLETED. A refusal that names the substrate is the honest answer until a
guest-side path exists.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from proving_ground.config import get_settings
from proving_ground.models.inject import Inject, InjectStatus
from proving_ground.models.vm import VM
from proving_ground.services.docker_service import DockerService

logger = logging.getLogger(__name__)

KUBERNETES_SUBSTRATE = "kubernetes"

SUBSTRATE_REFUSAL = (
    "This install runs on the Kubernetes substrate, where a range's machines are KubeVirt "
    "virtual machines. Running a command or placing a file inside a machine needs a guest-side "
    "path this platform does not have yet. Nothing was run and nothing was placed."
)

PLACE_FILE_REFUSAL = (
    "Placing a file from an inject is not implemented -- it only ever recorded the intent. "
    "Nothing was placed. Upload the file as an artifact and create an artifact placement "
    "against the machine, which does copy it."
)


class InjectService:
    """Run an inject's actions, or refuse in terms an exercise record can be read back from."""

    def __init__(
        self,
        db: Session,
        docker_service: DockerService,
        substrate: Optional[str] = None,
    ):
        self.db = db
        self.docker = docker_service
        # Read once, at construction, so the decision cannot change halfway through an inject
        # and so a caller can pin it. Callers that pass nothing get this host's own setting.
        self.substrate = substrate or get_settings().range_substrate

    def execute_inject(self, inject: Inject, vm_map: Dict[str, VM]) -> Dict[str, Any]:
        """
        Execute an inject's actions on target VMs.

        Args:
            inject: The Inject model to execute
            vm_map: Dict mapping VM hostnames to VM objects

        Returns:
            Dict with 'success' boolean and 'results' list
        """
        if self.substrate == KUBERNETES_SUBSTRATE:
            # Refused before EXECUTING is set, so nothing in the record implies it ever started.
            return self._refuse(inject, SUBSTRATE_REFUSAL)

        inject.status = InjectStatus.EXECUTING
        inject.executed_at = datetime.now(timezone.utc)
        self.db.commit()

        results = []
        success = True

        for action in inject.actions or []:
            action_type = action.get("action_type")
            params = action.get("parameters", {})

            try:
                if action_type == "run_command":
                    result = self._execute_command(params, vm_map)
                elif action_type == "place_file":
                    result = self._place_file(params, vm_map)
                else:
                    result = {"error": f"Unknown action type: {action_type}"}
                    success = False

                if "error" in result:
                    success = False

                results.append({"action": action, "result": result})
            except Exception as e:
                logger.error(f"Failed to execute action: {e}")
                results.append({"action": action, "error": str(e)})
                success = False

        # Update inject status
        inject.status = InjectStatus.COMPLETED if success else InjectStatus.FAILED
        inject.execution_log = str(results)
        self.db.commit()

        return {"success": success, "results": results}

    def _refuse(self, inject: Inject, reason: str) -> Dict[str, Any]:
        """Record an inject that could not run, having never claimed that it did.

        FAILED rather than left PENDING: somebody pressed Execute during a live exercise and the
        inject did not happen, so the timeline has to carry that. The reason goes into
        `execution_log` as well as the response, or it survives only until the page is closed.
        """
        logger.warning("Refusing inject %s: %s", inject.id, reason)
        inject.status = InjectStatus.FAILED
        inject.executed_at = datetime.now(timezone.utc)
        inject.execution_log = reason
        self.db.commit()
        return {"success": False, "results": [{"error": reason}]}

    def _execute_command(self, params: Dict, vm_map: Dict[str, VM]) -> Dict:
        """Execute a command on a target VM."""
        target_vm_name = params.get("target_vm")
        command = params.get("command")

        if target_vm_name not in vm_map:
            return {"error": f"VM {target_vm_name} not found"}

        vm = vm_map[target_vm_name]
        if not vm.container_id:
            return {"error": f"VM {target_vm_name} has no container"}

        exit_code, output = self.docker.exec_command(vm.container_id, command)
        return {"exit_code": exit_code, "output": output}

    def _place_file(self, params: Dict, vm_map: Dict[str, VM]) -> Dict:
        """Refuse a file placement rather than report one that did not happen.

        Nothing here ever copied a file. Returning an error is what puts the inject into FAILED,
        which is what a controller has to see; naming the artifact placement endpoint -- the path
        that does perform the copy -- is the difference between a dead end and a next step.
        """
        target_vm_name = params.get("target_vm")

        if target_vm_name not in vm_map:
            return {"error": f"VM {target_vm_name} not found"}

        return {
            "error": PLACE_FILE_REFUSAL,
            "filename": params.get("filename"),
            "target_path": params.get("target_path"),
        }

    def skip_inject(self, inject: Inject, reason: str = "") -> None:
        """Mark an inject as skipped."""
        inject.status = InjectStatus.SKIPPED
        inject.execution_log = f"Skipped: {reason}" if reason else "Skipped by user"
        self.db.commit()
