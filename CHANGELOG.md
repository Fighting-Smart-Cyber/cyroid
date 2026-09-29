# Changelog

All notable changes to PROVING GROUND will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **User-facing entries live in `frontend/src/lib/changelog.ts`**, which is what the in-app
> "What's new" modal renders. Add a release there and here together;
> `backend/tests/unit/test_changelog_agrees.py` fails if the two disagree about which versions
> exist. That file is the one to write carefully — it is what operators actually read.

## [0.56.0] - 2026-09-28

Anyone can now install CYROID from a clone, and the release images are public. Also the
in-cluster frontend, which had been serving a 404 since it was made unprivileged.

### Added

- **`scripts/quickstart.sh`** — a k3d cluster, the chart, and a URL, in one command. k3s runs
  inside Docker so nobody has to stand up Kubernetes first, and `--delete` leaves nothing
  behind. The k3s version is pinned: unpinned, k3d hands a different Kubernetes to every user,
  and k3d 5.9.0's own default (k3s v1.35.5) shut itself down during startup while this was
  being written, leaving a cluster that reported `1/1` servers and refused every connection.

  It does not install KubeVirt, CDI or Multus. A VM wants `/dev/kvm`, which a container on a
  laptop does not have, and the fallback is emulation that boots in minutes. Without them the
  platform, the learner record and capability-based ranges all work.

- **`publish-images-ghcr`** — release images at `ghcr.io/fighting-smart-cyber` as `cyroid-api`,
  `cyroid-worker`, `cyroid-frontend` and `cyroid-storage`. A `crane copy` of the manifest that
  was already built, scanned and signed, so the public image is the same bytes as the audited
  one. Manual, because a public registry cannot be un-published.

  `cyroid-storage` is a mirror of MinIO, and it is not optional: no anonymously pullable MinIO
  image remains — `docker.io/minio/minio` is gone and `quay.io` answers 401 — so without it a
  public install has no object store and the chart refuses to render.

- **`image.namePrefix`** on the chart, so one chart serves both audiences. `pg-` stays the
  default and no existing install changes; the published images are `cyroid-*`.

### Fixed

- **The in-cluster frontend was never ready.** `nginx` moved to port 8080 when it was made
  unprivileged, but the chart still declared `containerPort: 80`, so both probes got
  `connection refused`, the Service had no endpoints, and the ingress answered every request
  for the UI with its own 404. `/api` kept working, which is why it looked half-alive. The
  Compose path was unaffected, which is why nothing caught it.

- **`app_name` said PROVING GROUND.** It reaches a downloader in the OpenAPI title, at
  `/api/v1/version` and in the health response; the engine's default is now CYROID. A
  distribution sets `APP_NAME`.

- **The High-severity dependency CVEs** the shared DevSecOps pipeline found: `axios`, `jspdf`,
  `python-multipart`, `form-data`, `@tiptap/core`, `js-yaml`, `picomatch` and `black`.

- **Archive extraction** in the two export services no longer trusts the archive. A `.tar.gz`
  member named `../owned` wrote outside the destination; the guard that `image_import_service`
  already had is now shared by all three.

### Security

- The shared DevSecOps pipeline from `ci-templates` runs as a child pipeline on every master
  commit and tag — secrets over the full git history, SAST, dependency CVEs, end-of-life
  inventory, IaC, malware, and the ASD STIG checklist. It reports rather than blocks while the
  findings it opened are worked through.

## [0.55.1] - 2026-09-27

Groundwork for building CYROID's containers the way a DoD environment expects, and three
defects found by doing it. Nothing in the running product changes.

### Added

- **`ironbank/*/hardening_manifest.yaml`** and the build arguments they depend on. Iron Bank
  builds the image itself, from our Dockerfile, against a base it controls, passing
  `args: {BASE_IMAGE, BASE_TAG}` in -- a Dockerfile with a hardcoded `FROM` ignores them
  silently and is hardened against a base nobody chose. Both Dockerfiles take them now,
  defaulting to the current public base so every build is byte-identical.

  `BASE_IMAGE` is deliberately **empty**. The first draft said `redhat/ubi/ubi9` while
  `backend/Dockerfile` installs with `apt-get`; UBI has no apt, so that build would have failed
  on its first `RUN` -- against a base that was a guess, because reading the Iron Bank catalogue
  needs a repo1 account. A guard now refuses a base whose package manager the Dockerfile does
  not use, so the next guess fails in CI rather than at submission.

  This is the container supply chain, not an ATO. Iron Bank states that it "does not authorize
  or approve containers at this time" -- evidence and reciprocity only.

### Fixed

- **The frontend image claimed `org.opencontainers.image.licenses=MIT`.** It is AGPL-3.0
  (ADR-0008). Its `image.source` also pointed at `JongoDB/CYROID`, archived when `MIG-8` moved
  the engine (ADR-0011).
- **Every image published so far is labelled `org.opencontainers.image.version=dev`**, the
  Dockerfile default: `APP_VERSION` was never passed as a build argument.
- **Image builds did not run on a merge request that changes a Dockerfile** -- only on master,
  on a tag, or on a `feat/substrate-` branch. Editing one produced a green pipeline that never
  ran `docker build` once, and the first attempt was the release.

## [0.55.0] - 2026-09-27

A minor rather than a patch: `image.registry` becomes a **required** value. An install
that predates this refuses to upgrade until `scripts/install-k8s.sh` is re-run once.
See "Upgrading" below -- it is loud and recoverable, deliberately.

### Added

- **Trusted certificates, wildcard included, on hosts no CA can reach.**
  `scripts/setup-cert-manager-dns01.sh` installs cert-manager, a DNS-01 ClusterIssuer
  and a Certificate covering `<host>` and `*.<apps-host>` in one.

  DNS-01 is not a preference. **No ACME CA issues a wildcard over HTTP-01**, and every
  range application answers on its own label beneath the applications host -- so
  HTTP-01 cannot cover them however the install is arranged. That is the whole reason
  `ingress.tls.appsSecretName` exists: with HTTP-01 the best available answer for the
  applications is a *self-signed* certificate carrying the right name, because a
  browser refuses a name mismatch outright but will let you accept an unknown issuer.
  With DNS-01 one certificate covers both and the fallback is unnecessary.

  DNS-01 also proves control of the NAME rather than the address, so a host on RFC1918
  can hold a genuinely trusted certificate. `pg-devtest` does, from 10.10.100.100.

  Two things the script encodes because they cost an hour to find: a token file's
  trailing newline becomes part of the bearer token, and cert-manager backs off to
  **30 minutes** after repeated failures, so a token added late looks like it did
  nothing.

### Changed

- **The chart no longer names a registry.** `image.registry` and `minio.image` have no
  default; rendering without them fails naming the value. CYROID publishes no images,
  so a chart defaulting to one organisation's private registry named a host no other
  reader can reach, for images that are not there -- it was never a property of the
  software, but of who is deploying it. Ours moved to `scripts/registry.env`, which is
  also now the single copy of the `pg-storage` digest that `build:infra` reads.

  This is what had kept `publish-public` refusing since the in-cluster chart landed --
  six releases, caught correctly by the guard every time, unnoticed because the job is
  manual.

- `ingress.tls.appsSecretName` documents itself as the HTTP-01 fallback it always was.

### Fixed

- `install-k8s.sh --help` printed a hardcoded line range that had fallen behind the
  header; it prints the whole block now.
- `list_ranges`' docstring and CLAUDE.md described the superseded tag model, where
  having no tags meant public. Ranges carry an explicit `visibility` with PRIVATE as
  the default -- the tag rule was replaced precisely because it made every range
  visible to every non-student account.

### Upgrading

An install whose HelmRelease values predate this carries no `image.registry`, so the
next chart it takes **fails to render** rather than coming up wrong: helm-controller
reports `UpgradeFailed` and the running release stays exactly where it is. Re-run
`scripts/install-k8s.sh` once, with the same flags the install already uses, and
updates resume. An empty registry that *rendered* would have produced pods pulling
from nowhere, which is why this fails loudly instead.

## [0.54.3] - 2026-09-27

Nothing in the product changes. This exists so the tag carries an installer that works:
0.54.2's would refuse the very combination the preprod test plan asks for.

### Fixed

- **`--tls-secret-name` together with `--apps-tls-secret-name` produced YAML that
  `kubectl` refused**, so the install stopped at
  `error converting YAML to JSON: yaml: line 32: did not find expected key`. 0.54.2
  emitted the two optional keys as adjacent command substitutions inside the heredoc,
  and `$(...)` strips trailing newlines -- including the one meant to separate them, so
  both landed on one line. It failed before changing anything, which is the right
  failure badly explained. Both keys are echoed after the heredoc now, one statement
  each.

- **`--data-access-mode` without `--data-storage-class` aborted the `--source` install
  with no message.** The `data` block ended with `[ -n "$X" ] && echo ...` as the
  function's last statement, so `values_yaml` returned 1 and `set -euo pipefail` took
  the install down at `{ values_yaml; credential_values_yaml; } > values.yaml`. The Flux
  path survived only because `values_yaml | sed` hid the status behind the pipeline's
  last element. Both are `if` blocks now.

### Added

- `backend/tests/unit/test_install_values_are_yaml.py` runs `values_yaml` out of the
  script itself across all sixteen combinations of its four optional flags, asserting on
  parsed structure rather than on text. The 0.54.2 change had been checked with
  `helm template --set`, which renders the chart and never runs the script -- the chart
  was correct all three ways it was rendered, and the only broken component was the one
  nothing exercised. Against 0.54.2's script, 8 of the 17 tests fail.

## [0.54.2] - 2026-09-27

Three defects, all found on Azure the same afternoon, none of which any Proxmox
environment could have shown. The common cause is not a bug in any one of them:
a single-node cluster on local storage behind a self-signed certificate is not a
smaller version of a cloud cluster, it is a different one.

### Fixed

- **No blueprint-based range could start on AKS.** CDI auto-discovers a
  StorageProfile per storage class and concludes `volumeMode: Block` for every
  `disk.csi.azure.com` class. Its importer then runs as uid 107, non-root, with
  capabilities dropped, and cannot open the raw device:
  `blockdev: cannot open /dev/cdi-block-volume: Permission denied`. The
  DataVolume never completed and the range failed with `DataVolumeError` *after*
  the capability had installed, so it read as a partial failure rather than a
  structural one. Pinned to `Filesystem`. Measured either side: Block ->
  importer `CrashLoopBackOff`, 8m32s stuck in `Scheduling`; Filesystem ->
  importer `Completed`, VM running inside a minute.

  `local-path` does not support Block at all, so CDI falls back to Filesystem on
  k3s and every Proxmox environment silently took the working path.

- **Every range delete on AKS answered 502 and kept the row**, for a teardown
  that had succeeded. `destroy()` waits for the namespace, then `residue()`
  checks what is left; but a PV is cluster-scoped and outlives the namespace
  whose claim bound it, staying `Released` until the CSI driver has deleted the
  disk. Measured: namespace gone at 21s, PV gone at 34s. `residue()` run in that
  window called the teardown dirty, which is the right answer to a dirty
  teardown and the wrong one here. The volumes now get the same bounded wait the
  namespace already had.

  `local-path` PVs are host directories the provisioner reaps with the
  namespace, so the window does not exist on k3s.

- **No range application could open once the platform had a real certificate.**
  Both Ingresses take `ingress.tls.secretName`, and a certificate obtained over
  HTTP-01 carries the name that answered the challenge and nothing else --
  `*.<appsHost>` is a wildcard and no wildcard can be issued over HTTP-01 at
  all. A learner was served a certificate for a name that was not theirs, which
  a browser refuses outright rather than offering to continue. The applications
  host now takes `ingress.tls.appsSecretName` when one is named, and
  `scripts/install-k8s.sh` reads the SANs out of the secret it was given and
  refuses the combination that cannot work.

### Added

- **`--postgres-url`**, so the database comes from the environment rather than a
  pod. ADR-0012 names PostgreSQL as one of the five things a conformant cluster
  provides, and the chart has supported `postgres.enabled=false` since it was
  written -- the installer had no way to ask for it. Validated on devtest-az
  against an Azure Flexible Server: all 31 tables migrated cleanly to
  `f1e2d3c4b5a6` on PostgreSQL 16.15. The URL carries a password, so it travels
  in the same Secret the registry token does and never enters the HelmRelease
  spec. Nothing migrates existing data.

- **`--apps-tls-secret-name`**, above.

### Changed

- `install-k8s.sh --help` printed a hardcoded line range that had fallen
  seventeen lines behind the header it meant to print, so it ended mid-sentence
  and omitted `--source`. It prints the whole comment block now.

## [0.54.1] - 2026-09-26

### Fixed

- **The feedback dialog's contents touched its own border.** `Modal` pads only
  its header and exports `ModalBody` and `ModalFooter` as the wrappers that pad
  the rest; nineteen other components use them and this dialog did not, so the
  kind cards, both inputs and the context block all sat flush against the modal
  edge with no inset at all. Both views now use them, and the actions sit in a
  real footer. Nothing about the form's behaviour changes.

  Worth recording how it was found: the unit tests, the typecheck and the lint
  were all green over it. Layout is not something they can see, and this went
  out in 0.54.0 because a page that had never been looked at was treated as
  covered by the tests that did pass.

## [0.54.0] - 2026-09-26

Feedback becomes a channel rather than a table: what customers send can now be
read, filtered and triaged in the product. Plus the two portability defects that
the first Azure environments turned up.

### Added

- **A feedback inbox.** 0.53.0 shipped a way to send feedback and no way to read
  it -- the only route to a report was a `kubectl exec` query, which is what the
  release's own test plan had to print. `/feedback` is now a page, gated to
  admins and engineers, which is the server's own `STAFF_ROLES` rather than a
  narrower guess; the API already scoped rows so an author sees their own and
  staff see everything. Filter by kind and status, read the context a report
  carried as lines rather than JSON, follow a link to the range it is about, and
  set the status. Deliberately not a bug tracker -- no assignee, no priority, no
  comment thread -- because the model has none of those and inventing them in the
  page would put triage state somewhere nothing can query.

### Fixed

- **The feedback form did not look like a form.** Both fields computed to
  `border: 0px` with no padding: `border-gray-300` sets a border *colour*, and
  with no `@tailwindcss/forms` in this project there was no width to go with it,
  so the dialog read as static text and there was no visible place to type. Send
  was disabled in silence, with nothing saying a one-line summary was required.
  *Something is broken* was pre-selected, so every report defaulted to a bug
  whether or not the sender meant one. And the context line said "The range you
  are looking at" above a raw UUID, although the code had always preferred the
  range's name -- nothing ever supplied one. All four are fixed, and the dialog
  now says why Send is disabled instead of just greying out.
- **`ingress.host` may not be an IP address.** An Ingress rule's host has to be
  a DNS name; Kubernetes rejects an IP, and it rejects it while helm is applying,
  so the release failed part-installed complaining about a field the operator
  never set. Nothing had exercised it -- devtest installs with `localhost` and
  preprod with no host at all -- until an environment whose address *is* its
  host. Both the installer and the chart now refuse it before the cluster is
  touched and print the `<ip>.sslip.io` form that works. The certificate carries
  the bare IP in its IP SANs either way.
- **`pg-data` needs ReadWriteMany once a scheduler can spread its pods.** It is
  mounted by the API and both workers -- three pods. ReadWriteOnce attaches to
  one node while permitting many pods *on that node*, so the default was right on
  every single-node profile and silently wrong on a cluster with more than one:
  the pods placed elsewhere sat in `ContainerCreating` while the install reported
  success. A guard for this existed and was wrong twice over -- it took the
  largest replica count rather than the number of pods mounting the volume, and
  it read a values key that does not exist. It now counts the pods, and only
  objects when it can see the cluster really has more than one node.
  `install-k8s.sh` gains `--data-access-mode` and `--data-storage-class`.

## [0.53.3] - 2026-09-26

Release plumbing only. Nothing an operator or a learner would notice, and the
in-app "What's new" deliberately does not announce it.

### Fixed

- **`build:infra` no longer needs an upstream registry for an image we already
  mirrored.** It had been red on every tag since 0.52.1 -- including v0.53.1 and
  v0.53.2 -- on `quay.io/minio/minio`. Probed directly: the pinned digest,
  `:latest` and even the tag list all answer 401 to an anonymous token. MinIO
  has closed the community image on quay.io as it previously did on Docker Hub,
  so re-pinning to another digest cannot fix it; that is what the last fix tried
  when Hub withdrew it.

  Throughout, the artifact was already in our own registry and already being
  deployed from there -- the chart pins `pg-storage` by digest, which is why the
  platform kept working while the job failed, and why nothing noticed that the
  `pg-storage:<version>` tags stopped appearing. A digest names one exact image,
  so a copy we hold is what the pin asks for. The job now prefers our registry
  and keeps upstream only to seed a registry that does not have the image yet,
  failing rather than skipping if neither has it. Only the digest-pinned source
  gets that: a floating tag means "whatever upstream calls that now", and
  preferring our copy would silently freeze it.

  ADR-0007 already rules out a public-registry dependency at runtime. This is
  the same argument one step earlier, and it is what keeps a release
  reproducible after an upstream disappears.

## [0.53.2] - 2026-09-25

Range applications could not be opened at all on 0.53.0 or 0.53.1 -- the feature
0.53.0 was cut for. Found on pg-devtest by opening one.

### Fixed

- **The gateway could not verify the ticket it had just minted.** pg-gateway
  holds the public half of the handoff code's key pair and no private key, on
  purpose: it terminates traffic from the software a range trains against and
  must not be able to mint a console ticket or an access token. But the
  application *ticket* was signed with that same pair, and the gateway both
  mints it (on redeeming a code) and verifies it (on every later request). With
  no private key it signed with the `jwt_secret_key` fallback and then checked
  against the public key, so every ticket failed its own check. The handoff
  itself worked -- the cookie was set, the code was correctly refused on replay
  -- and the request straight after it answered *"This application needs a
  session"*, forever.

  The two credentials are now separated, because they are different problems.
  The **code** crosses an origin boundary -- the API mints it, the gateway
  verifies it, and the gateway cannot forge one -- so it stays asymmetric. The
  **ticket** never leaves one process, so asymmetry buys nothing and a half of a
  pair cannot work at all; it gets `APP_TICKET_SECRET`, shared with the API,
  which still serves the ForwardAuth path for ranges deployed before the
  switch-over, and with nothing else. Reusing `jwt_secret_key` would have worked
  and would have handed the gateway the one capability this arrangement exists
  to withhold.

  The chart generates the secret and keeps it across upgrades: one that rotated
  on upgrade would sign every learner out of their application mid-exercise.

  Nothing caught this because every test configured both halves of the pair on
  one settings object, so signing and verifying always agreed. The new tests
  configure the gateway's actual shape -- public key, no private key -- and fail
  on 0.53.1's code.

### Changed

- **The chart is published after the images its appVersion names.** `build:chart`
  is seconds of `helm package` and `helm push`; the image builds are minutes, and
  all four sat in the same CI stage with no ordering between them. Since the
  in-cluster update check decides what is available by listing chart versions,
  every release opened a window in which pressing *Update platform* advanced a
  host to a chart whose images did not exist yet. `build:chart` now waits for
  them. `build:infra` deliberately does not gate it -- the chart pins those
  images by digest, and that job fails whenever an upstream base is withdrawn.

## [0.53.1] - 2026-09-25

Two defects 0.53.0 shipped, both found moving pg-devtest onto the release.

### Fixed

- **`pg-gateway` could not start on a fresh install.** Its pod declared
  `runAsNonRoot: true` with no `runAsUser`, and the image has no `USER` line.
  `runAsNonRoot` is a refusal the kubelet resolves by reading the image, not a
  request it can satisfy, so every container was rejected with *"container has
  runAsNonRoot and image will run as root"* and the Helm upgrade failed on the
  rollout timeout. Because the rest of the platform had already rolled to the
  new version by then, the install came up working with only range application
  publishing down, and Flux had no rollback target. The pod now names uid
  65534, measured against the image rather than assumed, and keeps the
  read-only root filesystem and dropped capabilities it always had.
  `test_cluster_posture.py` now checks the pod/container `securityContext` pair
  the way the kubelet resolves it, over every workload the chart renders.
- **The update panel discarded the reason it had nothing to pull.** The check
  endpoint sets a `detail` on a successful check precisely to distinguish "you
  have the newest release" from "there are no releases" and "this install is
  not on a release"; the panel rendered that sentence only when the check
  *failed*, so both of the other cases showed a green "Up to date" and a
  greyed-out button with no explanation anywhere on the page. Four states are
  now distinct, and the disabled button's tooltip carries the same reason.

## [0.53.0] - 2026-09-25

Range applications stop depending on Traefik, the artifact library stops being readable by
everyone signed in, and customers can tell us things.

### Added

- **pg-gateway.** A range's applications were published through two `traefik.io/v1alpha1`
  middlewares -- a ForwardAuth and a StripPrefix -- chained onto an Ingress by annotation. No
  other ingress controller has an external-authorisation middleware, so that could not be
  ported to Azure Application Gateway, the AWS Load Balancer Controller, GKE or OpenShift. Both
  steps now run in a process of the platform's own, and what a cluster has to provide is one
  ordinary `networking.k8s.io/v1` Ingress. The gateway holds no database credentials, no
  object-store keys, and a public key it cannot sign with; its whole cluster permission is
  reading Ingress objects.
- **Feedback.** Tell us about a defect, an idea, or a problem with a guide or a range, from
  wherever you already are. What you were doing is read rather than asked for, and shown back
  before you send it. Only you and the people who work on it can see what you write.

### Fixed

- A range whose machines are all stopped no longer reports itself running -- which had the
  learner portal admitting students to an empty lab -- and starting one machine on a stopped
  range no longer hides it. Per-machine power is written to the activity log, as it was on the
  Docker substrate.
- An application is described as *published* rather than *reachable*. The flag never read a
  pod, so a crash-looping capability reported every application reachable; two of the three
  places that show it were also discarding the reason the platform gave and substituting a
  wrong one.
- **The artifact library was readable by anyone signed in**, on every route, including
  downloading the bytes. Reads are now the library roles or the uploader; changes are the
  uploader or an administrator.
- `minio.enabled: false` can now actually reach an external S3 endpoint: TLS was never enabled
  and the credentials were always ones the chart invented.
- Three assumptions that only held on single-node k3s: the reserved-CIDR guard, an untemplated
  Redis image that could not be mirrored, and a ReadWriteOnce volume silently pinning every
  replica to one node.

## [0.52.1] - 2026-09-22

0.52.0 published nothing. Its tag pipeline ran on a commit whose `backend:test` was failing, so
every build job after it was skipped -- no container images and, on an in-cluster install, no
chart. A host on the release channel therefore saw 0.51.0 as the newest version available,
correctly: 0.52.0 existed only as a git tag. This re-cuts the same content with artifacts.

### Fixed

- The worker-task test double handed one database session to both a task and its progress
  reporter, which commit from different threads. That corrupted the task's transaction under
  load and is what failed `backend:test` on the runner while passing on a developer machine.
  The deploy and start tasks' tests now get a session per call, on a file-backed database, so a
  second session gets a second connection as it does in production.

## [0.52.0] - 2026-09-22

The full-surface audit of the Docker → Kubernetes port: 102 findings, of which this release
closes the 17 critical and the high-severity security and terminology tranches. Thirty-three
file-disjoint lanes, each adversarially verified against the code it replaced. See
`docs/plans/2026-09-22-substrate-ui-audit.md`.

### Security

- **A range's applications are published on an origin of their own** (`ingress.appsHost`,
  `<range-id>.apps.<host>`). They were on the platform's, so a training workload's JavaScript was
  same-origin with the console and could read the operator's token out of `localStorage`. The
  platform cannot set a cookie for that host, so the ticket endpoint returns a URL carrying a
  one-use 30-second handoff code and the ForwardAuth target exchanges it for a host-only cookie.
  An install with no applications host publishes no applications.
- `decode_access_token` refuses a token carrying a `typ` that is not `access`; the 8-hour app
  ticket Traefik forwards into a range's application was a full API session.
- A learner assignment is an isolation boundary in placement, whatever the capabilities declare.
  A cohort with no per-learner scope collapsed into one namespace.
- Authorization added across `images`, `snapshots`, `content`, `walkthrough`, `scenarios`,
  `files`, `websocket`, `events`, `msel`, `connections` and the blueprint mutations; the
  blueprint routes' admin bypass never worked (it read `.name` off a list of strings).
- OVA extraction filters and refuses traversing, absolute and link members.
- Catalog: `git clone` positionals follow `--` and schemes are checked; every path declared in
  `index.json` resolves against the catalog root, including the two joins in the one-click
  installer the guarded path bypasses.
- An install permits only the chart repositories it lists (`capability_chart_repositories`);
  empty permits none, and the chart ships the repository its own sample needs.
- The frontend's security headers reach responses at all — `add_header` does not inherit across
  nginx blocks, and every location here sets its own.

### Fixed

- Admin's purge, completing/cancelling a training event, and deleting an instance destroy the
  namespace before dropping the row, and report residue instead of a silent success.
- Kubernetes blueprints round-trip through `GET`, `POST`, `PUT`, export and import; `GET` used
  to 500 and "update from range" used to overwrite one with an empty Era A config.
- Around thirty endpoints answered a bare 500 because the Docker client was constructed
  unconditionally; they answer 501 naming the substrate. `POST /networks` and `POST /vms` refuse
  on a Kubernetes range rather than writing a row that exists nowhere.
- `apply_scenario` subscripted a dataclass, so it 500'd for every scenario with events; it also
  refuses on Kubernetes, where there are no VM rows to map roles onto.
- An inject that places a file refuses instead of reporting a placement that never happened.

### Hardening

- **The session token no longer travels in a URL.** Five websocket routes and the Kubernetes
  VNC socket took the full session JWT in their query string, where it lands in browser history
  and in every proxy log on the way; a blueprint export sent it there too, on top of the
  `Authorization` header it was already sending. There is a five-minute, single-socket ticket
  now, delivered as a cookie, and a guard that fails the build on a routed `token` parameter.
- **The update check stops handing its credentials to whoever asks.** It followed the auth
  realm the registry itself named, with the deploy token attached. The realm must now be the
  registry's own host, a host beneath its parent domain, or one named in `UPDATE_REALM_HOSTS`.
- Admin-generated passwords come from a cryptographic source; the ISO fetch refuses link-local,
  loopback and private destinations and re-checks every redirect hop.
- The scenario cache keyed on an mtime equality test, so a rewrite inside one timestamp tick
  served a document that was not on disk. It is keyed on the bytes now.
- The application refuses to start on the JWT secret this repository ships, and the chart stops
  leaving `DEBUG` at its development default.

- **The control plane can stop being a cluster-wide exec key.** `rbac.rangePermissions:
  namespace` moves the range verbs out of the cluster-wide role and into a ClusterRole bound
  into each range's namespace: 108 permission tuples become 15. It defaults to `cluster`, which
  renders exactly what previous installs had — narrowing requires every live range to be
  redeployed first.
- A range's NetworkPolicy gained an egress floor. It was ingress-only, so a learner-controlled
  machine could reach the platform's own Postgres, Redis and MinIO.
- Start and stop no longer hold the HTTP request open for up to ten minutes; they dispatch to
  the worker, which records the lifecycle timestamps and events a stopped range had none of.
- A failed deploy puts its reason on the range instead of leaving it "deploying" forever.
- The chart: PodDisruptionBudgets at two replicas or more, `helm.sh/resource-policy: keep` on
  the data and database volumes, and an opt-in Traefik TLSStore.
- ~3,400 lines of unreachable wizard and range-builder code removed, with the `/users` page
  nothing linked to and the two wizard steps that collected settings nothing sent.

### Changed

- Every Era A surface is gated at the route, not the nav link. The capabilities store no longer
  answers any failure with the full Docker feature set — only a 404 means an install too old to
  answer.
- Terminology follows the substrate throughout; one product name, one toast stack, ARIA
  landmarks on the shell, a 404 route and an error boundary around the routed content.
- `frontend:test` runs the vitest suite in CI, blocking: 20 files, 303 tests. ESLint warnings
  155 → 98.

## [0.51.0] - 2026-09-21

### Added

- **Student-facing ingress for a range's applications (PG-62).** A capability declares
  `web: {service, port}`; on deploy it lands on the cluster's ingress at
  `/apps/<range-id>/<app>/` — one path per range and application, no DNS needed — behind a
  ForwardAuth middleware that asks the platform per request (a `typ: app` cookie ticket scoped to
  that range's path, minted by `POST /ranges/{id}/apps/{app}/ticket`, checked by
  `GET /range-apps/authz`) and a StripPrefix so the application sees `/`. The range's default-deny
  admits the ingress controller's pods by namespace and labels. An **Applications** row on the
  range page opens it. Proven on pg-devtest: no ticket 403, own ticket answers, another range's
  ticket 403.

## [0.50.0] - 2026-09-21

### Added

- **Console access for KubeVirt VMs (PG-61).** A range on the Kubernetes path lists its workloads
  with live state (`GET /ranges/{id}/workloads`) and opens a console to a running one: an in-app
  noVNC client over `/ws/vnc/k8s/{range}/{workload}`, which the API pipes to KubeVirt's
  `virtualmachineinstances/{name}/vnc` subresource with its ServiceAccount, after the container
  console's entitlement rule. No route into the range network, no VNC password, no port. Clipboard
  is wired both ways in the client; whether the guest honours client cut text depends on the guest.
- `install-k8s.sh --source` suspends a Flux-owned HelmRelease for the rehearsal (Flux reconciles a
  source build away within its interval otherwise); the operator path resumes it.
- The frontend build targets es2022 (noVNC 1.6 uses a top-level await).

## [0.49.4] - 2026-09-16

### Fixed

- **A release to update to.** No product change. Cut so an install at 0.49.3 has a newer release to move to; the in-UI update on the Kubernetes path was proven with it.

## [0.49.3] - 2026-09-16

### Fixed

- **Update platform on a Kubernetes install completes.** Pressing Update platform on a Kubernetes install failed with an internal error before anything happened. It now advances the install; the page reconnects once the new version is up.

## [0.49.2] - 2026-09-16

### Fixed

- **A release to update to.** No product change. Cut so that an install at 0.49.1 has a newer release to move to, which is how the in-UI update was proven on the Kubernetes path.

## [0.49.1] - 2026-09-16

### Fixed

- **The published chart installs under Flux.** 0.49.0 was the first release to publish the chart, and it could not be installed by the cluster: a label appeared twice in the rendered manifests, which the controller rejects. No product change; 0.49.1 is the same release with a chart that installs.

## [0.49.0] - 2026-09-16

### Fixed

- **"Could not check for updates" on a Kubernetes install.** The updater was Compose-shaped end
  to end — `git` in a sidecar against the checkout, then `compose.sh` — and a pod has neither.
  The release is now a Flux `HelmRelease` (`scripts/install-k8s.sh` creates it; an existing
  `helm`-CLI install is adopted by name), CI publishes the chart to the registry beside the images
  on every tag (`build:chart`), and the three update endpoints take an in-cluster branch:
  **check** reads the chart repository's tag list with the pull secret, **update** sets the
  HelmRelease's chart version to the newest X.Y.Z (resolved server-side, never from the request),
  **status** reads its Ready condition. `image.tag` defaults to the chart's `appVersion`, so one
  number names chart and images. New settings: `CHART_REPOSITORY`, `REGISTRY_CREDENTIALS_FILE`,
  `POD_NAMESPACE`, `HELM_RELEASE_NAME` — all set by the chart.
- The pull secret is chart-owned with `helm.sh/resource-policy: keep`; a rehearsal that removed it
  from the chart had helm delete it mid-upgrade, stranding the new pods with nothing to pull.

## [0.48.0] - 2026-09-16

Kubernetes path only; nothing visible on a Docker-based host.

### Added

- **A capability installs into the vcluster** (PG-41, PG-42, PG-58 — closed). A team-exercise
  range's `HelmRelease` stays in the host namespace and deploys into its vcluster through the
  kubeconfig the vcluster chart exports (`spec.kubeConfig`); hooks run inside the vcluster
  through a client built from the same kubeconfig. Teardown uninstalls the capability, then the
  vcluster, then the namespace, each waited for — no cluster-scoped RBAC left behind.
- **`CONTROL_PLANE_NAMESPACES`**: namespaces a range's default-deny NetworkPolicy lets in — where
  PROVING GROUND's own pods and Flux's helm-controller run. The chart sets it to its own namespace
  and `flux-system`. Without it the isolation floor kept the control plane out of the vcluster's
  API server.

### Fixed

- The vcluster's exported kubeconfig named a server (`…svc`) its certificate does not cover; it
  now names `vcluster.<namespace>`.
- The control plane's ServiceAccount can read the `vc-vcluster` secret (by name, not secrets at
  large).

## [0.47.0] - 2026-09-15

The Kubernetes substrate, end to end and installable. Nothing here is visible to a learner or an
instructor on a Docker-based host; every line is `RANGE_SUBSTRATE=kubernetes`, which only the
in-cluster install sets.

### Added

- **PROVING GROUND runs in-cluster.** `deploy/helm/proving-ground` deploys the API, workers and
  frontend as Kubernetes workloads, with Postgres, Redis (ADR-0015) and MinIO supplied by the
  chart on the k3s profile and by the environment elsewhere (ADR-0012). `scripts/install-k8s.sh`
  is the one command an operator runs; `scripts/setup-substrate-k3s.sh` prepares a bare host
  (k3s + KubeVirt + CDI + Multus + Flux, pinned). Proven on pg-devtest through the UI; installed
  on pg-preprod. Plan and record: `docs/plans/2026-09-15-pg-in-cluster-on-k3s.md`.
- **Ranges deploy as KubeVirt VMs on Multus networks** (PG-42, PG-58). `deploy_range_on_kubernetes`
  realises a v2 blueprint's networks and workloads, installs and seeds its capabilities, waits for
  the VMs and records the addresses they came up with. Each workload interface gets its own
  NetworkAttachmentDefinition carrying its static address, because KubeVirt overwrites the pod's
  Multus annotation. Teardown fails on residue rather than reporting it.
- **Stop/start/teardown/delete reach the cluster.** On a `kubernetes` host the range endpoints
  hand off to `api/kubernetes_ranges.py`; stop/start set `spec.running` on every VM, disks kept.
  A v2 blueprint can be instantiated from the UI; `proving_ground.tools.seed_k8s_blueprint` seeds
  one for a fresh install.
- **Placement:** a range with no training event is keyed on its own id, and one with neither event
  nor learner is a standalone namespace — two instructor sandboxes no longer share `pg-default`.
- **ADR-0002 amendment:** VMs and range networks live in the host cluster's namespace; a vcluster,
  where placed, holds the capability.

### Fixed

- **`verify()` evidence was a string of itself.** `kubernetes_asyncio` JSON-decodes any body it
  can and `str()`s the result, so every hook's `{"passed": true}` came back as a Python repr and
  fell to `raw_output`. Logs are read raw now.
- **Multus was unpinned** (`:snapshot-thick`); now `v4.3.1` by digest.
- **CI:** `backend:test` had no `git` (a contribution test runs `git apply`); substrate MRs build
  their own images, tagged by branch so they never overwrite `:master`.

## [0.46.1] - 2026-09-15

Re-cut of 0.46.0 so that a release exists with container images. **No product change.**

### Fixed

- **0.46.0 was tagged with no images.** Its tag pipeline ran on a commit whose `backend:test`
  was failing, and `build:api`, `build:worker` and `build:frontend` are gated behind the test
  stage, so all three were skipped. The git tag exists; the images never did — `pg-api`,
  `pg-worker` and `pg-frontend` carried `v0.45.0` as their only version tag. A host on the
  `release` channel would have offered the update and then failed to pull it. 0.46.1 is cut from
  a green master and carries the whole of 0.46.0's content.

### Added — not user-visible

- Era B substrate work continues behind `range_substrate`, which still defaults to `dind`: range
  networks and KubeVirt workloads wired into the Kubernetes deploy path, stop/start for KubeVirt
  VMs, and the range endpoints reaching the cluster. A host that does not opt in is unaffected.

## [0.46.0] - 2026-09-15

### Added

- **One-click catalog install with dependency resolution (PG-149).** Installing a blueprint
  resolves what it needs — base images, content, the blueprint itself — into an ordered plan
  before anything runs, so the progress total is known up front, a failure names its step, and
  a piece already present is skipped rather than reinstalled. Previously a blueprint could
  install "successfully" while the base image it referenced was never fetched.
- **Contribute blueprint improvements back to the catalog (PG-147).** A blueprint installed from
  the catalog and then fixed locally can be diffed against the catalog's own `blueprint.yaml`;
  the selected changes are applied onto that original document so key order, unknown keys and
  the catalog's schema choices survive, and the result is a reviewable patch rather than a
  rewrite. Values that merely equal the installer's defaults are not reported as changes.
- **A git-native content bundle for export and import (PG-104).** `content.yaml` with a fixed
  key order and no timestamps, `body.md` as real markdown, `walkthrough.yaml`, and an asset
  manifest with sha256 beside the bytes. Two exports of unchanged content are byte-identical;
  the transport tar.gz zeroes every mtime. Import is keyed on a slug, so re-importing updates
  rather than duplicates.
- **Redis is PG's own disposable broker (PG-39, ADR-0015).** Under the cluster capability
  contract Redis is not a service the environment supplies; PG ships its own digest-pinned,
  `emptyDir`, `save ""` deployment with a deny-all-plus-allow-labelled NetworkPolicy.
- **The capability contract is the IP boundary, enforced by the build (PG-28).**
  `backend/proving_ground/capability/` imports nothing from the rest of the engine, checked by
  a static scan and a subprocess probe that catches a lazy import inside a function.
- **Substrate (not user-visible):** versioned blueprint model for k8s ranges (PG-122), range
  lifecycle under both placement resolutions (PG-41), typed workload spec and CNI range networks
  (PG-42, PG-58), Multus working on k3s (PG-303). All behind `RANGE_SUBSTRATE=kubernetes`, which
  no host sets.

### Fixed

- **Two dramatiq actors were never registered**, so one-click install sat at "Queued for
  install… 0/4" forever and the async blueprint ZIP import dead-lettered silently. The worker
  only knows an actor whose module `proving_ground.tasks` imports;
  `test_every_actor_is_registered.py` now scans every `.send(` call site and fails on the next
  one. Found by driving the feature through the UI; the 121 tests that shipped with it call task
  bodies directly and never cross the broker.
- **`CatalogItemType.BASE_IMAGE` did not exist in the PostgreSQL enum**, so installing any base
  image failed at the insert. Migrated with `ALTER TYPE … ADD VALUE IF NOT EXISTS`. Tests run on
  SQLite, which does not enforce enum membership — that is how it shipped.

## [0.45.0] - 2026-09-09

### Added

- **KVM acceleration for range VMs, detected per range (`RANGE_KVM`).** `auto` probes `/dev/kvm`
  inside the range and enables it only where it is genuinely available, failing closed to
  software emulation; `on` and `off` force the decision. Measured on the same 4 vCPU / 8192 MiB
  Windows 11 range: **boot to RDP 804s under TCG, 96s and 126s under KVM**, and a fresh install
  ~4 hours against ~40 minutes. The default remains `off` — flipping it is a separate decision,
  because `auto` degrades silently and a host without KVM looks healthy while running 8x slower.
- **Hyper-V enlightenment control (`RANGE_HYPERV`).** On a nested-Hyper-V host — any Azure VM —
  passing the host's Hyper-V enlightenments through to a Windows guest hangs it at boot under
  KVM. `auto` reads the host clocksource and withholds them when it finds one; `on`/`off`
  override. This, not "nested virtualisation is broken", is why Windows would not boot under KVM.

### Fixed

- **A range redeployed onto a new DinD kept talking to the old one.** `get_range_client` cached
  on `range_id` alone and read `docker_url` only on a cache miss, so every redeploy of a range
  was handed the client built for whichever DinD it had first. The deploy logged the new
  container's address, waited for its daemon, then issued the next call to the previous one and
  failed with `No route to host`. It looked intermittent because the caches are per-process:
  teardown runs in the API and deploys run in the worker, so `close_range_client` only ever
  cleared the API's copy and restarting the worker "fixed" it.
- **A VM installing from an ISO was silently given a fraction of its RAM.** The VM container is
  capped at guest + QEMU overhead; extracting a 7.74 GiB install ISO left 7.96 GiB of page cache
  in that cgroup, nearly all of it reclaimable. dockur admits a guest by comparing `RAM_SIZE`
  against `memory.max - memory.current`, page cache counts toward `memory.current`, so an
  8192 MiB guest was cut to 1931 MiB with only a line in the container log — the deploy reported
  success. The container now includes room for the ISO's cache, measured from the file so a small
  Linux ISO is not charged what a Windows one costs, and clamped to what the range can back. A VM
  booting a disk it already has is sized exactly as before.
- **The pre-deployment disk check measured a path the container could not see**, so it reported
  free space for the wrong filesystem.
- **`RANGE_KVM`, `RANGE_HYPERV` and `RANGE_DEFAULT_*` are passed through to the `api` and
  `worker` services** in `docker-compose.yml`. `.env` alone does not reach a container: Compose
  reads it to expand `${VARS}`, it does not inject it, so a setting without an explicit
  passthrough keeps its default while `.env` claims otherwise.

### Tests

- Guards against enum-typed columns stored as bare strings, the defect behind 0.44.0's range
  visibility bug, across every enum column rather than the one that failed.

## [0.44.0] - 2026-09-03

### Fixed

- **`range.visibility` is stored as its enum value, not its name.** The column was added as
  `String(20)` with `server_default="private"` — the value — while `Mapped[RangeVisibility]` made
  SQLAlchemy persist member *names*, so the column held both spellings depending on which path
  wrote the row. Hydrating a `server_default`-written row raised
  `LookupError: 'private' is not among the defined enum values`, which failed the **entire**
  `GET /ranges` listing rather than the offending row — presenting as "I can see none of my
  ranges" and reading exactly like an authorization bug. Pinned with `values_callable`; migration
  `e7f8a9b0c1d2` backfills the mis-cased rows.

### Added

- **Update channels (`UPDATE_CHANNEL`).** `release` (default) follows the highest `vX.Y.Z` tag on
  the remote and compares it to this host's `VERSION`, so a host lands on a deliberately tagged
  commit and `app_version` keeps describing the running code. `branch` follows the tip of the
  current branch — the previous behaviour, appropriate for dev hosts. Passed through to the `api`
  and `worker` services in `docker-compose.yml`; `.env` alone does not reach a container.
- Release tags are read with `git ls-remote` rather than fetched. `git fetch --tags`
  force-updates every tag and fails the whole fetch with `would clobber existing tag` when any
  local tag disagrees with the remote's — the standing state of this repository, and what
  silently broke the update check once before.

## [0.43.0] - 2026-09-02

### Added

- **Range visibility**: `private` | `shared` | `public` on every range, with sharing by named user
  (`range_shares`) or matching tag. `GET`/`PUT /ranges/{id}/visibility`, and a Sharing control in the
  UI. Changing visibility requires ownership; being shared with a range does not let you widen it.

### Fixed — `SEC-10`, both halves

- **Range, network and VM endpoints check who is asking.** `GET`, `PUT`, `DELETE`, `/deploy`,
  `/start`, `/stop`, `/teardown` took `current_user` and never compared it to anything;
  `networks.py` had no 403 anywhere across ten endpoints. 67 endpoints are now guarded, with ten
  exemptions listed by name and reason.
- **The VNC data path checks who is asking.** `/vnc/{vm_id}` proxied to the console with Traefik
  injecting KasmVNC's hardcoded credentials, so knowing a VM id was full console access with no
  login. A short-lived per-VM ticket cookie plus Traefik `forwardAuth` now gates it, `forwardAuth`
  first in the chain.
- **Console access is an explicit entitlement** — owner, admin, assigned learner, or event
  participant. It previously ran on the read model, whose "untagged = public" rule entitled every
  non-student account to every console.

### Changed

- **Existing ranges migrate to `private`.** Preserving the old behaviour would mean marking
  everything public, which is the default this removes.
- Console tickets last 10 minutes and are session cookies.

## [0.42.3] - 2026-09-01

### Fixed

- **The public engine's README described the distribution, not the engine**, and named a customer
  programme in its subtitle — on a capability-agnostic engine. Rewritten; no published file names a
  customer, verified by scanning the publish filter's whole output.
- The install one-liner and the clone command each contained a literal space in the URL
  (`github.com/JongoDB/PROVING GROUND`), so neither could ever have worked.
- Removed a stale version badge (`0.35.35` against a real `0.42.2`), a "What's New in v0.35.x"
  section five minor releases old, and a table pointing at `ghcr.io/jongodb/cyroid-*` images that no
  longer receive builds.

### Changed

- `test_no_customer_names_in_code.py` now scans published prose (README, CONTRIBUTING, CHANGELOG),
  not only code — prose was invisible to it, which is how the customer name shipped. The test is
  itself excluded from publication: it carried the list of forbidden customer terms as a regex, so
  publishing it named them more plainly than the string it was written to catch.

## [0.42.2] - 2026-09-01

### Changed

- **The engine moved to `fighting-smart-cyber/cyroid`** and is published as CYROID rather than under
  the distribution's name (`MIG-8`, [ADR-0011](docs/adr/0011-the-engine-and-the-distribution-are-separate-products.md)).
  Full history moved — 1,020 commits, 24 branches, 206 tags, verified at parity. No product change.
- The product name is read from one module (`frontend/src/lib/branding.ts`) rather than 28 literals
  across ten files, so a distribution can re-theme from a single place.

## [0.42.1] - 2026-08-31

### Fixed

- **In-UI update took the platform down instead of updating it.** The update container mounted
  the repository at `/repo`, but `docker compose` inside it drives the *host's* daemon while
  resolving the compose files' relative bind mounts against its own filesystem. The host had no
  `/repo`, so Docker created every missing path as a directory — `config/registry-config.yml`,
  `VERSION` and `traefik.yml` among them — and the registry died on "not a directory", taking
  the API, workers and proxy with it. The repo is now bound at its host path, the only
  arrangement where a relative mount means the same thing on both sides.

### Added

- **The update card reports whether an update is available**, with how far behind the host is
  and the latest tag, via `GET /admin/infrastructure/update/check`.

### Changed

- A check that cannot reach the remote reports *that*, never "up to date" — the two must not
  render alike, or the update goes quiet exactly when the credential or remote is broken. The
  button disables only on a confirmed up-to-date and stays enabled when the check fails.
- The update and the check share one container spec, so the mount path has a single definition.

## [0.42.0] - 2026-08-30

### Fixed

- **Postgres, Redis and MinIO were published on `0.0.0.0`.** Now bound to loopback. Only 80 and
  443 remain public.
- **The frontend was an internet-facing Vite dev server** — a development tool that resolves and
  transforms modules per request. Now a production build behind nginx (`docker-compose.serve.yml`,
  on by default; `PG_SERVE_FRONTEND=0` opts out).
- **`api.insecure` served the Traefik dashboard and full API unauthenticated on :8080.** That
  listener no longer exists; the dashboard is a router on the internal entrypoint, loopback-published
  on **8082**.
- **The warm pool did not survive a Redis restart.** The ready set lives only in Redis, so
  recreating that container orphaned healthy members — still running, still holding their memory,
  never claimable. `reconcile_ready_set()` rebuilds it from container labels, refusing any member
  that a range already holds.
- Digest references were attempting a registry push that cannot succeed (`repo@sha256` is not a
  repository), erroring on every pinned deploy before falling back to tar.

### Added

- **Platform update from the UI** (Admin → Infrastructure), admin-only, using a Fernet-encrypted
  token the platform stores itself. See [ADR-0014](docs/adr/0014-the-platform-updates-itself-from-a-container-it-does-not-own.md).
- Traefik access logging for 4xx/5xx, recording the client address. Nothing in the stack recorded
  one before, so "who probed this host" could not be answered even after the fact.

### Changed

- `scripts/compose.sh` is the single definition of the compose overlay chain. `pg-update.sh` and
  the in-UI update both call it; they each carried their own copy before, so a host could be
  redeployed by either path and get a different stack.

## [0.41.1] - 2026-08-29

### Changed

- No customer names in shipped code — placeholder and example values referring to a specific
  customer replaced with generic ones. The platform is agnostic of who trains on it.

## [0.41.0] - 2026-08-29

### Added

- **Windows golden images**: capture a running Windows VM and deploy from it, booting the
  installed system rather than reinstalling. Stored sparsely — a 64 GiB disk occupies ~12 GiB.
- VM memory is validated against its range's cap at edit time instead of failing at deploy.

### Changed

- Golden-image deploys went from ~199s to ~75s, and to ~15s when a warm pool member is claimed.
  The pool now pre-pulls the exact runtime digests golden images pin, which is what makes the
  warm path reachable for Windows at all.
- QEMU headroom raised to 2 GiB after measurement: a 4 GiB guest settles at 4.91 GiB, so the
  previous 1 GiB allowance left ~0.1 GiB of real margin and guests were being OOM-killed.
- One definition of what a VM runs and clones (`image_resolution.py`) and of which image a VM came
  from (`VM.effective_image`), replacing copies that had drifted apart.

### Fixed

- Runtime digests were recorded against this host's local mirror address, which means nothing on
  another deployment. They are now stored registry-agnostically, with the mirror applied at pull.
- A leftover scratch directory was mistaken for a learner's disk, skipping the clone and leaving
  the VM to download Windows from Microsoft.
- The console, cloning and several other paths only understood VMs created from a base image and
  silently did nothing for golden images and snapshots.

---

> **Gap: 0.9.0 – 0.40.x are not recorded here.** Roughly two hundred tags were cut between
> January and August 2026 without changelog entries. They are not reconstructed below rather than
> being invented after the fact; `git log v0.8.1..v0.41.0` is the record. Entries resume at 0.41.0.

## [0.8.1] - 2026-01-18

### Added

- **VyOS Router Template**: New seed template allowing engineers to deploy additional VyOS routers within ranges for internal network segmentation under the edge router.
- **Edge Router Configuration in Range Wizard**: New configuration options for the edge router including:
  - DHCP server toggle for non-isolated networks
  - Custom DNS servers configuration
  - DNS search domain setting
- **Network OS Type**: Added `network` OS type for network device templates (VyOS, OPNsense, pfSense, etc.)

### Fixed

- **Training Scenarios Seeding**: Fixed scenario seeding that failed on first boot due to volume mount timing. Scenarios now seed correctly on container restart.

## [0.8.0] - 2026-01-18

### Added

- **Training Scenarios** ([#25](../../issues/25)): Pre-built MSEL packages for deploying realistic cyber training exercises with one click.
  - 4 ready-to-use scenarios: Ransomware Attack, APT Intrusion, Insider Threat, Incident Response Drill
  - New Training Scenarios page at `/scenarios` showing scenario cards with filtering
  - Role-based VM mapping: map scenario roles (e.g., "domain-controller") to actual range VMs
  - "Add Scenario" button on Range Detail page for running ranges
  - Scenario seeding from YAML files in `data/seed-scenarios/`
  - New API endpoints: `GET /scenarios`, `GET /scenarios/{id}`, `POST /ranges/{id}/scenario`

### Changed

- **Naming Updates**: Improved naming consistency across the application
  - Templates → VM Templates
  - Blueprints → Range Blueprints
  - Guided Builder → Range Wizard

## [0.7.3] - 2026-01-18

### Fixed

- **Seed Templates Not Visible** - Fixed query in templates API that excluded seed templates from non-admin users. Seed templates (built-in PROVING GROUND templates) are now always visible to all users regardless of visibility tag settings.
- **Seed Templates Not Mounted** - Added volume mount for `data/seed-templates` directory in docker-compose.yml so the API container can access seed template YAML files.
- **Template Response Schema** - Allow null `created_by` in template response for seed templates. Added `is_seed` and `seed_id` fields to response schema.

## [0.7.2] - 2026-01-18

### Fixed

- **API Startup Crash** - Fixed import error in template seeding that prevented API from starting (`SessionLocal` → `get_session_local()`)

## [0.7.1] - 2026-01-17

### Added

- **Docker Image Build UI**: Build custom Docker images directly from the Image Cache page with real-time progress tracking.
  - New "Build Images" tab in Image Cache
  - Lists all available Dockerfiles from `images/` directory
  - Background build with progress percentage, step tracking, and live logs
  - Build persists across page navigation (polling-based like pull tracking)
  - Cancel build functionality
  - No-cache rebuild option for fresh builds
  - New API endpoints: `/cache/images/buildable`, `/cache/images/build`, `/cache/images/build/{key}/status`

## [0.7.0] - 2026-01-17

### Added

- **PROVING GROUND Kali Attack Box** ([#28](../../issues/28)): Custom Docker image with comprehensive offensive security toolkit.
  - Based on `kasmweb/core-kali-rolling` with KasmVNC desktop access
  - Includes: Metasploit, Impacket suite, BloodHound, CrackMapExec, Evil-WinRM
  - Password tools: Hashcat, John, Hydra, Responder
  - Tunneling: Chisel, Ligolo-ng, Proxychains
  - Web testing: Feroxbuster, FFuf, SQLMap, Nikto
  - Wordlists: Rockyou, SecLists
  - PEAS scripts for privilege escalation
  - Multi-architecture support (x86_64 and ARM64)

- **Samba AD Domain Controller** ([#29](../../issues/29)): Docker image for Active Directory functionality on ARM64.
  - Full AD DC using Samba with LDAP, Kerberos, DNS
  - Works on Apple Silicon and other ARM64 hosts
  - Environment variable configuration for realm, domain, admin password
  - Optional test user creation

- **Built-in Template Repository**: Pre-configured templates that ship with PROVING GROUND.
  - Templates: Kali Attack Box, Samba DC, Windows Server 2022 DC, Ubuntu Desktop, Ubuntu Server
  - Auto-seeded on application startup
  - New `is_seed` and `seed_id` fields for template identification

- **VM Snapshot UI** ([#28](../../issues/28)): Create snapshots directly from running VMs.
  - New Camera icon button on VM action bar (visible when VM is running)
  - CreateSnapshotModal component with name and description fields
  - Auto-generated snapshot name with hostname and date
  - Integrates with existing /snapshots API

### Changed

- Template model now supports nullable `created_by` for seed templates
- Application startup seeds built-in templates if not already present

## [0.6.3] - 2026-01-17

### Added

- **Lifecycle Timestamps & Activity History** ([#23](../../issues/23)): Track when ranges are deployed, started, and stopped with user attribution.
  - Range model now includes `deployed_at`, `started_at`, `stopped_at` timestamps
  - EventLog now includes `user_id` to track who triggered events
  - Timestamps displayed in Range header with relative time (hover for exact)
  - New Activity tab on RangeDetail showing event history grouped by day
  - Events show username/email of who triggered them

## [0.6.2] - 2026-01-17

### Added

- **Granular Deployment Status** ([#24](../../issues/24)): Per-resource deployment tracking showing individual status for every network and VM during deployment.
  - New `/deployment-status` API endpoint returns structured status for each resource
  - Refactored DeploymentProgress component with per-resource rows
  - StatusIcon, ResourceRow, ResourceSection components for granular display
  - Router, networks, and VMs shown individually with status icons and durations
  - Added `network_id` field to EventLog for network-specific event tracking
  - Progress bar with elapsed time and resource counts

## [0.6.1] - 2026-01-17

### Fixed

- **Student Lab Panel Import** - Fixed react-resizable-panels v4.x API compatibility issue in StudentLab.tsx. Updated renamed exports (`PanelGroup` → `Group`, `PanelResizeHandle` → `Separator`) and props (`direction` → `orientation`).

## [0.6.0] - 2026-01-17

### Added

- **Range Blueprints** ([#18](../../issues/18)): Save ranges as reusable blueprints and deploy multiple isolated instances with auto-allocated subnets.
  - Save any range as a blueprint with "Save as Blueprint" button
  - Deploy instances from blueprints with automatic subnet offset (10.100 → 10.101 → 10.102)
  - Instance actions: reset (same version), redeploy (latest version), clone
  - Blueprints page with card grid showing all blueprints
  - Blueprint detail page with instances tab
  - Instance info banner on RangeDetail for blueprint-deployed ranges
  - New API endpoints: /blueprints, /instances
  - RangeBlueprint and RangeInstance database models

## [0.5.0] - 2026-01-17

### Added

- **Guided Range Builder Wizard** ([#19](../../issues/19)): New wizard-style interface for creating complete cyber training environments with minimal manual configuration.
  - 4 scenario presets: AD Enterprise Lab, Segmented Network (DMZ), Incident Response Lab, Penetration Testing Target
  - 5-step wizard flow: Scenario Selection → Zone Configuration → System Selection → Configuration Options → Review & Deploy
  - Auto-assigned subnets and IP addresses for each zone and system
  - AD configuration options: domain name, admin password, user count
  - Vulnerability level selection (cosmetic in v1)
  - Sequential deployment with progress indicator
  - "Guided Builder" button on Ranges page header
  - Empty state CTA prioritizes guided builder for first-time users
  - Template name-to-ID mapping for flexible preset definitions

## [0.4.11] - 2026-01-17

### Added

- **Student Lab Page with Walkthrough Panel** ([#8](../../issues/8)): New `/lab/:rangeId` page provides a student-focused experience with integrated step-by-step walkthrough alongside VNC consoles.
  - WalkthroughPanel: Collapsible left panel with phase navigation, step checklist, and markdown content
  - Progress tracking: Local storage + optional server sync with auto-save
  - VM integration: "Open VM" button switches console to referenced VM
  - Split-pane layout: Resizable panels with embedded VNC console
  - Markdown rendering: Code blocks, blockquotes (tips/warnings), headers
  - MSEL extension: Walkthrough content authored in YAML `walkthrough:` section
  - New WalkthroughProgress model for server-side progress persistence
  - "Open Lab" button on RangeDetail page for running ranges

## [0.4.10] - 2026-01-17

### Fixed

- **Delete Confirmation UX** ([#20](../../issues/20)): Replaced browser native `window.confirm()` popups with styled confirmation dialogs. Fixed double-click issue and improved visual consistency across all delete operations.
  - Created reusable ConfirmDialog component with danger/warning/info variants
  - Updated Range, Network, VM, Template, and Image Cache delete confirmations
  - Replaced `alert()` error messages with toast notifications

## [0.4.9] - 2026-01-17

### Added

- **Diagnostics Dashboard** ([#4](../../issues/4)): New "Diagnostics" tab on RangeDetail page provides visibility into component health, error history, and container logs without requiring SSH or Docker CLI access.
  - ComponentHealth: Collapsible status tree showing Range → Router → Networks → VMs with color-coded health indicators
  - ErrorTimeline: Chronological display of error events (vm_error, deployment_failed, inject_failed) with filtering
  - LogViewer: On-demand container log retrieval with refresh, copy-to-clipboard, and auto-scroll
  - Error badge on tab shows count of components in error state
  - `error_message` field added to VM and Range models to persist error details
  - New `GET /api/v1/vms/{id}/logs` endpoint for fetching container logs

## [0.4.8] - 2026-01-17

### Fixed

- **Traefik Network Connection** ([#17](../../issues/17)): Fixed bug where Traefik was only connected to isolated networks during deployment, causing VNC console access to fail for non-isolated networks. Traefik is now connected to all range networks regardless of isolation status.

## [0.4.7] - 2026-01-17

### Fixed

- **Console Connection Feedback** ([#15](../../issues/15)): Console windows now provide clear feedback when connections fail or timeout instead of showing blank screens.
  - VNC console shows 30-second timeout warning with troubleshooting options
  - Terminal console shows connection status and helpful error messages
  - Both consoles have a Help button with troubleshooting tips
  - Loading states clearly indicate connection progress
  - Error states provide actionable guidance (Retry, Keep Waiting, Close)

## [0.4.6] - 2026-01-17

### Added

- **AI-Friendly API Documentation** ([#12](../../issues/12)): Enhanced OpenAPI documentation with comprehensive descriptions, organized tags, and a dedicated `/api/v1/schema/ai-context` endpoint that provides a condensed API guide for AI assistants. Enables AI tools to generate valid PROVING GROUND configurations without source code access.

### Changed

- OpenAPI description now includes concepts guide, quick start, and authentication info
- API endpoints organized with descriptive tags in Swagger UI

## [0.4.5] - 2026-01-17

### Fixed

- **Console Opens in New Window** ([#13](../../issues/13)): Console button on Range Detail page now opens console in a new browser window by default (Shift+click for inline modal). Previously only worked from Execution Console.
- **Range Stop Cleans Up Router** ([#11](../../issues/11)): Stopping a range now properly stops the VyOS router container in addition to VMs. Starting a stopped range now starts the router before VMs.
- **Escape Key Closes Console** ([#14](../../issues/14)): Pressing Escape now closes the inline console modal and returns to the range view.

## [0.4.4] - 2026-01-16

### Added

- **Real-Time UI Updates via WebSocket** ([#5](../../issues/5)): Live status updates without page refresh. When deploying a range, starting VMs, or performing any operation, users now see status updates in real-time as they happen.
  - WebSocket event streaming with Redis pub/sub for scalable broadcasting
  - Selective subscription to specific ranges for efficient bandwidth usage
  - Toast notifications for significant events (deployment complete, VM errors)
  - Pulse animations on status badges when VM states change
  - Connection status indicator showing live update availability
  - `useRealtimeRange` React hook for easy integration

### Changed

- WebSocket endpoints enhanced with Redis pub/sub infrastructure
- EventService now broadcasts events to connected clients in real-time
- Added connection manager for WebSocket lifecycle and subscriptions

## [0.4.3] - 2026-01-16

### Added

- **Verbose Deployment Progress** ([#6](../../issues/6)): Real-time deployment status with visual stepper and expandable log panel. Shows step-by-step progress through router creation, network provisioning, and VM startup. Includes detailed event logging with timestamps and color-coded status indicators.

### Changed

- Added 9 new deployment event types for granular progress tracking
- Events API now supports filtering by event_types parameter
- Event log component updated with icons for all deployment events

## [0.4.2] - 2026-01-16

### Added

- **Multi-Architecture Support**: Native support for both x86_64 and ARM64 host systems with automatic architecture detection and emulation warnings for cross-architecture VMs

## [0.4.1] - 2026-01-16

### Added

- **Version Display in UI** ([#7](../../issues/7)): Application version is now displayed in the sidebar footer, showing version number and git commit hash when available. Added `/api/v1/version` endpoint returning version, commit, build date, and API version.

- **Console Pop-out as Default** ([#9](../../issues/9)): Clicking the console button on a running VM now opens the console in a dedicated browser window by default, providing a better multi-tasking experience. Use Shift+click to open inline (legacy behavior). Added standalone `/console/:vmId` route for pop-out windows with automatic console type detection (terminal vs VNC).

### Changed

- Console button icon changed from Terminal to ExternalLink to indicate pop-out behavior
- FastAPI app version now dynamically reads from config instead of hardcoded value

## [0.4.0] - 2026-01-15

### Added

- Execution Console with multi-panel dashboard
- MSEL (Master Scenario Events List) parser with Markdown/YAML support
- Manual inject execution from console
- Connection tracking for monitoring student activity
- Real-time event logging via WebSocket streaming
- Network interface management (add/remove NICs on running VMs)

## [0.3.0] - 2026-01-XX

### Added

- Range templating with import/export/clone
- Comprehensive range export with Docker images for offline deployment
- Range import with conflict detection and template resolution
- Artifact repository backed by MinIO with SHA256 verification
- Snapshot management and golden images for Windows VMs

## [0.2.0] - 2026-01-XX

### Added

- Multi-network support with custom subnets
- Visual range builder interface
- Range deployment orchestration
- VNC console access through Traefik proxy
- Dynamic network attachment for multi-homed VMs

## [0.1.0] - 2026-01-XX

### Added

- Initial release
- JWT authentication with user registration
- RBAC with 4 roles (Admin, Range Engineer, White Cell, Evaluator)
- ABAC with resource tags for fine-grained visibility
- VM template CRUD with 27+ OS templates
- Range CRUD with full lifecycle management
- Basic network management
