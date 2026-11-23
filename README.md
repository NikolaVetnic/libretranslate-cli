# lt — LibreTranslate CLI

A small command-line translator backed by a self-hosted [LibreTranslate](https://github.com/LibreTranslate/LibreTranslate) server running in Docker. Everything runs locally; no text leaves your machine.

The CLI is pure Python standard library (no dependencies).

## Setup

```powershell
# 1. Install the CLI (editable, into a virtualenv)
python -m venv .venv
.venv\Scripts\pip install -e .

# 2. Start the LibreTranslate container and wait until models are loaded
.venv\Scripts\lt up
```

The first `lt up` pulls the image and downloads language models, which takes a few minutes. Models are stored in the `lt-models` Docker volume, so later starts are fast.

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
lt status                          # is the server up?
lt logs -f                         # follow container logs
lt down                            # stop the container
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
| `LT_URL`                        | `http://localhost:5000`  | Server the CLI talks to                              |
| `LT_TARGET`                     | `en`                     | Default target language                              |
| `LT_SOURCE`                     | `auto`                   | Default source language                              |
| `LT_API_KEY`                    | –                        | API key, if the server requires one                  |
| `LT_COMPOSE_FILE`               | `./docker-compose.yml`   | Compose file used by `lt up/down/logs`               |

Server settings go in a `.env` file next to `docker-compose.yml` (see [.env.example](.env.example)):

- `LT_LOAD_ONLY` — comma-separated languages to load (default `en,nl,de,fr,es,it`; Norwegian is `nb`). Leave it empty to load every language. That download is several GB. LibreTranslate downloads models only when the model volume is empty. After changing this setting, run `lt down -v`, then `lt up`, to delete the cached models and download the new set.
- `LT_PORT` — host port (default `5000`). If you change it, also set `LT_URL`.
- `LT_IMAGE_TAG` — pin a specific image version instead of `latest`.

The LibreTranslate web UI is also available at http://localhost:5000.

## Exit codes

`0` success · `1` API or input error · `2` bad arguments · `3` server unreachable
