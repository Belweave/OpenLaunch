# OpenLaunch

[![GitHub stars](https://img.shields.io/github/stars/belweave/openlaunch?style=social)](https://github.com/belweave/openlaunch)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Container](https://img.shields.io/badge/container-ghcr.io%2Fbelweave%2Fopenlaunch-2496ED)](https://github.com/belweave/openlaunch/pkgs/container/openlaunch)

![OpenLaunch banner](./banner.png)

OpenLaunch is a self-hosted AI workspace for individuals and teams, combining chat, model management, retrieval, tools, collaboration, and automations with your own providers and infrastructure.

## What it supports

- **Anthropic and compatible APIs** — Claude model discovery, chat, streaming, tools, multimodal inputs, errors, and usage through the native Messages API.
- **OpenAI and OpenAI-compatible APIs** — connect OpenAI or any service exposing the familiar `/v1` model and chat endpoints.
- **Ollama** — run local models alongside hosted providers.
- **Workspace features** — reusable models, prompts, knowledge, tools, skills, web search, code execution, notes, channels, calendars, and automations.
- **Retrieval and administration** — file indexing, multiple vector databases, OAuth, LDAP, trusted headers, SCIM, groups, and granular permissions.

## Quick start

Docker Compose starts OpenLaunch and Ollama and stores their data in named volumes:

```bash
git clone https://github.com/belweave/openlaunch.git
cd openlaunch
docker compose up -d
```

Open [http://localhost:3000](http://localhost:3000), create the first account, then configure providers under **Admin Panel → Settings → Connections**.

To update an image-based installation:

```bash
git pull --ff-only
docker compose pull
docker compose up -d
```

Use `docker compose up -d --build` when you want to build the checked-out source locally.

## Provider configuration

Connections can be managed in the admin UI or initialized with `OLLAMA_BASE_URL`, `OPENAI_API_BASE_URL` and `OPENAI_API_KEY`, or `ANTHROPIC_API_BASE_URL` and `ANTHROPIC_API_KEY`.

For an Anthropic-compatible gateway, set the base URL through the version prefix—for example, `https://gateway.example.com/v1`. OpenLaunch calls its `/models` and `/messages` routes. The admin UI also supports Bearer authentication, custom headers, multiple connections, and per-connection model filters.

See [.env.example](./.env.example) for common settings. Container data lives at `/app/backend/data`; back up the volume before major upgrades.

## Read-only data source tools

OpenLaunch can expose multiple named PostgreSQL, SQL Server, Azure SQL, Snowflake, and Redis connections to models through the same native tool-calling harness used by OpenAI, Anthropic, and compatible endpoints. The feature is provider-neutral and disabled by default.

Configure connections as a JSON list in `DATA_SOURCE_CONNECTIONS`, or preferably place the same JSON in a secret-mounted file and set `DATA_SOURCE_CONNECTIONS_FILE`. For example:

```json
[
	{
		"id": "analytics-warehouse",
		"type": "snowflake",
		"description": "Curated analytics views",
		"config": {
			"account": "organization-account",
			"user": "agent_reader",
			"password": "replace-with-a-secret",
			"warehouse": "AGENT_WH",
			"database": "ANALYTICS",
			"schema": "PUBLISHED",
			"role": "AGENT_READER"
		},
		"access_grants": [
			{ "principal_type": "group", "principal_id": "group-id", "permission": "read" }
		]
	},
	{
		"id": "operational-cache",
		"type": "redis",
		"description": "Read-only operational cache",
		"url": "rediss://agent_reader:replace-with-a-secret@redis.example:6379/0",
		"access_grants": [
			{ "principal_type": "group", "principal_id": "group-id", "permission": "read" }
		]
	}
]
```

Supported connection forms are:

- `postgresql`: a dedicated `url` such as `postgresql://...`.
- `sql_server` or `azure_sql`: an ODBC `connection_string`, or `config` containing `server`, `database`, `user`, and `password`.
- `snowflake`: `config` values accepted by the Snowflake Python connector.
- `redis`: a `redis://`, `rediss://`, or `unix://` `url`.

Set `ENABLE_DATA_SOURCE_TOOLS=true`, then grant **Data Sources** permission only to intended groups. A connection with no `access_grants` is admin-only. Model settings can also disable the built-in for individual models.

SQL calls accept one `SELECT`, `WITH`, `EXPLAIN`, `SHOW`, or `DESCRIBE` statement, with time, row, query-length, and output-size limits. PostgreSQL additionally starts a database-enforced read-only transaction. SQL Server, Azure SQL, and Snowflake calls use rollback plus the SQL allowlist, but operators must still use dedicated least-privilege database roles and expose only approved objects. Redis offers a fixed set of bounded read operations rather than arbitrary commands. Never reuse OpenLaunch's application `DATABASE_URL` or an administrative account.

The original `ENABLE_SQL_DATABASE_TOOL` and `SQL_DATABASE_URL` variables remain as a migration path for a single PostgreSQL connection. See [.env.example](./.env.example) for all limits.

## Run from source

Source builds require Python 3.11 or 3.12 and Node.js 18–22:

```bash
npm ci
npm run build
python -m venv .venv
source .venv/bin/activate
pip install .
openlaunch serve
```

Use `npm run dev` for frontend development and `backend/dev.sh` for backend development. Before contributing, run the relevant checks from [package.json](./package.json) and [pyproject.toml](./pyproject.toml).

> [!CAUTION]
> Tools and functions can execute code on the OpenLaunch host. Grant creation permissions only to fully trusted users and use a stable `OPENLAUNCH_SECRET_KEY`, TLS, restricted CORS, and regular data backups in production.

## Project links

- [Releases](https://github.com/belweave/openlaunch/releases)
- [Issues](https://github.com/belweave/openlaunch/issues)
- [Discussions](https://github.com/belweave/openlaunch/discussions)
- [Security policy](./docs/SECURITY.md)
- [MIT License](./LICENSE)
