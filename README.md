# PixlVault

PixlVault is a local picture library server for organizing, filtering, and reviewing large image collections.

It provides:
- A browser-based interface
- Fast metadata and tag filtering
- Smart score sorting
- Character and set organization
- Local storage of your library data

PixlVault runs on your machine and serves the UI at a local web address.

## Install PixlVault

Choose one installation method.

### Option 1: Windows installer

Use this if you want the easiest setup on Windows.

1. Go to the GitHub Releases page for this repository.
2. Download the latest Windows installer `.exe`.
3. Run the installer.
4. Start PixlVault Server from the Start Menu shortcut.
5. Open your browser to `http://localhost:9537`.

## Option 2: Install from PyPI

Use this if you already have Python and want a pip install.

Requirements:
- Python 3.10 or newer

Install:

```bash
pip install pixlvault
```

Run:

```bash
pixlvault-server
```

Then open:

```text
http://localhost:9537
```

## Option 3: Clone and run manually

Use this if you want to run from source.

Requirements:
- Python 3.10 or newer
- Node.js 20 or newer
- npm

Steps:

```bash
git clone <your-repo-url>
cd pixlvault

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
# source .venv/bin/activate

pip install --upgrade pip
pip install -e .

cd frontend
npm ci
npm run build
cd ..

pixlvault-server
```

Then open:

```text
http://localhost:9537
```

## Installing plugins

PixlVault supports built-in plugins and user plugins.

- Built-in plugins are in `image-plugins/built-in/`.
- User plugins are in `image-plugins/user/`.
- Start from `image-plugins/user/plugin_template.py`.

To add your own plugin:

1. Copy `image-plugins/user/plugin_template.py` to a new `.py` file in `image-plugins/user/`.
2. Rename the plugin class and plugin id.
3. Implement the plugin `run()` method.
4. Restart PixlVault Server.

`plugin_template.py` is ignored by plugin discovery and will not be loaded as a plugin.

## First run and data location

On first run, PixlVault creates a user config directory and stores:
- Server config
- Database
- Imported media files

If you need to use a custom config path:

```bash
python -m pixlvault.app --server-config "C:\path\to\server-config.json"
```

## Server configuration

On first run, PixlVault generates a `server-config.json` file in the user config directory:

- **Linux / macOS:** `~/.config/pixlvault/server-config.json`
- **Windows:** `%APPDATA%\pixlvault\server-config.json`

You can also supply a custom path with `--server-config <path>`.

Edit the file and restart the server to apply changes.

### Network and port

| Key | Default | Description |
|-----|---------|-------------|
| `host` | `"localhost"` | Address the server binds to. Change to `"0.0.0.0"` to expose the server on the local network. |
| `port` | `9537` | TCP port the server listens on. |
| `cors_origins` | `[]` | Extra origins allowed to make credentialed cross-origin requests. `localhost`, `127.0.0.1`, and the server's own LAN IP are always permitted on any port. |

At startup the server detects its own LAN IP and automatically allows it on any port. This means the Vite dev server works over LAN (`http://192.168.1.5:5173` → `http://192.168.1.5:9537`) without any extra configuration, as long as network access is enabled via `host`.

Use `cors_origins` only if you need to allow origins on a different machine entirely.

### SSL / HTTPS

| Key | Default | Description |
|-----|---------|-------------|
| `require_ssl` | `false` | Enable HTTPS. When `true`, the server will use the key and certificate below. |
| `ssl_keyfile` | `<config_dir>/ssl/key.pem` | Path to the SSL private key file. |
| `ssl_certfile` | `<config_dir>/ssl/cert.pem` | Path to the SSL certificate file. |
| `cookie_samesite` | `"Lax"` | `SameSite` attribute for session cookies (`"Lax"`, `"Strict"`, or `"None"`). |
| `cookie_secure` | `false` | Set the `Secure` flag on session cookies. Enable when serving over HTTPS. |

### Storage

| Key | Default | Description |
|-----|---------|-------------|
| `image_root` | `<config_dir>/images` | Directory where imported media files are stored. |
| `watch_folders` | `[]` | List of folder entries to watch for new images and automatically import them. Each entry is an object with the fields below. |

Each entry in `watch_folders` has the following fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `folder` | string | — | Absolute path to the directory to monitor (recursively). |
| `delete_after_import` | boolean | `false` | When `true`, source files are deleted from the watch folder after a successful import. |

Example:

```json
"watch_folders": [
  { "folder": "/home/user/downloads/photos", "delete_after_import": false },
  { "folder": "/mnt/camera", "delete_after_import": true }
]
```

### Processing

| Key | Default | Description |
|-----|---------|-------------|
| `default_device` | `"cpu"` | Device used for AI processing (`"cpu"` or `"cuda"`). |
| `generate_thumbnails_on_startup` | `true` | Generate missing thumbnails when the server starts. |

### Logging

| Key | Default | Description |
|-----|---------|-------------|
| `log_level` | `"info"` | Log verbosity (`"debug"`, `"info"`, `"warning"`, `"error"`). |
| `log_file` | `<config_dir>/server.log` | Path to the log file. |

### Example config

```json
{
  "host": "localhost",
  "port": 9537,
  "log_level": "info",
  "require_ssl": false,
  "image_root": "/home/user/.config/pixlvault/images",
  "watch_folders": [
    { "folder": "/path/to/photos", "delete_after_import": false }
  ],
  "default_device": "cpu",
  "generate_thumbnails_on_startup": true
}
```

## Updating PixlVault

### PyPI install

```bash
pip install --upgrade pixlvault
```

### Source install

Pull latest changes, rebuild frontend, and reinstall:

```bash
git pull
cd frontend
npm ci
npm run build
cd ..
pip install -e .
```

## Troubleshooting

- If the page does not load, confirm the server process is running.
- If port `9537` is in use, set a different port in your server config file.
- If frontend assets are missing, rebuild frontend with `npm run build` and restart the server.
