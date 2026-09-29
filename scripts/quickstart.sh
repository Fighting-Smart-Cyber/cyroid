#!/usr/bin/env bash
# Stand CYROID up on a throwaway Kubernetes cluster, on one machine, in one command.
#
#   ./scripts/quickstart.sh              # create the cluster and install the release in VERSION
#   ./scripts/quickstart.sh --version 0.56.0
#   ./scripts/quickstart.sh --delete     # remove the cluster and everything in it
#
# WHY k3d. CYROID runs in Kubernetes -- ranges are namespaces, capabilities are Helm releases,
# and placement is a scheduling decision (ADR-0012). Docker Compose ran an earlier design and is
# being removed, so a quickstart built on it teaches the wrong thing. But requiring a novice to
# bring up a cluster first is a wall. k3d runs k3s inside Docker: one command, a real Kubernetes
# API, a real ingress, and `--delete` leaves nothing behind.
#
# WHAT THIS DOES NOT INSTALL, on purpose: KubeVirt, CDI and Multus. Those are for ranges whose
# machines are VMs, and a VM needs /dev/kvm, which a container on a laptop does not have -- the
# fallback is software emulation, which boots in minutes and would make a first run feel broken.
# Without them the platform, the learner record and capability-based ranges (a package installed
# as a Helm release, seeded, verified) all work, which is what SD-1 demonstrates. Add them later
# on a machine with nested virtualisation; scripts/setup-multus-k3s.sh is the next step.
#
# WHAT IT NEEDS: docker, k3d, kubectl, helm. It checks and tells you what is missing.
set -euo pipefail

CLUSTER="${CYROID_CLUSTER:-cyroid}"
HOST="${CYROID_HOST:-cyroid.localhost}"
GHCR="${CYROID_IMAGE_REPO:-ghcr.io/fighting-smart-cyber}"
# The chart bundles MinIO for object storage and has no default image for it, because there is no
# anonymously pullable MinIO left: docker.io/minio/minio is gone and quay.io answers 401 without
# credentials. CI mirrors it beside the platform images as `cyroid-storage`, which is the only
# copy a public install can reach.
CHART_DIR="$(cd "$(dirname "$0")/.." && pwd)/deploy/helm/proving-ground"
VERSION="$(cat "$(dirname "$0")/../VERSION" 2>/dev/null || echo "")"
# PINNED, and not incidentally. k3d defaults to whatever k3s shipped with the k3d release, so an
# unpinned quickstart hands a different Kubernetes to every user and a different one again next
# month -- the opposite of deterministic. It also matters concretely: k3d 5.9.0's default,
# k3s v1.35.5, shut itself down during startup on the machine this was written on
# ("cloud-controller-manager exited: ... configmaps extension-apiserver-authentication is
# forbidden"), leaving a cluster that reported 1/1 servers and refused every connection. v1.31.5
# comes up clean in about twenty seconds. Move this deliberately, after testing it.
K3S_IMAGE="${CYROID_K3S_IMAGE:-rancher/k3s:v1.31.5-k3s1}"
HTTP_PORT="${CYROID_HTTP_PORT:-8080}"
HTTPS_PORT="${CYROID_HTTPS_PORT:-8443}"
ACTION="install"

while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --delete)  ACTION="delete"; shift ;;
    --host)    HOST="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "$ACTION" = "delete" ]; then
  k3d cluster delete "$CLUSTER"
  echo "cluster '$CLUSTER' deleted. Nothing of it remains on this machine."
  exit 0
fi

# ---------------------------------------------------------------- preflight
missing=""
for tool in docker k3d kubectl helm; do
  command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
if [ -n "$missing" ]; then
  echo "missing:$missing" >&2
  echo "" >&2
  echo "  docker   https://docs.docker.com/get-docker/" >&2
  echo "  k3d      brew install k3d       | curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash" >&2
  echo "  kubectl  brew install kubectl   | https://kubernetes.io/docs/tasks/tools/" >&2
  echo "  helm     brew install helm      | https://helm.sh/docs/intro/install/" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "docker is installed but not running -- start Docker Desktop (or your engine) and retry." >&2
  exit 1
fi
if [ -z "$VERSION" ]; then
  echo "no version: pass --version, or run this from a clone that has a VERSION file." >&2
  exit 1
fi

echo "==> CYROID $VERSION on k3d cluster '$CLUSTER'"

# ------------------------------------------------------------------ cluster
# The loadbalancer publishes k3s's built-in Traefik on high ports, so this needs no root and does
# not collide with anything already on 80/443. `--wait` returns when the API is actually up.
create_cluster() {
  k3d cluster create "$CLUSTER" \
    --image "$K3S_IMAGE" \
    --port "${HTTP_PORT}:80@loadbalancer" \
    --port "${HTTPS_PORT}:443@loadbalancer" \
    --wait
}

if k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
  echo "==> cluster '$CLUSTER' already exists, reusing it"
  # Existence is not readiness. `k3d cluster list` reported `1/1` servers for a cluster whose API
  # server had shut down on startup, and every command after that failed with an EOF that looked
  # like a problem with the install rather than with the cluster. Ask the API itself.
  if ! kubectl --context "k3d-$CLUSTER" get --raw /readyz >/dev/null 2>&1; then
    echo "    it exists but is not answering -- recreating it"
    k3d cluster delete "$CLUSTER"
    create_cluster
  fi
else
  echo "==> creating the cluster"
  create_cluster
fi
kubectl config use-context "k3d-$CLUSTER" >/dev/null

# ------------------------------------------------------------------ install
# Everything the platform needs is in the chart: Postgres, Redis and MinIO as pods on the cluster's
# default storage class. That is not how a production install should run (ADR-0012 says the
# environment supplies them, and --postgres-url is how you point at a managed database) -- but for
# one machine and a throwaway cluster it is the difference between one command and five.
echo "==> installing the chart"
# Deliberately NOT --wait. The likeliest first-run failure is that no public image exists for
# this version yet, and `--wait` answers that with ten minutes of silence and then a timeout that
# names nothing. Installing without it and inspecting the pods lets the next block say which
# image could not be pulled, which is the one thing the user needs to know.
helm upgrade --install cyroid "$CHART_DIR" \
  --namespace pg-system --create-namespace \
  --set image.registry="$GHCR" \
  --set image.namePrefix=cyroid- \
  --set image.tag="$VERSION" \
  --set ingress.host="$HOST" \
  --set minio.image="$GHCR/cyroid-storage:$VERSION" \
  --set imagePullSecret=

echo "==> waiting for the platform to come up (up to 5 minutes)"
deadline=$(( $(date +%s) + 300 ))
while :; do
  # `pg-api`, not `cyroid-api`: the workload names are fixed in the chart templates and are
  # independent of both the release name and image.namePrefix. Renaming them is a chart
  # migration (Helm replaces rather than renames a Deployment), so they are left alone here.
  if kubectl -n pg-system rollout status deploy/pg-api --timeout=10s >/dev/null 2>&1; then
    break
  fi
  # A pull failure will never resolve on its own, so say so immediately rather than at the
  # deadline. This is the case a public user hits when no image has been published for the
  # version in VERSION -- which is true of any version before the first GHCR publish.
  bad="$(kubectl -n pg-system get pods -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}{.state.waiting.reason}{" "}{..image}{"\n"}{end}{end}' 2>/dev/null \
    | grep -E '^(ImagePullBackOff|ErrImagePull)' | head -1 || true)"
  if [ -n "$bad" ]; then
    echo "" >&2
    echo "FAILED: an image could not be pulled:" >&2
    echo "  $bad" >&2
    echo "" >&2
    echo "  If that image is under ghcr.io, no public image exists for $VERSION yet." >&2
    echo "  Public images are published per release by the publish-images-ghcr job; a version" >&2
    echo "  released before that job existed has none. Try the latest release:" >&2
    echo "" >&2
    echo "    ./scripts/quickstart.sh --version <a release with published images>" >&2
    echo "" >&2
    echo "  Or build them yourself and point at your own registry:" >&2
    echo "    helm upgrade --install cyroid $CHART_DIR -n pg-system \\" >&2
    echo "      --set image.registry=<your registry> --set image.tag=<your tag>" >&2
    exit 1
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "" >&2
    echo "the platform did not become ready within 5 minutes. What is running:" >&2
    kubectl -n pg-system get pods >&2
    exit 1
  fi
  sleep 5
done

echo ""
echo "==> ready"
echo ""
echo "  https://${HOST}:${HTTPS_PORT}/"
echo ""
echo "  The certificate is self-signed, so the browser will warn once. ${HOST} resolves to"
echo "  127.0.0.1 without any /etc/hosts entry: .localhost is reserved for exactly this (RFC 6761)."
echo ""
echo "  First account: register in the UI. The first user to register becomes the administrator,"
echo "  so do it before anyone else can reach the host."
echo ""
echo "  kubectl -n pg-system get pods        what is running"
echo "  kubectl -n pg-system logs deploy/pg-api -f"
echo "  ./scripts/quickstart.sh --delete     remove the cluster and everything in it"
