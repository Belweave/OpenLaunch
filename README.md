# OpenLaunch

OpenLaunch is an open-source, self-hosted AI workspace from Belweave. It provides a private interface for chatting with language models, working with documents, and extending AI workflows with custom tools.

## Features

- Connect to Ollama, the Anthropic API, and OpenAI-compatible APIs
- Chat with multiple models from one responsive interface
- Use documents and web content with retrieval-augmented generation (RAG)
- Add custom Python functions and tools
- Manage users, groups, roles, and permissions
- Run locally with Docker or deploy with Kubernetes

## Quick start

You need Docker with Docker Compose.

```bash
docker compose up -d
```

Open the app on local port `3000` after the services start. The included Compose configuration runs OpenLaunch with Ollama and stores their data in Docker volumes.

To stop the services:

```bash
docker compose down
```

Additional deployment notes are available in `INSTALLATION.md`.

## Development

The frontend requires Node.js 18–22 and npm.

```bash
npm install
npm run dev
```

Useful checks:

```bash
npm run check
npm run test:frontend
```

Contributions are welcome. Open an issue to discuss an idea or submit a pull request with an improvement.

## License

OpenLaunch is available under the MIT License in `LICENSE`. Anyone may use, copy, modify, and distribute it under the terms of that license.

## Star History

<a href="https://star-history.com/#belweave/openlaunch&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=belweave/openlaunch&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=belweave/openlaunch&type=Date" />
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=belweave/openlaunch&type=Date" />
  </picture>
</a>

---

Created by Preetham Kyanam.
