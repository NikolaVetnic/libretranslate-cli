"""Norwegian dictionary lookups via the public ordbokene.no API (Språkrådet / UiB).

API docs: https://ord.uib.no  ("bm" = Bokmålsordboka, "nn" = Nynorskordboka)
Only the looked-up word is sent; results are parsed into simple dataclasses.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

API = "https://ord.uib.no"
USER_AGENT = "lt-cli (LibreTranslate CLI dictionary lookup)"
CONCEPTS_MAX_AGE = 30 * 24 * 3600


class OrdbokError(Exception):
    pass


# --------------------------------------------------------------------------- data model

@dataclass
class Sense:
    explanations: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    subsenses: list["Sense"] = field(default_factory=list)


@dataclass
class Entry:
    article_id: int
    lemmas: list[str]
    pos: str                                   # NOUN, VERB, ADJ, ADV, ...
    genders: list[str] = field(default_factory=list)       # Masc, Fem, Neuter
    forms: dict[str, list[str]] = field(default_factory=dict)  # slot -> forms
    senses: list[Sense] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- HTTP

def _get_json(path: str, params: dict | None = None, timeout: float = 15):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise OrdbokError(f"ordbokene.no returned HTTP {e.code} for {path}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise OrdbokError(f"cannot reach ordbokene.no ({getattr(e, 'reason', e)})") from None
    except ValueError:
        raise OrdbokError(f"unexpected response from ordbokene.no for {path}") from None


def search(word: str, dictionary: str = "bm") -> list[int]:
    """Article ids whose lemma or any inflected form equals `word`."""
    res = _get_json("/api/articles", {"w": word, "dict": dictionary, "scope": "ei"})
    return list(res.get("articles", {}).get(dictionary, []))


def suggest(word: str, dictionary: str = "bm", n: int = 6) -> list[str]:
    res = _get_json("/api/suggest", {"q": word, "dict": dictionary, "n": n})
    return [s[0] for s in res.get("a", {}).get("similar", [])]


def _concepts(dictionary: str, cache_dir: Path | None) -> dict:
    """Abbreviation ids (e.g. 'e_l') -> expansions ('eller lignende'). Cached on disk."""
    cache = cache_dir / f"concepts-{dictionary}.json" if cache_dir else None
    if cache and cache.is_file() and time.time() - cache.stat().st_mtime < CONCEPTS_MAX_AGE:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass
    try:
        data = _get_json(f"/{dictionary}/concepts.json").get("concepts", {})
    except OrdbokError:
        return {}
    if cache:
        try:
            cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return data


# --------------------------------------------------------------------------- parsing

# inflection slot name -> tag set that identifies it (ordered for display)
NOUN_SLOTS = {
    "sg_indef": {"Sing", "Ind"},
    "sg_def": {"Sing", "Def"},
    "pl_indef": {"Plur", "Ind"},
    "pl_def": {"Plur", "Def"},
}
VERB_SLOTS = {
    "inf": {"Inf"},
    "pres": {"Pres"},
    "past": {"Past"},
    "perf_part": {"<PerfPart>"},
    "imp": {"Imp"},
    "pass": {"Inf", "Pass"},
    "pres_part": {"Adj", "<PresPart>"},
}
ADJ_SLOTS = {
    "pos_mf": {"Pos", "Masc/Fem", "Ind", "Sing"},
    "pos_n": {"Pos", "Neuter", "Ind", "Sing"},
    "pos_def_pl": {"Pos", "Def", "Sing"},
    "cmp": {"Cmp"},
    "sup_indef": {"Sup", "Ind"},
    "sup_def": {"Sup", "Def"},
}
SLOTS = {"NOUN": NOUN_SLOTS, "VERB": VERB_SLOTS, "ADJ": ADJ_SLOTS}


def _parse_forms(lemma_objs: list[dict]) -> tuple[str, list[str], dict[str, list[str]]]:
    pos, genders = "", []
    forms: dict[str, list[str]] = {}
    for lem in lemma_objs:
        paradigms = lem.get("paradigm_info") or []
        standard = [p for p in paradigms if p.get("standardisation") == "STANDARD"] or paradigms
        for p in standard:
            tags = p.get("tags") or []
            if tags and not pos:
                pos = tags[0]
            for g in tags[1:]:
                if g in ("Masc", "Fem", "Neuter") and g not in genders:
                    genders.append(g)
            slots = SLOTS.get(tags[0] if tags else "", {})
            for infl in p.get("inflection", []):
                wf, itags = infl.get("word_form"), set(infl.get("tags") or [])
                if not wf:
                    continue
                for slot, want in slots.items():
                    if itags == want and wf not in forms.setdefault(slot, []):
                        forms[slot].append(wf)
    if not pos:
        pos = "?"
    # keep slot display order
    order = list(SLOTS.get(pos, {}))
    return pos, genders, {k: forms[k] for k in order if forms.get(k)}


def _render(content: str, items: list[dict], concepts: dict) -> str:
    """Replace each `$` placeholder in content with the text of the matching item."""
    parts = content.split("$")
    out = [parts[0]]
    for i, rest in enumerate(parts[1:]):
        item = items[i] if i < len(items) else {}
        out.append(_item_text(item, concepts) + rest)
    return " ".join("".join(out).split())


def _item_text(item: dict, concepts: dict) -> str:
    t = item.get("type_")
    if t == "article_ref":
        if item.get("word_form"):
            return item["word_form"]
        lemmas = item.get("lemmas") or [{}]
        return lemmas[0].get("lemma", "")
    if t in ("usage", "superscript", "subscript"):
        return item.get("text", "")
    if t == "fraction":
        return f"{item.get('numerator')}/{item.get('denominator')}"
    if "id" in item:  # entity, relation, language, domain, grammar, ...
        c = concepts.get(item["id"]) or {}
        return c.get("expansion") or item["id"]
    return item.get("text", "")


def _parse_sense(defn: dict, concepts: dict) -> Sense:
    s = Sense()
    for el in defn.get("elements", []):
        t = el.get("type_")
        if t == "explanation":
            txt = _render(el.get("content", ""), el.get("items", []), concepts)
            if txt:
                s.explanations.append(txt)
        elif t == "example":
            q = el.get("quote") or {}
            txt = _render(q.get("content", ""), q.get("items", []), concepts)
            if txt:
                s.examples.append(txt)
        elif t == "definition":
            sub = _parse_sense(el, concepts)
            if sub.explanations or sub.examples or sub.subsenses:
                s.subsenses.append(sub)
        # sub_article (fixed expressions), compound_list, article_ref: skipped
    return s


def _parse_senses(body: dict, concepts: dict) -> list[Sense]:
    senses = []
    for d in body.get("definitions", []):
        s = _parse_sense(d, concepts)
        # unwrap container definitions that only hold numbered sub-senses
        if not s.explanations and not s.examples and s.subsenses:
            senses.extend(s.subsenses)
        elif s.explanations or s.examples or s.subsenses:
            senses.append(s)
    return senses


def fetch_entry(article_id: int, dictionary: str, concepts: dict) -> Entry:
    a = _get_json(f"/{dictionary}/article/{article_id}.json")
    lemma_objs = a.get("lemmas", [])
    pos, genders, forms = _parse_forms(lemma_objs)
    lemmas = []
    for lem in lemma_objs:
        if lem.get("lemma") and lem["lemma"] not in lemmas:
            lemmas.append(lem["lemma"])
    return Entry(article_id, lemmas, pos, genders, forms, _parse_senses(a.get("body", {}), concepts))


def lookup(word: str, dictionary: str = "bm", cache_dir: Path | None = None,
           max_entries: int = 6) -> list[Entry]:
    ids = search(word, dictionary)[:max_entries]
    if not ids:
        return []
    concepts = _concepts(dictionary, cache_dir)
    with ThreadPoolExecutor(max_workers=min(len(ids), 6)) as pool:
        return list(pool.map(lambda i: fetch_entry(i, dictionary, concepts), ids))
