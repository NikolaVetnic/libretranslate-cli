"""`lt` - translate text from the command line using a local LibreTranslate server."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, server
from .client import LibreTranslate, LTError, LTUnreachable
from .server import DockerError
SUBCOMMANDS = {"translate", "t", "detect", "languages", "langs", "up", "down", "status", "logs"}

EXIT_ERROR = 1
EXIT_UNREACHABLE = 3


# --------------------------------------------------------------------------- helpers

def _err(msg: str) -> None:
    print(f"lt: {msg}", file=sys.stderr)


def _client(args: argparse.Namespace) -> LibreTranslate:
    return LibreTranslate(args.url, api_key=args.api_key, timeout=args.timeout)


def _api(args: argparse.Namespace, fn):
    """Call fn(client). For a local server: start the container on demand if it is
    down, and record the use so the idle watcher stops it again later."""
    client = _client(args)
    local = server.is_local(args.url)
    try:
        result = fn(client)
    except LTUnreachable:
        if not (local and server.autostart_enabled()):
            raise
        server.ensure_running(args.url)
        result = fn(client)
    if local:
        server.record_use()
    return result


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


# --------------------------------------------------------------------------- commands

def cmd_translate(args: argparse.Namespace) -> int:
    inputs = _read_inputs(args)

    def run_one(q: str) -> dict:
        res = _api(args, lambda c: c.translate(q, target=args.to, source=args.source,
                                               fmt=args.format, alternatives=args.alternatives))
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
    res = _api(args, lambda c: c.detect(text))
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        for d in res:
            print(f"{d['language']}\t{d['confidence']}")
    return 0


def cmd_languages(args: argparse.Namespace) -> int:
    langs = _api(args, lambda c: c.languages())
    if args.json:
        print(json.dumps(langs, ensure_ascii=False, indent=2))
        return 0
    for lang in sorted(langs, key=lambda l: l["code"]):
        targets = ",".join(sorted(t for t in lang.get("targets", []) if t != lang["code"]))
        print(f"{lang['code']:<6} {lang['name']:<22} -> {targets}")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    if args.no_wait:
        return server.compose("up", "-d").returncode
    print(f"Starting LibreTranslate at {args.url} (the first start downloads models)",
          end="", file=sys.stderr, flush=True)
    took = server.start(args.url, args.wait_timeout)
    print(f" ready ({took:.0f}s)", file=sys.stderr)
    if server.is_local(args.url):
        server.record_use()
        _print_idle_info()
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    return server.compose("down", *(["--volumes"] if args.volumes else [])).returncode


def cmd_logs(args: argparse.Namespace) -> int:
    return server.compose("logs", "--tail", str(args.tail), *(["-f"] if args.follow else [])).returncode


def _print_idle_info() -> None:
    timeout = server.idle_timeout_seconds()
    if timeout <= 0:
        print("      auto-stop disabled (LT_IDLE_TIMEOUT=0)")
        return
    idle = time.time() - server.last_activity()
    left = max(timeout - idle, 0)
    watcher = "" if server.watcher_running() else " (no watcher running; next use starts one)"
    print(f"      idle {idle / 60:.0f} min, auto-stop in {left / 60:.0f} min{watcher}")


def cmd_status(args: argparse.Namespace) -> int:
    client = _client(args)
    try:
        langs = client.languages()
    except LTError as e:
        off = server.is_local(args.url) and server.autostart_enabled()
        print(f"{'OFF' if off else 'DOWN':<5} {args.url}" + (" (starts automatically on next use)" if off else f"  {e}"))
        return 0 if off else EXIT_UNREACHABLE
    print(f"UP    {client.url}  ({len(langs)} languages: {', '.join(sorted(l['code'] for l in langs))})")
    if server.is_local(args.url):
        _print_idle_info()
    return 0


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    default_url = os.environ.get("LT_URL") or f"http://localhost:{os.environ.get('LT_PORT', '5000')}"
    common.add_argument("--url", default=default_url,
                        help=f"LibreTranslate base URL (env LT_URL, default {default_url})")
    common.add_argument("--api-key", default=os.environ.get("LT_API_KEY"),
                        help="API key if the server requires one (env LT_API_KEY)")
    common.add_argument("--timeout", type=float, default=120.0, help="request timeout in seconds")
    common.add_argument("--json", action="store_true", help="print raw JSON response")

    p = argparse.ArgumentParser(
        prog="lt",
        description="Translate text using a local LibreTranslate server running in Docker.",
        epilog=(
            "examples:\n"
            '  lt "Good morning" -t nb       translate (source auto-detected)\n'
            "  echo Hallo | lt -t en         translate piped text\n"
            "  lt -F notes.txt -s en -t nb   translate a file\n"
            "  lt -t nb                      interactive mode\n"
            '  lt detect "Good morning"      detect language\n'
            "  lt languages                  list installed languages\n"
            "  lt status                     is the server up, and when will it auto-stop?\n"
            "\nA bare `lt ...` without a subcommand means `lt translate ...`.\n"
            "The container starts automatically when needed and stops after LT_IDLE_TIMEOUT\n"
            "minutes without use (default 15; 0 = never). Settings are read from the project's .env."
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
    sub.add_parser("status", parents=[common], help="show whether the server is up and when it will auto-stop").set_defaults(func=cmd_status)

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

    server.load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except LTUnreachable as e:
        local_auto = server.is_local(args.url) and server.autostart_enabled()
        _err(f"{e}" if local_auto else f"{e}\n    is the container running? try: lt up")
        return EXIT_UNREACHABLE
    except DockerError as e:
        _err(str(e))
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
