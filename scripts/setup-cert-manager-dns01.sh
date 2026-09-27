#!/bin/sh
# Trusted certificates for an install, including the range-applications wildcard.
#
#   ./scripts/setup-cert-manager-dns01.sh \
#       --host pg-devtest.defconai.dev --apps-host apps.pg-devtest.defconai.dev \
#       --token-file ~/.secrets/cf_token
#
# WHY DNS-01 AND NOT HTTP-01. Two reasons, and each on its own is sufficient:
#
#  1. `*.apps.<host>` is a wildcard, and NO ACME CA will issue a wildcard over HTTP-01. Every
#     range application answers on its own label beneath the applications host, so without a
#     wildcard each one would need its own certificate at deploy time. This is the whole reason
#     the chart grew `ingress.tls.appsSecretName` (!113): with HTTP-01 the best available answer
#     is a self-signed certificate for the applications, carrying the right name but untrusted.
#     With DNS-01 one certificate covers both hosts and that fallback is unnecessary.
#
#  2. HTTP-01 requires the CA to reach the host inbound. pg-devtest is 10.10.100.100 and no CA
#     can reach it. DNS-01 proves control of the NAME, so a private address is no obstacle --
#     which is how a lab host on RFC1918 gets a genuinely trusted certificate.
#
# The DNS provider here is Cloudflare because that is where our zones are. The token needs
# Zone:DNS:Edit and Zone:Zone:Read, scoped to the one zone -- Zone:Read because cert-manager
# looks the zone up before writing the challenge record.
#
# Idempotent: re-running adopts what already exists.
set -eu

CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.16.2}"
ACME_EMAIL="${ACME_EMAIL:-security@fightingsmartcyber.com}"
ACME_SERVER="${ACME_SERVER:-https://acme-v02.api.letsencrypt.org/directory}"
NAMESPACE="${PG_NAMESPACE:-pg-system}"
ISSUER=letsencrypt-dns01
SECRET_NAME="${PG_TLS_SECRET:-pg-tls-dns01}"

HOST=""; APPS_HOST=""; TOKEN_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --host)       HOST="$2"; shift 2 ;;
    --apps-host)  APPS_HOST="$2"; shift 2 ;;
    --token-file) TOKEN_FILE="$2"; shift 2 ;;
    --secret-name) SECRET_NAME="$2"; shift 2 ;;
    -h|--help)    awk 'NR==1{next} /^#/{print;next} {exit}' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$HOST" ] || { echo "--host is required" >&2; exit 2; }
[ -n "$TOKEN_FILE" ] || { echo "--token-file is required (a Cloudflare API token)" >&2; exit 2; }
[ -r "$TOKEN_FILE" ] || { echo "cannot read $TOKEN_FILE" >&2; exit 2; }

say() { printf '\n== %s\n' "$1"; }

say "cert-manager ${CERT_MANAGER_VERSION}"
kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml" >/dev/null
for d in cert-manager cert-manager-cainjector cert-manager-webhook; do
  kubectl -n cert-manager rollout status "deploy/$d" --timeout=5m >/dev/null
done
echo "ready"

# The token travels through a file, never through argv: an argument list is world-readable in
# `ps` for as long as the process lives. `tr` strips the trailing newline a text editor leaves
# behind -- it would otherwise be sent as part of the bearer token, and Cloudflare's rejection of
# that reads as an unhelpful 400.
say "Cloudflare token -> secret/cloudflare-api-token in cert-manager"
umask 077
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
tr -d '\r\n' < "$TOKEN_FILE" > "$tmp"
kubectl -n cert-manager create secret generic cloudflare-api-token \
  --from-file=api-token="$tmp" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "stored ($(wc -c < "$tmp" | tr -d ' ') bytes, newline stripped)"

say "ClusterIssuer ${ISSUER}"
cat <<YAML | kubectl apply -f - >/dev/null
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata: {name: ${ISSUER}}
spec:
  acme:
    email: ${ACME_EMAIL}
    server: ${ACME_SERVER}
    privateKeySecretRef: {name: ${ISSUER}-account}
    solvers:
      - dns01:
          cloudflare:
            apiTokenSecretRef: {name: cloudflare-api-token, key: api-token}
YAML
kubectl wait clusterissuer "$ISSUER" --for=condition=Ready --timeout=2m >/dev/null
echo "registered with the ACME server"

say "Certificate for ${HOST}${APPS_HOST:+ and *.${APPS_HOST}}"
{
  cat <<YAML
apiVersion: cert-manager.io/v1
kind: Certificate
metadata: {name: pg-tls, namespace: ${NAMESPACE}}
spec:
  secretName: ${SECRET_NAME}
  issuerRef: {name: ${ISSUER}, kind: ClusterIssuer}
  dnsNames:
    - ${HOST}
YAML
  [ -n "$APPS_HOST" ] && printf '    - "*.%s"\n' "$APPS_HOST"
} | kubectl apply -f - >/dev/null

# cert-manager backs off to 30 MINUTES after repeated failures. If the token secret arrived late
# the controller can sit idle long after the cause is fixed, which reads as "it is not working".
# A restart clears the queue; without it this wait would time out for no reason.
kubectl -n cert-manager rollout restart deploy/cert-manager >/dev/null
kubectl -n cert-manager rollout status deploy/cert-manager --timeout=3m >/dev/null
echo "waiting for issuance (DNS-01 propagation is usually 60-120s)"
if ! kubectl -n "$NAMESPACE" wait certificate pg-tls --for=condition=Ready --timeout=10m >/dev/null 2>&1; then
  echo "NOT ISSUED. What cert-manager says:" >&2
  kubectl -n "$NAMESPACE" get certificate pg-tls -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}: {.message}{"\n"}{end}' >&2
  kubectl -n "$NAMESPACE" get challenge -o jsonpath='{range .items[*]}{.spec.dnsName}: {.status.reason}{"\n"}{end}' 2>/dev/null >&2
  exit 1
fi

kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" -o jsonpath='{.data.tls\.crt}' \
  | base64 -d | openssl x509 -noout -subject -issuer -enddate -ext subjectAltName 2>/dev/null

cat <<SUMMARY

Point the install at it -- ONE secret covers both hosts, so appsSecretName is not needed:

  ./scripts/install-k8s.sh --host ${HOST}${APPS_HOST:+ --apps-host ${APPS_HOST}} \\
      --tls-secret-name ${SECRET_NAME}

cert-manager renews at about two thirds of the certificate's life, unattended, as long as the
token stays valid.
SUMMARY
