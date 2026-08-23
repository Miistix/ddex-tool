# DDEX Delivery & Ingestion Tool

A command-line tool for sending and receiving [DDEX ERN](https://ddex.net/) music release packages over SFTP — the standard format labels and distributors use to deliver metadata and audio between systems.

## What it does

This tool has two modes:

- **Deliver** — uploads a local DDEX delivery folder to a remote SFTP server, with a live progress bar.
- **Ingest** — connects to an SFTP server, downloads new deliveries, and processes them:
  - Verifies each delivery has the expected structure (`BatchComplete_*.xml`, a UPC folder, metadata XML, and a `resources/` folder)
  - Validates the metadata XML against the DDEX ERN 4.1.1 schema (if provided)
  - Parses and logs key metadata (Message ID, release title, ISRC) to a CSV
  - Moves successfully processed deliveries into a `processed/` folder, leaving anything that fails validation untouched for review

Deliveries are only moved/skipped once every release in a batch has been checked — a partial failure in a multi-release batch won't silently drop the releases that did succeed, and won't silently overwrite anything already on disk.

## Why

DDEX ERN is the industry-standard schema for music release notifications, but a lot of the tooling around it is either expensive enterprise software or ad hoc internal scripts. This started as a small personal tool for handling deliveries without either of those, with an emphasis on **not losing data silently** — every failure mode (missing files, failed validation, name collisions) is designed to leave a clear trail rather than fail quietly.

## Features

- Two-way SFTP support: deliver *and* ingest
- Live progress bars for uploads and downloads (`tqdm`)
- Structural + schema validation before anything is filed away
- Handles multi-release batches (multiple UPCs per delivery) independently
- Human-readable logs (`logs/ddex_log.txt`) and structured CSV output (`logs/releases.csv`)
- No destructive overwrites — collisions and failures are logged, not silently deleted

## Tech stack

- Python 3
- [`paramiko`](https://www.paramiko.org/) — SFTP
- [`lxml`](https://lxml.de/) — XML parsing and XSD validation
- [`python-dotenv`](https://pypi.org/project/python-dotenv/) — config
- [`tqdm`](https://tqdm.github.io/) — progress bars

## Setup

```bash
git clone https://github.com/<your-username>/<repo-name>.git
cd <repo-name>
pip install -r requirements.txt
cp .env.example .env
# then edit .env with your SFTP details
```

`.env` is only used for **Ingest** mode (the server you're downloading from). **Deliver** mode prompts for its own connection details each time you run it, since you're often delivering to a different server than you ingest from.

> ⚠️ **Don't skip the `cp .env.example .env` step.** The script only reads a file named exactly `.env` — leaving it as `.env.example`, or skipping this step entirely, means Ingest mode will fail with a confusing low-level connection error instead of a clear one, since it'll try to connect using empty/missing credentials.

## Usage

```bash
python3 ddex_ingest.py
```

You'll be prompted to choose:

```
[1] Deliver (upload a delivery to a remote SFTP server)
[2] Ingest (download and process incoming deliveries)
```

## Project structure

```
ddex_ingest.py       # main script
.env.example         # config template (copy to .env)
requirements.txt
profile/             # incoming deliveries land here before processing
processed/           # successfully validated deliveries end up here
logs/
  ddex_log.txt        # full run history
  releases.csv         # one row per successfully parsed release
schema/
  release-notification.xsd   # optional: DDEX ERN 4.1.1 schema for strict validation
                              # (not included — see note below)
```

> **Note on the DDEX schema:** `release-notification.xsd` (and its dependency, `avs411.xsd`) are not included in this repo. DDEX retains copyright over the ERN standard and its schema files, and requires a free Implementation Licence to use them — see [ddex.net/apply-ddex-implementation-licence](http://ddex.net/apply-ddex-implementation-licence). If you want strict schema validation, obtain the files yourself and place them in `schema/`. Without them, the script simply skips strict validation and logs a note saying so.

## Status

Personal/portfolio project — actively used, not published as a package. Contributions/suggestions welcome via issues.
