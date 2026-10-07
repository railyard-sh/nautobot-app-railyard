# nautobot-app-railyard

A [Nautobot](https://networktocode.com/nautobot/) app that connects Nautobot to **[Railyard](https://railyard.sh)**,
the data-centre design tool, in both directions:

- **Export from Railyard** — turn a Railyard design (locations, racks, devices, ports, structured cabling and
  power) into Nautobot objects, and keep them in step as the design changes.
- **Import into Railyard** — bring Nautobot locations, with their racks, devices and cables, into Railyard as a
  design baseline, and refresh it later without losing what was designed in Railyard.

Both are Nautobot Jobs, built on [railyard-sync](https://github.com/railyard-sh/railyard-sync), the shared core of
Railyard's DCIM integrations (the same code as its `railyard-sync export nautobot` and `import nautobot` commands
and the NetBox plugin's).

> [!WARNING]
> **Pre-1.0 software that writes to your Nautobot. Back up your Nautobot database before every export.** Run a
> **dry run** first and read its job log before applying anything, and try it on a copy of your Nautobot before
> pointing it at production. The app is provided under the Apache-2.0 licence **without warranty** (see
> [LICENSE](LICENSE)).

> Status: **early development (0.1).** Both jobs are covered by a test suite that runs against Nautobot 2.3 and
> 2.4 (see [Development](#development)).

## How it works

```
Export:  Railyard ── nautobot-sync document ──► railyard-sync (DiffSync) ──► Nautobot (API views, in-process)
Import:  Nautobot (API views, in-process) ──► railyard-sync snapshot ──► build / merge ──► Railyard project
```

- The export's source is Railyard's **Nautobot sync document**: the objects of Railyard's own Nautobot 2.x CSV
  bundle, as JSON, each with the Railyard object it came from. So the app creates exactly what an import of
  Railyard's bundle would, with Railyard's naming, statuses and cabling plan, and never ports them.
- Both jobs read and write Nautobot through **Nautobot's own REST API views, called in-process** as the user who
  ran the job: no HTTP and no API token, Nautobot's validation and filters exactly as its API applies them, the
  user's object permissions enforced per object, and every change in Nautobot's change log under that user.
- A **dry run** (the default for both jobs) writes nothing: the export runs in a database transaction that is
  always rolled back; the import does not save to Railyard.
- A real export is **all or nothing**: one database transaction, rolled back if Nautobot refused any write.

### What the export will and won't touch

Every object the export creates carries its project's **ownership tag**, named `RY:<project name> (<id>)`. The tag
is specific to one Railyard project on one Railyard instance (keyed by the project's id, not its name), and the
export recognises it by its exact description. It is enabled for every model the export tags.

- **It only ever updates or deletes objects it owns.** Nautobot's location types, statuses, manufacturers and
  roles cannot carry tags, so for those ownership is a `railyard_owner` custom field holding the tag's slug
  (created on the first real run; if the user may not create custom fields, those objects are created without an
  owner and are never changed afterwards).
- **It never takes over objects you created.** If Nautobot already has a location type, status, manufacturer,
  device type, role, location or rack of the same name, the export *uses* it (it places its devices in your rack)
  but never changes or deletes it. A status, role or location type used this way must already be enabled for what
  uses it (a role for devices, a location type for racks): otherwise the objects that need it are reported as
  conflicts and skipped, rather than refused by Nautobot.
- **Conflicts are skipped and reported.** A device of the same name already in the location, or a port that is
  already cabled, is left alone; the export skips it and whatever depends on it, and lists them in the job log.
- **One exception: components of a device the export created.** When the export creates a device whose type has
  component templates, Nautobot creates those components too; the export adopts (tags) the ones its design uses.
- **Deletes are opt-in and never reach beyond what it owns.** With **Allow deletes**, owned objects gone from
  Railyard are deleted, except where that would delete, modify or disconnect something the export doesn't own
  (your device in its rack, your IP address on its interface, your cable on its port); those are kept and listed.
- **Renames follow.** Devices carry their Railyard id in the `railyard_id` custom field, so a device renamed in
  Railyard is renamed in Nautobot, keeping its components, cables and history.
- If you remove the ownership tag (or owner field) from an object, the export stops managing it.

### What the import reads and keeps

Nautobot 2.x has no sites: every place is a location, typed by a location type (Region → Site → Building → Room,
as your Nautobot defines them). The import is built around the locations you choose: each becomes a data centre
in Railyard, the locations below it its spaces (keeping their location types), and those above it the groups it
sits in. **All locations** takes every location of the first type down the tree that may hold racks or devices
(your sites, typically).

Racks, racked and 0U devices with their ports and power inputs, device types (matched to Railyard's catalogue,
otherwise built from Nautobot's templates), data cables and power cables to PDU outlets come across; anything
Railyard does not model (unracked or child devices, modules, console ports, circuits, power panels and feeds) is
listed in the report, and primary power feeds set each rack's power capacity. Every imported object keeps an id
derived from its Nautobot UUID (`nbt-…`), so a refresh updates the estate; what was designed in Railyard is kept,
objects deleted in Nautobot are reported (and removed only with **Allow deletes**), and conflicts with the Railyard
design stop the save. Railyard checks the document before it is saved.

## Install

The app needs **Nautobot 2.3 or 2.4** on **Python 3.12** (railyard-sync's minimum).

```bash
pip install "nautobot-app-railyard @ git+https://github.com/railyard-sh/nautobot-app-railyard"   # (until published)
```

This also installs railyard-sync from its release tag. Then enable the app in `nautobot_config.py`:

```python
PLUGINS = ["nautobot_railyard"]

PLUGINS_CONFIG = {
    "nautobot_railyard": {
        "railyard_url": "https://railyard.sh",  # your Railyard (use https)
        # The Railyard token, from the first of these that is set:
        "railyard_token_secret": None,  # the name of a Nautobot Secret (e.g. from an env var or a file)
        "railyard_token_file": None,  # a file holding the token (e.g. a mounted secret)
        "railyard_token_env": "RAILYARD_TOKEN",  # an environment variable of the worker (the default)
        # "railyard_token": "ry_...",           # the value itself: not recommended
        # Which Railyard organisations and projects the jobs may use. Users pick from these; nothing else is
        # reachable. With none configured, only the token user's personal organisation is used.
        "railyard_org": "my-org",  # the default org (id, slug or name)
        "railyard_allowed_orgs": [],  # further orgs users may choose
        "railyard_allowed_projects": [],  # optional: only these project ids/slugs (then imports only refresh)
        # The URL this Nautobot is reached at. An import records it, so railyard-sync's CLI and this app recognise
        # the same source when refreshing an estate (default: from ALLOWED_HOSTS).
        "nautobot_url": "https://nautobot.example.com",
    },
}
```

Run `nautobot-server post_upgrade`, restart Nautobot and its workers, and **enable the two jobs** (Jobs → Jobs →
Railyard; Nautobot installs new jobs disabled).

### The Railyard token: least privilege, expiry and rotation

Every job uses the one token configured here, whichever Nautobot user runs it, and it can read **everything its
Railyard user can see** (Railyard tokens aren't scoped yet):

1. **Use a dedicated Railyard user**, not a person's account, a member of only the organisations you configure.
2. Give it the **viewer** role if you only export from Railyard; the import saves estates, so it needs
   **editor** in the organisations it imports into.
3. Create its token under **User settings → Personal access tokens**, with the shortest expiry you are prepared
   to rotate (90 days by default).
4. Store it as a Nautobot Secret, in a file readable only by Nautobot, or in the workers' environment — never in
   `nautobot_config.py` or version control. The jobs read it when they start, so a rotated file or secret needs
   no restart.
5. **Rotate it before it expires**, then revoke the old one in Railyard.

If the token is wrong, revoked or expired, Railyard answers 401 and the job fails saying so. A 403 means the token
works but its user may not read (or, for an import, change) that organisation or project.

### Nautobot permissions

The jobs use Nautobot's own permissions: they **re-check them when they run**, whatever queued them, and every read
and write goes through Nautobot's API as the user who ran the job, so object-level constraints apply per object.
Running a job also needs `extras.run_job` on it.

| To… | the user needs |
|---|---|
| run an **export** as a **dry run** | `view` on location type, location, manufacturer, device type, rack, device, interface, rear port, front port, power outlet, power port, cable, status and role, plus `extras.view_tag` and `extras.view_customfield` |
| **apply** an export | also `add` and `change` on those models |
| apply it with **Allow deletes** | also `delete` on those models |
| the **first** export of a project | also `extras.add_tag` / `change_tag` (the ownership tag) and `extras.add_customfield` / `change_customfield` (`railyard_id`, `railyard_owner`) — or an administrator runs the first export |
| run an **import** (dry run or not) | `view` on location, location type, rack, device, device type, the device components and their templates, cable, power panel and power feed |

## The two jobs

**Export from Railyard** — *Railyard organisation* (from the configured ones), *Railyard project* (id or URL slug),
*Merge request* (optional: export a merge request's draft), *Allow deletes*, *Dry run* (ticked by default). The
job log lists the plan per object type and per object, what was used as it is, conflicts, adopted components,
renames, refused and stale deletes, and warnings.

**Import into Railyard** — *Locations* (or *All locations*), *Railyard organisation*, *Refresh project* (the estate
an earlier import created) **or** *Or create project* (a name for a new one), *Allow deletes*, *Dry run* (ticked by
default). The job log shows what was read, the build report (what was not modelled and why), the merge's diff for
a refresh, Railyard's check, and the saved revision.

## A paid deliverable on hosted Railyard

The export's source, the Nautobot sync document, is a **deliverable**: on hosted Railyard (railyard.sh) the
project's plan must include deliverables, and its rack count must be within the plan's deliverable limit. A
refusal fails the job with the plans that would allow it and whether a Project Pass for the estate would; nothing
is written to Nautobot. A self-hosted Railyard with billing off allows it. Imports are limited by the plan's rack
cap per estate, which Railyard enforces when the estate is saved.

## Development

```bash
# The app's tests, against a real Nautobot (Postgres and Redis on localhost, or see development/):
pip install "nautobot==2.4.*" -e .
nautobot-server --config=development/nautobot_config.py test nautobot_railyard
ruff check . && ruff format --check .
```

`development/` has a Docker Compose environment (Nautobot, worker, Postgres, Redis) with the app installed; see
[development/README.md](development/README.md). CI runs the suite against Nautobot 2.3 and 2.4.
See [CLAUDE.md](CLAUDE.md) for the design.

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
