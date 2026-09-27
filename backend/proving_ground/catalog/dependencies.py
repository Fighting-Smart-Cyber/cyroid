"""Work out everything a catalog blueprint needs before installing it (PG-149).

Installing a blueprint used to install whatever its own directory happened to
mention, in whatever order the code reached it, and log a warning when a piece
was missing. That is why a blueprint could install "successfully" and then fail
to deploy: the base image it referenced was never there.

This module answers the question first and separately -- what does this item
need, what is already satisfied, and what is simply not in the catalog -- and
returns it as an ordered plan. Resolving before installing is what makes the
progress reporting honest (the total is known up front), the errors specific
(a step has a name), and the whole thing idempotent (a satisfied step is
skipped rather than reinstalled).

The resolver is pure: the caller supplies what exists and what is already
installed. That keeps catalog layout and database state out of it, and means
the ordering rules can be tested without either.

"Everything it needs" has a second dimension the first version of this module
did not model. A catalog item is written for one of two substrates, and an
install runs on one of them: an Era A blueprint describes VMs and networks the
Kubernetes substrate refuses at deploy, and an Era B blueprint describes
machines and capability packages the Docker substrate has nothing to do with.
The item can be wholly present in the catalog and still be undeployable here.
So the plan now also carries a `SupportVerdict` -- whether this install can run
what the item describes, and the sentence saying why not.

It also means "its dependencies" is a different list per era, not the same list
judged differently. A v1 blueprint depends on Docker image projects, which are
installed here. A v2 blueprint depends on the chart repositories its
capabilities pull from and the disk images its machines boot, neither of which
is fetched at install time -- helm-controller and CDI fetch them at deploy. Both
are modelled as first-class requirements rather than steps, so the plan can say
up front that a chart repository is not permitted or a machine has nothing to
boot, instead of reporting a clean install of something that cannot deploy.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from proving_ground.capability.blueprint import SCHEMA_VERSION_K8S, SCHEMA_VERSION_LEGACY
from proving_ground.capability.models import Scope
from proving_ground.capability.specs import ChartRepositoryPolicy

BASE_IMAGE = "base_image"
IMAGE = "image"
CONTENT = "content"
BLUEPRINT = "blueprint"
# Not an install step kind -- see CapabilityRequirement -- but the prefix its key carries, so a
# caller reporting a blocked capability names it the same way it names a step.
CAPABILITY = "capability"
# Likewise not a step: see DiskImageRequirement.
DISK_IMAGE = "disk_image"

# The two values of RANGE_SUBSTRATE. An empty substrate means the caller did not say, and the
# resolver then answers exactly what it answered before any of this existed: no verdict.
SUBSTRATE_KUBERNETES = "kubernetes"
SUBSTRATE_DOCKER = "dind"

ERA_A_ON_KUBERNETES = (
    "This is an Era A blueprint: it describes VMs and networks for the Docker substrate. "
    "This install deploys a range as a Kubernetes namespace of machines and capabilities, so "
    "the blueprint would install and then be refused at deploy."
)
ERA_B_ON_DOCKER = (
    "This is a Kubernetes blueprint: it describes machines and capability charts. This install "
    "runs on Docker, which deploys neither, so the blueprint would install and then be refused "
    "at deploy."
)
NO_IMAGE_LIBRARY = "this install has no image library to build or store it in"
# A v2 blueprint's machines boot the disk images the document names, so the Era A image
# directories an index entry lists are not its dependencies and building them installs nothing the
# range would use.
DOCKER_IMAGES_ON_A_K8S_BLUEPRINT = (
    "is a Kubernetes blueprint, so the Docker image directories its index entry lists are not "
    "what its machines boot: those are named in the blueprint's own workloads"
)

# Install order is a dependency order, not a preference. A blueprint's VMs
# reference base images and built images by tag, so both must exist before the
# blueprint row is created.
#
# Content comes last, which differs from the order PG-149 sketched. In this
# catalog format content is not an independent item a blueprint refers to: it
# ships inside the blueprint directory (content.json, or a walkthrough in
# blueprint.yaml) and is created by the blueprint installer as it builds the
# config. So the content stage cannot precede the blueprint -- it confirms the
# content was created and linked, which is what makes a blueprint whose content
# silently failed to attach visible instead of reported as a clean install.
INSTALL_ORDER: Sequence[str] = (BASE_IMAGE, IMAGE, BLUEPRINT, CONTENT)

_ORDER_INDEX = {kind: i for i, kind in enumerate(INSTALL_ORDER)}


@dataclass(frozen=True)
class Dependency:
    """One unit of work in an install plan."""

    kind: str
    ref: str
    name: str
    path: str = ""
    satisfied: bool = False
    available: bool = True
    note: str = ""
    # Present in the catalog, but this substrate cannot run it. Separate from `available`, which
    # says the catalog does not contain it: both stop the step, and a caller that reported them
    # together would tell the user a file is missing from a repository it is sitting in.
    unsupported_here: bool = False

    @property
    def key(self) -> str:
        return f"{self.kind}/{self.ref}"

    @property
    def label(self) -> str:
        """What the progress log should say while this step runs."""
        verbs = {
            BASE_IMAGE: "Installing VM image",
            IMAGE: "Building image",
            CONTENT: "Linking content",
            BLUEPRINT: "Installing blueprint",
        }
        return f"{verbs.get(self.kind, 'Installing')}: {self.name}"


@dataclass(frozen=True)
class CapabilityRequirement:
    """A capability package the item's blueprint declares.

    Deliberately not a step. Nothing is fetched at install time: helm-controller pulls the chart
    when a range is deployed, from a repository this install has to permit first. Counting a
    capability among the steps would make the progress bar promise work that never runs. What the
    resolver can answer up front is whether the declaration is complete and whether this install
    would be allowed to fetch the chart at all -- and both are answers the user wants before the
    install, not a range deploy later.

    `available` mirrors `Dependency.available`: false means this install cannot supply it, and
    `note` says which of the two reasons applies.
    """

    name: str
    version: str = ""
    scope: str = ""
    chart_name: str = ""
    chart_version: str = ""
    repository: str = ""
    available: bool = True
    note: str = ""

    @property
    def key(self) -> str:
        return f"{CAPABILITY}/{self.name}"

    @property
    def label(self) -> str:
        chart = self.chart_name or "(no chart named)"
        if self.chart_version:
            chart = f"{chart} {self.chart_version}"
        where = f" from {self.repository}" if self.repository else ""
        return f"Capability: {self.name} ({chart}{where})"


@dataclass(frozen=True)
class DiskImageRequirement:
    """A disk image one of a v2 blueprint's machines boots.

    Deliberately not a step, for the same reason a capability is not: nothing is fetched at
    install time. CDI imports the image into a DataVolume when a range is deployed, so counting it
    among the steps would make the progress bar promise work that never runs, and installing it
    into the Docker image library would put it somewhere the Kubernetes substrate never looks.

    What the resolver can answer up front is whether each machine names an image at all. A
    workload with a boot disk and no `bootImage` gets a blank DataVolume from
    `kubevirt.virtual_machine_manifest`, which is a range that deploys, comes up and boots
    nothing -- the exact failure this whole verdict exists to move earlier.
    """

    workload: str
    image: str = ""
    available: bool = True
    note: str = ""

    @property
    def key(self) -> str:
        return f"{DISK_IMAGE}/{self.workload}"

    @property
    def label(self) -> str:
        return f"Disk image for {self.workload}: {self.image or '(none named)'}"


@dataclass(frozen=True)
class SupportVerdict:
    """Whether this install can run what the item describes.

    Distinct from availability, which is about the catalog. An item whose every dependency is
    present can still be undeployable on this substrate, and that is the case the catalog was
    silent about: the storefront finished with green ticks and nothing it installed could be
    deployed. `reason` is written to be shown to the user verbatim.
    """

    supported: bool = True
    reason: str = ""
    schema_version: Optional[int] = None


@dataclass
class InstallPlan:
    """Everything that must happen, in order, to install one blueprint."""

    steps: List[Dependency] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    capabilities: List[CapabilityRequirement] = field(default_factory=list)
    disk_images: List[DiskImageRequirement] = field(default_factory=list)
    support: SupportVerdict = SupportVerdict()

    @property
    def blocked_capabilities(self) -> List[CapabilityRequirement]:
        """Capabilities this install cannot supply, whatever the catalog contains."""
        return [c for c in self.capabilities if not c.available]

    @property
    def blocked_disk_images(self) -> List[DiskImageRequirement]:
        """Machines whose disk image this install could not resolve to anything bootable."""
        return [d for d in self.disk_images if not d.available]

    @property
    def pending(self) -> List[Dependency]:
        """Steps that will actually run."""
        return [s for s in self.steps if not s.satisfied and s.available]

    @property
    def satisfied(self) -> List[Dependency]:
        return [s for s in self.steps if s.satisfied]

    @property
    def missing(self) -> List[Dependency]:
        """Dependencies the catalog claims but does not contain."""
        return [s for s in self.steps if not s.available and not s.unsupported_here]

    @property
    def unsupported_steps(self) -> List[Dependency]:
        """Steps the catalog contains and this install cannot run."""
        return [s for s in self.steps if s.unsupported_here]

    @property
    def total_steps(self) -> int:
        return len(self.pending)

    def describe(self) -> str:
        """A one-line summary for the job's opening progress message."""
        parts = [f"{self.total_steps} step(s)"]
        if self.satisfied:
            parts.append(f"{len(self.satisfied)} already installed")
        if self.missing:
            parts.append(f"{len(self.missing)} missing from catalog")
        if self.unsupported_steps:
            parts.append(f"{len(self.unsupported_steps)} not supported on this substrate")
        summary = ", ".join(parts)
        # Appended rather than replacing the counts: an operator reading a job log still needs to
        # know what it did, and a refusal that hides the plan is harder to act on than one that
        # sits beside it.
        if not self.support.supported:
            summary = f"{summary} -- not supported on this install"
        return summary


def resolve_install_plan(
    item: dict,
    *,
    index_items: Iterable[dict],
    installed_item_ids: Set[str],
    available_image_dirs: Set[str],
    installed_image_projects: Set[str] = frozenset(),
    has_content: bool = False,
    content_installed: bool = False,
    document: Optional[Mapping[str, Any]] = None,
    substrate: str = "",
    permitted_repositories: Optional[Sequence[str]] = None,
) -> InstallPlan:
    """Build the ordered install plan for a blueprint catalog item.

    Args:
        item: the blueprint's entry in the catalog index.
        index_items: every entry in the index, used to resolve base images by id.
        installed_item_ids: catalog item ids already installed from this source.
        available_image_dirs: image names that exist under the catalog's
            ``images/`` with a Dockerfile. A required image not in this set is
            reported as missing rather than silently skipped.
        installed_image_projects: image project names already registered
            locally, which do not need building again.
        has_content: whether the blueprint ships content (content.json or an
            embedded walkthrough).
        content_installed: whether that content is already in the library.
        document: the blueprint document itself (blueprint.yaml, parsed), which
            is where the schema version, the capability packages and the
            machines whose disk images a range boots are declared. The index
            entry is consulted only if it is not supplied, and an index entry
            names no workloads, so a plan resolved without the document cannot
            report an item's disk images at all.
        substrate: this install's RANGE_SUBSTRATE. Omitted means "do not judge":
            the plan then reports what it always reported, which keeps a caller
            that has not been taught about substrates working unchanged.
        permitted_repositories: the chart repositories this install permits, as
            configured by an operator. ``None`` means the caller did not ask, so
            no repository is judged; an empty sequence means none are permitted,
            which is the platform's own default and a real answer.

    Returns:
        An InstallPlan whose steps are in dependency order.
    """
    plan = InstallPlan()

    # blueprint.yaml is remote content, so it is whatever the file happened to say. Unchecked it
    # reaches .get() below and raises AttributeError, turning a catalog file that is a list or a
    # bare string into a 500 from the install-plan route instead of a refusal naming the item.
    source: Mapping[str, Any] = item
    document_error = ""
    if isinstance(document, Mapping):
        source = document
    elif document is not None:
        source = {}
        document_error = "is not an object, so nothing can be read from it"

    schema_version, version_error = _schema_version(source)
    version_error = document_error or version_error
    # Read before the steps are built rather than after, because what a v2 blueprint depends on is
    # a different list, not the same list judged differently: its machines boot the disk images
    # the document names and its capabilities pull charts, and neither is a Docker image directory
    # under the catalog's images/.
    is_kubernetes_blueprint = schema_version == SCHEMA_VERSION_K8S

    if is_kubernetes_blueprint:
        declared_docker = _unique(
            list(item.get("requires_base_images") or []) + list(item.get("requires_images") or [])
        )
        if declared_docker:
            plan.warnings.append(
                f"'{item.get('id', '')}' {DOCKER_IMAGES_ON_A_K8S_BLUEPRINT}; "
                f"ignoring: {', '.join(declared_docker)}"
            )
    else:
        _docker_image_steps(
            plan,
            item,
            index_items=index_items,
            installed_item_ids=installed_item_ids,
            available_image_dirs=available_image_dirs,
            installed_image_projects=installed_image_projects,
        )

    if has_content:
        plan.steps.append(
            Dependency(
                kind=CONTENT,
                ref=item.get("id", ""),
                name=item.get("name") or item.get("id", ""),
                satisfied=content_installed,
                note="already installed" if content_installed else "",
            )
        )

    item_id = item.get("id", "")
    plan.steps.append(
        Dependency(
            kind=BLUEPRINT,
            ref=item_id,
            name=item.get("name") or item_id,
            path=item.get("path", ""),
            satisfied=item_id in installed_item_ids,
            note="already installed" if item_id in installed_item_ids else "",
        )
    )

    plan.steps.sort(key=lambda s: _ORDER_INDEX.get(s.kind, len(INSTALL_ORDER)))

    policy = (
        None
        if permitted_repositories is None
        else ChartRepositoryPolicy(tuple(permitted_repositories))
    )
    plan.capabilities = _capability_requirements(source, policy)
    for blocked in plan.blocked_capabilities:
        plan.warnings.append(f"capability '{blocked.name}' cannot run here: {blocked.note}")

    if is_kubernetes_blueprint:
        plan.disk_images = _disk_image_requirements(source)
        for disk in plan.disk_images:
            if not disk.available:
                plan.warnings.append(f"machine '{disk.workload}' {disk.note}")
            elif disk.note:
                plan.warnings.append(f"machine '{disk.workload}' boots an image that {disk.note}")

    if substrate == SUBSTRATE_KUBERNETES:
        _refuse_docker_image_steps(plan)

    plan.support = _support_verdict(schema_version, version_error, substrate, plan)
    return plan


def _docker_image_steps(
    plan: InstallPlan,
    item: dict,
    *,
    index_items: Iterable[dict],
    installed_item_ids: Set[str],
    available_image_dirs: Set[str],
    installed_image_projects: Set[str],
) -> None:
    """Append the Era A image steps an index entry declares.

    Only reached for a v1 blueprint. `requires_base_images` names catalog index entries and
    `requires_images` names directories under the catalog's ``images/`` -- both are Docker image
    projects, which is what a v1 range is made of.
    """
    base_image_items: Dict[str, dict] = {
        entry.get("id"): entry
        for entry in index_items
        if isinstance(entry, dict) and entry.get("type") == BASE_IMAGE and entry.get("id")
    }

    for ref in _unique(item.get("requires_base_images") or []):
        entry = base_image_items.get(ref)
        if entry is None:
            plan.steps.append(
                Dependency(
                    kind=BASE_IMAGE,
                    ref=ref,
                    name=ref,
                    available=False,
                    note="not listed in the catalog index",
                )
            )
            plan.warnings.append(f"base image '{ref}' is required but is not in the catalog index")
            continue
        plan.steps.append(
            Dependency(
                kind=BASE_IMAGE,
                ref=ref,
                name=entry.get("name") or ref,
                path=entry.get("path", ""),
                satisfied=ref in installed_item_ids,
                note="already installed" if ref in installed_item_ids else "",
            )
        )

    for ref in _unique(item.get("requires_images") or []):
        available = ref in available_image_dirs
        satisfied = ref in installed_image_projects
        if not available and not satisfied:
            plan.warnings.append(f"image '{ref}' is required but has no Dockerfile in the catalog")
        plan.steps.append(
            Dependency(
                kind=IMAGE,
                ref=ref,
                name=ref,
                path=f"images/{ref}",
                # An image already built locally needs nothing from the catalog,
                # so a missing Dockerfile is only a problem when it is absent.
                available=available or satisfied,
                satisfied=satisfied,
                note=(
                    "already installed"
                    if satisfied
                    else ("" if available else "no Dockerfile in the catalog")
                ),
            )
        )


def _schema_version(source: Mapping[str, Any]) -> tuple[Optional[int], str]:
    """The schema version a blueprint declares, and the complaint if it is not one.

    Absence is not an error: a blueprint with no ``schemaVersion`` is v1 by definition, which is
    what every blueprint written before Era B is. A version this engine does not know is refused
    rather than guessed at, for the same reason ``read_blueprint`` refuses it -- installing a
    newer blueprint by ignoring the fields it declares produces a range missing whatever they
    said.
    """
    declared = source.get("schemaVersion", SCHEMA_VERSION_LEGACY)
    if isinstance(declared, bool) or not isinstance(declared, int):
        return None, f"declares schemaVersion {declared!r}, which is not a version number"
    if declared not in (SCHEMA_VERSION_LEGACY, SCHEMA_VERSION_K8S):
        return None, (
            f"declares schemaVersion {declared}, which this install does not understand "
            f"(known: {SCHEMA_VERSION_LEGACY}, {SCHEMA_VERSION_K8S})"
        )
    return declared, ""


def _capability_requirements(
    source: Mapping[str, Any], policy: Optional[ChartRepositoryPolicy]
) -> List[CapabilityRequirement]:
    """Read the capability packages a blueprint declares.

    Read from a v1 document too: capabilities are era-neutral -- a capability does not care which
    substrate runs it -- and a v1 blueprint that declares one was having it dropped silently.

    Nothing here raises. The contract's own parser refuses a bad declaration, which is right at
    deploy and wrong here: the whole point of a plan is to report every problem at once instead of
    stopping at the first.
    """
    declared = source.get("capabilities") or []
    if not isinstance(declared, Sequence) or isinstance(declared, (str, bytes)):
        return [
            CapabilityRequirement(
                name="capabilities",
                available=False,
                # Phrased as a predicate on the name: the verdict shown to the user reads
                # "<name> <note>", and a note that repeated the word stuttered there.
                note="is not a list, so no capability can be read from it",
            )
        ]
    return [_capability(entry, index, policy) for index, entry in enumerate(declared)]


def _capability(
    entry: Any, index: int, policy: Optional[ChartRepositoryPolicy]
) -> CapabilityRequirement:
    where = f"capabilities[{index}]"
    if not isinstance(entry, Mapping):
        return CapabilityRequirement(name=where, available=False, note="must be an object")

    chart = entry.get("chart")
    chart = chart if isinstance(chart, Mapping) else {}
    requirement = CapabilityRequirement(
        name=_text(entry.get("name")) or where,
        version=_text(entry.get("version")),
        scope=_text(entry.get("scope")),
        chart_name=_text(chart.get("name")),
        chart_version=_text(chart.get("version")),
        repository=_text(chart.get("repository")),
    )

    if not requirement.chart_name or not requirement.repository:
        return replace(
            requirement,
            available=False,
            note="declares no chart name and repository, so there is nothing to deploy",
        )
    if not requirement.scope:
        return replace(
            requirement,
            available=False,
            note=(
                "declares no scope; one of "
                f"{', '.join(s.value for s in Scope)} is required, with no default"
            ),
        )
    if requirement.scope not in {s.value for s in Scope}:
        return replace(
            requirement,
            available=False,
            note=(
                f"declares scope {requirement.scope!r}; must be one of "
                f"{', '.join(s.value for s in Scope)}"
            ),
        )
    if policy is not None and not policy.permits(requirement.repository):
        # The repository is handed to helm-controller, which fetches whatever chart it finds
        # there, so which repositories an install trusts is an operator's decision and not a
        # catalog author's. Reported here so it is known before the install rather than at the
        # first deploy.
        return replace(
            requirement,
            available=False,
            note=f"this install does not permit charts from {requirement.repository}",
        )
    return requirement


def _disk_image_requirements(source: Mapping[str, Any]) -> List[DiskImageRequirement]:
    """Read the disk image each of a v2 blueprint's machines boots.

    Nothing here raises, for the same reason `_capability_requirements` does not: a plan reports
    every problem at once, and the contract's own parser is what refuses a bad declaration at the
    moment the blueprint is stored.
    """
    declared = source.get("workloads") or []
    if not isinstance(declared, Sequence) or isinstance(declared, (str, bytes)):
        return [
            DiskImageRequirement(
                workload="workloads",
                available=False,
                note="is not a list, so no machine can be read from it",
            )
        ]
    requirements: List[DiskImageRequirement] = []
    for index, entry in enumerate(declared):
        where = f"workloads[{index}]"
        if not isinstance(entry, Mapping):
            requirements.append(
                DiskImageRequirement(workload=where, available=False, note="must be an object")
            )
            continue
        name = _text(entry.get("name")) or where
        image = _text(entry.get("bootImage"))
        if not image:
            if _has_boot_disk(entry):
                requirements.append(
                    DiskImageRequirement(
                        workload=name,
                        available=False,
                        note=(
                            "declares a boot disk but names no bootImage, so it would be given a "
                            "blank volume and boot nothing"
                        ),
                    )
                )
            # A machine with no boot disk has nothing to import, so it is not a requirement at
            # all rather than an unsatisfied one.
            continue
        requirements.append(
            DiskImageRequirement(
                workload=name,
                image=image,
                # Not a refusal: a tag resolves on a connected install and the range comes up.
                # It is reported because ADR-0007 makes air-gap a constraint now -- a mirrored
                # registry has the digest and not the tag that pointed at it -- and because what
                # the machine boots can change under an exercise that was assessed against it.
                note=("" if "@" in image else "is named by tag rather than by digest"),
            )
        )
    return requirements


def _has_boot_disk(entry: Mapping[str, Any]) -> bool:
    disks = entry.get("disks") or []
    if not isinstance(disks, Sequence) or isinstance(disks, (str, bytes)):
        return False
    return any(isinstance(d, Mapping) and bool(d.get("boot")) for d in disks)


def _refuse_docker_image_steps(plan: InstallPlan) -> None:
    """Mark image steps unavailable on a substrate that has no image library.

    Both image step kinds go through the Docker daemon -- one builds a Dockerfile, the other
    imports a disk image into the library. A Kubernetes install has neither, and a machine there
    boots the digest-pinned image its blueprint names. Left available, these steps are counted in
    the total and then fail one at a time inside the job.

    Marked `unsupported_here` as well as unavailable, because the two are not the same fact. The
    installer reports every unavailable step to the user as "missing from the catalog", and these
    are sitting in the catalog exactly where the index says they are.
    """
    for index, step in enumerate(plan.steps):
        if step.kind not in (BASE_IMAGE, IMAGE) or step.satisfied or not step.available:
            continue
        plan.steps[index] = replace(
            step, available=False, unsupported_here=True, note=NO_IMAGE_LIBRARY
        )
        plan.warnings.append(f"'{step.ref}' is a Docker image and {NO_IMAGE_LIBRARY}")


def _support_verdict(
    schema_version: Optional[int], version_error: str, substrate: str, plan: InstallPlan
) -> SupportVerdict:
    """Whether this install can run the item, and the sentence that says why not."""
    if version_error:
        return SupportVerdict(supported=False, reason=f"This blueprint {version_error}.")
    if substrate == SUBSTRATE_KUBERNETES and schema_version == SCHEMA_VERSION_LEGACY:
        return SupportVerdict(False, ERA_A_ON_KUBERNETES, schema_version)
    if substrate == SUBSTRATE_DOCKER and schema_version == SCHEMA_VERSION_K8S:
        return SupportVerdict(False, ERA_B_ON_DOCKER, schema_version)
    blocked = plan.blocked_capabilities
    if blocked:
        detail = "; ".join(f"{c.name} {c.note}" for c in blocked)
        return SupportVerdict(
            supported=False,
            reason=f"This blueprint declares a capability this install cannot run -- {detail}.",
            schema_version=schema_version,
        )
    blocked_disks = plan.blocked_disk_images
    if blocked_disks:
        detail = "; ".join(f"{d.workload} {d.note}" for d in blocked_disks)
        return SupportVerdict(
            supported=False,
            reason=f"This blueprint declares a machine that cannot boot -- {detail}.",
            schema_version=schema_version,
        )
    return SupportVerdict(supported=True, schema_version=schema_version)


def _text(value: Any) -> str:
    """A declared string, or empty. A number is a version often enough to be worth accepting."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _unique(values: Iterable[str]) -> List[str]:
    """Preserve catalog order, drop repeats and blanks."""
    seen: Set[str] = set()
    out: List[str] = []
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


class DependencyInstallError(RuntimeError):
    """A named step failed, so the caller can say which one.

    The whole point of AC "handles errors gracefully (shows which dependency
    failed)": a bare exception from three frames down does not tell the user
    whether their blueprint or one of its images is the problem.
    """

    def __init__(self, dependency: Dependency, cause: Optional[BaseException] = None):
        self.dependency = dependency
        self.cause = cause
        detail = f": {cause}" if cause else ""
        super().__init__(f"{dependency.label} failed{detail}")
