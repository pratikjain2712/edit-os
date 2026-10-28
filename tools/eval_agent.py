"""Score the editing agent (edit_os/agent) against a copy editor's real output, as tools/eval_ce.py does.

Each article folder holds te.docx (style editor) and ce.docx (copy editor, built on te.docx). Per paragraph:
O = text with only the style editor's changes (the copy editor's starting point), T = the copy editor's result,
U = O with the agent's edits. The agent runs on a .docx built from the O paragraphs.

Reported per article: edits kept, precision (share of edits that, applied alone, bring the paragraph closer to T),
closure ((d(O,T) - d(U,T)) / d(O,T), token edit distance), paragraphs helped / hurt, queries and how many sit in
a paragraph where the copy editor left a comment (eval_ce's rule), cost (as billed when the calls were made;
cached reruns are free) and turns. Precision is also broken down by edit kind and confidence.

Usage: python tools/eval_agent.py DATA_DIR [--articles A,B] [--extra DIR,DIR] [--dump out.jsonl]
       [--model M] [--effort E] [--budget USD]
DATA_DIR: article folders (any journal); --extra adds single article folders. --dump (or EVAL_AGENT_DUMP) writes
every edit and query with context and the copy editor's verdict, for manual review. Paid: needs
EDITOS_ANTHROPIC_API_KEY; EDITOS_CE_EXAMPLES for the few-shot edits, EDITOS_STYLESHEETS for the stylesheet.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import eval_ce  # noqa: E402
from edit_os import agent, docmodel, style as style_mod  # noqa: E402
from edit_os.agent import context  # noqa: E402


def _comments_by_anchor(ce: bytes) -> list[tuple[str, str]]:
    """(comment text, anchored text) of every comment in the copy editor's file."""
    out = []
    for line in eval_ce.comments(ce).split("\n"):
        if not line.strip():
            continue
        text, _, anchor = line.rpartition(" [")
        out.append((text, anchor[:-1] if anchor.endswith("]") else anchor))
    return out


def evaluate(article_dir: str, dump=None) -> dict:
    name = os.path.basename(article_dir.rstrip("/"))
    with open(os.path.join(article_dir, "te.docx"), "rb") as f:
        te = f.read()
    with open(os.path.join(article_dir, "ce.docx"), "rb") as f:
        ce = f.read()
    paras = eval_ce.views(ce, eval_ce.authors(te))
    st = style_mod.load(eval_ce.journal_of(article_dir))
    st.settings["copyedit"]["agent"] = {**(st.settings["copyedit"].get("agent") or {}), "enabled": True}
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "o.docx")
        eval_ce.build_docx(paras, path)
        ms = docmodel.load(path)
        assert len(ms.paragraphs) == len(paras), (len(ms.paragraphs), len(paras))
        res = agent.run(ms, st, article=name)

    labels = {p.id: context.label(i) for i, p in enumerate(ms.paragraphs)}
    by_para = defaultdict(list)
    for c in res.changes.changes:
        by_para[c.para_id].append(c)
    r = Counter(edits=len(res.changes.changes), queries=len(res.queries))
    hits, proposed = Counter(), Counter()
    o_of = {}
    for p, (_, o, t, _) in zip(ms.paragraphs, paras):
        o_of[p.id] = (o, t)
        cs = by_para.get(p.id, [])
        d_o = eval_ce.distance(o, t)
        u = eval_ce.apply(o, cs)
        d_u = eval_ce.distance(u, t)
        u_c = eval_ce.apply(o, [c for c in cs if c.provenance.get("confidence") == "certain"])
        r["dUT_certain"] += eval_ce.distance(u_c, t)
        r["hurt_certain"] += eval_ce.distance(u_c, t) > d_o
        r["dOT"] += d_o
        r["dUT"] += d_u
        r["ce_paragraphs"] += o != t
        if u != o:
            r["helped"] += d_u < d_o
            r["hurt"] += d_u > d_o
        for c in cs:
            agree = eval_ce.closer(eval_ce.apply(o, [c]), o, t)
            kind = c.provenance.get("edit_kind", "other")
            conf = c.provenance.get("confidence", "")
            for key in ("all", "kind:" + kind, "confidence:" + conf):
                proposed[key] += 1
                hits[key] += agree
            if dump:
                dump.write(json.dumps({
                    "type": "edit", "article": name, "para": labels[p.id], "kind": kind, "confidence": conf,
                    "old": c.old, "new": c.new, "reason": c.reason,
                    "context": o[max(0, c.start - 150):c.end + 150], "at": c.start - max(0, c.start - 150),
                    "editor_agrees": agree, "editor_paragraph_changed": o != t,
                    "editor_text": t[max(0, c.start - 150):c.end + 200] if o != t else "",
                }, ensure_ascii=False) + "\n")
    comments = _comments_by_anchor(ce)
    for q in res.queries:
        o, _ = o_of.get(q.para_id, ("", ""))
        here = [(text, a) for text, a in comments if len(a.strip()) >= 3 and a.strip() in o]
        r["queries_matched"] += bool(here)
        # stricter: a comment anchored on text that overlaps or is within 100 characters of the query's anchor
        near = [text for text, a in here if any(abs(m.start() - q.start) <= 100 + len(a) for m in
                                                 re.finditer(re.escape(a.strip()), o))]
        r["queries_near"] += bool(near) and q.end > q.start
        if dump:
            dump.write(json.dumps({"type": "query", "article": name, "para": labels.get(q.para_id), "text": q.text,
                                   "anchor": o[q.start:q.end], "paragraph": o[:600],
                                   "editor_comments_here": [t for t, _ in here]}, ensure_ascii=False) + "\n")
    r["ce_comments"] = len(comments)
    r["turns"] = res.turns
    return dict(name=name, r=r, hits=hits, proposed=proposed, cost=res.cost, spent=res.spent,
                usage=dict(res.usage), drops=dict(res.drops), stop=res.stop)


def main(argv):
    for flag, env in (("--model", "EDITOS_AGENT_MODEL"), ("--effort", "EDITOS_AGENT_EFFORT"),
                      ("--budget", "EDITOS_AGENT_BUDGET")):
        if flag in argv:
            os.environ[env] = argv[argv.index(flag) + 1]
    dirs = sorted(d for d in glob.glob(os.path.join(argv[0], "*")) if os.path.exists(os.path.join(d, "ce.docx")))
    if "--extra" in argv:
        dirs += argv[argv.index("--extra") + 1].split(",")
    if "--articles" in argv:
        names = argv[argv.index("--articles") + 1].split(",")
        dirs = [d for d in dirs if os.path.basename(d.rstrip("/")) in names]
    dump_path = argv[argv.index("--dump") + 1] if "--dump" in argv else os.environ.get("EVAL_AGENT_DUMP")
    dump = open(dump_path, "a", encoding="utf-8") if dump_path else None
    tot, hits, proposed = Counter(), Counter(), Counter()
    cost = spent = 0.0
    print(f"{'article':16s} {'edits':>5s} {'prec':>6s} {'closure':>8s} {'help':>4s} {'hurt':>4s} {'queries':>7s} "
          f"{'q-match':>7s} {'q-near':>6s} {'cost $':>7s} turns stop")
    for d in dirs:
        out = evaluate(d, dump)
        r = out["r"]
        tot.update({k: v for k, v in r.items() if isinstance(v, int)})
        hits.update(out["hits"])
        proposed.update(out["proposed"])
        cost += out["cost"]
        spent += out["spent"]
        prec = out["hits"]["all"] / out["proposed"]["all"] if out["proposed"]["all"] else 0
        clos = (r["dOT"] - r["dUT"]) / r["dOT"] if r["dOT"] else 0
        print(f"{out['name']:16s} {r['edits']:5d} {prec:6.1%} {clos:8.2%} {r['helped']:4d} {r['hurt']:4d} "
              f"{r['queries']:7d} {r['queries_matched']:7d} {r['queries_near']:6d} {out['cost']:7.3f} {r['turns']:5d} {out['stop']}"
              f"   (CE comments {r['ce_comments']}, CE paragraphs {r['ce_paragraphs']}; certain only: closure "
              f"{(r['dOT'] - r['dUT_certain']) / max(1, r['dOT']):.2%}, hurt {r['hurt_certain']})")
        print(f"    usage {out['usage']}  refused {out['drops']}")
    n = max(1, len(dirs))
    clos_c = (tot["dOT"] - tot["dUT_certain"]) / tot["dOT"] if tot["dOT"] else 0
    clos = (tot["dOT"] - tot["dUT"]) / tot["dOT"] if tot["dOT"] else 0
    prec = hits["all"] / proposed["all"] if proposed["all"] else 0
    print(f"\nTOTAL articles {len(dirs)}  edits {tot['edits']}  precision {prec:.1%}  closure {clos:.2%}  helped "
          f"{tot['helped']} hurt {tot['hurt']}  queries {tot['queries']} matched {tot['queries_matched']} (near {tot['queries_near']})  "
          f"cost ${cost:.2f} (${cost / n:.2f}/article; paid this run ${spent:.2f})\n"
          f"certain edits only: closure {clos_c:.2%}  hurt {tot['hurt_certain']}")
    for k in sorted(proposed, key=lambda k: (k.split(":")[0], -proposed[k])):
        if k != "all":
            print(f"  {k:28s} proposed {proposed[k]:4d}  precision {hits[k] / proposed[k]:6.1%}")
    if dump:
        dump.close()


if __name__ == "__main__":
    main(sys.argv[1:])
