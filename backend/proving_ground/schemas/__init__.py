# backend/proving_ground/schemas/__init__.py
from proving_ground.schemas.user import UserBase, UserCreate, UserUpdate, UserResponse
from proving_ground.schemas.auth import LoginRequest, TokenResponse
from proving_ground.schemas.network import (
    NetworkBase,
    NetworkCreate,
    NetworkUpdate,
    NetworkResponse,
)
from proving_ground.schemas.vm import VMBase, VMCreate, VMUpdate, VMResponse
from proving_ground.schemas.range import (
    RangeBase,
    RangeCreate,
    RangeUpdate,
    RangeResponse,
    RangeDetailResponse,
)
from proving_ground.schemas.event_log import EventLogCreate, EventLogResponse, EventLogList
from proving_ground.schemas.blueprint import (
    BlueprintCreate,
    BlueprintUpdate,
    BlueprintResponse,
    BlueprintDetailResponse,
    InstanceDeploy,
    InstanceResponse,
    BlueprintConfig,
    NetworkConfig,
    VMConfig,
)
from proving_ground.schemas.snapshot import (
    SnapshotBase,
    SnapshotCreate,
    SnapshotUpdate,
    SnapshotResponse,
    SnapshotBrief,
)

# Image Library schemas
from proving_ground.schemas.base_image import (
    BaseImageBase,
    BaseImageCreate,
    BaseImageUpdate,
    BaseImageResponse,
    BaseImageBrief,
)
from proving_ground.schemas.golden_image import (
    GoldenImageBase,
    GoldenImageCreate,
    GoldenImageUpdate,
    GoldenImageResponse,
    GoldenImageBrief,
    GoldenImageImportRequest,
)

__all__ = [
    "UserBase",
    "UserCreate",
    "UserUpdate",
    "UserResponse",
    "LoginRequest",
    "TokenResponse",
    "NetworkBase",
    "NetworkCreate",
    "NetworkUpdate",
    "NetworkResponse",
    "VMBase",
    "VMCreate",
    "VMUpdate",
    "VMResponse",
    "RangeBase",
    "RangeCreate",
    "RangeUpdate",
    "RangeResponse",
    "RangeDetailResponse",
    "EventLogCreate",
    "EventLogResponse",
    "EventLogList",
    "BlueprintCreate",
    "BlueprintUpdate",
    "BlueprintResponse",
    "BlueprintDetailResponse",
    "InstanceDeploy",
    "InstanceResponse",
    "BlueprintConfig",
    "NetworkConfig",
    "VMConfig",
    "SnapshotBase",
    "SnapshotCreate",
    "SnapshotUpdate",
    "SnapshotResponse",
    "SnapshotBrief",
    # Image Library
    "BaseImageBase",
    "BaseImageCreate",
    "BaseImageUpdate",
    "BaseImageResponse",
    "BaseImageBrief",
    "GoldenImageBase",
    "GoldenImageCreate",
    "GoldenImageUpdate",
    "GoldenImageResponse",
    "GoldenImageBrief",
    "GoldenImageImportRequest",
]
