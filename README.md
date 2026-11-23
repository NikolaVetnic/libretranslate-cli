# lt — LibreTranslate CLI

A small command-line translator backed by a self-hosted [LibreTranslate](https://github.com/LibreTranslate/LibreTranslate) server running in Docker. Everything runs locally; no text leaves your machine.

The CLI is pure Python standard library (no dependencies).

## Setup

```powershell
# 1. Install the CLI (editable, into a virtualenv)
python -m venv .venv
.venv\Scripts\pip install -e .

# 2. (Optional) Start the container now: pulls the image and downloads models
.venv\Scripts\lt up
```

The first start pulls the image (~1 GB) and downloads language models, which takes a few minutes. Models are stored in the `lt-models` Docker volume, so later starts take a few seconds.

## Install globally (any terminal, no venv)

Use [pipx](https://pipx.pypa.io). It installs the tool in its own isolated environment and puts only `lt`, `ltn` and `lte` on your PATH. Install in editable mode (`-e`) so `lt` can find `docker-compose.yml` and `.env` in this folder. Code changes and `git pull` then take effect without reinstalling.

**Windows (Git Bash, PowerShell or cmd)**

```bash
python -m pip install --user pipx
python -m pipx ensurepath          # adds %USERPROFILE%\.local\bin to your user PATH
# open a new terminal, then:
pipx install -e /c/path/to/libretranslate-cli
```

**macOS**

```bash
brew install pipx
pipx ensurepath                    # adds ~/.local/bin to your PATH
# open a new terminal, then:
pipx install -e ~/path/to/libretranslate-cli
```

Requirements: Python 3.9+ and Docker (Docker Desktop, OrbStack or Colima on macOS). The image runs natively on both Intel and Apple Silicon. After installing, `ltn God morgen` works from any folder.

To update after a `git pull`, nothing is needed. If `pyproject.toml` changed (for example, new commands), run `pipx reinstall lt-cli`. To remove the tool, run `pipx uninstall lt-cli`.

> **Git Bash note:** if interactive mode (`ltn` with no arguments) doesn't respond, Git Bash isn't presenting a real terminal to Windows programs. Run `winpty ltn` instead. Text arguments, files and pipes work normally.

## On-demand server

The container only runs while you're using it:

1. **Starts on demand.** When a command needs the server and it's off, `lt` starts the container. This takes about 4 seconds once models are downloaded. Later calls take about 0.5 seconds.
2. **Stops when idle.** Each use records a timestamp. A small background watcher process (`pythonw`, no window) stops the container after `LT_IDLE_TIMEOUT` minutes (default 15) without use, then exits. A file lock ensures only one watcher runs.
3. **Uses little memory.** The server runs a single worker (`LT_THREADS=1`) and uses about 250 MB of RAM with English and Norwegian loaded. LibreTranslate's default of 4 workers used about 1.5 GB.
4. **Never starts on its own.** The container has `restart: "no"`, so it doesn't come up when Docker starts.

`lt status` shows whether the server is up and how long until it auto-stops. Only CLI usage counts as activity. If you use the web UI at http://localhost:5000, it may be stopped while you're still on the page.

Docker Desktop's *Resource Saver* mode (enabled by default) shuts down Docker's own Linux VM a few minutes after the last container stops. In practice, nothing keeps running between translations.

## Usage

```text
lt "Good morning" -t nl            # translate, source language auto-detected
lt -s en -t de "How are you?"      # explicit source language
echo "Hallo wereld" | lt -t en     # translate piped text
lt -F notes.txt -t fr > notes.fr   # translate a file
lt -t es                           # interactive mode (:q to quit)
lt "Hello" -t it -a 3              # show up to 3 alternative translations
lt "Bonjour" -t en -v              # show the detected source language
lt detect "Goedemorgen"            # detect the language
lt languages                       # list loaded languages and their targets
lt status                          # is the server up? when will it auto-stop?
lt up                              # start now (otherwise starts on first use)
lt down                            # stop now
lt logs -f                         # follow container logs
```

Running `lt ...` without a subcommand is the same as `lt translate ...`. Add `--json` to any command to get the raw API response.

If every argument is an existing file, `lt` translates the files' contents. Each file's result is printed under a `==> name <==` header. Otherwise the arguments are joined and translated as text.

### Norwegian ↔ English shortcuts

| Command | Equivalent to          |
| ------- | ---------------------- |
| `ltn`   | `lt -s nb -t en ...`   |
| `lte`   | `lt -s en -t nb ...`   |

```text
ltn God morgen, hvordan har du det?   # text (quotes optional)
lte "Where is the train station?"
ltn brev.txt                          # one or more files
lte -F report.txt > rapport.txt       # explicit file
Get-Content brev.txt | ltn            # piped text
ltn                                   # interactive mode
```

All `translate` options still work, e.g. `ltn --json ...` or `ltn -a 2 ...`.

## Configuration

| Env var                         | Default                  | Purpose                                              |
| ------------------------------- | ------------------------ | ---------------------------------------------------- |
| `LT_URL`                        | `http://localhost:$LT_PORT` | Server the CLI talks to                           |
| `LT_AUTOSTART`                  | `1`                      | Start the local container when a command needs it    |
| `LT_IDLE_TIMEOUT`               | `15`                     | Minutes without use before auto-stop (`0` = never)   |
| `LT_TARGET`                     | `en`                     | Default target language                              |
| `LT_SOURCE`                     | `auto`                   | Default source language                              |
| `LT_API_KEY`                    | –                        | API key, if the server requires one                  |
| `LT_COMPOSE_FILE`               | `./docker-compose.yml`   | Compose file used by `lt up/down/logs`               |

All settings can also go in a `.env` file next to `docker-compose.yml` (see [.env.example](.env.example)). Docker Compose reads it for the server, and `lt` reads it for its own defaults. Real environment variables take precedence. Server settings:

- `LT_LOAD_ONLY` — comma-separated languages to load (default `en,nl,de,fr,es,it`; Norwegian is `nb`). Leave it empty to load every language. That download is several GB. LibreTranslate downloads models only when the model volume is empty. After changing this setting, run `lt down -v`, then `lt up`, to delete the cached models and download the new set.
- `LT_PORT` — host port (default `5000`).
- `LT_THREADS` — worker processes (default `1`). Each one holds its own copy of the models, so raise this only if many requests arrive at the same time.
- `LT_IMAGE_TAG` — pin a specific image version instead of `latest`.

The LibreTranslate web UI is also available at http://localhost:5000.

## Exit codes

`0` success · `1` API or input error · `2` bad arguments · `3` server unreachable
