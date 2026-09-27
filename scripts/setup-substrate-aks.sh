#!/usr/bin/env bash
# Install the Era B substrate on an existing AKS cluster: KubeVirt + CDI + Multus + Traefik + Flux.
#
# The Azure counterpart of setup-substrate-k3s.sh. That script builds its own cluster (it installs
# k3s); this one does not -- AKS is created by `az aks create`, and what the cluster needs after
# that is everything below. Derived from building devtest-az and preprod-az on 2026-09-26, so
# every version here is one that has actually run on AKS.
#
#   KUBECONFIG=/path/to/aks.kubeconfig ./scripts/setup-substrate-aks.sh
#
# Idempotent. Safe to re-run. It touches no Azure resource -- create the cluster first:
#
#   az aks create -g <rg> -n <name> -l <region> --kubernetes-version 1.36.4 \
#     --node-count 3 --node-vm-size <sku> --network-plugin azure \
#     --network-plugin-mode overlay --pod-cidr 10.102.0.0/16 \
#     --service-cidr 10.101.0.0/16 --dns-service-ip 10.101.0.10
#
# ---------------------------------------------------------------------------- what differs on AKS
#
# 1. KubeVirt's virt-operator cannot schedule. Its Deployment carries a
#    `requiredDuringScheduling` nodeAffinity demanding `node-role.kubernetes.io/control-plane` or
#    `/master`, and AKS's control plane is MANAGED -- no node carries either label, so every pod
#    is Unschedulable with "0/N nodes are available: N node(s) didn't match Pod's node
#    affinity/selector". k3s's single node happens to have the label, which is exactly why this
#    was invisible until the first AKS cluster.
#
#    Patching the Deployment is not enough: virt-operator builds its install-strategy Job from
#    the same template, so the Job stays Pending and the KubeVirt CR never leaves Deploying. The
#    fix that holds is `spec.infra.nodePlacement` and `spec.workloads.nodePlacement` on the CR,
#    which is the field KubeVirt provides for this. Labelling a worker
#    `node-role.kubernetes.io/control-plane` also works and is a lie about the node's role.
#
# 2. Multus needs NONE of setup-multus-k3s.sh's four corrections. Those exist because k3s keeps
#    CNI config and binaries in its own directories and symlinks the plugins absolutely into a
#    hashed path, so they dangle inside a container. AKS is a stock layout -- /etc/cni/net.d and
#    /opt/cni/bin -- the binaries are REAL FILES, and the plugin set already includes `static`,
#    which is the IPAM a range network needs and the one k3s lacks. The upstream manifest applies
#    unmodified, and Multus writes 00-multus.conf ahead of 15-azure-swift-overlay.conflist.
#
# 3. AKS ships no ingress controller. k3s bundles Traefik; here it is installed by helm, with the
#    same class name so the chart's ingress and its TLSStore work unchanged.
#
# 4. pg-data must be ReadWriteMany. It is mounted by the API and both workers -- three pods -- and
#    Azure Disk is RWO and zonal, so on a multi-node cluster the pods the scheduler places
#    elsewhere sit in ContainerCreating with FailedAttachVolume while the install reports success.
#    That is install-k8s.sh's business, not this script's, but it is the next thing to get wrong:
#
#      ./scripts/install-k8s.sh --version X.Y.Z --host <name> --apps-host apps.<name> \
#        --data-access-mode ReadWriteMany --data-storage-class azurefile-csi
#
#    `--host` must be a DNS NAME. An Ingress rule rejects an IP; on a cluster reached by address,
#    <ip>.sslip.io is a name for the same address. Better: an Azure public IP's DNS label gives a
#    real FQDN for free, and cert-manager can then get a trusted certificate over HTTP-01 --
#    see --tls-secret-name.
#
# 5. CDI defaults to Block mode on Azure Disk and its importer cannot open the device. Pinned to
#    Filesystem below; without it no blueprint-based range can start. See the note at that step.
set -euo pipefail

KUBEVIRT_VERSION="${KUBEVIRT_VERSION:-v1.9.0}"
CDI_VERSION="${CDI_VERSION:-v1.66.1}"
MULTUS_VERSION="${MULTUS_VERSION:-v4.3.1}"
FLUX_VERSION="${FLUX_VERSION:-v2.9.5}"

say() { printf '\n== %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

for tool in kubectl helm curl tar; do have "$tool" || { echo "missing: $tool" >&2; exit 1; }; done

say "cluster"
kubectl get nodes >/dev/null || { echo "cannot reach a cluster: set KUBECONFIG" >&2; exit 1; }
nodes="$(kubectl get nodes --no-headers | wc -l | tr -d ' ')"
kubectl get nodes -o custom-columns='NAME:.metadata.name,ZONE:.metadata.labels.topology\.kubernetes\.io/zone,CPU:.status.capacity.cpu'
echo "${nodes} node(s)"

# Hardware virtualisation, before anything is installed on top of it. KubeVirt will happily fall
# back to software emulation, which is 8x slower and not what this substrate is for; on AKS the
# nested-virt capability comes from the VM SKU, so a wrong SKU fails here rather than mysteriously
# later.
say "hardware virtualisation on every node"
kubectl delete ds kvm-probe -n default --ignore-not-found >/dev/null 2>&1 || true
cat <<'PROBE' | kubectl apply -f - >/dev/null
apiVersion: apps/v1
kind: DaemonSet
metadata: {name: kvm-probe, namespace: default}
spec:
  selector: {matchLabels: {app: kvm-probe}}
  template:
    metadata: {labels: {app: kvm-probe}}
    spec:
      containers:
        - name: p
          image: mcr.microsoft.com/cbl-mariner/base/core:2.0
          securityContext: {privileged: true}
          command: ["sh","-c","test -e /dev/kvm && grep -qE 'vmx|svm' /proc/cpuinfo && echo KVM_OK && sleep 3600 || { echo KVM_MISSING; sleep 3600; }"]
          volumeMounts: [{name: dev, mountPath: /dev}]
      volumes: [{name: dev, hostPath: {path: /dev}}]
PROBE
kubectl rollout status ds/kvm-probe --timeout=5m >/dev/null
missing=0
for p in $(kubectl get pods -l app=kvm-probe -o name); do
  kubectl logs "$p" 2>/dev/null | grep -q KVM_OK || missing=1
done
kubectl delete ds kvm-probe --ignore-not-found >/dev/null 2>&1 || true
if [ "$missing" = 1 ]; then
  echo "FAIL /dev/kvm or the vmx/svm CPU flag is absent on at least one node." >&2
  echo "     The node SKU has to support nested virtualisation -- Dsv3/Dsv5 do; check the SKU" >&2
  echo "     and that no Deny-mode pod-security policy blocks a privileged pod." >&2
  exit 1
fi
echo "OK   /dev/kvm and vmx/svm on all ${nodes} node(s); a privileged DaemonSet was admitted"

say "KubeVirt ${KUBEVIRT_VERSION}"
kubectl apply -f "https://github.com/kubevirt/kubevirt/releases/download/${KUBEVIRT_VERSION}/kubevirt-operator.yaml" >/dev/null
kubectl apply -f "https://github.com/kubevirt/kubevirt/releases/download/${KUBEVIRT_VERSION}/kubevirt-cr.yaml" >/dev/null
# Note 1 above. Set BEFORE waiting: without it the CR never becomes Available and the wait below
# burns its full timeout on a pod that can never be scheduled.
kubectl -n kubevirt patch kv kubevirt --type=merge -p '{"spec":{"infra":{"nodePlacement":{"nodeSelector":{"kubernetes.io/os":"linux"}}},"workloads":{"nodePlacement":{"nodeSelector":{"kubernetes.io/os":"linux"}}}}}' >/dev/null
kubectl -n kubevirt patch deploy virt-operator --type=json \
  -p '[{"op":"remove","path":"/spec/template/spec/affinity/nodeAffinity/requiredDuringSchedulingIgnoredDuringExecution"}]' >/dev/null 2>&1 || true
kubectl -n kubevirt wait kv kubevirt --for=condition=Available --timeout=15m >/dev/null
echo "phase: $(kubectl -n kubevirt get kv kubevirt -o jsonpath='{.status.phase}')"

say "CDI ${CDI_VERSION}"
kubectl apply -f "https://github.com/kubevirt/containerized-data-importer/releases/download/${CDI_VERSION}/cdi-operator.yaml" >/dev/null
kubectl apply -f "https://github.com/kubevirt/containerized-data-importer/releases/download/${CDI_VERSION}/cdi-cr.yaml" >/dev/null
kubectl wait cdi cdi --for=condition=Available --timeout=10m >/dev/null
echo "phase: $(kubectl get cdi cdi -o jsonpath='{.status.phase}')"

# ---------------------------------------------------------------------------------------------
# Note 5. CDI picks Block mode on Azure Disk, and its importer cannot open the device.
#
# CDI auto-discovers a StorageProfile per storage class. For every disk.csi.azure.com class it
# concludes `volumeMode: Block`, because the driver supports it. The importer then runs as
# runAsUser 107, runAsNonRoot, capabilities dropped, and tries to open the raw device:
#
#     blockdev: cannot open /dev/cdi-block-volume: Permission denied
#
# The DataVolume never completes, the VM sits in Scheduling until the deploy times out, and the
# range fails with DataVolumeError -- after the capability has already installed, so the failure
# looks partial rather than structural.
#
# k3s cannot reach this: local-path does not support Block at all, so CDI falls back to
# Filesystem there and every Proxmox environment has silently taken the working path. It is also
# invisible to a containerDisk VM test, which bypasses CDI entirely -- which is how both Azure
# clusters came up "validated" while no blueprint-based range could ever have started on them.
#
# Pinning the profile to Filesystem is the fix: CDI writes a disk.img into a filesystem volume
# instead of dd-ing to a device it has no permission for. Measured either side of this on
# preprod-az: Block -> importer CrashLoopBackOff and 8m32s stuck in Scheduling; Filesystem ->
# importer Completed and the VM Running inside a minute.
say "CDI storage profile: Filesystem on Azure Disk (note 5)"
cat <<'PROFILE' | kubectl apply -f - >/dev/null
apiVersion: cdi.kubevirt.io/v1beta1
kind: StorageProfile
metadata: {name: default}
spec:
  claimPropertySets:
    - accessModes: [ReadWriteOnce]
      volumeMode: Filesystem
PROFILE
echo "volumeMode: $(kubectl get storageprofile default -o jsonpath='{.spec.claimPropertySets[0].volumeMode}')"

say "Multus ${MULTUS_VERSION} (upstream manifest -- note 2: no k3s corrections here)"
kubectl apply -f "https://raw.githubusercontent.com/k8snetworkplumbingwg/multus-cni/${MULTUS_VERSION}/deployments/multus-daemonset-thick.yml" >/dev/null
kubectl -n kube-system rollout status ds/kube-multus-ds --timeout=10m >/dev/null
echo "ready: $(kubectl -n kube-system get ds kube-multus-ds -o jsonpath='{.status.numberReady}')/$(kubectl -n kube-system get ds kube-multus-ds -o jsonpath='{.status.desiredNumberScheduled}')"

say "Traefik (note 3: AKS ships no ingress controller)"
helm repo add traefik https://traefik.github.io/charts >/dev/null 2>&1 || true
helm repo update traefik >/dev/null 2>&1
helm upgrade --install traefik traefik/traefik --namespace kube-system \
  --set ingressClass.enabled=true --set ingressClass.isDefaultClass=true \
  --set providers.kubernetesCRD.enabled=true --set providers.kubernetesIngress.enabled=true \
  --set service.type=LoadBalancer --wait --timeout 10m >/dev/null
for i in $(seq 1 60); do
  LB="$(kubectl -n kube-system get svc traefik -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
  [ -n "$LB" ] && break
  sleep 5
done
echo "LoadBalancer: ${LB:-<pending>}"

# source- and helm-controller ONLY. kustomize- and notification-controller would widen the
# accreditation boundary for nothing (ADR-0012, 2026-09-13 amendment), and `flux install` with no
# --components installs the lot including the image-automation controllers. PG uses
# helm-controller as a chart installer and nothing else.
say "Flux ${FLUX_VERSION}: source-controller and helm-controller only"
# Fetched to a temp dir rather than installed globally: this script runs from an operator's
# workstation, not on the cluster, and it has no business putting a binary on their PATH.
#
# The CLI is not a convenience here. `kubectl apply -f flux/install.yaml` -- the obvious
# alternative when the CLI is absent -- installs the WHOLE toolkit, including kustomize-,
# notification- and the two image-automation controllers. That is how devtest-az and preprod-az
# first came up with seven controllers instead of two, against ADR-0012's amendment. Only
# `flux install --components=` filters them.
FLUX_BIN=""
if have flux && flux version --client 2>/dev/null | grep -q "$FLUX_VERSION"; then
  FLUX_BIN="$(command -v flux)"
else
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m)"; case "$arch" in x86_64) arch=amd64 ;; aarch64|arm64) arch=arm64 ;; esac
  tmp="$(mktemp -d)"
  echo "fetching flux ${FLUX_VERSION} (${os}/${arch}) to a temp dir"
  curl -sSL -o "${tmp}/flux.tgz" \
    "https://github.com/fluxcd/flux2/releases/download/${FLUX_VERSION}/flux_${FLUX_VERSION#v}_${os}_${arch}.tar.gz"
  tar -xzf "${tmp}/flux.tgz" -C "$tmp" flux
  chmod 0755 "${tmp}/flux"
  FLUX_BIN="${tmp}/flux"
fi
"$FLUX_BIN" install --components=source-controller,helm-controller >/dev/null
kubectl -n flux-system rollout status deploy/source-controller --timeout=5m >/dev/null
kubectl -n flux-system rollout status deploy/helm-controller --timeout=5m >/dev/null
kubectl -n flux-system get deploy

say "verifying -- the cluster, not the log"
ok=1
check() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; ok=0; fi; }
check "all nodes Ready"              "[ \"\$(kubectl get nodes --no-headers | grep -c ' Ready ')\" = \"${nodes}\" ]"
check "KubeVirt Deployed"            "[ \"\$(kubectl -n kubevirt get kv kubevirt -o jsonpath='{.status.phase}')\" = Deployed ]"
check "virt-handler on every node"   "[ \"\$(kubectl -n kubevirt get ds virt-handler -o jsonpath='{.status.numberReady}')\" = \"${nodes}\" ]"
check "CDI Deployed"                 "[ \"\$(kubectl get cdi cdi -o jsonpath='{.status.phase}')\" = Deployed ]"
check "Multus on every node"         "[ \"\$(kubectl -n kube-system get ds kube-multus-ds -o jsonpath='{.status.numberReady}')\" = \"${nodes}\" ]"
check "NetworkAttachmentDefinition CRD" "kubectl get crd network-attachment-definitions.k8s.cni.cncf.io >/dev/null 2>&1"
check "Traefik ingress class"        "kubectl get ingressclass traefik >/dev/null 2>&1"
check "Traefik has a LoadBalancer IP" "[ -n \"\$(kubectl -n kube-system get svc traefik -o jsonpath='{.status.loadBalancer.ingress[0].ip}')\" ]"
check "Flux helm-controller Available" "kubectl -n flux-system get deploy helm-controller -o jsonpath='{.status.availableReplicas}' | grep -q '^[1-9]'"
check "no kustomize-controller"      "! kubectl -n flux-system get deploy kustomize-controller >/dev/null 2>&1"
check "devices.kubevirt.io/kvm on every node" \
  "[ \"\$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.allocatable.devices\.kubevirt\.io/kvm}{\"\n\"}{end}' | grep -cv '^0*\$')\" = \"${nodes}\" ]"
check "an RWX storage class exists"  "kubectl get sc azurefile-csi >/dev/null 2>&1"
check "CDI storage profile is Filesystem" \
  "[ \"\$(kubectl get storageprofile default -o jsonpath='{.spec.claimPropertySets[0].volumeMode}')\" = Filesystem ]"
[ "$ok" = 1 ] || { echo; echo "substrate is NOT ready"; exit 1; }

cat <<SUMMARY

substrate ready on $(kubectl config current-context)
  ${nodes} nodes · KubeVirt ${KUBEVIRT_VERSION} · CDI ${CDI_VERSION} · Multus ${MULTUS_VERSION} · Flux ${FLUX_VERSION}
  Traefik LoadBalancer: ${LB:-<pending>}

next, as the person installing PROVING GROUND -- note 4 above, both flags matter:

  ./scripts/install-k8s.sh --version <X.Y.Z> \\
    --host ${LB:-<ip>}.sslip.io --apps-host apps.${LB:-<ip>}.sslip.io \\
    --data-access-mode ReadWriteMany --data-storage-class azurefile-csi
SUMMARY
