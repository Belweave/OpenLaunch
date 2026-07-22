# OpenLaunch

[![GitHub stars](https://img.shields.io/github/stars/belweave/openlaunch?style=social)](https://github.com/belweave/openlaunch)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Container](https://img.shields.io/badge/container-ghcr.io%2Fbelweave%2Fopenlaunch-2496ED)](https://github.com/belweave/openlaunch/pkgs/container/openlaunch)

![OpenLaunch banner](./banner.png)

OpenLaunch is a self-hosted AI workspace for OpenAI-compatible APIs, Ollama, retrieval-augmented generation, tools, models, notes, channels, and team collaboration. It is designed to run locally or on your own infrastructure, with a responsive web interface and a Python backend.

## Highlights

- Connect Ollama and OpenAI-compatible model providers.
- Build model presets with prompts, knowledge, tools, skills, and access controls.
- Index local or cloud-backed files for retrieval with multiple vector database options.
- Collaborate through chats, channels, notes, calendars, and scheduled automations.
- Configure LDAP, OAuth, trusted-header authentication, SCIM, groups, and granular permissions.
- Run as a PWA or deploy with Python, Docker, or Docker Compose.

## Quick start with Docker Compose

```bash
git clone https://github.com/belweave/openlaunch.git
cd openlaunch
docker compose up -d
```

Open [http://localhost:3000](http://localhost:3000). The Compose stack stores application data in the `openlaunch` named volume.

To update:

```bash
git pull --ff-only
docker compose pull
docker compose up -d
```

## Run the container directly

```bash
docker run -d \
  -p 3000:8080 \
  --add-host=host.docker.internal:host-gateway \
  -v openlaunch:/app/backend/data \
  --name openlaunch \
  --restart unless-stopped \
  ghcr.io/belweave/openlaunch:main
```

Set `OLLAMA_BASE_URL` when Ollama is not reachable at the default host. Anthropic can be configured in the admin Connections screen or with `ANTHROPIC_API_KEY` and the optional `ANTHROPIC_API_BASE_URL`. See [.env.example](./.env.example) for canonical configuration names.

## Run from source

OpenLaunch supports Python 3.11 and 3.12 and Node.js 18 through 22.

```bash
npm ci
npm run build
python -m venv .venv
source .venv/bin/activate
pip install .
openlaunch serve
```

For frontend development, run `npm run dev`. For backend development, install the project and start `backend/dev.sh`.

## Configuration and compatibility

Canonical product-specific environment variables use the `OPENLAUNCH_` prefix. At startup, OpenLaunch also recognizes the former short-prefix environment variables for upgrade compatibility, but new deployments should use only the canonical names.

The application data directory remains `/app/backend/data` in containers. When moving an existing installation to the new `openlaunch` Docker volume, copy the contents of the previous data volume before starting the new container.

## Releases and updates

Release discovery and in-app update notices use [belweave/openlaunch releases](https://github.com/belweave/openlaunch/releases). The backend checks the latest release through the GitHub releases API; it does not contact a separate project update service.

## Contributing

Issues, feature proposals, and pull requests are welcome:

- [Issue tracker](https://github.com/belweave/openlaunch/issues)
- [Discussions](https://github.com/belweave/openlaunch/discussions)
- [Security policy](./docs/SECURITY.md)

Before submitting a change, run the relevant frontend and backend checks described in [package.json](./package.json) and [pyproject.toml](./pyproject.toml).

## License

OpenLaunch is licensed under the [MIT License](./LICENSE).
