"""`KubernetesApiClient` reads hook logs raw.

`kubernetes_asyncio` JSON-decodes any response body it can and then `str()`s the result, so a
verify hook's one line of JSON -- the contract with hook authors -- came back as the Python repr
`{'passed': True, ...}`, which no longer parses, and the runtime fell back to `raw_output`.
Observed on pg-devtest 2026-09-15; every verify() evidence object had been silently downgraded
to a string of itself.
"""

from types import SimpleNamespace

from proving_ground.capability.kubernetes_client import KubernetesApiClient

RAW = '{"passed": true, "convoy_id": "C-17"}\n'


class RawResponse:
    async def text(self):
        return RAW


class CoreStub:
    def __init__(self):
        self.calls = []

    async def list_namespaced_pod(self, *, namespace, label_selector):
        return SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name="hook-1"))])

    async def read_namespaced_pod_log(self, *, name, namespace, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("_preload_content", True):
            # What the library does to a JSON body when it is allowed to preload it.
            import json

            return str(json.loads(RAW))
        return RawResponse()


async def test_a_json_log_line_survives_the_client_verbatim():
    client = KubernetesApiClient.__new__(KubernetesApiClient)
    client._core = CoreStub()
    logs = await client._job_logs(namespace="pg-x", name="verify")
    assert logs == RAW
    assert client._core.calls == [{"_preload_content": False}]


class TestConnectPicksTheRightConfig:
    async def test_a_pod_uses_in_cluster_config(self, monkeypatch):
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
        monkeypatch.delenv("KUBECONFIG", raising=False)
        picked = []

        async def in_cluster():
            picked.append("in-cluster")

        async def from_kubeconfig(*a, **k):
            picked.append("kubeconfig")

        monkeypatch.setattr(KubernetesApiClient, "in_cluster", in_cluster)
        monkeypatch.setattr(KubernetesApiClient, "from_kubeconfig", from_kubeconfig)
        await KubernetesApiClient.connect()
        assert picked == ["in-cluster"]

    async def test_an_explicit_kubeconfig_wins_even_in_a_pod(self, monkeypatch, tmp_path):
        # A pod pointed at a vcluster's kubeconfig must use it, not the host it runs on.
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
        monkeypatch.setenv("KUBECONFIG", str(tmp_path / "vcluster.yaml"))
        picked = []

        async def from_kubeconfig(*a, **k):
            picked.append("kubeconfig")

        monkeypatch.setattr(KubernetesApiClient, "from_kubeconfig", from_kubeconfig)
        await KubernetesApiClient.connect()
        assert picked == ["kubeconfig"]

    async def test_a_shell_uses_kubeconfig(self, monkeypatch):
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        monkeypatch.delenv("KUBECONFIG", raising=False)
        picked = []

        async def from_kubeconfig(*a, **k):
            picked.append("kubeconfig")

        monkeypatch.setattr(KubernetesApiClient, "from_kubeconfig", from_kubeconfig)
        await KubernetesApiClient.connect()
        assert picked == ["kubeconfig"]
