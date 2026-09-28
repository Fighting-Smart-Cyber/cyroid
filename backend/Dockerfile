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

EXPOSE 8000

CMD ["uvicorn", "proving_ground.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
