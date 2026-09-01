# backend/proving_ground/models/__init__.py
from proving_ground.models.base import Base
from proving_ground.models.user import User, UserRole, UserAttribute, AVAILABLE_ROLES
from proving_ground.models.resource_tag import ResourceTag
from proving_ground.models.vm_enums import OSType, VMType, LinuxDistro
from proving_ground.models.range import Range, RangeStatus
from proving_ground.models.network import Network
from proving_ground.models.vm import VM, VMStatus, BootSource
from proving_ground.models.vm_network import VMNetwork
from proving_ground.models.artifact import (
    Artifact,
    ArtifactPlacement,
    ArtifactType,
    MaliciousIndicator,
    PlacementStatus,
)
from proving_ground.models.snapshot import Snapshot
from proving_ground.models.event_log import EventLog, EventType
from proving_ground.models.connection import Connection, ConnectionProtocol, ConnectionState
from proving_ground.models.msel import MSEL
from proving_ground.models.inject import Inject, InjectStatus
from proving_ground.models.router import RangeRouter, RouterStatus
from proving_ground.models.walkthrough_progress import WalkthroughProgress
from proving_ground.models.blueprint import RangeBlueprint, RangeInstance
from proving_ground.models.content import Content, ContentAsset, ContentType
from proving_ground.models.event import TrainingEvent, EventParticipant, EventStatus
from proving_ground.models.notification import Notification, NotificationType, NotificationSeverity

# Image Library models
from proving_ground.models.base_image import BaseImage, ImageType
from proving_ground.models.golden_image import GoldenImage, GoldenImageSource
from proving_ground.models.platform_secret import PlatformSecret

# Catalog models
from proving_ground.models.catalog import (
    CatalogSource,
    CatalogInstalledItem,
    CatalogSourceType,
    CatalogSyncStatus,
    CatalogItemType,
)

__all__ = [
    "Base",
    "User",
    "UserRole",
    "UserAttribute",
    "AVAILABLE_ROLES",
    "ResourceTag",
    "OSType",
    "VMType",
    "LinuxDistro",
    "Range",
    "RangeStatus",
    "Network",
    "VM",
    "VMStatus",
    "BootSource",
    "VMNetwork",
    "Artifact",
    "ArtifactPlacement",
    "ArtifactType",
    "MaliciousIndicator",
    "PlacementStatus",
    "Snapshot",
    "EventLog",
    "EventType",
    "Connection",
    "ConnectionProtocol",
    "ConnectionState",
    "MSEL",
    "Inject",
    "InjectStatus",
    "RangeRouter",
    "RouterStatus",
    "WalkthroughProgress",
    "RangeBlueprint",
    "RangeInstance",
    "Content",
    "ContentAsset",
    "ContentType",
    "TrainingEvent",
    "EventParticipant",
    "EventStatus",
    "Notification",
    "NotificationType",
    "NotificationSeverity",
    # Image Library
    "BaseImage",
    "ImageType",
    "GoldenImage",
    "PlatformSecret",
    "GoldenImageSource",
    # Catalog
    "CatalogSource",
    "CatalogInstalledItem",
    "CatalogSourceType",
    "CatalogSyncStatus",
    "CatalogItemType",
]
