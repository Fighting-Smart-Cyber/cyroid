# The content bundle

A **content bundle** is how a Content Library item leaves PROVING GROUND: a
directory of ordinary files that git can diff and a person can review.

Content is authored by engineers, promoted between environments by GitOps, and
has to ship air-gapped. All three want the same artifact, and they want it to be
*stable*: two exports of unchanged content must be identical, byte for byte, or
a diff shows noise and a checksum recorded on one side of an air gap means
nothing on the other.

## Layout

```
<slug>/
  content.yaml       metadata, fixed key order, no timestamps
  body.md            the markdown, as markdown
  walkthrough.yaml   the structured walkthrough, when there is one
  assets.yaml        manifest: filename, mime type, size, sha256
  assets/<filename>  asset bytes, verbatim
```

`content.yaml`:

```yaml
schema_version: 1
id: lab-guide
title: Lab Guide
description: How to run the lab.
content_type: student_guide
version: '1.2'
organization: Example Org
tags:
  - networking
  - tier-1
```

## What makes it stable

- **No timestamps anywhere.** The previous `?format=json` export stamped
  `exported_at` into the payload, so the same unchanged content exported
  differently every time.
- **Markdown is stored as markdown.** In the JSON export it was a quoted
  string, so every newline was an escape and a one-word change showed as one
  rewritten line.
- **Fixed key order** in `content.yaml`, so a diff never reorders.
- **Tags are sorted.** They come out of a JSON column in arbitrary order.
- **Line endings are normalised** to `\n` with a guaranteed trailing newline.
- The `tar.gz` used for transport sets `mtime=0` on every member and on the
  gzip header, for the same reason.

`id` is a slug derived from the title, not a database UUID: the same content
promoted from one environment to another is a different row with a different
id, and matching on that would make every promotion look like a new item.

## Importing

Import is decided before it is done, and converges rather than accumulating:

| Situation | Action |
|---|---|
| Not in the library | `create` |
| Present and identical | `unchanged` — nothing is written |
| Present and different | `conflict` — reported, nothing is written |
| Present and different, `overwrite=true` | `update`, listing what it replaced |

Re-running the same import is therefore a no-op, which is what GitOps promotion
needs. Content that exists and differs is never replaced silently; replacing an
author's local edits is the one outcome that cannot be undone.

An asset whose bytes disagree with its recorded `sha256` is refused, so a
truncated transfer cannot put content in the library that nobody reviewed.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/content/{id}/bundle` | export as `tar.gz` |
| `POST /api/v1/content/bundle/preview` | report what an import would do |
| `POST /api/v1/content/bundle/import` | import, `?overwrite=` to replace |

The Content Library's per-item menu offers **Export as bundle**.

## Not yet done

The bundle is not yet carried inside a Zarf package — Zarf packaging is PI-3
work ([ADR-0007](adr/0007-one-artifact-serves-both-connected-and-air-gapped-delivery.md)
amendment), and there is no package for it to travel in. The layout above is
the contract that packaging will consume: a directory, no absolute paths, no
generated timestamps, everything needed to rebuild the item present in the
tree.
