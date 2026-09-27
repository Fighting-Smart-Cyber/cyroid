"""Catalog-facing logic that is not part of the catalog *service*.

The service owns syncing and installing. This package owns the reverse
direction: working out how a locally-edited blueprint differs from the catalog
item it came from, and rendering that difference as something a catalog
maintainer can review.
"""
