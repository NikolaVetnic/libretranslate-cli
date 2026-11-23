"""`lt` - translate text from the command line using a local LibreTranslate server."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .client import LibreTranslate, LTError, LTUnreachable

DEFAULT_URL = "http://localhost:5000"
SUBCOMMANDS = {"translate", "t", "detect", "languages", "langs", "up", "down", "status", "logs"}

EXIT_ERROR = 1
EXIT_UNREACHABLE = 3


# --------------------------------------------------------------------------- helpers

def _err(msg: str) -> None:
    print(f"lt: {msg}", file=sys.stderr)


def _client(args: argparse.Namespace) -> LibreTranslate:
    return LibreTranslate(args.url, api_key=args.api_key, timeout=args.timeout)


def _read_file(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def _read_inputs(args: argparse.Namespace) -> list[tuple[str | None, str]] | None:
    """Inputs as (label, text) pairs. None means 'go interactive'.

    Sources, in order: --file, positional args (if every arg is an existing file they are
    read as files, otherwise they are joined as text), piped stdin.
    """
    if getattr(args, "file", None):
        return [(args.file, _read_file(args.file))]
    if args.text:
        if all(Path(a).is_file() for a in args.text):
            return [(a, _read_file(a)) for a in args.text]
        return [(None, " ".join(args.text).strip())]
    if not sys.stdin.isatty():
        return [(None, sys.stdin.read().strip())]
    return None


def _find_compose_file() -> Path:
    candidates = []
    if os.environ.get("LT_COMPOSE_FILE"):
        candidates.append(Path(os.environ["LT_COMPOSE_FILE"]))
    candidates.append(Path.cwd() / "docker-compose.yml")
    # repo root when installed in editable mode: src/lt_cli/cli.py -> ../../
    candidates.append(Path(__file__).resolve().parents[2] / "docker-compose.yml")
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        "docker-compose.yml not found; set LT_COMPOSE_FILE or run from the project directory"
    )


def _compose(*compose_args: str) -> int:
    if not shutil.which("docker"):
        _err("docker is not on PATH")
        return EXIT_ERROR
    compose_file = _find_compose_file()
    cmd = ["docker", "compose", "-f", str(compose_file), *compose_args]
    return subprocess.call(cmd, cwd=compose_file.parent)


def _wait_ready(client: LibreTranslate, timeout: float) -> bool:
    print(f"Waiting for LibreTranslate at {client.url} (first start downloads models)", end="", file=sys.stderr, flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.is_ready():
            print(" ready.", file=sys.stderr)
            return True
        print(".", end="", file=sys.stderr, flush=True)
        time.sleep(3)
    print(" timed out.", file=sys.stderr)
    return False


# --------------------------------------------------------------------------- commands

def cmd_translate(args: argparse.Namespace) -> int:
    client = _client(args)
    inputs = _read_inputs(args)

    def run_one(q: str) -> dict:
        res = client.translate(q, target=args.to, source=args.source, fmt=args.format,
                               alternatives=args.alternatives)
        det = res.get("detectedLanguage") or {}
        if args.source == "auto" and det and not det.get("confidence"):
            # LibreTranslate falls back to "en, 0%" when detection fails (e.g. Norwegian:
            # langdetect says "no" but the model is "nb"), so the text may be untranslated.
            _err("could not detect the source language; specify it with -s/--from (e.g. -s nb)")
        return res

    if inputs is None:  # interactive REPL
        print(f"interactive: {args.source} -> {args.to}. Ctrl+D / Ctrl+Z+Enter or :q to quit.",
              file=sys.stderr)
        while True:
            try:
                line = input("> ")
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                return 0
            if line.strip() in {":q", ":quit", ":exit"}:
                return 0
            if not line.strip():
                continue
            try:
                res = run_one(line)
            except LTError as e:
                _err(str(e))
                continue
            print(_format_result(res, args))

    many = len(inputs) > 1
    for i, (label, text) in enumerate(inputs):
        if many:
            print(f"{'' if i == 0 else chr(10)}==> {label} <==")
        if not text:
            _err(f"no text to translate{f' in {label}' if label else ''}")
            if not many:
                return EXIT_ERROR
            continue
        print(_format_result(run_one(text), args))
    return 0


def _format_result(res: dict, args: argparse.Namespace) -> str:
    if args.json:
        return json.dumps(res, ensure_ascii=False, indent=2)
    out = res.get("translatedText", "")
    if args.verbose and (det := res.get("detectedLanguage")):
        out = f"[{det.get('language')} {det.get('confidence')}%] {out}"
    for alt in res.get("alternatives", []) or []:
        out += f"\n  ~ {alt}"
    return out


def cmd_detect(args: argparse.Namespace) -> int:
    text = "\n".join(t for _, t in _read_inputs(args) or [])
    if not text.strip():
        _err("no text given")
        return EXIT_ERROR
    res = _client(args).detect(text)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        for d in res:
            print(f"{d['language']}\t{d['confidence']}")
    return 0


def cmd_languages(args: argparse.Namespace) -> int:
    langs = _client(args).languages()
    if args.json:
        print(json.dumps(langs, ensure_ascii=False, indent=2))
        return 0
    for lang in sorted(langs, key=lambda l: l["code"]):
        targets = ",".join(sorted(t for t in lang.get("targets", []) if t != lang["code"]))
        print(f"{lang['code']:<6} {lang['name']:<22} -> {targets}")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    rc = _compose("up", "-d")
    if rc != 0 or args.no_wait:
        return rc
    return 0 if _wait_ready(_client(args), args.wait_timeout) else EXIT_UNREACHABLE


def cmd_down(args: argparse.Namespace) -> int:
    return _compose("down", *(["--volumes"] if args.volumes else []))


def cmd_logs(args: argparse.Namespace) -> int:
    return _compose("logs", "--tail", str(args.tail), *(["-f"] if args.follow else []))


def cmd_status(args: argparse.Namespace) -> int:
    client = _client(args)
    try:
        langs = client.languages()
    except LTError as e:
        print(f"DOWN  {e}")
        return EXIT_UNREACHABLE
    print(f"UP    {client.url}  ({len(langs)} languages: {', '.join(sorted(l['code'] for l in langs))})")
    return 0


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=os.environ.get("LT_URL", DEFAULT_URL),
                        help=f"LibreTranslate base URL (env LT_URL, default {DEFAULT_URL})")
    common.add_argument("--api-key", default=os.environ.get("LT_API_KEY"),
                        help="API key if the server requires one (env LT_API_KEY)")
    common.add_argument("--timeout", type=float, default=120.0, help="request timeout in seconds")
    common.add_argument("--json", action="store_true", help="print raw JSON response")

    p = argparse.ArgumentParser(
        prog="lt",
        description="Translate text using a local LibreTranslate server running in Docker.",
        epilog=(
            "examples:\n"
            "  lt up                         start the Docker container and wait until ready\n"
            '  lt "Good morning" -t nl       translate (source auto-detected)\n'
            "  echo Hallo | lt -t en         translate piped text\n"
            "  lt -F notes.txt -s en -t de   translate a file\n"
            "  lt -t fr                      interactive mode\n"
            '  lt detect "Goedemorgen"       detect language\n'
            "  lt languages                  list installed languages\n"
            "\nA bare `lt ...` without a subcommand means `lt translate ...`."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"lt {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    t = sub.add_parser("translate", aliases=["t"], parents=[common], help="translate text (default)")
    t.add_argument("text", nargs="*", help="text to translate (omit to read stdin / go interactive)")
    t.add_argument("-t", "--to", default=os.environ.get("LT_TARGET", "en"),
                   help="target language code (env LT_TARGET, default en)")
    t.add_argument("-s", "--from", dest="source", default=os.environ.get("LT_SOURCE", "auto"),
                   help="source language code (default auto)")
    t.add_argument("-F", "--file", help="read text from a UTF-8 file")
    t.add_argument("--format", choices=["text", "html"], default="text", help="input format")
    t.add_argument("-a", "--alternatives", type=int, default=0, help="number of alternative translations")
    t.add_argument("-v", "--verbose", action="store_true", help="show detected source language")
    t.set_defaults(func=cmd_translate)

    d = sub.add_parser("detect", parents=[common], help="detect the language of text")
    d.add_argument("text", nargs="*")
    d.add_argument("-F", "--file", help="read text from a UTF-8 file")
    d.set_defaults(func=cmd_detect)

    l = sub.add_parser("languages", aliases=["langs"], parents=[common], help="list supported languages")
    l.set_defaults(func=cmd_languages)

    u = sub.add_parser("up", parents=[common], help="start the LibreTranslate container")
    u.add_argument("--no-wait", action="store_true", help="don't wait for the server to become ready")
    u.add_argument("--wait-timeout", type=float, default=900, help="seconds to wait (default 900)")
    u.set_defaults(func=cmd_up)

    dn = sub.add_parser("down", parents=[common], help="stop and remove the container")
    dn.add_argument("-v", "--volumes", action="store_true",
                    help="also delete downloaded models (needed after changing LT_LOAD_ONLY)")
    dn.set_defaults(func=cmd_down)
    sub.add_parser("status", parents=[common], help="check whether the server is up").set_defaults(func=cmd_status)

    g = sub.add_parser("logs", parents=[common], help="show container logs")
    g.add_argument("-f", "--follow", action="store_true")
    g.add_argument("--tail", type=int, default=100)
    g.set_defaults(func=cmd_logs)
    return p


def _preset(source: str, target: str):
    """Entry point that runs `lt translate -s SOURCE -t TARGET <args>`."""
    def entry() -> int:
        return main(["translate", "-s", source, "-t", target, *sys.argv[1:]])
    return entry


main_nb_en = _preset("nb", "en")  # `ltn`: Norwegian -> English
main_en_nb = _preset("en", "nb")  # `lte`: English -> Norwegian


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Default to `translate` when no subcommand is given.
    if not argv or (argv[0] not in SUBCOMMANDS and argv[0] not in {"-h", "--help", "--version"}):
        argv.insert(0, "translate")

    # Make sure non-ASCII output works on Windows consoles / pipes.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except LTUnreachable as e:
        _err(f"{e}\n    is the container running? try: lt up")
        return EXIT_UNREACHABLE
    except LTError as e:
        _err(str(e))
        return EXIT_ERROR
    except (FileNotFoundError, UnicodeDecodeError) as e:
        _err(str(e))
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
