"""Score the tool against the final copy-edited file (`ce.docx`, the copy editor's tracked file).

Each article folder holds `original.zip` (the manuscript), `ce.docx` (the final copy-edited file:
tracked changes accepted = what the journal publishes) and, optionally, `te.docx` (the style
editor's tracked file, as a yardstick). For each article the pipeline runs on the original, the
suggestions are accepted (as Autopilot would, or all of them) and the clean copy is rendered. Then:

- rule precision: per rule, the accepted changes whose result (with a few words of context) is
  in the final text ("right") against those where the final text kept the original wording
  ("wrong"); neither or both = unclear. The context is our own edited paragraph (a caption set in
  sentence case word by word is judged against the whole lowered caption); --single judges each change
  alone in the original text (the older measure, misled where the final text rewrote the neighbours);
- bigram closure: a word-bigram multiset distance to the final text (order- and
  paragraph-split-independent; punctuation and case kept), separately for the running text and
  the reference list, and for the front matter (the running text before the abstract / main text). closure = share of the original->final differences our clean copy
  removes; with te.docx the style editor's own file is scored the same way.

Usage: python tools/eval_final.py ARTICLE_DIR... --journal jehp --out OUT
       [--accept autopilot|all] [--reuse] [--min N] [--examples RULE] [--narrow] [--pending] [--single]

--accept autopilot (default) accepts what Autopilot would (its `hold` list and the second check of
Claude's edits apply); all accepts every suggestion. --reuse scores the records already in OUT.
--examples RULE prints that rule's right/wrong changes with context (for tuning).
"""

from __future__ import annotations

import argparse
import collections
import glob
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from evaluate import tracked_pairs  # noqa: E402

TOK = re.compile(r"\w+|[^\w\s]")
HEAD = re.compile(r"<H\d>")


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", t.replace(" ", " ")).strip()


def final_text(docx_bytes: bytes) -> list[str]:
    """The paragraphs of a tracked file with every change accepted."""
    return [norm(a) for _, a in tracked_pairs(docx_bytes) if norm(a)]


def plain_paras(path: str) -> list[str]:
    from edit_os import docmodel

    return [norm(p.text) for p in docmodel.load(path).paragraphs if norm(p.text)]


# --- bigram closure -------------------------------------------------------------------------------

def grams(paras: list[str]) -> collections.Counter:
    c = collections.Counter()
    for p in paras:
        t = TOK.findall(HEAD.sub("", p))
        c.update(zip(t, t[1:]))
    return c


def gram_distance(a: collections.Counter, b: collections.Counter) -> int:
    return sum(((a - b) + (b - a)).values())


def split_refs(paras: list[str]) -> dict[str, list[str]]:
    """{'text': everything but the reference list, 'references': the list (to the first caption)}."""
    low = [HEAD.sub("", p).strip().lower() for p in paras]
    r = next((i for i, t in enumerate(low) if re.match(r"references?\b", t)), len(paras))
    end = next((i for i in range(r + 1, len(paras)) if re.match(r"(table|figure|fig\.?)\s*\d", low[i])), len(paras))
    return {"text": paras[:r] + paras[end:], "references": paras[r:end]}


_BODY = re.compile(r"(abstract|introduction|background|summary|dear\s+(?:editor|sir)|sir,|respected)\b", re.I)


def split_front(paras: list[str]) -> list[str]:
    """The front matter: the paragraphs before the abstract / main text (running head to cite line)."""
    low = [HEAD.sub("", p).strip() for p in paras]
    end = next((i for i, t in enumerate(low[:80]) if _BODY.match(t)), 0)
    return paras[:end]


def bigram_scores(orig: list[str], ours: list[str], ce: list[str], te: list[str] | None = None) -> dict:
    """Per section {'orig': d(original, CE), 'ours': d(ours, CE), 'te': d(style editor, CE) or None}; 'front' is
    the part of 'text' before the abstract / main text."""
    files = {"orig": orig, "ours": ours, "te": te}
    sp = {k: dict(split_refs(v), front=split_front(v)) for k, v in files.items() if v is not None}
    target = dict(split_refs(ce), front=split_front(ce))
    out = {}
    for sec in ("text", "references", "front"):
        g = grams(target[sec])
        out[sec] = {k: (gram_distance(grams(sp[k][sec]), g) if k in sp else None) for k in files}
    return out


def closure(d0: int, d1: int) -> float:
    return (d0 - d1) / d0 if d0 else 0.0


# --- rule precision ------------------------------------------------------------------------------

def rule_name(reason: str) -> str:
    return re.split(r"[ :(]", reason or "", maxsplit=1)[0]


def _nz(t: str) -> str:
    return norm(HEAD.sub("", t))


def _context(text: str, s: int, e: int, width: int) -> tuple[str, str]:
    """Whole words of `text` around s..e; a paragraph's start or end is marked with "\u00b6" (as between the
    final text's paragraphs), so a period added at the end is not found inside a longer sentence."""
    before, after = text[max(0, s - width):s], text[e:e + width]
    before = before.split(" ", 1)[-1] if " " in before and s > width else before
    after = after.rsplit(" ", 1)[0] if " " in after and e + width < len(text) else after
    if s <= width and not text[:max(0, s - len(before))].strip():
        before = "\u00b6 " + before
    if e + width >= len(text):
        after += " \u00b6"
    return before, after


def _verdict(old_s: str, new_s: str, ce_joined: str) -> str:
    hit_new, hit_old = new_s in ce_joined, old_s in ce_joined
    return "right" if hit_new and not hit_old else "wrong" if hit_old and not hit_new else "unclear"


def judge_change(change, text: str, ce_joined: str, width: int = 25, narrow: bool = False) -> tuple[str, str, str] | None:
    """One change on its own, in the original text's context: ('right' | 'wrong' | 'unclear', old in
    context, new in context), or None when it has no visible text (format only, or too short to look
    up). With `narrow`, an unclear change is looked up again with less context."""
    if change.format_only:
        return None
    if narrow:
        first = judge_change(change, text, ce_joined, width)
        if first is None or first[0] != "unclear":
            return first
        second = judge_change(change, text, ce_joined, 10)
        return second if second is not None and len(second[2]) >= 12 else first
    before, after = _context(text, change.start, change.end, width)
    new_s, old_s = _nz(before + change.new + after), _nz(before + change.old + after)
    if new_s == old_s or len(new_s) < 8:
        return None
    return _verdict(old_s, new_s, ce_joined), old_s, new_s


def judge_in_ours(changes, text: str, ce_joined: str, width: int = 25) -> list[tuple]:
    """Every change of one paragraph in the context of OUR edited paragraph (all of `changes` applied):
    right when our wording around it is in the final text, wrong when the same with this one change
    undone is. A caption set in sentence case word by word is judged against the whole lowered caption,
    not against its neighbours' original capitals. [(change, verdict, old in context, new in context)]."""
    from edit_os.tracked import PARAGRAPH_BREAK

    edits = sorted((c for c in changes if not c.format_only), key=lambda c: (c.start, c.end))
    kept, last = [], -1
    for c in edits:  # overlapping changes cannot both apply: the later one is judged on its own
        if c.start >= last:
            kept.append(c)
            last = c.end

    def br(t: str) -> str:
        return t.replace(PARAGRAPH_BREAK, " \u00b6 ")

    out, pos, mine, at = [], 0, "", {}
    for c in kept:
        mine += br(text[pos:c.start])
        at[id(c)] = len(mine)
        mine += br(c.new)
        pos = c.end
    mine += br(text[pos:])
    for c in edits:
        if c not in kept:
            j = judge_change(c, text, ce_joined, width)
            if j:
                out.append((c, *j))
            continue
        new, s = br(c.new), at[id(c)]
        before, after = _context(mine, s, s + len(new), width)
        new_s, old_s = _nz(before + new + after), _nz(before + c.old + after)
        if new_s == old_s or len(new_s) < 8:
            continue
        out.append((c, _verdict(old_s, new_s, ce_joined), old_s, new_s))
    return out


def rule_precision(changes, texts: dict[str, str], ce_paras: list[str], stat=None, examples=None,
                   only_rule: str | None = None, narrow: bool = False, tag: str = "", pending: bool = False,
                   single: bool = False):
    """Add the accepted `changes` to `stat` ({rule: [right, wrong, unclear]}); with `examples` (a list)
    and `only_rule`, also collect (verdict, old, new) for that rule. With `pending`, suggestions left for
    the editor count too (the precision a held rule would have if accepted). Each change is judged in our
    edited text (`judge_in_ours`); `single` judges it alone in the original text (`judge_change`)."""
    from edit_os.changes import Status

    stat = stat if stat is not None else collections.defaultdict(lambda: [0, 0, 0])
    ce_joined = _nz("\u00b6 " + " \u00b6 ".join(ce_paras) + " \u00b6")
    picked = [c for c in changes if c.status == Status.ACCEPTED or (pending and c.status == Status.SUGGESTED)]
    judged = []
    if single:
        for c in picked:
            j = judge_change(c, texts.get(c.para_id, ""), ce_joined, narrow=narrow)
            if j:
                judged.append((c, *j))
    else:
        by_para: dict[str, list] = {}
        for c in picked:
            by_para.setdefault(c.para_id, []).append(c)
        for pid, cs in by_para.items():
            judged += judge_in_ours(cs, texts.get(pid, ""), ce_joined)
    for c, verdict, old_s, new_s in judged:
        rule = rule_name(c.reason)
        stat[rule][("right", "wrong", "unclear").index(verdict)] += 1
        if examples is not None and only_rule and (rule == only_rule or rule.startswith(only_rule + ".")):
            examples.append((verdict, old_s, new_s, f"{tag} {c.reason}".strip()))
    return stat


# --- running the tool ----------------------------------------------------------------------------

def run_article(adir: str, journal: str, out: str, accept: str = "autopilot", reuse: bool = False) -> str:
    """Run the pipeline on `adir`/original.zip into `out`/<article>, accept and render the clean copy."""
    from edit_os import autopilot, pipeline, style as style_mod
    from edit_os.changes import Status

    od = os.path.join(out, os.path.basename(adir.rstrip("/")))
    if reuse and glob.glob(os.path.join(od, "*_clean.docx")):
        return od
    st = style_mod.load(journal)
    pipeline.edit(os.path.join(adir, "original.zip"), st, od, editor="Edit OS check")
    _, cs, ins, qd = pipeline.load_records(od)
    if accept == "all":
        acc = {c.id for c in list(cs.changes) + list(ins) if c.status == Status.SUGGESTED}
    else:
        texts = pipeline.paragraph_texts(pipeline.source_path(od))
        acc = set(autopilot.plan(list(cs.changes), ins, st, texts=texts).accept)
    for item in list(cs.changes) + list(ins):
        if item.id in acc:
            item.status = Status.ACCEPTED
    pipeline.save_records(od, cs, ins, qd)
    pipeline.render_outputs(od, include_pending=False, editor="Edit OS check")
    return od


def score_article(adir: str, od: str, stat, bigrams, examples=None, only_rule=None, narrow=False,
                  pending=False, single=False) -> dict:
    from edit_os import pipeline

    ce = final_text(open(os.path.join(adir, "ce.docx"), "rb").read())
    te_path = os.path.join(adir, "te.docx")
    te = final_text(open(te_path, "rb").read()) if os.path.exists(te_path) else None
    src = pipeline.source_path(od)
    orig = plain_paras(src)
    ours = plain_paras(glob.glob(os.path.join(od, "*_clean.docx"))[0])
    _, cs, _, _ = pipeline.load_records(od)
    rule_precision(cs.changes, pipeline.paragraph_texts(src), ce, stat, examples, only_rule, narrow,
                   os.path.basename(adir.rstrip("/")), pending, single)
    b = bigram_scores(orig, ours, ce, te)
    for sec, d in b.items():
        for k, v in d.items():
            if v is not None:
                bigrams[sec][k] += v
        bigrams[sec]["has_te"] = bigrams[sec].get("has_te", True) and d["te"] is not None
    return b


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--journal", default="jehp")
    ap.add_argument("--out", required=True)
    ap.add_argument("--accept", choices=["autopilot", "all"], default="autopilot")
    ap.add_argument("--reuse", action="store_true")
    ap.add_argument("--min", type=int, default=3, help="smallest right+wrong count to list a rule")
    ap.add_argument("--examples", help="print this rule's judged changes")
    ap.add_argument("--narrow", action="store_true", help="look an unclear change up again with less context")
    ap.add_argument("--pending", action="store_true", help="rule precision also over suggestions left pending")
    ap.add_argument("--single", action="store_true", help="judge every change alone (no clusters)")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    stat = collections.defaultdict(lambda: [0, 0, 0])
    bigrams = collections.defaultdict(collections.Counter)
    examples: list = []
    for d in a.dirs:
        name = os.path.basename(d.rstrip("/"))
        try:
            od = run_article(d, a.journal, a.out, a.accept, a.reuse)
            b = score_article(d, od, stat, bigrams, examples, a.examples, a.narrow, a.pending, a.single)
        except Exception as e:  # one broken article does not stop the run
            print(f"ERR {name}: {e!r}"[:300], flush=True)
            continue
        print(f"{name:16s} " + "  ".join(
            f"{sec}: ours {closure(v['orig'], v['ours']):6.1%}"
            + (f" editor {closure(v['orig'], v['te']):6.1%}" if v["te"] is not None else "")
            for sec, v in b.items()), flush=True)
    print()
    for sec, v in bigrams.items():
        line = f"TOTAL {sec:10s} differences original->CE {v['orig']:6d} | ours removes {closure(v['orig'], v['ours']):6.1%}"
        if v.get("has_te"):
            line += f" | style editor's file removes {closure(v['orig'], v['te']):6.1%}"
        print(line)
    print(f"\n{'rule':34s} {'right':>6s} {'wrong':>6s} {'unclear':>8s}  precision")
    for r, (ok, bad, unc) in sorted(stat.items(), key=lambda kv: -(kv[1][0] + kv[1][1])):
        if ok + bad >= a.min:
            print(f"{r:34s} {ok:6d} {bad:6d} {unc:8d}  {ok / (ok + bad):6.0%}")
    tot_ok, tot_bad = sum(v[0] for v in stat.values()), sum(v[1] for v in stat.values())
    if tot_ok + tot_bad:
        print(f"{'all':34s} {tot_ok:6d} {tot_bad:6d} {sum(v[2] for v in stat.values()):8d}  "
              f"{tot_ok / (tot_ok + tot_bad):6.0%}")
    if a.examples:
        print(f"\nexamples for {a.examples}:")
        for verdict, old, new, reason in examples:
            print(f"  [{verdict:7s}] {old!r}\n            -> {new!r}   ({reason[:100]})")


if __name__ == "__main__":
    main()
