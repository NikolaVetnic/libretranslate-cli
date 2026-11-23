"""`lt` - translate text from the command line using a local LibreTranslate server."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__, checker, ordbok, server
from .client import LibreTranslate, LTError, LTUnreachable
from .server import DockerError
from .style import Style, is_terminal, make_style
SUBCOMMANDS = {"translate", "t", "word", "w", "check", "c", "detect", "languages", "langs", "up", "down", "status", "logs"}

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
    if not is_terminal(sys.stdin):
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


# --------------------------------------------------------------------------- word (dictionary)

POS_LABELS = {
    "NOUN": "substantiv", "VERB": "verb", "ADJ": "adjektiv", "ADV": "adverb",
    "ADP": "preposisjon", "PRON": "pronomen", "DET": "determinativ", "CCONJ": "konjunksjon",
    "SCONJ": "subjunksjon", "INTJ": "interjeksjon", "NUM": "tallord", "PROPN": "egennavn",
    "SYM": "symbol", "ABBR": "forkortelse", "EXPR": "uttrykk",
}
GENDERS = {"Masc": "hankjønn", "Fem": "hunkjønn", "Neuter": "intetkjønn"}
ARTICLES = {"bm": {"Masc": "en", "Fem": "ei", "Neuter": "et"},
            "nn": {"Masc": "ein", "Fem": "ei", "Neuter": "eit"}}
DICT_NAMES = {"bm": "Bokmålsordboka", "nn": "Nynorskordboka"}


def _headword_query(e: ordbok.Entry) -> str:
    """Phrase a headword so the MT model translates it as a dictionary word."""
    lemma = e.lemmas[0]
    if e.pos == "VERB":
        return f"Å {lemma}."
    if e.pos == "NOUN" and e.genders:
        return f"{ARTICLES['bm'][e.genders[0]].capitalize()} {lemma}."
    return f"{lemma[:1].upper()}{lemma[1:]}."


def _clean_mt(text: str) -> str:
    text = text.strip().rstrip(".!")
    return text[:1].lower() + text[1:]


def _translate_batch(args: argparse.Namespace, texts: list[str], source: str, target: str) -> dict[str, str]:
    """Translate many strings in one request. Returns {} (with a warning) if unavailable."""
    texts = list(dict.fromkeys(t for t in texts if t))
    if not texts:
        return {}
    try:
        out = _api(args, lambda c: c.translate_many(texts, target=target, source=source))
    except (LTError, DockerError) as e:
        _err(f"translations unavailable ({e})")
        return {}
    return dict(zip(texts, out))


GENDER_COLORS = {"Masc": "bblue", "Fem": "bmagenta", "Neuter": "bgreen"}
POS_STYLE = {  # icon, color
    "VERB": ("🏃", "bgreen"), "NOUN": ("📦", "bblue"), "ADJ": ("🎨", "byellow"),
    "ADV": ("💨", "bcyan"), "PRON": ("👤", "bcyan"),
}
WEAK_PAST_ENDINGS = ("et", "a", "te", "de", "dde")


def _is_irregular(e: ordbok.Entry) -> bool:
    """Heuristic: a verb is irregular if no past form is stem + a weak ending."""
    if e.pos != "VERB" or not e.forms.get("past"):
        return False
    inf = (e.forms.get("inf") or e.lemmas)[0]
    stem = inf[:-1] if inf.endswith("e") and len(inf) > 2 else inf
    stems = {stem} | ({stem[:-1]} if len(stem) > 2 and stem[-1] == stem[-2] else set())
    return not any(p.startswith(st) and p[len(st):] in WEAK_PAST_ENDINGS
                   for p in e.forms["past"] for st in stems)


def _alts(S: Style, forms: list[str], *styles: str) -> tuple[str, str]:
    """(plain, styled) rendering of alternative forms joined by ' / '."""
    plain = " / ".join(forms)
    return plain, S(" / ", "dim").join(S(f, *styles) for f in forms)


def _cell(plain: str, styled: str, width: int) -> str:
    return styled + " " * max(width - len(plain), 0)


def _sense_lines(S: Style, s: ordbok.Sense, label: str, indent: str, tr: dict, n_examples: int) -> list[str]:
    lines = []
    pad = " " * len(label)
    styled_label = S(label, "bold", "bcyan")
    if s.explanations:
        lines.append(f"{indent}{styled_label} {'; '.join(s.explanations)}")
        english = [tr[x] for x in s.explanations if tr.get(x)]
        if english:
            lines.append(f"{indent}{pad} {S('→ ' + '; '.join(english), 'green', 'italic')}")
    else:
        lines.append(f"{indent}{styled_label}")
    for ex in s.examples[:n_examples]:
        line = f"{indent}{pad}   {S.icon('💬')}{S('«' + ex + '»', 'yellow')}"
        if tr.get(ex):
            line += "  " + S("→ " + tr[ex], "gray", "italic")
        lines.append(line)
    for j, sub in enumerate(s.subsenses):
        lines += _sense_lines(S, sub, f"{chr(ord('a') + j % 26)})", indent + pad, tr, n_examples)
    return lines


def _collect_texts(senses: list[ordbok.Sense], n_examples: int) -> list[str]:
    out = []
    for s in senses:
        out += s.explanations + s.examples[:n_examples] + _collect_texts(s.subsenses, n_examples)
    return out


def _forms_lines(S: Style, e: ordbok.Entry, dictionary: str) -> list[str]:
    f = e.forms
    if e.pos == "NOUN":
        art_plain = "/".join(ARTICLES[dictionary][g] for g in e.genders)
        art_styled = S("/", "dim").join(S(ARTICLES[dictionary][g], GENDER_COLORS[g], "bold") for g in e.genders)
        cells = {k: _alts(S, f.get(k, []), "bold") for k in ("sg_indef", "sg_def", "pl_indef", "pl_def")}
        if art_plain and f.get("sg_indef"):
            cells["sg_indef"] = (f"{art_plain} {cells['sg_indef'][0]}", f"{art_styled} {cells['sg_indef'][1]}")
        width = max(len(cells["sg_indef"][0]), len(cells["pl_indef"][0]), len("ubestemt")) + 3
        return [
            f"  {' ' * 10}{_cell('ubestemt', S('ubestemt', 'dim', 'underline'), width)}{S('bestemt', 'dim', 'underline')}",
            f"  {_cell('entall', S('entall', 'dim'), 10)}{_cell(*cells['sg_indef'], width)}{cells['sg_def'][1]}",
            f"  {_cell('flertall', S('flertall', 'dim'), 10)}{_cell(*cells['pl_indef'], width)}{cells['pl_def'][1]}",
        ]

    rows: list[tuple[str, str]] = []  # (label, styled value)
    if e.pos == "VERB":
        hi = ("bold", "byellow") if _is_irregular(e) else ("bold",)

        def form(slot, prefix="", styles=("bold",)):
            if not f.get(slot):
                return ""
            styled = _alts(S, f[slot], *styles)[1]
            return f"{S(prefix, 'dim')} {styled}" if prefix else styled

        rows = [
            ("infinitiv", form("inf", "å")),
            ("presens", form("pres")),
            ("preteritum", form("past", styles=hi)),
            ("perfektum", form("perf_part", "har", styles=hi)),
            ("imperativ", form("imp")),
            ("passiv", form("pass")),
            ("pres. partisipp", form("pres_part")),
        ]
    elif e.pos == "ADJ":
        dot = S(" · ", "dim")
        positive = [_alts(S, f[k], "bold")[1] for k in ("pos_mf", "pos_n", "pos_def_pl") if f.get(k)]
        sup = [_alts(S, f[k], "bold")[1] for k in ("sup_indef", "sup_def") if f.get(k)]
        rows = [("positiv", dot.join(positive)),
                ("komparativ", _alts(S, f.get("cmp", []), "bold")[1]),
                ("superlativ", dot.join(sup))]
    return [f"  {_cell(k, S(k, 'dim'), 16)}{v}" for k, v in rows if v]


def _entry_lines(e: ordbok.Entry, tr: dict, args: argparse.Namespace) -> list[str]:
    S: Style = args.style
    icon, color = POS_STYLE.get(e.pos, ("🔹", "bcyan"))
    if e.pos == "NOUN" and e.genders:
        color = GENDER_COLORS[e.genders[0]]

    pos_plain = POS_LABELS.get(e.pos, e.pos.lower())
    pos_styled = S(pos_plain, "dim")
    if e.pos == "NOUN" and e.genders:
        pos_plain += ", " + "/".join(GENDERS[g] for g in e.genders)
        pos_styled += S(", ", "dim") + S("/", "dim").join(S(GENDERS[g], GENDER_COLORS[g]) for g in e.genders)
    if _is_irregular(e):
        pos_plain += ", uregelrett" + ("   " if S.emoji else "")
        pos_styled += S(", ", "dim") + S.icon("⚡") + S("uregelrett", "byellow")

    lemma = " / ".join(e.lemmas)
    head = f"{S.icon(icon)}{S(lemma, 'bold', color)}  {S('(', 'dim')}{pos_styled}{S(')', 'dim')}"
    plain_len = len(lemma) + len(pos_plain) + 4 + (3 if S.emoji else 0)
    en = tr.get(_headword_query(e))
    if en:
        gloss = f"≈ {_clean_mt(en)}"
        head += "  " + S(gloss, "bold", "green")
        plain_len += 2 + len(gloss)
    lines = [head]
    if S.color:
        lines.append(S("─" * plain_len, "gray"))
    lines += _forms_lines(S, e, args.dict)
    if args.brief:
        return lines
    senses = e.senses if args.all else e.senses[: args.max_senses]
    if senses:
        lines.append("")
    for i, s in enumerate(senses, 1):
        lines += _sense_lines(S, s, f"{i}.", "  ", tr, args.examples)
    if len(e.senses) > len(senses):
        more = len(e.senses) - len(senses)
        lines.append(S(f"  … {more} more meaning{'s' if more > 1 else ''} (use --all)", "dim"))
    return lines


def _headword_glosses(args: argparse.Namespace, entries: list[ordbok.Entry]) -> dict[str, str]:
    """{headword query: English gloss}. Verbs prefer a 'to ...' rendering among alternatives."""
    queries = list(dict.fromkeys(_headword_query(e) for e in entries))
    try:
        results = _api(args, lambda c: c.translate_many_alts(queries, target="en", source="nb", alternatives=3))
    except (LTError, DockerError) as e:
        _err(f"translations unavailable ({e})")
        return {}
    out = {}
    for q, options in zip(queries, results):
        best = options[0]
        if q.startswith("Å "):
            best = next((o for o in options if o.lower().startswith("to ")), best)
        out[q] = best
    return out


def _norm_word(text: str) -> str:
    text = text.strip().strip(".!?,;:").lower()
    for prefix in ("å ", "en ", "ei ", "et ", "ein ", "eit "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def _verify_glosses(args: argparse.Namespace, entries: list[ordbok.Entry], tr: dict) -> None:
    """Drop headword glosses that don't survive a round trip back to Norwegian.

    Single-word MT is unreliable for rarer words (e.g. 'skreve' -> 'to write'); if the
    English gloss doesn't translate back to one of the entry's own forms, hide it and
    let the translated definitions speak instead.
    """
    queries = {e.article_id: _headword_query(e) for e in entries}
    glosses = {q: _clean_mt(tr[q]) for q in queries.values() if tr.get(q)}
    back = _translate_batch(args, list(glosses.values()), "en", "nb")
    for e in entries:
        q = queries[e.article_id]
        if q not in glosses:
            continue
        own = {_norm_word(x) for x in e.lemmas} | {_norm_word(f) for fs in e.forms.values() for f in fs}
        if _norm_word(back.get(glosses[q], "")) not in own:
            tr.pop(q, None)


def _lookup_one(args: argparse.Namespace, word: str) -> int:
    dictionary = args.dict
    entries = ordbok.lookup(word, dictionary, cache_dir=server.state_dir())
    translate = not args.no_translate

    if not entries and translate and dictionary == "bm":
        # maybe it's an English word: translate it to Norwegian and look that up
        nb = _clean_mt(_translate_batch(args, [word], "en", "nb").get(word, ""))
        if nb and nb.lower() != word.lower():
            entries = ordbok.lookup(nb, dictionary, cache_dir=server.state_dir())
            if entries:
                _err(f"'{word}' is not in {DICT_NAMES[dictionary]}; showing '{nb}' (translated from English)")

    if not entries:
        hints = ordbok.suggest(word, dictionary)
        _err(f"'{word}' not found in {DICT_NAMES[dictionary]}"
             + (f". Did you mean: {', '.join(hints)}?" if hints else ""))
        return EXIT_ERROR

    tr: dict[str, str] = {}
    if translate:
        tr.update(_headword_glosses(args, entries))
        texts = []
        if not args.brief:
            for e in entries:
                texts += _collect_texts(e.senses if args.all else e.senses[: args.max_senses], args.examples)
        tr.update(_translate_batch(args, texts, "nb", "en"))
        _verify_glosses(args, entries, tr)

    if args.json:
        print(json.dumps({"query": word, "dictionary": dictionary,
                          "entries": [e.to_dict() for e in entries], "translations": tr},
                         ensure_ascii=False, indent=2))
        return 0
    print("\n\n".join("\n".join(_entry_lines(e, tr, args)) for e in entries))
    return 0


def cmd_word(args: argparse.Namespace) -> int:
    word = " ".join(args.text).strip()
    if word:
        return _lookup_one(args, word)
    if not is_terminal(sys.stdin):
        words = [w.strip() for w in sys.stdin.read().splitlines() if w.strip()]
        rc = 0
        for i, w in enumerate(words):
            if i:
                print("\n" + args.style("─" * 40, "gray") + "\n")
            rc = max(rc, _lookup_one(args, w))
        return rc
    print(f"{DICT_NAMES[args.dict]} lookup. Ctrl+D / Ctrl+Z+Enter or :q to quit.", file=sys.stderr)
    while True:
        try:
            w = input(args.style("ord> ", "bold", "bcyan")).strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return 0
        if w in {":q", ":quit", ":exit"}:
            return 0
        if w:
            try:
                _lookup_one(args, w)
            except (ordbok.OrdbokError, LTError) as e:
                _err(str(e))
            print()


# --------------------------------------------------------------------------- check (grammar)

class _Spinner:
    """Tiny stderr spinner shown while waiting, only when stderr is a terminal."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, message: str, enabled: bool):
        self.message, self.enabled = message, enabled
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        i = 0
        while not self._stop.wait(0.1):
            sys.stderr.write(f"\r{self.FRAMES[i % len(self.FRAMES)]} {self.message}")
            sys.stderr.flush()
            i += 1

    def __enter__(self):
        if self.enabled:
            self._thread.start()
        return self

    def __exit__(self, *exc):
        if self.enabled:
            self._stop.set()
            self._thread.join()
            sys.stderr.write("\r" + " " * (len(self.message) + 2) + "\r")
            sys.stderr.flush()


def _highlight(S: Style, text: str, fragments: list[str], *styles: str) -> str:
    """Style each fragment's first occurrence in text (left to right, no overlaps)."""
    if not S.color:
        return text
    out, pos = [], 0
    for frag in fragments:
        i = text.find(frag, pos) if frag else -1
        if i < 0:
            continue
        out += [text[pos:i], S(frag, *styles)]
        pos = i + len(frag)
    return "".join(out) + text[pos:]


def _check_lines(S: Style, r: checker.CheckResult, args: argparse.Namespace) -> list[str]:
    if args.only_corrected:
        return [r.corrected]
    lines = []
    if r.is_correct or not r.corrections:
        lines.append(S.icon("✅", "") + S("No mistakes found.", "bold", "bgreen"))
        lines.append(f"   {S(r.corrected, 'bold')}")
    else:
        lines.append(S.icon("✏️", "") + S("Corrected:", "bold"))
        lines.append("   " + _highlight(S, r.corrected, [c.correction for c in r.corrections], "bold", "bgreen"))
    if r.translation:
        lines.append(f"   {S('→ ' + r.translation, 'gray', 'italic')}")
    if r.corrections:
        n = len(r.corrections)
        lines += ["", S.icon("🔍") + S(f"{n} correction{'s' if n > 1 else ''}", "bold")]
        for i, c in enumerate(r.corrections, 1):
            label = f"{i}."
            change = f"{S(c.original, 'red', 'strike')} {S('→', 'dim')} {S(c.correction, 'bold', 'bgreen')}"
            cat = f"  {S('[' + c.category + ']', 'dim')}" if c.category else ""
            lines.append(f"  {S(label, 'bold', 'bcyan')} {change}{cat}")
            if c.explanation:
                lines.append(f"  {' ' * len(label)} {c.explanation}")
    if args.verbose:
        lines += ["", S(f"({r.model}, {r.duration_s:.1f}s)", "dim")]
    return lines


def _check_one(args: argparse.Namespace, text: str) -> None:
    model = args.model or os.environ.get("LT_CHECK_MODEL") or checker.DEFAULT_MODEL
    spin = _Spinner(f"Checking with Claude ({model})…",
                    is_terminal(sys.stderr) and not args.json and args.style.color)
    with spin:
        r = checker.check(text, args.dict, model, workdir=server.state_dir())
    if args.json:
        print(json.dumps(asdict(r), ensure_ascii=False, indent=2))
    else:
        print("\n".join(_check_lines(args.style, r, args)))


def cmd_check(args: argparse.Namespace) -> int:
    inputs = _read_inputs(args)
    if inputs is None:  # interactive
        print(f"Grammar check ({checker.LANGUAGE[args.dict]}). Ctrl+D / Ctrl+Z+Enter or :q to quit.",
              file=sys.stderr)
        while True:
            try:
                line = input(args.style("sjekk> ", "bold", "bcyan")).strip()
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                return 0
            if line in {":q", ":quit", ":exit"}:
                return 0
            if line:
                try:
                    _check_one(args, line)
                except checker.CheckError as e:
                    _err(str(e))
                print()
    many = len(inputs) > 1
    for i, (label, text) in enumerate(inputs):
        if many:
            print(f"{'' if i == 0 else chr(10)}{args.style('==> ' + str(label) + ' <==', 'bold')}")
        if not text:
            _err(f"no text to check{f' in {label}' if label else ''}")
            if not many:
                return EXIT_ERROR
            continue
        _check_one(args, text)
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
    common.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                        help="colors/emoji (default auto: on in a terminal, unless LT_PRETTY=0)")

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
            "  lt word skrev                 dictionary: forms + meanings (same as: ltw skrev)\n"
            '  lt check "Jeg liker å spiser" grammar check with explanations (same as: ltc ...)\n'
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

    w = sub.add_parser(
        "word", aliases=["w"], parents=[common],
        help="Norwegian dictionary: inflections, meanings, examples (+ English)",
        description="Look up a Norwegian word (any inflected form) in Bokmålsordboka via "
                    "ordbokene.no, with English machine translations from LibreTranslate.",
    )
    w.add_argument("text", nargs="*", help="word or expression (omit for interactive mode)")
    w.add_argument("--nn", dest="dict", action="store_const", const="nn", default="bm",
                   help="use Nynorskordboka instead of Bokmålsordboka")
    w.add_argument("-b", "--brief", action="store_true", help="only forms and headword translation")
    w.add_argument("-e", "--examples", type=int, default=2, metavar="N",
                   help="examples per meaning (default 2)")
    w.add_argument("-m", "--max-senses", type=int, default=5, metavar="N",
                   help="meanings to show per entry (default 5)")
    w.add_argument("--all", action="store_true", help="show all meanings")
    w.add_argument("-n", "--no-translate", action="store_true", help="skip English translations")
    w.set_defaults(func=cmd_word)

    c = sub.add_parser(
        "check", aliases=["c"], parents=[common],
        help="check Norwegian text for mistakes and explain corrections (uses Claude Code)",
        description="Check Norwegian text for spelling, grammar and word-order mistakes. "
                    "Runs your installed Claude Code CLI non-interactively (no tools, no MCP), "
                    "so it uses your Claude subscription; the text is sent to Anthropic.",
    )
    c.add_argument("text", nargs="*", help="text or file(s) to check (omit to read stdin / go interactive)")
    c.add_argument("-F", "--file", help="read text from a UTF-8 file")
    c.add_argument("--nn", dest="dict", action="store_const", const="nn", default="bm",
                   help="check Nynorsk instead of Bokmål")
    c.add_argument("-m", "--model", help=f"Claude model (env LT_CHECK_MODEL, default {checker.DEFAULT_MODEL})")
    c.add_argument("-o", "--only-corrected", action="store_true", help="print only the corrected text")
    c.add_argument("-v", "--verbose", action="store_true", help="show model and time taken")
    c.set_defaults(func=cmd_check)

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


def main_check() -> int:
    """`ltc`: Norwegian grammar check."""
    return main(["check", *sys.argv[1:]])


def main_word() -> int:
    """`ltw`: Norwegian dictionary lookup."""
    return main(["word", *sys.argv[1:]])


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
    args.style = make_style(args.color)
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
    except (ordbok.OrdbokError, checker.CheckError) as e:
        _err(str(e))
        return EXIT_UNREACHABLE
    except (FileNotFoundError, UnicodeDecodeError) as e:
        _err(str(e))
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
