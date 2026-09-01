/**
 * Product changelog for the in-app "What's new" modal: catch operators and
 * instructors up on what changed when a version ships.
 *
 * Keep it USER-FACING — describe what is different for the person using
 * PROVING GROUND, not how it was implemented. Add an entry whenever VERSION is
 * bumped for something anyone would notice. Newest first; CHANGELOG[0] is the
 * latest release.
 *
 * This file is the source for the modal. CHANGELOG.md carries the same
 * releases for people reading the repository, and
 * backend/tests/unit/test_changelog_agrees.py fails if the two disagree about
 * which versions exist -- one of them is always the one someone forgets.
 *
 * The running version comes from GET /api/v1/version, not from a constant
 * here: the API reads it from the VERSION file, and a second copy in the
 * frontend is exactly the kind of thing that drifts (frontend/src/version.ts
 * sat at 0.31.2 for eleven releases before anyone noticed, which is why it has
 * no importers any more).
 */

export type ChangeKind = 'feature' | 'improvement' | 'fix'

export interface ChangeEntry {
  kind: ChangeKind
  text: string
}

export interface Release {
  version: string // "0.42.1"
  date: string // ISO date, "2026-08-31"
  title: string // short headline
  highlights: ChangeEntry[]
}

export const CHANGELOG: Release[] = [
  {
    version: '0.42.3',
    date: '2026-09-01',
    title: 'Corrections to the open-source engine\'s documentation',
    highlights: [
      {
        kind: 'fix',
        text: 'No product change. The open-source engine\'s README described the wrong product, named a specific customer programme on the front page of what is a customer-agnostic engine, and carried install and clone commands that could never have worked — both contained a space in the URL. It also advertised a version five releases old and container images that are no longer built.',
      },
    ],
  },
  {
    version: '0.42.2',
    date: '2026-09-01',
    title: 'The engine moved to its own home and is published as CYROID',
    highlights: [
      {
        kind: 'improvement',
        text: 'Nothing changes in the product. Behind it, the platform\'s source moved to its own repository and the open-source release is now published as CYROID rather than under this distribution\'s name. CYROID is the engine — range lifecycle, curriculum, learner record, assessment — and PROVING GROUND is a distribution built on it. The separation was always the intent; the repositories now match it.',
      },
    ],
  },
  {
    version: '0.42.1',
    date: '2026-08-31',
    title: 'The update button works, and now tells you whether there is anything to update',
    highlights: [
      {
        kind: 'fix',
        text: 'Updating from the UI took the platform down instead of updating it. The update ran from a directory the host did not have, so Docker created the platform\'s own config files as empty folders and the image registry refused to start, taking the API, the workers and the proxy with it. The update now runs against the repository where it actually lives, which is the only arrangement where the two agree.',
      },
      {
        kind: 'feature',
        text: 'The update card now says whether an update is available before you run one, with how far behind this host is and the latest released version. Previously the only way to find out was to run a full update and read the log.',
      },
      {
        kind: 'improvement',
        text: 'If that check cannot reach the code remote it says so, rather than reporting "up to date". The two are not the same, and showing them the same way would hide a broken credential at exactly the moment it matters. The update button stays available whenever the check could not run.',
      },
    ],
  },
  {
    version: '0.42.0',
    date: '2026-08-30',
    title: 'Only the product is reachable from outside the host now',
    highlights: [
      {
        kind: 'fix',
        text: 'The database, the cache and object storage were each listening on the public network interface. They now accept connections only from the host itself. Nothing about how you use PROVING GROUND changes; there is simply far less of it exposed.',
      },
      {
        kind: 'fix',
        text: 'The web interface was being served by a development tool that compiles pages on request. It is now a pre-built site served by a normal web server. Pages load faster and the request-time compiler is no longer part of the public surface.',
      },
      {
        kind: 'fix',
        text: 'The proxy dashboard was served with no password on a second, separate port. That port no longer exists; the dashboard moved behind the ordinary internal entrypoint, reachable through an SSH tunnel on 8082.',
      },
      {
        kind: 'improvement',
        text: 'Requests that fail are now logged with the address they came from. Nothing anywhere recorded that before, so a question like "who probed this host" could not be answered even after the fact.',
      },
      {
        kind: 'fix',
        text: 'The warm pool of pre-booted ranges stopped working after any restart of the cache, silently: the pre-booted containers kept running and holding their memory but could never be handed out, so every deploy paid full cold-start cost. The pool now recovers them.',
      },
      {
        kind: 'feature',
        text: 'Administrators can update the platform from Admin Settings instead of needing shell access, using a token the platform stores itself.',
      },
    ],
  },
  {
    version: '0.41.1',
    date: '2026-08-29',
    title: 'No customer names in shipped code',
    highlights: [
      {
        kind: 'improvement',
        text: 'Example and placeholder values referring to a specific customer were replaced with generic ones. The platform is agnostic of who is training on it, and the code now reads that way.',
      },
    ],
  },
  {
    version: '0.41.0',
    date: '2026-08-29',
    title: 'Windows ranges deploy from a golden image in seconds',
    highlights: [
      {
        kind: 'feature',
        text: 'A Windows VM can be captured as a golden image and deployed from it, booting the installed system directly instead of reinstalling. A 64 GB disk is stored in about 12 GB, because empty space is skipped rather than copied.',
      },
      {
        kind: 'improvement',
        text: 'Deploying a range from a golden image went from just over three minutes to well under one, and to about fifteen seconds when a pre-booted range is waiting for it. The pool now pre-fetches the exact image versions golden images depend on, which is what makes the fast path reachable for Windows.',
      },
      {
        kind: 'fix',
        text: 'A captured image recorded the address of this host\'s local image mirror, which means nothing on any other deployment. Images are now recorded in a form that resolves anywhere, with the local mirror applied only when fetching.',
      },
      {
        kind: 'fix',
        text: 'A leftover temporary file could be mistaken for a learner\'s disk, which skipped the copy and left the VM trying to download Windows from Microsoft instead of starting the prepared one.',
      },
      {
        kind: 'improvement',
        text: 'A VM is now checked against its range\'s memory limit when you set it, rather than failing at deploy time. The allowance above the guest\'s own RAM was also raised to 2 GB after measuring what a running guest actually needs — 4 GB guests were being killed for running slightly over an allowance that was too tight.',
      },
      {
        kind: 'fix',
        text: 'The console, cloning and several other features only worked for VMs created from a base image, and quietly did nothing for VMs created from a golden image or a snapshot.',
      },
    ],
  },
]

/** Numeric compare: >0 if a>b, <0 if a<b, 0 if equal. Missing or non-numeric
 *  parts sort as 0, so a malformed version never throws. */
export function compareVersions(a: string, b: string): number {
  const pa = a.split('.').map((n) => parseInt(n, 10) || 0)
  const pb = b.split('.').map((n) => parseInt(n, 10) || 0)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0)
    if (d !== 0) return d > 0 ? 1 : -1
  }
  return 0
}

/**
 * Releases newer than the one this browser last acknowledged.
 *
 * A browser that has never acknowledged anything gets the most recent release
 * only, not the whole history: someone opening PROVING GROUND for the first
 * time does not need a modal listing everything that ever changed.
 */
export function releasesSince(lastSeen: string | null): Release[] {
  if (!lastSeen) return CHANGELOG.slice(0, 1)
  return CHANGELOG.filter((r) => compareVersions(r.version, lastSeen) > 0)
}
