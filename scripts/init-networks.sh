#!/bin/bash
# PROVING GROUND Network Initialization Script
#
# Creates the required Docker networks for PROVING GROUND infrastructure.
# Run this before starting docker-compose if networks don't exist.
#
# Networks:
#   - pg-mgmt (172.30.0.0/24): PROVING GROUND infrastructure services
#   - pg-ranges (172.30.1.0/24): Range DinD containers

set -euo pipefail

echo "=== Initializing PROVING GROUND Networks ==="

# Create management network if not exists
if ! docker network inspect pg-mgmt &>/dev/null; then
    echo "Creating pg-mgmt network..."
    docker network create \
        --driver bridge \
        --subnet 172.30.0.0/24 \
        --gateway 172.30.0.1 \
        pg-mgmt
    echo "Created pg-mgmt (172.30.0.0/24)"
else
    echo "pg-mgmt network already exists"
fi

# Create ranges network if not exists
if ! docker network inspect pg-ranges &>/dev/null; then
    echo "Creating pg-ranges network..."
    docker network create \
        --driver bridge \
        --subnet 172.30.1.0/24 \
        --gateway 172.30.1.1 \
        pg-ranges
    echo "Created pg-ranges (172.30.1.0/24)"
else
    echo "pg-ranges network already exists"
fi

echo ""
echo "=== Networks initialized ==="
docker network ls | grep proving_ground
echo ""
echo "Ready to start PROVING GROUND with: docker-compose up -d"
