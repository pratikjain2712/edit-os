"""The agent's tools and the code that checks everything the agent proposes."""

from __future__ import annotations

import re
from collections import Counter
from itertools import count

from .. import provenance, references
from ..changes import Change
from ..copyedit import claude_pass
from ..docmodel import Manuscript, Paragraph
from ..queries import Query
from ..style import JournalStyle

KINDS = ["grammar", "punctuation", "article", "agreement", "word_choice", "rewording", "consistency",
         "capitalisation", "hyphenation", "spelling", "other"]


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


_S = {"type": "string"}
TOOLS = [
    {
        "name": "read_paragraphs",
        "description": "Exact current text of paragraphs P<from>..P<to> (inclusive, at most 40), with their tags.",
        "input_schema": _obj({"from_id": _S, "to_id": _S}),
    },
    {
        "name": "search_text",
        "description": "Find a word or phrase (case-insensitive, plain text, not a regex) in the whole manuscript. "
                       "Returns up to 30 hits: paragraph id and the text around each hit. Use it to check how a "
                       "term is written elsewhere or where something is mentioned.",
        "input_schema": _obj({"text": _S}),
    },
    {
        "name": "propose_edits",
        "description": "Propose copy edits. Each edit replaces `old` (copied exactly from paragraph `paragraph_id`, "
                       "unique there) with `new`. Every edit is checked in code and answered kept / refused with "
                       "the reason. Send many edits in one call.",
        "input_schema": _obj({"edits": {"type": "array", "items": _obj({
            "paragraph_id": _S, "old": _S, "new": _S,
            "kind": {"type": "string", "enum": KINDS},
            "reason": _S,
            "confidence": {"type": "string", "enum": ["certain", "likely"]},
        })}}),
    },
    {
        "name": "add_queries",
        "description": "Add author queries. `anchor` is a short exact phrase of the paragraph the query is about; "
                       "`text` is the query to the author. A query naming a reference number is refused until "
                       "check_reference has been called for that number.",
        "input_schema": _obj({"queries": {"type": "array", "items": _obj({
            "paragraph_id": _S, "anchor": _S, "text": _S})}}),
    },
    {
        "name": "check_reference",
        "description": "Reference n of the reference list: the entry as written, its first author and year as parsed "
                       "from the entry, the result of the code's bibliographic lookup (PubMed / CrossRef record "
                       "matched on title, authors and year; 'not verified' when none matched), and the paragraphs "
                       "that cite it. The only source you may use for statements about a reference.",
        "input_schema": _obj({"number": {"type": "integer"}}),
    },
]
for _t in TOOLS:
    _t["strict"] = True

_ids = count(1)
# Words a split sentence may gain ("..., this was" -> ". This was") and capitalised sentence openers that are no name.
_LINK_WORDS = {"and", "but", "so", "this", "these", "it", "which", "that", "they", "however", "the", "was", "were",
               "is", "are", "also", "thus", "therefore", "there"}
_OPENERS = set("""the a an this these those that in on at however although though it its there we our they their
he she his her as for of to with by from after before during while when since because both all most some many
further furthermore moreover also thus therefore hence similarly additionally overall here among between each no
not only one such other another several few patients participants results conclusion conclusions""".split())
_SENTENCE_BREAK = re.compile(r"[.?!][\"”’)]?\s+(?=[A-Z])")
_REF_NUMBER = re.compile(r"\b(?:ref(?:erence)?s?\.?|citation)\s*(?:no\.?\s*)?\[?(\d{1,3})", re.I)
_CITE = re.compile(r"\[(\d{1,3}(?:\s*[,–—\-]\s*\d{1,3})*)\]|\((\d{1,3}(?:\s*[,–—\-]\s*\d{1,3})*)\)")


def _numbers(group: str) -> set[int]:
    out = set()
    for part in re.split(r"\s*,\s*", group.strip()):
        m = re.fullmatch(r"(\d{1,3})\s*[–—\-]\s*(\d{1,3})", part)
        if m and int(m.group(1)) < int(m.group(2)) <= int(m.group(1)) + 50:
            out.update(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.isdigit():
            out.add(int(part))
    return out


def cited_numbers(text: str, superscript: list[tuple[int, int]] = ()) -> set[int]:
    """Reference numbers cited in a paragraph: [1,3-5], (2) and superscript runs."""
    out = set()
    for m in _CITE.finditer(text):
        out |= _numbers(m.group(1) or m.group(2))
    for s, e in superscript or ():
        span = text[s:e].strip()
        if re.fullmatch(r"[\d,–—\-\s]+", span or "x"):
            out |= _numbers(span)
    return out


class Workspace:
    """The manuscript as the agent sees it, and everything it has proposed (kept or refused)."""

    def __init__(self, ms: Manuscript, style: JournalStyle, ids: dict[str, Paragraph], editable: set[str],
                 cfg: dict, resolver=None):
        self.ms, self.style, self.ids, self.editable, self.cfg = ms, style, ids, editable, cfg
        self.label = {p.id: pid for pid, p in ids.items()}
        self.changes: list[Change] = []
        self.queries: list[Query] = []
        self.log: list[dict] = []  # every proposal with its verdict
        self.drops: Counter = Counter()
        self.checked_refs: set[int] = set()
        self._refs = None
        self.resolver = resolver  # (entry text, style) -> refengine.Resolution; tests pass a fake

    # --- reading -------------------------------------------------------------------------------
    def _para(self, pid: str) -> Paragraph | None:
        return self.ids.get(pid.strip("[] "))

    def read_paragraphs(self, from_id: str, to_id: str) -> dict:
        keys = list(self.ids)
        try:
            a, b = keys.index(from_id.strip("[] ")), keys.index(to_id.strip("[] "))
        except ValueError:
            return {"error": "unknown paragraph id"}
        out = []
        for k in keys[a:min(b, a + 39) + 1]:
            p = self.ids[k]
            out.append({"id": k, "editable": p.id in self.editable, "text": p.text})
        return {"paragraphs": out}

    def search_text(self, text: str) -> dict:
        needle = text.strip().lower()
        if len(needle) < 2:
            return {"error": "search text too short"}
        hits = []
        for pid, p in self.ids.items():
            low = p.text.lower()
            at = low.find(needle)
            while at >= 0 and len(hits) < 30:
                hits.append({"id": pid, "context": p.text[max(0, at - 60):at + len(needle) + 60]})
                at = low.find(needle, at + 1)
        return {"hits": hits, "truncated": len(hits) >= 30}

    # --- edits ---------------------------------------------------------------------------------
    def propose_edits(self, edits: list[dict]) -> dict:
        return {"results": [self._propose(e) for e in edits[:int(self.cfg.get("max_edits_per_call", 80))]]}

    def _propose(self, e: dict) -> dict:
        p = self._para(e.get("paragraph_id", ""))
        verdict = self._check(p, e)
        entry = {"type": "edit", **{k: e.get(k) for k in ("paragraph_id", "old", "new", "kind", "reason",
                                                           "confidence")}}
        if isinstance(verdict, Change):
            self.changes.append(verdict)
            self.log.append({**entry, "kept": True, "change_id": verdict.id})
            return {"paragraph_id": e.get("paragraph_id"), "old": e.get("old"), "kept": True}
        self.drops[verdict] += 1
        self.log.append({**entry, "kept": False, "why": verdict})
        return {"paragraph_id": e.get("paragraph_id"), "old": e.get("old"), "kept": False, "why": verdict}

    def _check(self, p: Paragraph | None, e: dict) -> Change | str:
        if p is None:
            return "unknown paragraph id"
        if p.id not in self.editable:
            return "paragraph is not editable (heading, table, reference list or read-only)"
        cfg = {**self.cfg, "max_words": int(self.cfg.get("max_words", 20)),
               "max_chars": int(self.cfg.get("max_chars", 160))}
        c, why = claude_pass.verify(p, {**e, "confidence": "certain"}, cfg)
        if c is None:
            return {"not_found": "old text not found in the paragraph (copy it exactly)",
                    "not_unique": "old text occurs more than once in the paragraph: add a neighbouring word",
                    "too_long": "edit too long: keep each edit within one clause",
                    "digits": "edit touches numbers", "symbols": "edit touches symbols or citation brackets",
                    "acronym": "edit touches an abbreviation", "name": "edit touches a capitalised name",
                    "protected": "edit touches a citation, URL or tag", "spelling_variant":
                    "British/American spelling is the spelling checker's", "no_change": "new equals old",
                    "partial_word": "edit starts or ends inside a word", "hidden": "edit next to hidden content",
                    "empty_old": "old is empty"}.get(why, why)
        if not c.old.strip() and not c.new.strip() or c.old.strip() == c.new.strip() and c.old != c.new:
            return "spacing is handled by the rules"
        if _SENTENCE_BREAK.search(c.old):
            return "edit spans more than one sentence"
        added = Counter(w.lower() for w in re.findall(r"[A-Za-z]+", c.new)) - \
            Counter(w.lower() for w in re.findall(r"[A-Za-z]+", c.old))
        t = p.text
        ctx_old = t[max(0, c.start - 3):c.end + 3]
        ctx_new = t[max(0, c.start - 3):c.start] + c.new + t[c.end:c.end + 3]
        if len(_SENTENCE_BREAK.findall(ctx_new)) > len(_SENTENCE_BREAK.findall(ctx_old)) and set(added) - _LINK_WORDS:
            return "edit adds a sentence (splitting a run-on sentence is fine; new content is not)"
        if not c.old.strip() and sum(added.values()) > 4 or sum(added.values()) > 12:
            return "edit adds too much text"
        name = self._touches_name(p.text, c)
        if name:
            return f"edit touches a capitalised name ({name})"
        if e.get("confidence") == "likely" and self.cfg.get("min_confidence", "likely") == "certain":
            return "only certain edits are kept"
        if any(k.para_id == c.para_id and (c.start < k.end and k.start < c.end or c.start == k.start)
               for k in self.changes):
            return "overlaps an edit already kept in this paragraph"
        kind = e.get("kind") if e.get("kind") in KINDS else "other"
        why_text = (e.get("reason") or "").strip()
        c.id = f"ag{next(_ids)}"
        c.reason = f"agent.{kind}" + (f" ({why_text[:120]})" if why_text else "")
        c.author = "Edit OS (AI)"
        c.provenance = provenance.make("ai", "Claude editing agent", why_text, checker="agent", edit_kind=kind,
                                       confidence=e.get("confidence", ""), model=self.cfg.get("model", ""))
        return c

    def _touches_name(self, text: str, c: Change) -> str:
        """A capitalised word the edit removes or changes that may be a name (claude_pass.verify misses one that
        starts a sentence): kept unless it is a common sentence opener or the manuscript writes it lower case and
        never capitalised mid-sentence."""
        s, e = c.start, c.end
        while s > 0 and text[s - 1].isalnum():
            s -= 1
        while e < len(text) and text[e].isalnum():
            e += 1
        new_words = set(re.findall(r"\w+", text[s:c.start] + c.new + text[c.end:e]))
        for m in re.finditer(r"\b[A-Z][a-z]+\b", text[s:e]):
            w = m.group()
            if w in new_words:
                continue
            if re.match(r"\s+et\.?\s+al", text[s + m.end():]):
                return w
            if w.lower() in _OPENERS:
                continue
            if w.lower() in new_words and not self._mid_capital(w):
                continue  # "the Patient" -> "the patient"
            if not self._lower_used(w) or self._mid_capital(w):
                return w
        return ""

    def _all_text(self) -> str:
        if not hasattr(self, "_text"):
            self._text = "\n".join(p.text for p in self.ms.paragraphs)
        return self._text

    def _lower_used(self, w: str) -> bool:
        return re.search(rf"\b{re.escape(w.lower())}\b", self._all_text()) is not None

    def _mid_capital(self, w: str) -> bool:
        return re.search(rf"[a-z,;]\s+{re.escape(w)}\b", self._all_text()) is not None

    # --- queries -------------------------------------------------------------------------------
    def add_queries(self, queries: list[dict]) -> dict:
        return {"results": [self._query(q) for q in queries[:30]]}

    def _query(self, q: dict) -> dict:
        p = self._para(q.get("paragraph_id", ""))
        entry = {"type": "query", **{k: q.get(k) for k in ("paragraph_id", "anchor", "text")}}
        why = ""
        text = (q.get("text") or "").strip()
        if p is None:
            why = "unknown paragraph id"
        elif not text:
            why = "empty query"
        elif len(self.queries) >= int(self.cfg.get("max_queries", 25)):
            why = "query limit reached for this article"
        elif sum(x.para_id == p.id for x in self.queries) >= 2:
            why = "at most two queries per paragraph"
        elif q.get("anchor") and p.text.count(q["anchor"]) != 1:
            why = "anchor must occur exactly once in the paragraph"
        else:
            missing = {int(n) for n in _REF_NUMBER.findall(text)} - self.checked_refs
            if missing:
                why = f"call check_reference first for: {sorted(missing)}"
        if why:
            self.drops["query: " + why.split(":")[0]] += 1
            self.log.append({**entry, "kept": False, "why": why})
            return {"paragraph_id": q.get("paragraph_id"), "kept": False, "why": why}
        made = claude_pass.query(p, {"anchor": q.get("anchor", ""), "text": text})
        made.check = "agent.query"
        self.queries.append(made)
        self.log.append({**entry, "kept": True})
        return {"paragraph_id": q.get("paragraph_id"), "kept": True}

    # --- references ----------------------------------------------------------------------------
    def references(self):
        if self._refs is None:
            self._refs = {r.number: r for r in references.extract(self.ms) if r.number is not None}
        return self._refs

    def check_reference(self, number: int) -> dict:
        refs = self.references()
        if number not in refs:
            return {"error": f"no reference {number} in the list (the list has {len(refs)} numbered entries)"}
        if len(self.checked_refs) >= int(self.cfg.get("max_reference_checks", 30)) and number not in self.checked_refs:
            return {"error": "reference check limit reached for this article"}
        self.checked_refs.add(number)
        ref = refs[number]
        out = {"number": number, "entry": ref.full_text[:600]}
        from .. import refengine

        parsed = refengine.parse(ref.full_text)
        out["first_author_in_entry"] = parsed.first_surname
        out["year_in_entry"] = parsed.year
        try:
            res = (self.resolver or refengine.resolve)(ref.full_text, self.style)
        except Exception as exc:  # network trouble: say so, never guess
            res = None
            out["lookup"] = f"lookup failed ({type(exc).__name__}); treat as not verified"
        if res is not None:
            if res.record is not None and res.status in (refengine.MATCHED, refengine.FORMAT_FIXED):
                r = res.record
                out["lookup"] = "verified"
                out["record"] = {"source": r.source, "title": r.title, "first_authors": [a[0] for a in r.authors[:3]],
                                 "year": r.year, "journal": r.journal}
            else:
                out["lookup"] = "not verified: " + (res.message or "no matching record")
            if res.queries:
                out["code_queries"] = [k for k, _ in res.queries]
        out["cited_in"] = [self.label[p.id] for p in self.ms.paragraphs
                           if p.id in self.label and p.id in self.editable_or_body() and number in cited_numbers(p.text, p.superscript)]
        return out

    def editable_or_body(self) -> set[str]:
        if not hasattr(self, "_body"):
            from ..rules import body_paragraphs
            self._body = {p.id for p in body_paragraphs(self.ms)}
        return self._body

    # --- dispatch ------------------------------------------------------------------------------
    def call(self, name: str, args: dict) -> dict:
        try:
            if name == "read_paragraphs":
                return self.read_paragraphs(str(args.get("from_id", "")), str(args.get("to_id", "")))
            if name == "search_text":
                return self.search_text(str(args.get("text", "")))
            if name == "propose_edits":
                return self.propose_edits([e for e in args.get("edits") or [] if isinstance(e, dict)])
            if name == "add_queries":
                return self.add_queries([q for q in args.get("queries") or [] if isinstance(q, dict)])
            if name == "check_reference":
                return self.check_reference(int(args.get("number", 0)))
        except (TypeError, ValueError) as exc:
            return {"error": f"bad input: {exc}"}
        return {"error": f"unknown tool {name}"}
