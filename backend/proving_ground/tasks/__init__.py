# proving_ground/tasks/__init__.py
"""Dramatiq task definitions for async operations."""
import dramatiq
from dramatiq.brokers.redis import RedisBroker
from proving_ground.config import get_settings

settings = get_settings()

# Configure Redis broker
redis_broker = RedisBroker(url=settings.redis_url)
dramatiq.set_broker(redis_broker)

from .deployment import deploy_range_task, teardown_range_task
from .vm_tasks import start_vm_task, stop_vm_task
from .blueprint_export import export_blueprint_async
from .pool import refill_pool_task

# Importing the module is what registers its actor with the broker. Without
# this line the worker runs `dramatiq proving_ground.tasks` with no
# install_catalog_item_async defined, every one-click install is enqueued to
# nobody, and the progress modal waits forever on a message that went to the
# dead-letter queue.
from .catalog_install import install_catalog_item_async
from .blueprint_import import import_blueprint_async

__all__ = [
    "deploy_range_task",
    "teardown_range_task",
    "start_vm_task",
    "stop_vm_task",
    "export_blueprint_async",
    "refill_pool_task",
    "install_catalog_item_async",
    "import_blueprint_async",
]
