{{- /* Release-level labels. `part-of` lives in pg.component (pods and selectors need it, and
the two are used together on most objects); a key emitted twice is a duplicate helm-controller's
strict YAML parser rejects -- and the helm CLI's does not, which is how it shipped. */ -}}
{{- define "pg.labels" -}}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{- define "pg.component" -}}
app.kubernetes.io/name: {{ . }}
app.kubernetes.io/part-of: proving-ground
{{- end }}

{{- define "pg.image" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- if or (not $tag) (eq $tag "0.0.0") }}{{ fail "no image tag: set image.tag, or package the chart with --app-version (scripts/install-k8s.sh and CI both do)" }}{{ end -}}
{{- if not .Values.image.registry }}{{ fail "no image registry: set image.registry to a registry this cluster can pull from. CYROID publishes no images -- build them and name where they live (scripts/install-k8s.sh does this from scripts/registry.env or PG_IMAGE_REPO)" }}{{ end -}}
{{ .Values.image.registry }}/{{ .name }}:{{ $tag }}
{{- end }}

{{- define "pg.imageTag" -}}
{{ default .Chart.AppVersion .Values.image.tag }}
{{- end }}

{{- define "pg.imagePullSecrets" -}}
{{- with .Values.imagePullSecret }}
imagePullSecrets:
  - name: {{ . }}
{{- end }}
{{- end }}

{{- /*
Whether this release owns Traefik's default certificate. The TLSStore in templates/ingress.yaml
and the copy of the certificate secret beside it in templates/secrets.yaml are two objects that
have to agree, and they are written in different files; this is the one condition both read.
Non-empty means yes.

Opt-in only, and never on by itself. Traefik does not pick a certificate by the names an Ingress
declared TLS for -- its Kubernetes provider reads `spec.tls[].secretName` and ignores
`spec.tls[].hosts` entirely, then matches the handshake against the SANs of every certificate it
has loaded, wildcards included, falling back to the address the connection arrived on when the
client sent no SNI. So the SAN list built in templates/secrets.yaml is what gets the certificate
served, for a range's applications on the wildcard as much as for the platform, and a store that
replaces what the controller serves for every OTHER host is not the price of that. It is here for
the install whose Traefik is configured so that the SAN match is not enough, and that operator
asks for it by name.
*/ -}}
{{- /*
Which secret the applications host serves. Its own when one is named, the platform's otherwise --
which is correct only because the generated certificate carries `*.<appsHost>`. See values.yaml.
*/ -}}
{{- define "pg.appsTlsSecretName" -}}
{{- default .Values.ingress.tls.secretName .Values.ingress.tls.appsSecretName -}}
{{- end }}

{{- define "pg.tlsDefaultStore" -}}
{{- $mode := toString .Values.ingress.tls.defaultStore.enabled -}}
{{- if not (has $mode (list "" "true" "false")) -}}
{{- fail (printf "ingress.tls.defaultStore.enabled must be true, false or left unset, not %q" $mode) -}}
{{- end -}}
{{- if and .Values.ingress.enabled .Values.ingress.tls.enabled (eq $mode "true") -}}
{{- if ne .Values.ingress.tls.secretName "pg-tls" -}}
{{- fail "ingress.tls.defaultStore.enabled is true but ingress.tls.secretName names a secret this chart did not create. The store and the certificate it names both have to live in rangeIngress.controllerNamespace, and this chart will not copy a private key it did not generate into a namespace it does not own. Put the TLSStore and a copy of your secret in that namespace yourself, or leave secretName at pg-tls." -}}
{{- end -}}
{{- /*
Without a namespace the store and the copied secret both land in the release namespace, where
the copy collides with the original: two Secrets named pg-tls in one render, and a default store
Traefik will not read because it is not where the controller looks.
*/ -}}
{{- if not .Values.rangeIngress.controllerNamespace -}}
{{- fail "ingress.tls.defaultStore.enabled is true but rangeIngress.controllerNamespace is empty. The store has to be created in the namespace the ingress controller runs in -- kube-system on k3s -- or Traefik never reads it." -}}
{{- end -}}
yes
{{- end -}}
{{- end }}

{{/* Environment shared by the API and both workers. */}}
{{- define "pg.appEnv" -}}
# What GET /api/v1/version reports. The image is built without APP_VERSION baked in, so the
# release tag is the version -- the same number the chart and the images carry.
- name: APP_VERSION
  value: {{ include "pg.imageTag" . | quote }}
# In-cluster updates: which HelmRelease this install is, where the chart is published, and the
# pull secret that reads the registry (mounted, not copied into env).
- name: POD_NAMESPACE
  valueFrom: {fieldRef: {fieldPath: metadata.namespace}}
- name: HELM_RELEASE_NAME
  value: {{ .Release.Name | quote }}
- name: CHART_REPOSITORY
  value: {{ .Values.chartRepository | quote }}
# Hosts the chart registry's auth realm may name besides its own host and its own parent
# domain. The update check refuses to send the pull credentials anywhere else -- a registry that
# is compromised or impersonated would otherwise harvest the token that pulls every image this
# install runs -- and the refusal names this setting, so it has to be reachable from the pod:
# `kubectl set env` on the deployment is reverted by the next Flux reconcile.
- name: UPDATE_REALM_HOSTS
  value: {{ .Values.updateRealmHosts | default "" | quote }}
- name: REGISTRY_CREDENTIALS_FILE
  value: /var/run/pg/registry/.dockerconfigjson
- name: RANGE_SUBSTRATE
  value: {{ .Values.rangeSubstrate | quote }}
# The warm range pool is a DinD mechanism; on the cluster there is no pool to refill.
- name: RANGE_POOL_ENABLED
  value: "false"
# Let into every range's default-deny: this namespace (hooks against a vcluster's API) and
# Flux's (helm-controller deploying into it).
- name: CONTROL_PLANE_NAMESPACES
  value: {{ printf "%s,%s" .Release.Namespace .Values.fluxNamespace | quote }}
# Student-facing ingress for range applications (PG-62).
- name: RANGE_INGRESS_CLASS
  value: {{ .Values.rangeIngress.className | quote }}
- name: RANGE_INGRESS_PATH_PREFIX
  value: {{ .Values.rangeIngress.pathPrefix | quote }}
- name: RANGE_INGRESS_AUTHZ_URL
  value: {{ printf "http://pg-api.%s.svc:8000/api/v1/range-apps/authz" .Release.Namespace | quote }}
# The host a range's applications answer beneath -- and the reason they do not answer on this
# one. It has to be here rather than on the API alone: the deploy workers are what publish the
# per-range Ingress, so a worker without this value publishes nothing while the API offers the
# application, and the learner gets a URL that resolves to no route.
- name: RANGE_APPS_HOST
  value: {{ .Values.ingress.appsHost | quote }}
# Empty here means a blueprint may name no chart repository at all, so this has to reach the
# deploy workers as well as the API: they are what parses a blueprint on the way to applying it.
- name: CAPABILITY_CHART_REPOSITORIES
  value: {{ .Values.capabilityChartRepositories | quote }}
# Which RBAC posture rbac.yaml rendered. The control plane writes a RoleBinding per range only
# in the narrow mode; in the broad one it already holds those verbs and is not granted the
# rolebinding write, so attempting it would 403 on every range creation.
# Never left at the code default, which is true: this is a deployed install, not a laptop. It is
# what turns on the refusal to start with the JWT secret this repository ships, and it keeps
# FastAPI's debug behaviour off a surface that faces learners.
- name: DEBUG
  value: {{ .Values.debug | default false | quote }}
- name: RANGE_RBAC_SCOPE
  value: {{ (.Values.rbac | default dict).rangePermissions | default "cluster" | quote }}
- name: INGRESS_CONTROLLER_NAMESPACE
  value: {{ .Values.rangeIngress.controllerNamespace | quote }}
- name: INGRESS_CONTROLLER_POD_LABELS
  value: {{ .Values.rangeIngress.controllerPodLabels | quote }}
{{- /*
  The cluster's own pod and service CIDRs, so a range network that would collide with them is
  refused at blueprint validation rather than producing intermittent breakage. Hardcoded to
  k3s's before this, which made the guard wrong on every other distribution in both directions.
*/}}
- name: CLUSTER_RESERVED_CIDRS
  value: {{ .Values.clusterReservedCidrs | default "10.42.0.0/16,10.43.0.0/16" | quote }}
- name: DATABASE_URL
  valueFrom: {secretKeyRef: {name: pg-platform, key: DATABASE_URL}}
- name: REDIS_URL
  value: redis://pg-redis:6379/0
- name: MINIO_ENDPOINT
  value: {{ if .Values.minio.enabled }}pg-minio:9000{{ else }}{{ .Values.minio.externalEndpoint | quote }}{{ end }}
{{- /*
  MINIO_SECURE was never emitted at all, so the backend kept its `False` default and every
  client dialled plain HTTP -- including against whatever `externalEndpoint` named. Harmless
  on the pod network to the bundled MinIO; it simply does not work against a real endpoint.
*/}}
- name: MINIO_SECURE
  value: {{ if .Values.minio.enabled }}"false"{{ else }}{{ .Values.minio.externalSecure | default false | quote }}{{ end }}
{{- /*
  Credentials. The bundled MinIO's are generated into pg-platform; an external store's come
  from the operator, by reference to a secret they created wherever possible, so the keys do
  not pass through values.yaml or sit in the release's stored manifest.
*/}}
{{- $minioSecretName := ternary "pg-platform" (.Values.minio.existingSecret | default "pg-platform") .Values.minio.enabled }}
- name: MINIO_ACCESS_KEY
  valueFrom: {secretKeyRef: {name: {{ $minioSecretName | quote }}, key: MINIO_ACCESS_KEY}}
- name: MINIO_SECRET_KEY
  valueFrom: {secretKeyRef: {name: {{ $minioSecretName | quote }}, key: MINIO_SECRET_KEY}}
- name: MINIO_BUCKET
  value: {{ .Values.minio.bucket | default "proving-ground-artifacts" | quote }}
- name: MINIO_CONTENT_BUCKET
  value: {{ .Values.minio.contentBucket | default "proving-ground-content" | quote }}
- name: MINIO_CREATE_BUCKET
  value: {{ .Values.minio.createBucket | default false | quote }}
- name: JWT_SECRET_KEY
  valueFrom: {secretKeyRef: {name: pg-platform, key: JWT_SECRET_KEY}}
{{- /* The API signs range-application credentials; pg-gateway only verifies them. */}}
- name: APP_TOKEN_PRIVATE_KEY
  valueFrom: {secretKeyRef: {name: pg-platform, key: APP_TOKEN_PRIVATE_KEY}}
- name: APP_TOKEN_PUBLIC_KEY
  valueFrom: {secretKeyRef: {name: pg-platform, key: APP_TOKEN_PUBLIC_KEY}}
{{- /* The application ticket's own secret. Shared with pg-gateway and nothing else: the gateway
     mints and verifies this one itself, so it cannot use a key it holds only half of, and it
     must not be handed jwt_secret_key. The API still needs it for the pre-gateway ForwardAuth
     path that ranges deployed before the switch-over are still served by. */}}
- name: APP_TICKET_SECRET
  valueFrom: {secretKeyRef: {name: pg-platform, key: APP_TICKET_SECRET}}
- name: SCENARIOS_DIR
  value: /data/proving-ground/scenarios
- name: GLOBAL_SHARED_DIR
  value: /data/proving-ground/shared
- name: BRANDING_PRODUCT_NAME
  value: {{ .Values.branding.productName | quote }}
- name: BRANDING_TAGLINE
  value: {{ .Values.branding.tagline | quote }}
- name: BRANDING_PRIMARY_PALETTE
  value: {{ .Values.branding.primaryPalette | quote }}
{{- end }}

{{- define "pg.appVolumeMounts" -}}
- name: data
  mountPath: /data/proving-ground
{{- with .Values.imagePullSecret }}
- name: registry-credentials
  mountPath: /var/run/pg/registry
  readOnly: true
{{- end }}
{{- end }}

{{- define "pg.appVolumes" -}}
- name: data
  persistentVolumeClaim:
    claimName: pg-data
{{- with .Values.imagePullSecret }}
- name: registry-credentials
  secret:
    secretName: {{ . }}
{{- end }}
{{- end }}

{{- define "pg.waitForDb" -}}
until python -c "import os, psycopg2; psycopg2.connect(os.environ['DATABASE_URL'])" 2>/dev/null; do echo 'Waiting for database...'; sleep 2; done
{{- end }}
