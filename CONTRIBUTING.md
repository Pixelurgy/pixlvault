# PixlVault

A REST API server for PixlVault

## Development

- Install dependencies: `pip install -e .`
- Run server: `python -m pixlvault.server`

## Tagger Benchmark

- Benchmark tagging throughput on a folder of media:
	- `python scripts/benchmark_tagger.py /path/to/images --limit 256 --runs 3`
- Tune batch/concurrency between runs via env vars:
	- `PIXLVAULT_TAGGER_MAX_CONCURRENT_GPU=96`
	- `PIXLVAULT_TAGGER_MAX_CONCURRENT_CPU=8`
	- `PIXLVAULT_CUSTOM_TAGGER_BATCH=24`

## Image Plugins

- Built-in plugins live in `image-plugins/built-in/`.
- Current built-ins: `colour_filter`, `scaling`, `brightness_contrast`, `blur_sharpen`.
- User plugins live in `image-plugins/user/`.
- Start from the template: `image-plugins/user/plugin_template.py`.
- Copy the template to a new `.py` file in `image-plugins/user/`, then rename class/id and implement `run()`.
- `plugin_template.py` is intentionally ignored by plugin discovery.

## Database Migrations (Alembic)

PixlVault uses Alembic for schema changes. The server runs migrations on startup.

- Set the database URL with `PIXLVAULT_DB_URL` (defaults to `sqlite:///vault.db`).
- Create a new migration after model changes:
	- `python -m alembic revision --autogenerate -m "describe change"`
- Apply migrations manually if needed:
	- `python -m alembic upgrade head`

## Publishing

- Build frontend: `cd frontend && npm ci && npm run build && cd ..`
- Build Python package: `python -m build`
- Upload: `twine upload dist/*`

## Docker

The Docker image is a two-stage build — Node builds the frontend, then Python + CUDA form the runtime layer.

### Build

```bash
docker build -t pixlvault:dev .
```

Or via Compose (also starts the container):

```bash
docker compose up --build
```

### Run (without rebuilding)

```bash
docker compose up
```

### Useful flags during development

```bash
# Rebuild only the runtime stage (faster if only Python source changed)
docker build --target frontend-builder -t pixlvault:frontend .
docker build -t pixlvault:dev .

# Open a shell inside a running container
docker exec -it pixlvault bash

# Tail logs
docker logs -f pixlvault

# Inspect the persistent data volume
docker volume inspect pixlvault_pixlvault-data
```

### GPU access

The image expects the NVIDIA Container Toolkit to be installed on the host. See the **Option 4: Docker** section in [README.md](README.md) for setup instructions.

To test GPU visibility inside the container:

```bash
docker run --rm --gpus all pixlvault:dev python -c \
  "import torch; print(torch.cuda.get_device_name(0))"
```

### CPU-only build (no GPU required)

Remove the `deploy.resources` block from `docker-compose.yml` and set `"default_device": "cpu"` in the generated `server-config.json` (located in the `pixlvault-data` volume at `/data/config/server-config.json`).



### GitHub tagged releases to PyPI

- Workflow file: [.github/workflows/publish-pypi.yml](.github/workflows/publish-pypi.yml)
- Trigger: push tag matching `v*` (for example `v0.7.0`)
- Behavior:
	- builds frontend bundle
	- verifies tag matches `[project].version` in [pyproject.toml](pyproject.toml)
	- builds wheel/sdist
	- publishes to PyPI using Trusted Publishing

One-time PyPI setup:

- In PyPI, open your project → **Publishing** → **Add a new pending publisher**
- Set:
	- **Owner**: your GitHub org/user
	- **Repository**: `pixlvault`
	- **Workflow name**: `publish-pypi.yml`
	- **Environment name**: leave blank (unless you add one in the workflow)

Release command sequence:

- Update version in [pyproject.toml](pyproject.toml)
- Commit and push
- Create/push tag: `git tag v0.7.0 && git push origin v0.7.0`
