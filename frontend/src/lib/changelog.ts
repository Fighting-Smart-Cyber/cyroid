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
  /**
   * Omit for a normal release. `false` keeps the release out of the in-app
   * modal.
   *
   * Some releases are real and worth recording but contain nothing a learner
   * or an operator would notice — a repository move, a documentation fix. They
   * still need an entry, because CHANGELOG.md is the record and
   * test_changelog_agrees.py requires every shipped VERSION to have one. What
   * they do not need is a modal interrupting someone to announce repository
   * plumbing: a "What's new" that keeps crying wolf is one people learn to
   * dismiss without reading, which costs us the releases that do matter.
   */
  userFacing?: boolean
  highlights: ChangeEntry[]
}

export const CHANGELOG: Release[] = [
  {
    version: '0.55.0',
    date: '2026-09-27',
    title: 'Certificates that browsers trust',
    highlights: [
      {
        kind: 'feature',
        text: 'An install can now get a real certificate covering the platform AND every range application, so nobody is asked to accept a warning \u2014 including on a host that is not reachable from the internet.',
      },
      {
        kind: 'improvement',
        text: 'An install now says where its images come from instead of assuming. Existing installs must be re-installed once before they will take a newer version; they keep running until you do.',
      },
    ],
  },
  {
    version: '0.54.3',
    date: '2026-09-27',
    // Nothing a learner or an operator sees in the product: the defect is in the install
    // script, which only somebody standing up a cluster ever runs. Recorded because
    // CHANGELOG.md is the record, not announced because a modal that cries wolf stops
    // being read.
    userFacing: false,
    title: 'Install script fix',
    highlights: [
      {
        kind: 'fix',
        text: 'Installing with a separate certificate for range applications produced invalid configuration and stopped. Nothing in the product changes.',
      },
    ],
  },
  {
    version: '0.54.2',
    date: '2026-09-27',
    title: 'Ranges work on Azure',
    highlights: [
      {
        kind: 'fix',
        text: 'On Azure, no range built from a blueprint could start at all \u2014 its disk never finished being prepared. Ranges now come up in about a minute.',
      },
      {
        kind: 'fix',
        text: 'Deleting a range reported a server error and left it in the list, even though it had in fact been torn down. Deleting it once is now enough.',
      },
      {
        kind: 'fix',
        text: 'On an install with a certificate of its own, range applications would not open \u2014 the browser refused the certificate they were served. They open again.',
      },
    ],
  },
  {
    version: '0.54.1',
    date: '2026-09-26',
    title: 'A tidier feedback box',
    highlights: [
      {
        kind: 'fix',
        text: 'The box you send feedback in had no breathing room \u2014 the buttons and fields ran right up to its edges. It is spaced properly now.',
      },
    ],
  },
  {
    version: '0.54.0',
    date: '2026-09-26',
    title: 'We can read what you tell us',
    highlights: [
      {
        kind: 'feature',
        text: 'Everything sent through \u201cTell us something\u201d now has a home: the people who work on the platform can read it, group it by what kind of thing it is, and say where it has got to. You can still only see your own.',
      },
      {
        kind: 'improvement',
        text: 'The form you send it with is easier to use \u2014 the boxes look like boxes, it no longer assumes you are reporting a fault before you have said so, it tells you what is missing instead of just greying out Send, and it names the range you are looking at rather than showing its id.',
      },
    ],
  },
  {
    version: '0.53.3',
    date: '2026-09-26',
    title: 'Release plumbing',
    // Nothing a learner or an operator would notice: the release pipeline stopped
    // depending on an upstream registry for an image we had already mirrored. Recorded
    // because every shipped VERSION needs an entry; not announced, because a modal that
    // interrupts someone to report CI plumbing is one they learn to dismiss unread.
    userFacing: false,
    highlights: [
      {
        kind: 'fix',
        text: 'The release pipeline no longer re-fetches an image the platform already holds, so a release cannot be broken by an upstream registry withdrawing it.',
      },
    ],
  },
  {
    version: '0.53.2',
    date: '2026-09-25',
    title: 'Range applications open again',
    highlights: [
      {
        kind: 'fix',
        text: 'Opening an application from a range stopped at \u201cThis application needs a session\u201d, however many times you tried. It opens normally now. Nothing you did caused it and nothing you were working on was lost.',
      },
    ],
  },
  {
    version: '0.53.1',
    date: '2026-09-25',
    title: 'Range applications reach their pods again',
    highlights: [
      {
        kind: 'fix',
        text: 'On a fresh 0.53.0 install the component that serves a range\u2019s applications could not start, so opening an application from a range gave nothing. The platform itself was unaffected. It starts and serves normally now.',
      },
      {
        kind: 'fix',
        text: 'The update panel no longer says \u201cUp to date\u201d when there is genuinely nothing to pull for some other reason \u2014 for instance an install that is not on a release. It now says so and gives the reason, instead of a greyed-out button with no explanation.',
      },
    ],
  },
  {
    version: '0.53.0',
    date: '2026-09-25',
    title: 'Range applications stand on their own, and you can tell us things',
    highlights: [
      {
        kind: 'feature',
        text: 'You can now tell us about a problem, an idea, or something wrong with a guide or a range, from wherever you already are. What you were doing is filled in for you and shown before you send it, and only you and the people who work on it can see what you write.',
      },
      {
        kind: 'improvement',
        text: 'A range\'s applications no longer depend on one particular ingress controller, so this platform can be installed somewhere that does not run Traefik.',
      },
      {
        kind: 'fix',
        text: 'A range whose machines are all stopped no longer says it is running. Students were being let into labs with nothing in them.',
      },
      {
        kind: 'fix',
        text: 'The artifact library was readable, and downloadable, by anyone signed in. It is now limited to the people who own or maintain it.',
      },
      {
        kind: 'fix',
        text: 'An application is described as published rather than reachable, and when one cannot be opened the reason shown is the real one.',
      },
    ],
  },
  {
    version: '0.52.1',
    date: '2026-09-22',
    title: 'The 0.52.0 release, this time with something to install',
    highlights: [
      {
        kind: 'fix',
        text: 'Version 0.52.0 was tagged but never built, so there was nothing for a host to update to and 0.51.0 remained the newest version on offer. Everything described under 0.52.0 arrives here.',
      },
    ],
  },
  {
    version: '0.52.0',
    date: '2026-09-22',
    title: 'A Kubernetes install stops offering the Docker product',
    highlights: [
      {
        kind: 'fix',
        text: 'A range\'s application now opens on an address of its own rather than on this one. It shared an address with the console before, which meant the software you are training on could read your signed-in session out of your browser.',
      },
      {
        kind: 'fix',
        text: 'Ending a class, purging ranges from Admin, or deleting an instance used to remove the record while leaving the machines, their disks and their networks running on the cluster with nothing pointing at them. All three take the environment down first now, and tell you by name if anything is left behind.',
      },
      {
        kind: 'fix',
        text: 'Learners assigned to the same training event each get their own environment. Before this, a class whose blueprint did not explicitly ask for per-learner slices shared one — so each learner\'s machines replaced the last learner\'s.',
      },
      {
        kind: 'fix',
        text: 'Update Blueprint no longer replaces a Kubernetes blueprint with an empty one and reports success. Blueprints on this substrate can now be opened, edited and exported at all.',
      },
      {
        kind: 'improvement',
        text: 'Pages and buttons that can only work on the Docker substrate are no longer offered here — the image cache, the VM library, the range wizard, Add Network, Add VM. What is left says what it is in the words this substrate uses: machines, namespaces, capabilities. Where something is genuinely unavailable, it now says so and why instead of failing with no message.',
      },
      {
        kind: 'improvement',
        text: 'The Diagnostics tab shows the range as it exists on the cluster — its machines, their addresses and the software installed in them — instead of tools for a container that is not there. Machines can be stopped and started one at a time.',
      },
      {
        kind: 'improvement',
        text: 'Training-event actions report what went wrong instead of failing silently, the learner view is reachable from the navigation, and one broken panel no longer blanks the whole application.',
      },
    ],
  },
  {
    version: '0.51.0',
    date: '2026-09-21',
    title: 'Open a range\'s application in your browser',
    highlights: [
      {
        kind: 'feature',
        text: 'On a Kubernetes install, a range\'s web application — the software you are training on — now has its own address, listed under Applications on the range page. Click it and it opens in a new window, for you and no one else: each range gets its own address, and only the people entitled to that range can reach it.',
      },
    ],
  },
  {
    version: '0.50.0',
    date: '2026-09-21',
    title: 'Open a console to a virtual machine on the Kubernetes path',
    highlights: [
      {
        kind: 'feature',
        text: 'On a Kubernetes install, a range page now lists its virtual machines under Workloads — what each is running, how big it is, and its addresses — and each running machine has a Console button that opens its screen in a new window, with keyboard and mouse. Copy in the machine to paste in your browser; the clipboard button sends your browser\'s clipboard to the machine.',
      },
    ],
  },
  {
    version: '0.49.4',
    date: '2026-09-16',
    title: 'A release to update to',
    userFacing: false,
    highlights: [
      {
        kind: 'fix',
        text: 'No product change. Cut so an install at 0.49.3 has a newer release to move to; the in-UI update on the Kubernetes path was proven with it.',
      },
    ],
  },
  {
    version: '0.49.3',
    date: '2026-09-16',
    title: 'Update platform on a Kubernetes install completes',
    userFacing: false,
    highlights: [
      {
        kind: 'fix',
        text: 'Pressing Update platform on a Kubernetes install failed with an internal error before anything happened. It now advances the install; the page reconnects once the new version is up.',
      },
    ],
  },
  {
    version: '0.49.2',
    date: '2026-09-16',
    title: 'A release to update to',
    userFacing: false,
    highlights: [
      {
        kind: 'fix',
        text: 'No product change. Cut so that an install at 0.49.1 has a newer release to move to, which is how the in-UI update was proven on the Kubernetes path.',
      },
    ],
  },
  {
    version: '0.49.1',
    date: '2026-09-16',
    title: 'The published chart installs under Flux',
    userFacing: false,
    highlights: [
      {
        kind: 'fix',
        text: '0.49.0 was the first release to publish the chart, and it could not be installed by the cluster: a label appeared twice in the rendered manifests, which the controller rejects. No product change; 0.49.1 is the same release with a chart that installs.',
      },
    ],
  },
  {
    version: '0.49.0',
    date: '2026-09-16',
    title: 'Check for updates and Update work on a Kubernetes install',
    highlights: [
      {
        kind: 'fix',
        text: 'On a Kubernetes install, Admin → Infrastructure said "Could not check for updates" and the Update button could not work. Both now do: checking reads the releases published for the platform, and updating moves the install to the newest one. The page reconnects on its own once the new version is up.',
      },
    ],
  },
  {
    version: '0.48.0',
    date: '2026-09-16',
    title: 'Team exercises get their own cluster inside the range',
    userFacing: false,
    highlights: [
      {
        kind: 'feature',
        text: 'For operators on the Kubernetes path: a team exercise now installs its training software into a private cluster of its own inside the range, and tears all of it down cleanly. Nothing changes for Docker-based hosts.',
      },
    ],
  },
  {
    version: '0.47.0',
    date: '2026-09-15',
    title: 'PROVING GROUND can now run inside a Kubernetes cluster',
    userFacing: false,
    highlights: [
      {
        kind: 'feature',
        text: 'For operators: PROVING GROUND installs into a Kubernetes cluster with one command (scripts/install-k8s.sh) and its ranges become real virtual machines on that cluster, each on its own networks. Nothing changes for an existing Docker-based host.',
      },
      {
        kind: 'feature',
        text: 'On the Kubernetes path, Stop and Start on a range now stop and start its virtual machines, keeping their disks; Delete removes everything the range created and refuses to finish if anything was left behind.',
      },
    ],
  },
  {
    version: '0.46.1',
    date: '2026-09-15',
    title: 'Re-cut of 0.46.0, this time with images',
    userFacing: false,
    highlights: [
      {
        kind: 'fix',
        text: 'No product change. 0.46.0 was tagged from a commit whose tests were failing, and the image builds run only after the tests pass \u2014 so the tag shipped without any container images behind it and could not actually be installed. This release carries the whole of 0.46.0 from a green build.',
      },
    ],
  },
  {
    version: '0.46.0',
    date: '2026-09-15',
    title: 'Install a blueprint and everything it needs in one click, and send your fixes back',
    highlights: [
      {
        kind: 'feature',
        text: 'Installing a blueprint from the catalog now brings its base images with it, in the right order, with progress you can watch. Previously a blueprint could install "successfully" and then fail to deploy because an image it needed was never fetched.',
      },
      {
        kind: 'feature',
        text: 'Fixed a blueprint you installed from the catalog? Contribute it back. The Contribute button on a blueprint shows exactly what you changed against the catalog\'s copy, lets you pick which changes to send, and produces a patch the catalog maintainers can review as-is.',
      },
      {
        kind: 'feature',
        text: 'Content can be exported as a bundle — real markdown, walkthrough and assets in a folder — that comes out identical every time the content is unchanged, so it can be checked in, diffed, and carried across an air gap. Importing the same bundle twice updates the content rather than duplicating it.',
      },
      {
        kind: 'fix',
        text: 'One-click install could sit at "Queued for install" forever, and importing a blueprint ZIP could silently do nothing. Both were being dropped before they ran. Both now run.',
      },
      {
        kind: 'fix',
        text: 'Installing any base image from the catalog failed with a database error about BASE_IMAGE. It installs now.',
      },
    ],
  },
  {
    version: '0.45.0',
    date: '2026-09-09',
    title: 'Windows ranges boot in under two minutes where the hardware allows it',
    highlights: [
      {
        kind: 'feature',
        text: 'Windows ranges can now use hardware acceleration where the machine supports it. On the same range, booting to a usable desktop went from about thirteen minutes to under two, and building a Windows image from an installer went from roughly four hours to forty minutes. Ask your administrator to turn it on for a machine — see RANGE_KVM.',
      },
      {
        kind: 'fix',
        text: 'Redeploying a range after tearing it down could fail every time with a network error, until the machine was restarted. Redeploys now work as they should.',
      },
      {
        kind: 'fix',
        text: 'A virtual machine installing Windows from an installer disc could quietly run on a quarter of the memory you gave it, turning a forty-minute install into hours. Nothing reported a problem. Installs now get the memory they were configured with.',
      },
      {
        kind: 'fix',
        text: 'The disk-space check shown before deploying a range was reading the wrong filesystem, so the figure it reported could be unrelated to the space actually available.',
      },
    ],
  },
  {
    version: '0.44.0',
    date: '2026-09-03',
    title: 'Your ranges are visible again, and updates follow releases',
    highlights: [
      {
        kind: 'fix',
        text: 'Ranges could disappear from the list entirely — including ones you created yourself. It looked like a permissions problem and was not: a single unreadable record failed the whole list, so you saw none of your ranges rather than one missing one. Nothing was ever lost, and everything is back.',
      },
      {
        kind: 'feature',
        text: 'Updates now follow releases. Check for updates tells you when a new version has been released, rather than every time anything changes, and the version you see is the version you are running. Administrators can put a machine back on the old behaviour of following every change — see UPDATE_CHANNEL.',
      },
    ],
  },
  {
    version: '0.43.0',
    date: '2026-09-02',
    title: 'Ranges are private by default, and only the right people reach a console',
    highlights: [
      {
        kind: 'feature',
        text: 'You choose who can see each range: private, shared with named people, or visible to everyone signed in. Use the Sharing button on a range. Sharing shows someone a range — it does not let them change it or open its consoles.',
      },
      {
        kind: 'fix',
        text: 'Every range you had already made becomes PRIVATE with this update. Previously a range with no tags was treated as public, so anything you built was visible to every colleague whether you intended it or not. Nothing is lost — re-share anything others should see.',
      },
      {
        kind: 'fix',
        text: 'Consoles now check who is asking. Opening a VM console previously needed only its address: anyone who had one could take control of that machine, with no sign-in at all, because the platform supplied the console password on their behalf. Console access is now limited to the range\'s owner, administrators, and the learner or event participants it belongs to.',
      },
      {
        kind: 'fix',
        text: 'Deleting, deploying, starting, stopping or tearing down a range — and the same for its networks and VMs — now requires being its owner or an administrator. Any signed-in account could previously do all of it to anyone\'s range by knowing its address.',
      },
    ],
  },
  {
    version: '0.42.3',
    date: '2026-09-01',
    title: 'Corrections to the open-source engine\'s documentation',
    userFacing: false,
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
    userFacing: false,
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
/**
 * Both take the release list as an argument, defaulting to CHANGELOG.
 *
 * That is for the tests, and it earns its keep: written against the live
 * CHANGELOG they passed whether or not the filtering worked at all, because
 * removing every `userFacing: false` left the assertions iterating empty sets.
 * A test that cannot fail is worse than no test — it reads as coverage.
 */
export function userFacingReleases(releases: Release[] = CHANGELOG): Release[] {
  return releases.filter((r) => r.userFacing !== false)
}

export function releasesSince(
  lastSeen: string | null,
  releases: Release[] = CHANGELOG
): Release[] {
  const shown = userFacingReleases(releases)
  if (!lastSeen) return shown.slice(0, 1)
  return shown.filter((r) => compareVersions(r.version, lastSeen) > 0)
}
