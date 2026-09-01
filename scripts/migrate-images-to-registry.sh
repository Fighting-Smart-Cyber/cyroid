#!/bin/bash
# Migrate existing cached images to registry
#
# This script pushes all proving_ground/* images from the host Docker daemon
# to the local registry and optionally removes them from the host.
#
# Usage: ./scripts/migrate-images-to-registry.sh [--remove-after-push]

set -e

REGISTRY_URL="${REGISTRY_URL:-127.0.0.1:5000}"

echo "PROVING GROUND Image Migration to Registry"
echo "==================================="
echo "Registry: $REGISTRY_URL"
echo ""

# Check if registry is reachable
if ! curl -s --fail "$REGISTRY_URL/v2/" > /dev/null 2>&1; then
    echo "ERROR: Cannot reach registry at $REGISTRY_URL"
    echo "Make sure the registry container is running: docker compose ps registry"
    exit 1
fi

# Get list of proving_ground/* images on host
images=$(docker images --format "{{.Repository}}:{{.Tag}}" | grep "^proving_ground/" | grep -v "<none>")

if [ -z "$images" ]; then
    echo "No proving_ground/* images found on host. Nothing to migrate."
    exit 0
fi

echo "Found $(echo "$images" | wc -l | tr -d ' ') images to migrate:"
echo "$images"
echo ""

# Parse options
REMOVE_AFTER_PUSH=false
if [ "$1" = "--remove-after-push" ]; then
    REMOVE_AFTER_PUSH=true
    echo "Will remove images from host after successful push."
    echo ""
fi

# Migrate each image
success_count=0
fail_count=0

for image in $images; do
    echo "Processing $image..."

    # Tag for registry
    registry_tag="$REGISTRY_URL/$image"
    docker tag "$image" "$registry_tag"

    # Push to registry
    if docker push "$registry_tag"; then
        echo "  ✓ Pushed to registry"
        success_count=$((success_count + 1))

        # Remove from host if requested
        if [ "$REMOVE_AFTER_PUSH" = true ]; then
            docker rmi "$registry_tag" "$image" 2>/dev/null || true
            echo "  ✓ Removed from host"
        else
            docker rmi "$registry_tag" 2>/dev/null || true  # Remove registry tag only
        fi
    else
        echo "  ✗ Failed to push"
        fail_count=$((fail_count + 1))
        docker rmi "$registry_tag" 2>/dev/null || true
    fi
    echo ""
done

echo "Migration complete!"
echo "  Succeeded: $success_count"
echo "  Failed: $fail_count"

if [ $fail_count -gt 0 ]; then
    exit 1
fi
