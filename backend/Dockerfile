# backend/Dockerfile
#
# BASE_IMAGE/BASE_TAG are build arguments because Iron Bank's hardening_manifest.yaml passes the
# base in as exactly those two names (`args: {BASE_IMAGE, BASE_TAG}`) and builds against
# registry1.dso.mil. Defaulting them to the public base keeps every existing build identical --
# `docker build backend/` still produces what it always did -- while making the swap to a
# hardened base a value change rather than an edit to this file.
ARG BASE_IMAGE=docker.io/library/python
ARG BASE_TAG=3.11-slim
FROM ${BASE_IMAGE}:${BASE_TAG}

# Version passed from CI build (from root VERSION file)
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# The five labels Iron Bank's schema requires, and they have to be true: the licence is checked
# against the SPDX list and the vendor/version against the submission.
LABEL org.opencontainers.image.title="cyroid-api" \
      org.opencontainers.image.description="CYROID API and worker -- range lifecycle, capability runtime, learner record" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.vendor="Fighting Smart Cyber" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.source="https://github.com/Fighting-Smart-Cyber/cyroid"

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    p7zip-full \
    genisoimage \
    iptables \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ASD STIG APSC-DV-000510 (execute without excessive account permissions): this image deliberately
# still runs as root, and the frontend image no longer does. Two things have to change first, and
# adding a `USER` line without them breaks the product in one case and weakens it silently in the
# other:
#
#   1. /var/run/docker.sock is bind-mounted into the api and worker services and reached through
#      docker.from_env(). A non-root process needs to be in the group that owns that socket, whose
#      gid differs from host to host, so it has to be supplied at deploy time (group_add with the
#      host's docker gid) rather than baked in here. Guessing a gid produces a container that cannot
#      manage ranges at all.
#   2. docker_service.py runs iptables locally - `subprocess.run(["iptables", "-N", chain])` and the
#      flush/delete calls around it - to build the per-range chain. That needs CAP_NET_ADMIN. Worse
#      for a silent failure: those calls pass capture_output=True and do not check the return code,
#      so as an unprivileged user they would fail quietly and the network isolation they implement
#      would stop being enforced while everything still looked healthy. That is a worse outcome than
#      running as root, which is why this is not a one-line change.
#
# The route to closing it: move the local iptables work into the DinD container (the rest of the
# isolation already goes through dind_container.exec_run(..., privileged=True)), make those calls
# check their exit status, then add a non-root user here and group_add at deploy time.

EXPOSE 8000

CMD ["uvicorn", "proving_ground.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
