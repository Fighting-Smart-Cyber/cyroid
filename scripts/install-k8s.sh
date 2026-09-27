#!/usr/bin/env bash
# Install or update PROVING GROUND *in* a Kubernetes cluster -- the path pg-preprod is on.
#
#   ./scripts/install-k8s.sh                        # the version in VERSION, as a Flux HelmRelease
#   ./scripts/install-k8s.sh --version 0.49.0       # a specific published release
#   ./scripts/install-k8s.sh --host pg.example.org  # the name the ingress answers to
#   ./scripts/install-k8s.sh --apps-host apps.example.org   # where a range's applications answer
#   ./scripts/install-k8s.sh --range-permissions namespace   # narrow the control plane's RBAC
#   ./scripts/install-k8s.sh --data-access-mode ReadWriteMany --data-storage-class azurefile-csi
#   ./scripts/install-k8s.sh --tls-secret-name pg-tls-letsencrypt   # a certificate of your own
#   ./scripts/install-k8s.sh --apps-tls-secret-name pg-tls   # ...and a generated one for the apps
#   ./scripts/install-k8s.sh --postgres-url postgresql://user:pw@host:5432/proving_ground
#
# --postgres-url switches the chart off its in-cluster Postgres and onto a database the
# environment supplies -- ADR-0012 says that is how every profile except k3s is meant to run, and
# on Azure it is a Flexible Server with managed backups and point-in-time restore instead of one
# pod on one volume. The URL carries a password, so it travels in the same Secret the registry
# token does and never appears in the HelmRelease spec.
#
# The database must already exist and be reachable from the cluster. Nothing migrates the data:
# an install that already has ranges and learners keeps them in the old database until somebody
# moves them deliberately.
#
# --tls-secret-name points the ingress at a secret something else maintains -- cert-manager, or a
# certificate you loaded by hand -- instead of the self-signed one the chart generates. The chart
# only generates `pg-tls`, so naming anything else turns generation off rather than leaving two
# certificates fighting over the same ingress.
#
# Before reaching for --apps-tls-secret-name: if you control the DNS zone, run
# scripts/setup-cert-manager-dns01.sh instead. DNS-01 issues a wildcard, so ONE trusted
# certificate covers the platform and every range application, and it works on a private address
# that no CA can reach. --apps-tls-secret-name is the HTTP-01 fallback, described next.
#
# --apps-tls-secret-name is the one the APPLICATIONS host uses, when it is not the same one. A
# wildcard cannot be issued over HTTP-01, so a certificate obtained that way covers `--host` and
# nothing beneath `--apps-host`; pointing both at it serves every learner's application a
# certificate for a name that is not theirs. `--apps-tls-secret-name pg-tls` keeps the generated
# wildcard for the applications and the real certificate for the platform. This script checks the
# secret you name and refuses the combination that does not work. On a host with a real DNS name that is how the
# browser warning goes away; a self-signed certificate warns however correct its SANs are.
#
# pg-data is mounted by the API and both workers -- three pods. ReadWriteOnce attaches to one
# node and permits many pods THERE, so the default is right on a single-node profile and wrong on
# any cluster a scheduler can spread across: the pods placed elsewhere sit in ContainerCreating
# with FailedAttachVolume while the install reports success. On a multi-node profile pass
# ReadWriteMany with a class that supports it -- on Azure `azurefile-csi`, not the default Azure
# Disk class, which is RWO and zonal. The chart refuses the broken combination when it can see
# the cluster has more than one node.
#   ./scripts/install-k8s.sh --source --tag <sha>   # dev: this checkout's chart, images at a sha
#
# --apps-host is a DNS suffix, and each range's applications answer on a label beneath it
# (`<range-id>.apps.example.org`). It needs a wildcard record pointing at this same ingress and a
# certificate the controller will serve for that name. Without it, no range application is
# published at all: on the platform's own host a training application is same-origin with the
# console and its JavaScript can read the operator's session out of the browser.
#
# The certificate the chart generates carries a `*.<apps-host>` SAN, and Traefik chooses a
# certificate by its SANs rather than by anything the Ingress declares, so a range's applications
# are served the same one as the platform. An install pointed at a secret of its own
# (ingress.tls.secretName) needs that wildcard in it, or every application a learner opens
# presents the controller's default certificate and a warning.
#
# The release is owned by Flux's helm-controller (already on the substrate): this script creates
# a HelmRepository pointing at the OCI repository CI publishes the chart to, and a HelmRelease at
# the version asked for. That is what lets the in-UI "Update" work in-cluster -- it advances the
# HelmRelease, and the same controller rolls it out. Running this script again is an update too.
# An install that was made with `helm upgrade --install` (0.47/0.48) is adopted: the HelmRelease
# names the same release and helm-controller upgrades what is there.
#
# Deterministic: same checkout, same inputs, same cluster state. What it needs beyond the checkout
# it installs pinned (helm, for --source) or asks for once and keeps (registry credentials, in
# ~/.config/pg/registry-credentials). Prerequisites: a cluster scripts/setup-substrate-k3s.sh has
# prepared, and a kubeconfig (KUBECONFIG, ~/.kube/config, or k3s's).
#
# Uninstall: `kubectl -n pg-system delete helmrelease pg`; the data PVCs and secrets are kept.

set -euo pipefail

HELM_VERSION="${HELM_VERSION:-v3.16.4}"
NAMESPACE="${PG_NAMESPACE:-pg-system}"
RELEASE="${PG_RELEASE:-pg}"
CRED_FILE="${PG_REGISTRY_CREDENTIALS:-$HOME/.config/pg/registry-credentials}"
# Where this organisation's images and chart live. Read from a file rather than defaulted here,
# because the engine is public and publishes no images: a default naming one private registry is
# wrong for every other reader, and it is what kept publish-public refusing. See scripts/registry.env.
if [ -f "${0%/*}/registry.env" ]; then . "${0%/*}/registry.env"; fi
REGISTRY_HOST="${PG_REGISTRY_HOST:-}"
IMAGE_REPO="${PG_IMAGE_REPO:-}"
MINIO_IMAGE="${PG_MINIO_IMAGE:-}"
CHART_REPOSITORY="${PG_CHART_REPOSITORY:-}"
CHART_OCI="${PG_CHART_OCI:-}"

cd "$(dirname "$0")/.."
CHART=deploy/helm/proving-ground
VERSION="$(tr -d '[:space:]' < VERSION)"
TAG=""
HOST=""
APPS_HOST=""
RANGE_PERMISSIONS=""
DATA_ACCESS_MODE=""
DATA_STORAGE_CLASS=""
TLS_SECRET_NAME=""
APPS_TLS_SECRET_NAME=""
POSTGRES_URL=""
SOURCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --tag)     TAG="$2"; shift 2 ;;
    --host)    HOST="$2"; shift 2 ;;
    --apps-host) APPS_HOST="$2"; shift 2 ;;
    --range-permissions) RANGE_PERMISSIONS="$2"; shift 2 ;;
    --data-access-mode)  DATA_ACCESS_MODE="$2"; shift 2 ;;
    --data-storage-class) DATA_STORAGE_CLASS="$2"; shift 2 ;;
    --tls-secret-name)   TLS_SECRET_NAME="$2"; shift 2 ;;
    --apps-tls-secret-name) APPS_TLS_SECRET_NAME="$2"; shift 2 ;;
    --postgres-url)      POSTGRES_URL="$2"; shift 2 ;;
    --source)  SOURCE=1; shift ;;
    # The whole header, not a line range: '2,34p' had already fallen 17 lines behind the block
    # it meant to print, so --help ended mid-sentence and omitted --source entirely.
    -h|--help) awk 'NR==1{next} /^#/{print;next} {exit}' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n== %s\n' "$1"; }

# An applications host with no platform host is the one combination that undoes what it is for:
# the platform's own Ingress carries no host rule, so it answers on the applications host too,
# and the application is back on the console's origin. The chart refuses to render it; saying so
# here means the operator is told before helm is.
# Narrowing takes the range verbs out of the cluster-wide role and puts them in a ClusterRole
# bound into each range's namespace. A range created BEFORE the narrowing has no such binding, so
# every operation on it -- stop, start, console, teardown -- answers 403 until it is redeployed.
# Saying so here is the difference between an operator choosing that and discovering it.
case "$RANGE_PERMISSIONS" in
  ""|cluster|namespace) ;;
  *) echo "--range-permissions must be 'cluster' or 'namespace', not '$RANGE_PERMISSIONS'" >&2; exit 2 ;;
esac
if [ "$RANGE_PERMISSIONS" = namespace ]; then
  say "narrowing the control plane's RBAC to per-range namespaces"
  echo "Every range that already exists must be redeployed after this: a range created before"
  echo "the narrowing carries no RoleBinding, and stop, start, console and teardown will 403."
fi

if [ -z "$REGISTRY_HOST" ] || [ -z "$IMAGE_REPO" ] || [ -z "$CHART_OCI" ]; then
  echo "This install does not know where its images are." >&2
  echo >&2
  echo "CYROID publishes no images: you build them and say where they live. Write" >&2
  echo "scripts/registry.env (see the comments in it) or export:" >&2
  echo "  PG_REGISTRY_HOST=registry.example.org" >&2
  echo "  PG_IMAGE_REPO=registry.example.org/your-org/cyroid" >&2
  echo "  PG_CHART_OCI=oci://registry.example.org/your-org/cyroid/charts" >&2
  echo "  PG_MINIO_IMAGE=registry.example.org/your-org/pg-storage@sha256:..." >&2
  exit 2
fi

if [ -n "$APPS_HOST" ] && [ -z "$HOST" ]; then
  echo "--apps-host needs --host: without a host of its own the platform answers on the" >&2
  echo "applications host as well, which puts a range's application back on this origin." >&2
  exit 2
fi

# An Ingress rule's host must be a DNS NAME. Kubernetes rejects an IP in `spec.rules[].host`, and
# it does so when helm applies the object -- so the release fails part-installed, naming a field
# the operator never set directly. The chart refuses this too; catching it here means the cluster
# is never touched.
#
# Not dropped-and-continued: a rule with no host matches every name, including the applications
# host, which is the separation --apps-host exists to create.
case "$HOST" in
  *[!0-9.]*|"") ;;
  *)
    echo "--host $HOST is an IP address, and an Ingress rule's host has to be a DNS name." >&2
    echo "A wildcard resolver gives you a name for that same address at no cost:" >&2
    echo "  --host ${HOST}.sslip.io --apps-host apps.${HOST}.sslip.io" >&2
    echo "The certificate carries the IP in its IP SANs either way." >&2
    exit 2 ;;
esac

# ---------------------------------------------------------------- kubeconfig
if [ -z "${KUBECONFIG:-}" ]; then
  if [ -r "$HOME/.kube/config" ]; then export KUBECONFIG="$HOME/.kube/config"
  elif [ -r /etc/rancher/k3s/k3s.yaml ]; then export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
  else echo "no kubeconfig: set KUBECONFIG, or run scripts/setup-substrate-k3s.sh first" >&2; exit 1
  fi
fi
kubectl get nodes >/dev/null || { echo "cannot reach the cluster with $KUBECONFIG" >&2; exit 1; }

# ---------------------------------------------------- does your certificate cover the apps host?
#
# A certificate obtained over HTTP-01 covers the name that answered the challenge and nothing
# else. `*.<apps-host>` is a wildcard, and no wildcard can be issued over HTTP-01 at all -- so an
# install that points both Ingresses at the same brought-in secret serves every learner opening
# an application a certificate for a name that is not theirs. The browser does not offer to
# continue on a name mismatch the way it does for an unknown issuer; it refuses.
#
# This is checked here because here is the last moment anybody can act on it, and because the
# symptom otherwise appears to a learner rather than to the operator. Only when the secret
# already exists: cert-manager may not have issued yet on a first install, and a check that
# cannot see the certificate says nothing rather than guessing.
if [ -n "$APPS_HOST" ] && [ -n "$TLS_SECRET_NAME" ] && [ "$TLS_SECRET_NAME" != pg-tls ] && [ -z "$APPS_TLS_SECRET_NAME" ]; then
  sans="$(kubectl -n "$NAMESPACE" get secret "$TLS_SECRET_NAME" -o jsonpath='{.data.tls\.crt}' 2>/dev/null \
          | base64 -d 2>/dev/null | openssl x509 -noout -ext subjectAltName 2>/dev/null || true)"
  if [ -n "$sans" ] && ! printf '%s' "$sans" | grep -q "DNS:\*\.${APPS_HOST}\b"; then
    echo "$TLS_SECRET_NAME does not cover the applications host." >&2
    echo "  it has:   $(printf '%s' "$sans" | tr -d ' \n' | sed 's/^X509v3SubjectAlternativeName://')" >&2
    echo "  it needs: DNS:*.${APPS_HOST}" >&2
    echo >&2
    echo "Every application a learner opens would be served a certificate for another name," >&2
    echo "which a browser refuses outright rather than warning about. Either put that wildcard" >&2
    echo "in the certificate (DNS-01; HTTP-01 cannot issue one), or keep the generated one for" >&2
    echo "the applications alone:" >&2
    echo "  --tls-secret-name ${TLS_SECRET_NAME} --apps-tls-secret-name pg-tls" >&2
    exit 2
  fi
fi
kubectl get crd helmreleases.helm.toolkit.fluxcd.io >/dev/null 2>&1 \
  || { echo "Flux's helm-controller is not on this cluster; run scripts/setup-substrate-k3s.sh" >&2; exit 1; }

# ------------------------------------------------ the node's address as a certificate SAN
# The chart generates its certificate on first install, and without the node's address in it the
# install this script prints when --host is not given -- https://<node-ip>/ -- is reached by a
# name the certificate never claimed. The browser warning that follows is indistinguishable from
# an interception, which is how operators learn to click through them.
#
# Only an address that parses is passed: the chart refuses a SAN it cannot read rather than
# dropping it silently, and a detection that returned a hostname or nothing would then fail the
# install over a certificate detail. A cluster that already has its pg-tls keeps the certificate
# it has, so this changes nothing on an existing install.
#
# `|| true` for the same reason: under `set -e -o pipefail` a kubectl that fails here would abort
# an install that is otherwise fine, and an empty answer is already handled two lines down.
NODE_IP="$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null | awk '{print $1}' || true)"
case "$NODE_IP" in
  ''|*[!0-9.]*) NODE_IP="" ;;
esac
if [ -n "$NODE_IP" ]; then EXTRA_IPS="[\"${NODE_IP}\"]"; else EXTRA_IPS="[]"; fi

# ---------------------------------------------------- registry credentials
if [ ! -r "$CRED_FILE" ]; then
  say "registry credentials"
  echo "The images and the chart come from ${REGISTRY_HOST}, which needs a deploy token"
  echo "(GitLab: cyroid -> Settings -> Repository -> Deploy tokens, scope read_registry)."
  read -r -p "deploy token username: " reg_user
  read -r -s -p "deploy token: " reg_token; echo
  mkdir -p "$(dirname "$CRED_FILE")"
  (umask 077; printf 'PG_REGISTRY_USER=%s\nPG_REGISTRY_TOKEN=%s\n' "$reg_user" "$reg_token" > "$CRED_FILE")
fi
# shellcheck disable=SC1090
. "$CRED_FILE"

say "namespace ${NAMESPACE} and the chart-pull secret"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
# Flux fetches the chart with this one, before any release exists. The chart renders its own
# pull secret (pg-registry) for the pods from the same credentials; two objects on purpose, so
# neither helm nor Flux ever owns the other's.
kubectl -n "$NAMESPACE" create secret docker-registry pg-chart-pull \
  --docker-server="$REGISTRY_HOST" --docker-username="$PG_REGISTRY_USER" \
  --docker-password="$PG_REGISTRY_TOKEN" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

values_yaml() {
  cat <<VALUES
image:
  registry: "${IMAGE_REPO}"
  tag: "${TAG}"
ingress:
  host: "${HOST}"
  appsHost: "${APPS_HOST}"
  tls:
    extraIPs: ${EXTRA_IPS}
VALUES
  # Emitted after the heredoc, one echo per key, and NOT as `$(... printf ... \n ...)` inside it.
  # Command substitution strips trailing newlines, including the one separating two adjacent
  # substitutions -- which silently produced
  #     secretName: "pg-tls-letsencrypt"    appsSecretName: "pg-tls"
  # on a single line, and `kubectl apply` refused the manifest with "did not find expected key".
  # An `if` rather than `[ -n "$X" ] && echo`, because under `set -e` a false test is the list's
  # exit status.
  if [ -n "$TLS_SECRET_NAME" ]; then
    echo "    secretName: \"${TLS_SECRET_NAME}\""
  fi
  if [ -n "$APPS_TLS_SECRET_NAME" ]; then
    echo "    appsSecretName: \"${APPS_TLS_SECRET_NAME}\""
  fi
  if [ -n "$CHART_REPOSITORY" ]; then
    echo "chartRepository: \"${CHART_REPOSITORY}\""
  fi
  if [ -n "$MINIO_IMAGE" ]; then
    echo "minio:"
    echo "  image: \"${MINIO_IMAGE}\""
  fi
  # Only when asked for: an empty block would overwrite the chart's defaults with empty strings,
  # and `data` is a claim that is kept across upgrades -- getting it wrong is not a rollback.
  if [ -n "$DATA_ACCESS_MODE" ] || [ -n "$DATA_STORAGE_CLASS" ]; then
    echo "data:"
    # `if`, not `[ -n "$X" ] && echo`: the second of those is the function's last statement, so
    # when it is the one that is unset the function returns 1. Under `set -e` that aborts the
    # --source install at `{ values_yaml; credential_values_yaml; } > values.yaml`, with no
    # message -- passing --data-access-mode without --data-storage-class was enough to do it.
    if [ -n "$DATA_ACCESS_MODE" ]; then
      echo "  accessMode: \"${DATA_ACCESS_MODE}\""
    fi
    if [ -n "$DATA_STORAGE_CLASS" ]; then
      echo "  storageClassName: \"${DATA_STORAGE_CLASS}\""
    fi
  fi
}
credential_values_yaml() {
  cat <<VALUES
registryCredentials:
  username: "${PG_REGISTRY_USER}"
  password: "${PG_REGISTRY_TOKEN}"
VALUES
  # Same Secret, same reason: a connection string with a password in it must not sit in the
  # HelmRelease, which `kubectl get helmrelease -o yaml` prints to anyone who can read the
  # namespace. Emitted only when given -- an empty externalUrl with postgres.enabled=false
  # would leave the chart pointing at nothing.
  if [ -n "$POSTGRES_URL" ]; then
    printf 'postgres:\n  enabled: false\n  externalUrl: "%s"\n' "$POSTGRES_URL"
  fi
}

if [ "$SOURCE" = 1 ]; then
  # ------------------------------------------------------------ dev: helm CLI
  # This checkout's chart, images at --tag. For reviewing a merge request on pg-devtest before
  # its chart is published; not the path an operator is on.
  if ! command -v helm >/dev/null || [ "$(helm version --template '{{.Version}}')" != "$HELM_VERSION" ]; then
    say "installing helm ${HELM_VERSION}"
    arch="$(uname -m)"; case "$arch" in x86_64) arch=amd64 ;; aarch64|arm64) arch=arm64 ;; esac
    tmp="$(mktemp -d)"
    curl -sSL -o "${tmp}/helm.tgz" "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz"
    tar -xzf "${tmp}/helm.tgz" -C "$tmp"
    sudo install -m 0755 "${tmp}/linux-${arch}/helm" /usr/local/bin/helm
    rm -rf "$tmp"
  fi
  [ -n "$TAG" ] || TAG="$VERSION"
  # A Flux-owned release reconciles itself back to its HelmRelease within minutes, undoing a
  # source build under the tester (seen on pg-devtest: the API restarted mid-test). Suspend it
  # for the rehearsal; the operator path below resumes it.
  if kubectl -n "$NAMESPACE" get helmrelease "$RELEASE" >/dev/null 2>&1; then
    say "suspending HelmRelease ${RELEASE} so Flux does not revert the source build"
    kubectl -n "$NAMESPACE" patch helmrelease "$RELEASE" --type merge -p '{"spec":{"suspend":true}}' >/dev/null
    echo "   (run this script without --source to resume Flux ownership)"
  fi
  say "helm upgrade --install ${RELEASE} from source (chart ${VERSION}, images :${TAG})"
  pkgdir="$(mktemp -d)"
  helm package "$CHART" --version "$VERSION" --app-version "$TAG" -d "$pkgdir" >/dev/null
  (umask 077; { values_yaml; credential_values_yaml; } > "${pkgdir}/values.yaml")
  helm upgrade --install "$RELEASE" "${pkgdir}/proving-ground-${VERSION}.tgz" \
    --namespace "$NAMESPACE" --values "${pkgdir}/values.yaml" --wait --timeout 15m
  rm -rf "$pkgdir"
else
  # ------------------------------------------------------ operator: Flux
  say "HelmRelease ${RELEASE} at chart ${VERSION} from ${CHART_OCI} (Flux resumes if suspended)"
  # The registry token reaches the chart through a Secret the HelmRelease reads values from,
  # not through the HelmRelease's own spec, which is not a secret.
  kubectl -n "$NAMESPACE" create secret generic pg-registry-values \
    --from-literal=values.yaml="$(credential_values_yaml)" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  cat <<MANIFEST | kubectl apply -f - >/dev/null
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: proving-ground
  namespace: ${NAMESPACE}
spec:
  type: oci
  interval: 1h
  url: ${CHART_OCI}
  secretRef:
    name: pg-chart-pull
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: ${RELEASE}
  namespace: ${NAMESPACE}
spec:
  interval: 10m
  suspend: false
  releaseName: ${RELEASE}
  chart:
    spec:
      chart: proving-ground
      version: "${VERSION}"
      sourceRef:
        kind: HelmRepository
        name: proving-ground
  install:
    remediation: {retries: 2}
  upgrade:
    remediation: {retries: 2}
  valuesFrom:
    - kind: Secret
      name: pg-registry-values
      valuesKey: values.yaml
  values:
$(values_yaml | sed 's/^/    /')
MANIFEST
  say "waiting for helm-controller"
  for i in $(seq 1 180); do
    ready="$(kubectl -n "$NAMESPACE" get helmrelease "$RELEASE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
    reason="$(kubectl -n "$NAMESPACE" get helmrelease "$RELEASE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].reason}' 2>/dev/null || true)"
    deployed="$(kubectl -n "$NAMESPACE" get helmrelease "$RELEASE" -o jsonpath='{.status.history[0].chartVersion}' 2>/dev/null || true)"
    if [ "$ready" = True ] && [ "$deployed" = "$VERSION" ]; then break; fi
    case "$reason" in InstallFailed|UpgradeFailed|ArtifactFailed)
      echo "helm-controller: $(kubectl -n "$NAMESPACE" get helmrelease "$RELEASE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].message}')" >&2
      exit 1 ;;
    esac
    sleep 5
  done
  [ "$ready" = True ] && [ "$deployed" = "$VERSION" ] || { echo "HelmRelease did not become Ready at ${VERSION} in 15 minutes" >&2; kubectl -n "$NAMESPACE" get helmrelease "$RELEASE" -o jsonpath='{.status.conditions}' >&2; exit 1; }
fi

say "rollouts"
kubectl -n "$NAMESPACE" rollout status statefulset/pg-postgres --timeout=5m 2>/dev/null || true
for d in pg-redis pg-minio pg-api pg-worker pg-worker-deploy pg-frontend; do
  kubectl -n "$NAMESPACE" rollout status deployment/$d --timeout=10m
done

say "installed"
node_ip="$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
kubectl -n "$NAMESPACE" get pods
echo
echo "PROVING GROUND ${VERSION}:  https://${HOST:-$node_ip}/"
echo "(self-signed certificate unless ingress.tls.secretName was pointed at your own)"
if [ -n "$APPS_HOST" ]; then
  echo "Range applications: https://<range-id>.${APPS_HOST}/  -- needs a wildcard record for"
  echo "*.${APPS_HOST} pointing here. The generated certificate carries that wildcard as a SAN,"
  echo "so it is what a learner is served; a certificate of your own has to carry it too."
fi
echo
echo "A sample Kubernetes blueprint for a first range:"
echo "  kubectl -n ${NAMESPACE} exec deploy/pg-api -- python -m proving_ground.tools.seed_k8s_blueprint"
