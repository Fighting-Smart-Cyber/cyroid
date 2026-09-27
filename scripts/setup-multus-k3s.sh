#!/usr/bin/env bash
# Install Multus CNI on a k3s node so range networks work — COSMOS PG-303.
#
# The upstream manifest assumes a stock Kubernetes layout and is wrong about k3s in four
# separate ways. Each was found the hard way; each is corrected below with the reason, because
# every one of them fails silently or misleadingly.
#
#   1. k3s keeps its CNI *config* in /var/lib/rancher/k3s/agent/etc/cni/net.d, not /etc/cni/net.d.
#   2. k3s keeps its CNI *binaries* in /var/lib/rancher/k3s/data/cni, not /opt/cni/bin — so the
#      shim lands where containerd will never look for it.
#   3. `binDir` and `cniConfigDir` in the daemon config are resolved in the multus *container's*
#      mount namespace, not the host's. k3s's binaries are absolute symlinks into a hashed data
#      directory, which dangle inside the container: present in `ls`, unopenable by `stat`. This
#      is the one that costs hours — the plugin is visibly right there and still "not found".
#   4. k3s's bundled plugin set has no `static` IPAM, which is what a range network needs in order
#      to give a learner the address the exercise text promises.
#
# Idempotent. Safe to re-run.
#
# Pinned (whiteboard rule 8: pin everything). The upstream manifest -- even at a release tag --
# names the image `:snapshot-thick`, which moves with `master`; the image is therefore patched to
# a release digest after apply. Bumping Multus means changing MULTUS_VERSION and MULTUS_DIGEST
# together; the digest is the one for the `<version>-thick` tag:
#   curl -sI -H "Authorization: Bearer $(curl -s 'https://ghcr.io/token?scope=repository:k8snetworkplumbingwg/multus-cni:pull' | jq -r .token)" \
#        -H 'Accept: application/vnd.oci.image.index.v1+json' \
#        https://ghcr.io/v2/k8snetworkplumbingwg/multus-cni/manifests/v4.3.1-thick | grep -i docker-content-digest

set -euo pipefail

MULTUS_VERSION="${MULTUS_VERSION:-v4.3.1}"
MULTUS_DIGEST="${MULTUS_DIGEST:-sha256:a357a79359b80ddd5f4699117946cacf3b54f5baecf7944535594863e875fc25}"
MULTUS_IMAGE="ghcr.io/k8snetworkplumbingwg/multus-cni@${MULTUS_DIGEST}"
MULTUS_MANIFEST="${MULTUS_MANIFEST:-https://raw.githubusercontent.com/k8snetworkplumbingwg/multus-cni/${MULTUS_VERSION}/deployments/multus-daemonset-thick.yml}"
CNI_PLUGINS_VERSION="${CNI_PLUGINS_VERSION:-v1.6.2}"
K3S_CNI_CONF_DIR=/var/lib/rancher/k3s/agent/etc/cni/net.d
K3S_CNI_BIN_DIR=/var/lib/rancher/k3s/data/cni
STD_CNI_BIN_DIR=/opt/cni/bin

say() { printf '\n== %s\n' "$1"; }

say "real CNI plugin binaries into ${STD_CNI_BIN_DIR}"
# Copies, not symlinks: the multus container mounts this directory, and a symlink into k3s's
# hashed data dir does not resolve inside that mount namespace.
sudo mkdir -p "$STD_CNI_BIN_DIR"
k3s_cni="$(readlink -f "${K3S_CNI_BIN_DIR}/cni")"
for plugin in bandwidth bridge firewall flannel host-local loopback portmap cni; do
  sudo cp -f "$k3s_cni" "${STD_CNI_BIN_DIR}/${plugin}"
done

say "upstream reference plugins ${CNI_PLUGINS_VERSION} (k3s ships no 'static' IPAM)"
tmp="$(mktemp -d)"
curl -sSL -o "${tmp}/cni-plugins.tgz" \
  "https://github.com/containernetworking/plugins/releases/download/${CNI_PLUGINS_VERSION}/cni-plugins-linux-amd64-${CNI_PLUGINS_VERSION}.tgz"
sudo tar -xzf "${tmp}/cni-plugins.tgz" -C "$STD_CNI_BIN_DIR" ./static
rm -rf "$tmp"

say "multus daemonset ${MULTUS_VERSION}, image pinned to ${MULTUS_DIGEST}"
kubectl apply -f "$MULTUS_MANIFEST"
kubectl -n kube-system set image ds/kube-multus-ds kube-multus="$MULTUS_IMAGE" >/dev/null
init_containers="$(kubectl -n kube-system get ds kube-multus-ds -o jsonpath='{range .spec.template.spec.initContainers[*]}{.name}{"\n"}{end}')"
for c in $init_containers; do
  kubectl -n kube-system set image ds/kube-multus-ds "${c}=${MULTUS_IMAGE}" >/dev/null
done

say "repointing volumes at k3s's directories"
# volumes[0] is the CNI config dir, volumes[1] the binary dir. cnibin stays /opt/cni/bin so the
# container sees the real binaries placed above; the shim is copied into k3s's dir separately.
kubectl -n kube-system patch ds kube-multus-ds --type=json -p "$(cat <<PATCH
[{"op":"replace","path":"/spec/template/spec/volumes/0/hostPath/path","value":"${K3S_CNI_CONF_DIR}"},
 {"op":"replace","path":"/spec/template/spec/volumes/1/hostPath/path","value":"${STD_CNI_BIN_DIR}"}]
PATCH
)"

say "daemon config — container-namespace paths"
current="$(kubectl -n kube-system get cm multus-daemon-config -o jsonpath='{.data.daemon-config\.json}')"
updated="$(printf '%s' "$current" | python3 -c '
import json, sys
config = json.load(sys.stdin)
# Both are container paths. cniConfigDir unset means multus generates its config and never writes
# it: no error, no file, and containerd quietly keeps using flannel alone.
config["cniConfigDir"] = "/host/etc/cni/net.d"
config["binDir"] = "/opt/cni/bin"
print(json.dumps(config))
')"
kubectl -n kube-system create cm multus-daemon-config \
  --from-literal=daemon-config.json="$updated" --dry-run=client -o yaml | kubectl apply -f -

kubectl -n kube-system delete pod -l app=multus --wait=true >/dev/null 2>&1 || true
kubectl -n kube-system rollout status ds/kube-multus-ds --timeout=300s

say "shim into k3s's binary directory, where containerd looks"
sudo cp -f "${STD_CNI_BIN_DIR}/multus-shim" "${K3S_CNI_BIN_DIR}/multus-shim"

say "verifying"
for i in $(seq 1 30); do
  [ -f "${K3S_CNI_CONF_DIR}/00-multus.conf" ] && break
  sleep 2
done
if sudo test -f "${K3S_CNI_CONF_DIR}/00-multus.conf"; then
  echo "OK   00-multus.conf written — multus is in the CNI chain"
else
  echo "FAIL 00-multus.conf absent; multus generated its config but did not persist it"
  exit 1
fi
echo
echo "Now prove it end to end:  python scripts/smoke-range-networks.py"
