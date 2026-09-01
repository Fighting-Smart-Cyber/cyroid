"""Deploys run on their own queue so they can be throttled independently.

Concurrency is enforced by the worker consuming this queue (see the
worker-deploy service in docker-compose.yml), not by a rate limiter.
"""


def test_deploy_task_is_on_the_deploys_queue():
    from proving_ground.tasks.deployment import deploy_range_task

    assert deploy_range_task.queue_name == "deploys"


def test_other_tasks_stay_on_the_default_queue():
    """Only deploys are throttled; everything else keeps full parallelism."""
    from proving_ground.tasks.deployment import teardown_range_task
    from proving_ground.tasks.vm_tasks import start_vm_task, stop_vm_task

    for actor in (teardown_range_task, start_vm_task, stop_vm_task):
        assert actor.queue_name == "default", f"{actor.actor_name} must not be throttled"


def test_queue_name_comes_from_config():
    from proving_ground.config import get_settings

    assert get_settings().deploy_queue_name == "deploys"
