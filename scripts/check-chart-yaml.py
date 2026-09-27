#!/usr/bin/env python3
"""Render the chart and parse it the way helm-controller does: duplicate keys are an error.

The helm CLI's parser tolerates a mapping key given twice; Flux's post-renderer (yaml.v3) does
not. A chart that installs by hand and fails under the controller is exactly the kind of gap a
release should not discover. Run before packaging:  python scripts/check-chart-yaml.py
"""
import subprocess
import sys

import yaml


class Strict(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)

rendered = subprocess.run(
    ["helm", "template", "pg", "deploy/helm/proving-ground", "-n", "pg-system",
     "--set", "image.tag=check", "--set", "registryCredentials.username=u",
     "--set", "registryCredentials.password=p",
     # No default any more, deliberately -- see values.yaml. Any value renders.
     "--set", "image.registry=registry.example.invalid/cyroid",
     "--set", "minio.image=registry.example.invalid/pg-storage@sha256:" + "0" * 64],
    check=True, capture_output=True, text=True,
).stdout
docs = 0
for doc in yaml.load_all(rendered, Loader=Strict):
    docs += 1
print(f"OK  {docs} documents, no duplicate keys")
