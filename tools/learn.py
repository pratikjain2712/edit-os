"""Learning logic for tools/daily_learn.py: deterministic, explainable decisions over per-article metrics.

Everything here is a pure function of the per-article result records daily_learn.py writes (see `RESULT`), so it is
unit-tested on synthetic numbers with no network or manuscripts:

- `article_summary` / `journal_metrics`: scores per journal (style closure, bigram closure vs the final file,
  references, front matter, rule precision, QC);
- `autopilot_decisions`: rules to hold from Autopilot (precision < hold_below on >= min_judged judged edits over the
  window) and rules to release (a hold this tool added whose precision recovered above release_above). The rules stay
  on either way: a held rule is still suggested, the editor decides. Holds the owner set are never released here;
- `proposals`: editor edits the tool does not make (text edit patterns, formatting changes, final-file bigrams), with
  consistency counts; reported only, never applied;
- `owner_questions`: stylesheet rules nearly all editors go against (the stylesheet stays; the owner confirms);
- `regressions`: metric drops against the previous run beyond noise, QC-blocked articles, crashes;
- `render_markdown`: the report (no manuscript sentence: examples are masked snippets of at most `MAX_WORDS` words);
- `patch_hold_yaml`: the `autopilot.hold` change applied to a journal style file (comments kept).

RESULT (one article, as daily_learn.evaluate_article writes it):
    article, journal, date, commit, seconds, crashes [{step, error}],
    style {dOT, dUT, helped, hurt, matched, paragraphs, changes}      tools/evaluate.py --join-paragraphs
    bigrams {text|references|front: {orig, ours, te}}                  tools/eval_final.py (vs ce.docx)
    rules {key: [right, wrong, unclear]}  rules_autopilot {...}        eval_final judging (all suggestions / accepted)
    examples {key: {right: [snippet], wrong: [snippet]}}
    patterns {key: [missed, applied, kept, example]}                   editor-only text edits (te.docx)
    missed_bigrams {bigram: n}                                         final-file bigrams the editor added, we did not
    format_editor {label@ctx: [n, hit]}                                tools/eval_format.py (editor's formatting)
    refs {refs, dOT, dUT, exact_before, exact_after}                   tools/eval_refs.py
    front {field: [n, exact, similarity]}                              tools/eval_frontmatter.py
    qc {passed, errors, warnings, failed {check: severity}}            QC report (qc.check incl. content.*)
    usage {calls, input_tokens, output_tokens, cost}
"""

from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict

DEFAULTS = {
    "window": 30,            # last N evaluated (not held-out) articles per journal for rule precision / proposals
    "min_judged": 20,        # right + wrong edits before a hold / release is proposed
    "hold_below": 0.8,       # precision under this -> hold from autopilot
    "release_above": 0.9,    # a hold this tool added, precision back above this -> release
    "conflict_below": 0.2,   # stylesheet rule with precision under this: nearly all editors differ -> owner question
    "min_articles": 3,       # proposals: seen in at least this many articles
    "min_missed": 5,         # proposals: the editor did it (and we did not) at least this often
    "min_consistency": 0.7,  # proposals: where the editors did it, share of the occurrences they changed
    "noise": 0.01,           # regression: a held-out metric dropping by more than this (absolute)
    "top": 10,               # list lengths in the report
}

MAX_WORDS = 8
NON_SHEET = ("claude", "languagetool", "spelling", "ref_lookup")  # not stylesheet rules: tool heuristics / lookups
LEARNER_MARK = "# daily_learn holds"

_TOK = re.compile(r"\w+|\s+|[^\w\s]")
_SMALL = {"a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "by", "with", "from", "as", "is",
          "was", "were", "are", "be", "et", "al", "vs", "per", "not", "no", "than", "that", "this", "these", "who"}


# --- keys and masking ------------------------------------------------------------------------------

def rule_name(reason: str) -> str:
    return re.split(r"[ :(]", reason or "", maxsplit=1)[0]


def hold_key(reason: str) -> str:
    """The `autopilot.hold` entry that matches this suggestion's reason (autopilot._held: equal, or followed by
    ':' '.' ' '): the rule name ("fig_captions.case", "claude.grammar"), or the first two words of a prose reason
    ("use abbreviation after its definition" -> "use abbreviation")."""
    r = (reason or "").strip()
    key = rule_name(r)
    first = r.split(" ", 1)[0]
    words = r.split(" ")
    if key == first and len(words) > 1 and not re.search(r"[._]", first) and re.fullmatch(r"[a-z]+", words[1]):
        key = " ".join(words[:2])
    ok = r == key or r.startswith((key + ":", key + ".", key + " "))
    return key if ok and key else r


def mask_token(t: str) -> str:
    """A token as a pattern sees it: numbers 9, lowercase words kept, names / capitalised words 'Aa', acronyms
    'AA', whitespace ' '."""
    if t.isspace():
        return " "
    if re.fullmatch(r"\d+", t):
        return "9"
    if re.fullmatch(r"[^\W\d_]+", t):
        if t.islower():
            return t if len(t) <= 14 else "w"
        if t.isupper() and len(t) > 1:
            return "AA"
        return "Aa"
    if re.search(r"\d", t):
        return re.sub(r"\d+", "9", t)
    return t


def mask_context(t: str) -> str:
    """A context token, coarser: small function words kept, other words by shape."""
    m = mask_token(t)
    if m.isalpha() and m.islower() and m not in _SMALL:
        return "w"
    return m


def tokens(s: str) -> list[str]:
    return _TOK.findall(s or "")


def snippet(text: str, max_words: int = MAX_WORDS) -> str:
    """At most `max_words` words of `text`, masked (numbers, names, e-mails, links): safe for the report."""
    text = re.sub(r"\S+@\S+", "<email>", text or "")
    text = re.sub(r"https?://\S+|www\.\S+", "<url>", text)
    words = text.split()
    words = words[:max_words]
    out = []
    for i, w in enumerate(words):
        w = re.sub(r"\d", "9", w)
        core = re.sub(r"^\W+|\W+$", "", w)
        if i and len(core) > 3 and core[:1].isupper() and core.lower() not in _SMALL:
            w = w.replace(core, "Aa" if not core.isupper() else "AA")
        out.append(w)
    return " ".join(out)


def change_snippet(old_s: str, new_s: str, max_words: int = MAX_WORDS) -> str:
    """'old -> new' around the changed words only (a couple of words of context), masked."""
    a, b = old_s.split(), new_s.split()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ops = [op for op in sm.get_opcodes() if op[0] != "equal"]
    if not ops:
        return snippet(new_s, max_words)
    i1, i2, j1, j2 = ops[0][1], ops[-1][2], ops[0][3], ops[-1][4]
    half = max(1, (max_words - max(i2 - i1, j2 - j1)) // 2)
    old_part = " ".join(a[max(0, i1 - half):i2 + half])
    new_part = " ".join(b[max(0, j1 - half):j2 + half])
    return f"{snippet(old_part, max_words)} -> {snippet(new_part, max_words)}"


# --- editor text patterns ------------------------------------------------------------------------

SEP = "␟"  # between the parts of a pattern key: left context, old, new, right context


def edit_ops(o: str, t: str, max_len: int = 4) -> Counter:
    """The small edits that turn paragraph `o` into `t`, as pattern keys (left, old, new, right): masked tokens,
    one coarse context token each side ('¶' at the paragraph's ends)."""
    a, b = tokens(o), tokens(t)
    ma, mb = [mask_token(x) for x in a], [mask_token(x) for x in b]
    out: Counter = Counter()
    sm = difflib.SequenceMatcher(None, ma, mb, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" or i2 - i1 > max_len or j2 - j1 > max_len:
            continue
        old, new = "".join(ma[i1:i2]), "".join(mb[j1:j2])
        if old == new:
            continue
        left = mask_context(a[i1 - 1]) if i1 > 0 else "¶"
        right = mask_context(a[i2]) if i2 < len(a) else "¶"
        out[SEP.join((left, old, new, right))] += 1
    return out


def occurrences(o: str, key: str) -> int:
    """How often the pattern's starting point (left, old, right) occurs in paragraph `o`."""
    left, old, _, right = key.split(SEP)
    seq = [left] + [mask_token(x) for x in tokens(old)] + [right]
    a = ["¶"] + [mask_token(x) for x in tokens(o)] + ["¶"]
    ctx = ["¶"] + [mask_context(x) for x in tokens(o)] + ["¶"]
    n, k = 0, len(seq)
    for i in range(len(a) - k + 1):
        if ctx[i] == seq[0] and ctx[i + k - 1] == seq[-1] and a[i + 1:i + k - 1] == seq[1:-1]:
            n += 1
    return n


def paragraph_patterns(o: str, t: str, u: str) -> dict[str, list[int]]:
    """{key: [missed, applied, kept]} for one paragraph: original `o`, the editor's `t`, ours `u`. missed: the
    editor's edits we did not make; kept: places the same edit could have been made and the editor left."""
    ed, ours = edit_ops(o, t), edit_ops(o, u)
    out = {}
    for key, n in ed.items():
        out[key] = [max(0, n - ours.get(key, 0)), n, max(0, occurrences(o, key) - n)]
    return out


def pattern_label(key: str) -> str:
    left, old, new, right = key.split(SEP)
    show = lambda s: "␣" if s == " " else s.replace(" ", "␣") if s else "∅"  # noqa: E731
    return f"{left}[{show(old)} -> {show(new)}]{right}"


# --- per-article and per-journal numbers ----------------------------------------------------------

def closure(d0: float, d1: float) -> float | None:
    return (d0 - d1) / d0 if d0 else None


def _add_rules(into: dict, rules: dict) -> None:
    for k, v in (rules or {}).items():
        cur = into.setdefault(k, [0, 0, 0])
        for i in range(3):
            cur[i] += v[i]


def journal_metrics(results: list[dict]) -> dict:
    """Totals and scores over a journal's article results (crashed steps simply contribute nothing)."""
    m: dict = {"articles": len(results)}
    st = Counter()
    for r in results:
        st.update({k: v for k, v in (r.get("style") or {}).items() if isinstance(v, (int, float))})
    m["style_closure"] = closure(st["dOT"], st["dUT"])
    for sec in ("text", "references", "front"):
        o = sum((r.get("bigrams") or {}).get(sec, {}).get("orig") or 0 for r in results)
        u = sum((r.get("bigrams") or {}).get(sec, {}).get("ours") or 0 for r in results)
        m[f"{sec}_closure"] = closure(o, u)
    rf = Counter()
    for r in results:
        rf.update(r.get("refs") or {})
    m["refs_identical"] = rf["exact_after"] / rf["refs"] if rf["refs"] else None
    m["refs_closure"] = closure(rf["dOT"], rf["dUT"])
    n = sim = 0.0
    for r in results:
        for v in (r.get("front") or {}).values():
            n += v[0]
            sim += v[2]
    m["front_similarity"] = sim / n if n else None
    rules: dict = {}
    for r in results:
        _add_rules(rules, r.get("rules_autopilot"))
    right, wrong = sum(v[0] for v in rules.values()), sum(v[1] for v in rules.values())
    m["rule_precision"] = right / (right + wrong) if right + wrong else None
    m["qc_blocked"] = sum(1 for r in results if r.get("qc") and not r["qc"].get("passed"))
    m["qc_failed"] = dict(Counter(c for r in results for c, sev in ((r.get("qc") or {}).get("failed") or {}).items()
                                  if sev == "error"))
    m["crashes"] = sum(1 for r in results if r.get("crashes"))
    return m


METRICS = ["style_closure", "text_closure", "references_closure", "front_closure", "refs_identical",
           "front_similarity", "rule_precision"]
LABELS = {"style_closure": "style", "text_closure": "text", "references_closure": "refs", "front_closure": "front",
          "refs_identical": "refs =", "front_similarity": "FM sim", "rule_precision": "rule prec"}


def window(results: list[dict], n: int) -> list[dict]:
    """The latest `n` article results (by the article's date, then evaluation time)."""
    return sorted(results, key=lambda r: (r.get("date") or "", r.get("evaluated_at") or ""))[-n:]


def rule_stats(results: list[dict], key: str = "rules") -> dict[str, list[int]]:
    out: dict = {}
    for r in results:
        _add_rules(out, r.get(key))
    return out


def precision(v) -> float | None:
    return v[0] / (v[0] + v[1]) if v[0] + v[1] else None


# --- autopilot hold / release ---------------------------------------------------------------------

def held(key: str, hold) -> bool:
    """Same matching as autopilot._held: is a suggestion with reason `key` held by `hold`?"""
    return any(key == h or key.startswith((h + ":", h + ".", h + " ")) for h in hold or [])


def autopilot_decisions(stats: dict[str, list[int]], hold: list[str], learner: list[str], pinned=(),
                        cfg: dict | None = None, examples: dict | None = None) -> dict:
    """{add: [...], release: [...], owner_release: [...], evidence: {key: {...}}} for one journal.
    add: rules under hold_below on >= min_judged edits not already held; release: holds this tool added (`learner`)
    back above release_above; owner_release: holds the owner set that now score above release_above (report only).
    `pinned` rules are never changed (the owner decided)."""
    c = {**DEFAULTS, **(cfg or {})}
    out = {"add": [], "release": [], "owner_release": [], "evidence": {}}

    def ev(key, v, why):
        e = {"right": v[0], "wrong": v[1], "unclear": v[2], "precision": round(precision(v), 3), "why": why}
        if examples and key in examples:
            e["examples"] = examples[key]
        out["evidence"][key] = e

    for key, v in sorted(stats.items()):
        p, judged = precision(v), v[0] + v[1]
        if p is None or judged < c["min_judged"] or key in pinned or any(held(key, [x]) for x in pinned):
            continue
        if key in learner and p > c["release_above"]:
            out["release"].append(key)
            ev(key, v, f"precision {p:.0%} > {c['release_above']:.0%} on {judged} judged edits")
        elif key in hold and key not in learner and p > c["release_above"]:
            out["owner_release"].append(key)
            ev(key, v, f"held by the owner; precision now {p:.0%} on {judged} judged edits")
        elif p < c["hold_below"] and not held(key, hold):
            out["add"].append(key)
            ev(key, v, f"precision {p:.0%} < {c['hold_below']:.0%} on {judged} judged edits")
    return out


def new_hold(hold: list[str], add, release) -> list[str]:
    return [h for h in hold if h not in set(release)] + [a for a in add if a not in hold]


def is_sheet_rule(key: str) -> bool:
    return not key.startswith(NON_SHEET)


def owner_questions(stats: dict[str, list[int]], journal: str, cfg: dict | None = None,
                    sheet_rule=is_sheet_rule) -> list[dict]:
    """Stylesheet rules nearly all editors go against (precision < conflict_below on >= min_judged): the tool keeps
    following the stylesheet; the owner confirms whether the rule is still wanted."""
    c = {**DEFAULTS, **(cfg or {})}
    out = []
    for key, v in sorted(stats.items(), key=lambda kv: -(kv[1][0] + kv[1][1])):
        p, judged = precision(v), v[0] + v[1]
        if p is not None and judged >= c["min_judged"] and p < c["conflict_below"] and sheet_rule(key):
            out.append({"journal": journal, "rule": key, "kept_original": v[1], "judged": judged,
                        "question": f"{journal}: the stylesheet rule `{key}` is applied, but the final files keep the "
                                    f"author's wording in {v[1]} of {judged} cases. Is the rule still wanted?"})
    return out


# --- proposals --------------------------------------------------------------------------------------

def proposals(results: list[dict], journal: str, cfg: dict | None = None) -> list[dict]:
    """Editor edits the tool does not make, consistent across articles. Never applied: listed for review."""
    c = {**DEFAULTS, **(cfg or {})}
    n_art = len(results)
    out = []
    pat: dict = defaultdict(lambda: [0, 0, 0, 0, None])  # missed, applied, kept, articles, example
    for r in results:
        for key, v in (r.get("patterns") or {}).items():
            p = pat[key]
            p[0] += v[0]
            p[1] += v[1]
            p[2] += v[2]
            p[3] += v[0] > 0
            p[4] = p[4] or (v[3] if len(v) > 3 else None)
    for key, (missed, applied, kept, arts, ex) in pat.items():
        cons = applied / (applied + kept) if applied + kept else 0
        if missed >= c["min_missed"] and arts >= c["min_articles"] and cons >= c["min_consistency"]:
            out.append({"kind": "text", "journal": journal, "pattern": pattern_label(key), "missed": missed,
                        "articles": arts, "of": n_art, "consistency": round(cons, 3), "applied": applied,
                        "kept": kept, "example": ex})
    fmt: dict = defaultdict(lambda: [0, 0, 0])  # editor count, tool matched, articles
    for r in results:
        for lab, (n, hit) in (r.get("format_editor") or {}).items():
            f = fmt[lab]
            f[0] += n
            f[1] += hit
            f[2] += n - hit > 0
    for lab, (n, hit, arts) in fmt.items():
        if n - hit >= c["min_missed"] and arts >= c["min_articles"] and hit / n < 0.5:
            out.append({"kind": "format", "journal": journal, "pattern": lab, "missed": n - hit, "articles": arts,
                        "of": n_art, "consistency": round(arts / n_art, 3) if n_art else 0, "applied": n,
                        "kept": None, "example": None})
    big: dict = defaultdict(lambda: [0, 0])
    for r in results:
        for g, n in (r.get("missed_bigrams") or {}).items():
            big[g][0] += n
            big[g][1] += 1
    for g, (n, arts) in big.items():
        if n >= c["min_missed"] and arts >= c["min_articles"]:
            out.append({"kind": "bigram", "journal": journal, "pattern": g, "missed": n, "articles": arts,
                        "of": n_art, "consistency": round(arts / n_art, 3) if n_art else 0, "applied": None,
                        "kept": None, "example": None})
    out.sort(key=lambda p: (-p["articles"], -p["missed"]))
    return out


# --- regressions ------------------------------------------------------------------------------------

def regressions(today: dict, previous: dict | None, journal: str, cfg: dict | None = None,
                same_set: bool = True) -> list[dict]:
    """Held-out metrics that dropped by more than `noise` since the previous run (same held-out set), and more
    blocked articles / crashes."""
    c = {**DEFAULTS, **(cfg or {})}
    out = []
    if previous and same_set:
        for k in METRICS:
            a, b = previous.get(k), today.get(k)
            if a is not None and b is not None and a - b > c["noise"]:
                out.append({"journal": journal, "kind": "metric", "metric": k, "before": round(a, 4),
                            "after": round(b, 4), "what": f"{journal} held-out {LABELS[k]}: {a:.1%} -> {b:.1%}"})
        if today.get("qc_blocked", 0) > previous.get("qc_blocked", 0):
            out.append({"journal": journal, "kind": "qc", "what": f"{journal} held-out QC-blocked articles "
                        f"{previous.get('qc_blocked', 0)} -> {today['qc_blocked']}"})
    if today.get("crashes"):
        out.append({"journal": journal, "kind": "crash", "what": f"{journal}: {today['crashes']} article(s) with "
                    "a crashed step"})
    return out


# --- the report -----------------------------------------------------------------------------------

def _pct(x) -> str:
    return "-" if x is None else f"{x:.1%}"


def _delta(a, b) -> str:
    if a is None or b is None:
        return ""
    d = (a - b) * 100
    return f" ({d:+.1f})" if abs(d) >= 0.05 else ""


def render_markdown(rep: dict) -> str:
    """The daily report. `rep` is the JSON report daily_learn.py writes (see there)."""
    c = rep.get("config", DEFAULTS)
    L = [f"# Daily learning {rep['date']} (commit {rep['commit'][:10]})", ""]
    u = rep.get("usage", {})
    L.append(f"New articles evaluated: {rep.get('new_count', 0)} | held-out: {rep.get('heldout_count', 0)} | "
             f"stored today: {rep.get('stored_count', 0)} | queued: {rep.get('queued_count', 0)} | "
             f"crashes: {rep.get('crash_count', 0)} | run time {rep.get('seconds', 0) / 60:.1f} min | "
             f"Claude {u.get('calls', 0)} calls ~${u.get('cost', 0):.2f}")
    L.append("")
    cols = METRICS + ["qc_blocked"]
    head = "| journal | set | n | " + " | ".join(LABELS.get(k, "QC blocked") for k in cols) + " |"
    L += ["## Scores per journal", "", "Held-out: a fixed set per journal, the trend is against the previous run "
          "(in points). New: today's articles. Closures are the share of the original->final differences removed.",
          "", head, "|" + "---|" * (len(cols) + 3)]
    for j, js in sorted(rep.get("journals", {}).items(), key=lambda kv: kv[1].get("priority", 99)):
        for setname in ("heldout", "new"):
            m = js.get(setname)
            if not m or not m.get("articles"):
                continue
            prev = js.get("heldout_previous") if setname == "heldout" else None
            cells = [(_pct(m.get(k)) + (_delta(m.get(k), prev.get(k)) if prev else "")) for k in METRICS]
            cells.append(str(m.get("qc_blocked", 0)))
            L.append(f"| {j} | {setname} | {m['articles']} | " + " | ".join(cells) + " |")
    L.append("")
    regs = rep.get("regressions", [])
    L += ["## Regressions", ""] + ([f"- {r['what']}" for r in regs] or ["- none"]) + [""]
    L += ["## Autopilot hold / release (safe actions: `daily_learn.py apply-safe`)", ""]
    any_ap = False
    for j, a in sorted(rep.get("autopilot", {}).items()):
        for verb in ("add", "release", "owner_release"):
            for key in a.get(verb, []):
                any_ap = True
                e = a["evidence"].get(key, {})
                what = {"add": "hold", "release": "release", "owner_release": "owner may release"}[verb]
                L.append(f"- **{j}** {what} `{key}`: right {e.get('right')} / wrong {e.get('wrong')} "
                         f"({e.get('why', '')})")
                for ex in (e.get("examples") or {}).get("wrong", [])[:1]:
                    L.append(f"  - kept by the editors: {ex}")
    if not any_ap:
        L.append("- no change")
    L += ["", "The rules stay on: a held rule is still suggested and the editor decides.", ""]
    L += ["## New rule proposals (never applied automatically)", ""]
    props = rep.get("proposals", [])[: c.get("top", 10)]
    for p in props:
        cons = f", consistency {p['consistency']:.0%}" if p.get("consistency") is not None else ""
        ex = f" e.g. {p['example']}" if p.get("example") else ""
        L.append(f"- **{p['journal']}** {p['kind']} `{p['pattern']}`: editors {p['missed']}x, tool 0, in "
                 f"{p['articles']}/{p['of']} articles{cons}.{ex}")
    if not props:
        L.append("- none with enough evidence yet")
    L += ["", "## Owner questions", ""]
    qs = rep.get("owner_questions", [])
    L += [f"- {q['question']}" for q in qs] or ["- none"]
    L.append("")
    crashes = rep.get("crashes", [])
    if crashes:
        L += ["## Crashes", ""] + [f"- {x['article']}: {x['step']} {x['error']}" for x in crashes] + [""]
    qc = rep.get("qc_blocked", [])
    if qc:
        L += ["## QC-blocked articles (new)", ""] + [f"- {x['article']}: {', '.join(x['checks']) or 'errors'}"
                                                     for x in qc] + [""]
    return "\n".join(L).rstrip() + "\n"


# --- the style file -------------------------------------------------------------------------------

def learner_holds(yaml_text: str) -> list[str]:
    """The holds this tool added, from the marker comment in the journal style's autopilot block."""
    m = re.search(rf"^\s*{re.escape(LEARNER_MARK)}[^:\n]*:\s*(.*)$", yaml_text, re.M)
    return [x.strip() for x in m.group(1).split(",") if x.strip()] if m and m.group(1).strip() else []


def _flow(items: list[str], indent: int, width: int = 116) -> str:
    """`hold: [a, b, ...]` wrapped like the hand-written style files."""
    lines, cur = [], " " * indent + "hold: ["
    pad = " " * (len(cur))
    for i, it in enumerate(items):
        piece = it + (", " if i < len(items) - 1 else "]")
        if len(cur) + len(piece.rstrip()) > width and not cur.endswith("["):
            lines.append(cur.rstrip())
            cur = pad
        cur += piece
    if not items:
        cur += "]"
    lines.append(cur)
    return "\n".join(lines)


def patch_hold_yaml(text: str, hold: list[str], learner: list[str]) -> str:
    """The journal style file with `autopilot.hold` set to `hold` and the marker comment listing `learner` (the
    holds this tool added). Everything else, comments included, is kept. Adds an `autopilot:` block if none."""
    mark = f"  {LEARNER_MARK} (autopilot only; the rules stay on; tools/daily_learn.py): {', '.join(learner)}"
    lines = text.split("\n")
    top = next((i for i, ln in enumerate(lines) if re.match(r"^autopilot:\s*(#.*)?$", ln)), None)
    if top is None:
        body = text.rstrip("\n") + "\n\nautopilot:\n" + (mark + "\n" if learner else "") + _flow(hold, 2) + "\n"
        return body
    end = next((i for i in range(top + 1, len(lines)) if lines[i] and not lines[i].startswith((" ", "#"))),
               len(lines))
    block = lines[top + 1:end]
    block = [ln for ln in block if LEARNER_MARK not in ln]
    h = next((i for i, ln in enumerate(block) if re.match(r"^\s+hold:", ln)), None)
    if h is not None:
        indent = len(block[h]) - len(block[h].lstrip())
        stop = h
        if "[" in block[h]:
            while "]" not in block[stop].split("#")[0]:
                stop += 1
        else:  # block list: "hold:" then "- item" lines
            while stop + 1 < len(block) and re.match(r"^\s+-\s", block[stop + 1]):
                stop += 1
        block[h:stop + 1] = ([mark] if learner else []) + _flow(hold, indent).split("\n")
    else:
        block = ([mark] if learner else []) + _flow(hold, 2).split("\n") + block
    return "\n".join(lines[:top + 1] + block + lines[end:])
