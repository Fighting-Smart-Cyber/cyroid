#!/usr/bin/env bash
# Install the Era B substrate on a bare Ubuntu host: k3s + KubeVirt + CDI + Multus + Flux.
#
# This is the whole of what Claude Code may do to pg-preprod. PROVING GROUND itself is installed
# afterwards, by a person, with scripts/install-k8s.sh. Every version here is the one proven on
# pg-devtest (docs/plans/2026-09-10-k3s-kubevirt-substrate.md and its 2026-09-15 section).
#
# Idempotent: re-running converges the host to these versions and touches nothing else. It needs
# sudo, curl, python3, a real /dev/kvm (nested virtualisation on a VM host), and egress to
# github.com, get.k3s.io, quay.io and ghcr.io. Air-gapped delivery is SRE-7 / Zarf, later.
#
#   ./scripts/setup-substrate-k3s.sh
#
# k3s's Traefik is ENABLED here: on this profile PROVING GROUND runs in the cluster and Traefik
# is its ingress. (pg-devtest was built with it disabled, for a Compose stack that never came;
# this script re-enables it there.)

set -euo pipefail

K3S_VERSION="${K3S_VERSION:-v1.36.4+k3s1}"
KUBEVIRT_VERSION="${KUBEVIRT_VERSION:-v1.9.0}"
CDI_VERSION="${CDI_VERSION:-v1.66.1}"
FLUX_VERSION="${FLUX_VERSION:-v2.9.5}"

here="$(cd "$(dirname "$0")" && pwd)"
say() { printf '\n== %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

for tool in curl python3 sudo; do have "$tool" || { echo "missing: $tool" >&2; exit 1; }; done
arch="$(uname -m)"; case "$arch" in x86_64) arch=amd64 ;; aarch64|arm64) arch=arm64 ;; esac

say "hardware virtualisation"
if [ ! -e /dev/kvm ]; then
  echo "FAIL /dev/kvm is absent. KubeVirt would fall back to software emulation (8x slower and" >&2
  echo "     not what this substrate is for). On a Proxmox guest: cpu: host, nested=Y on the host." >&2
  exit 1
fi
echo "OK   /dev/kvm present"

# ------------------------------------------------------------------------ k3s
say "k3s ${K3S_VERSION} (Traefik enabled)"
want_install=1
if have k3s && k3s --version | grep -q "$K3S_VERSION"; then
  # Same version, but a unit still carrying `--disable traefik` (pg-devtest's original build)
  # has to be rewritten; the installer does that when re-run.
  if ! grep -q "'traefik'" /etc/systemd/system/k3s.service 2>/dev/null; then want_install=0; fi
fi
if [ "$want_install" = 1 ]; then
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" sh -s - server \
    --write-kubeconfig-mode 644
else
  echo "already at ${K3S_VERSION}"
fi
mkdir -p "$HOME/.kube"
sudo cp /etc/rancher/k3s/k3s.yaml "$HOME/.kube/config"
sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config"; chmod 600 "$HOME/.kube/config"
export KUBECONFIG="$HOME/.kube/config"
grep -q 'KUBECONFIG=' "$HOME/.bashrc" 2>/dev/null || echo 'export KUBECONFIG=$HOME/.kube/config' >> "$HOME/.bashrc"
# The API server 403s for a few seconds after a (re)start -- a race, not a regression.
for i in $(seq 1 60); do kubectl get nodes >/dev/null 2>&1 && break; sleep 2; done
kubectl wait node --all --for=condition=Ready --timeout=5m >/dev/null
kubectl get nodes

# ------------------------------------------------------------------- KubeVirt
say "KubeVirt ${KUBEVIRT_VERSION}"
kubectl apply -f "https://github.com/kubevirt/kubevirt/releases/download/${KUBEVIRT_VERSION}/kubevirt-operator.yaml" >/dev/null
kubectl apply -f "https://github.com/kubevirt/kubevirt/releases/download/${KUBEVIRT_VERSION}/kubevirt-cr.yaml" >/dev/null
kubectl -n kubevirt wait kv kubevirt --for=condition=Available --timeout=15m >/dev/null
echo "phase: $(kubectl -n kubevirt get kv kubevirt -o jsonpath='{.status.phase}')  emulation: $(kubectl -n kubevirt get kv kubevirt -o jsonpath='{.spec.configuration.developerConfiguration.useEmulation}' | sed 's/^$/unset (real KVM)/')"

if ! have virtctl || ! virtctl version --client 2>/dev/null | grep -q "${KUBEVIRT_VERSION#v}"; then
  sudo curl -sSL -o /usr/local/bin/virtctl \
    "https://github.com/kubevirt/kubevirt/releases/download/${KUBEVIRT_VERSION}/virtctl-${KUBEVIRT_VERSION}-linux-${arch}"
  sudo chmod 0755 /usr/local/bin/virtctl
fi
echo "virtctl: $(virtctl version --client 2>/dev/null | grep -o 'GitVersion:"[^"]*"' | head -1)"

# ------------------------------------------------------------------------ CDI
say "CDI ${CDI_VERSION}"
kubectl apply -f "https://github.com/kubevirt/containerized-data-importer/releases/download/${CDI_VERSION}/cdi-operator.yaml" >/dev/null
kubectl apply -f "https://github.com/kubevirt/containerized-data-importer/releases/download/${CDI_VERSION}/cdi-cr.yaml" >/dev/null
kubectl wait cdi cdi --for=condition=Available --timeout=10m >/dev/null
echo "phase: $(kubectl get cdi cdi -o jsonpath='{.status.phase}')"

# --------------------------------------------------------------------- Multus
say "Multus (scripts/setup-multus-k3s.sh -- four k3s corrections, image pinned)"
bash "${here}/setup-multus-k3s.sh"

# ----------------------------------------------------------------------- Flux
say "Flux ${FLUX_VERSION}: source-controller and helm-controller only"
if ! have flux || ! flux version --client 2>/dev/null | grep -q "$FLUX_VERSION"; then
  tmp="$(mktemp -d)"
  curl -sSL -o "${tmp}/flux.tgz" \
    "https://github.com/fluxcd/flux2/releases/download/${FLUX_VERSION}/flux_${FLUX_VERSION#v}_linux_${arch}.tar.gz"
  tar -xzf "${tmp}/flux.tgz" -C "$tmp" flux
  sudo install -m 0755 "${tmp}/flux" /usr/local/bin/flux
  rm -rf "$tmp"
fi
# Not the toolkit: kustomize- and notification-controller would widen the accreditation boundary
# for nothing (ADR-0012, 2026-09-13 amendment). PG uses helm-controller as a chart installer.
flux install --components=source-controller,helm-controller >/dev/null
kubectl -n flux-system rollout status deploy/source-controller --timeout=5m >/dev/null
kubectl -n flux-system rollout status deploy/helm-controller --timeout=5m >/dev/null
kubectl -n flux-system get deploy

# ----------------------------------------------------------------- verifying
say "verifying -- the file, not the log"
ok=1
check() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; ok=0; fi; }
check "node Ready"                 "kubectl get nodes --no-headers | grep -q ' Ready '"
check "KubeVirt Deployed"          "[ \"\$(kubectl -n kubevirt get kv kubevirt -o jsonpath='{.status.phase}')\" = Deployed ]"
check "CDI Deployed"               "[ \"\$(kubectl get cdi cdi -o jsonpath='{.status.phase}')\" = Deployed ]"
check "00-multus.conf in the CNI chain" "sudo test -f /var/lib/rancher/k3s/agent/etc/cni/net.d/00-multus.conf"
check "Multus image pinned by digest" "kubectl -n kube-system get ds kube-multus-ds -o jsonpath='{.spec.template.spec.containers[0].image}' | grep -q '@sha256:'"
check "Flux helm-controller Available" "kubectl -n flux-system get deploy helm-controller -o jsonpath='{.status.availableReplicas}' | grep -q '^[1-9]'"
check "Traefik ingress class"      "kubectl get ingressclass traefik >/dev/null 2>&1"
check "local-path is the default StorageClass" "kubectl get sc local-path -o jsonpath='{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}' | grep -q true"
check "devices.kubevirt.io/kvm allocatable" "kubectl get node -o jsonpath='{.items[0].status.allocatable.devices\.kubevirt\.io/kvm}' | grep -qv '^0*$'"
[ "$ok" = 1 ] || { echo; echo "substrate is NOT ready"; exit 1; }

cat <<SUMMARY

substrate ready on $(hostname) ($(hostname -I | awk '{print $1}'))
  k3s ${K3S_VERSION} (Traefik on) · KubeVirt ${KUBEVIRT_VERSION} · CDI ${CDI_VERSION} · Multus $(kubectl -n kube-system get ds kube-multus-ds -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*@//' | cut -c1-19)… · Flux ${FLUX_VERSION}
  kubeconfig: $HOME/.kube/config

next, as the person installing PROVING GROUND:   ./scripts/install-k8s.sh
SUMMARY
