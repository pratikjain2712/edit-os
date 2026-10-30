"""Copy-edit text rules: capitalised common terms, prefixes, number words, statistics
semicolons and ranges in prose.

Each finder is `text, cfg, ctx -> [(start, end, new), ...]` on one paragraph's text view; the
checker (`collect`) turns them into `Change` records (layer LANGUAGE, source RULE, reason
"text.<rule>"). Settings live under `text:` in the journal style, one switch per rule: ranges are
on everywhere, the others only where the journal's copy editors were measured to make the edit
(numbers in base.yaml).

These are copy-editor edits, not style-editor edits: in the corpus of 53,683 editor edits on 160
articles (style editor and copy editor kept apart) the copy editors closed 264 prefixes, turned 142
number words into digits, worded 31 ranges and made 25 comma -> semicolon edits; the style editors
made 0, 0, 1 and 0 (their case changes are in headings). So they run with the copy-edit checkers
(`edit_os.copyedit.CHECKERS`), after spelling and before abbreviations, LanguageTool and the Claude
pass, and are scored with tools/eval_ce.py. (Run as style rules, they cut IJO's style closure
from 22.7% to 9.7%.)

Scope: body paragraphs before the reference list, figure legends included (table cells only with
`text.include_tables`); never the title page, headings, the reference list, URLs, DOIs, e-mail
addresses or citations.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .changes import Change
from .docmodel import Manuscript, Paragraph
from .style import JournalStyle

# --- context ---------------------------------------------------------------------------

_WORDS = re.compile(r"(?<![\w\-])[A-Za-z][a-z]+(?![\w])")
_COMPOUND = re.compile(r"(?<![\w\-–])[A-Za-z]{2,}[-–][a-z]{2,}(?![\w\-–])")


@dataclass
class Context:
    """What the rules know about the whole manuscript: how often each word is written in
    lower case in running text (so "Vitreous" mid-sentence is a common noun when the author
    also writes "vitreous")."""
    lower: Counter = field(default_factory=Counter)
    hyphenated: set = field(default_factory=set)  # compounds the author writes with a hyphen ("health-related")

    @classmethod
    def from_texts(cls, texts) -> "Context":
        c = Counter()
        hy = set()
        for t in texts:
            for m in _WORDS.finditer(t):
                w = m.group(0)
                if w[0].islower():
                    c[w] += 1
            hy.update(m.group(0).lower() for m in _COMPOUND.finditer(t) if "-" in m.group(0))
        return cls(c, hy)


_PROTECT = re.compile(
    r"https?://\S+|www\.\S+|\S+@\S+\.\w+|\bdoi:\s*\S+|\b10\.\d{4,}/\S+"
    r"|\[\d[\d,\-–\s]*\]|<\/?[A-Za-z]{1,4}\d?>"
)


def _protected(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _PROTECT.finditer(text)]


# Citations, closing quotes and brackets after a sentence's full stop: ".[1] The", ".¹² The", '." The'
_CITE_TAIL = re.compile(r"(?:\[[^\[\]]{1,24}\]|[⁰¹²³⁴⁵⁶⁷⁸⁹,\-–]+|[\"”’')\]])*\s*$")


def _sentence_start(text: str, i: int) -> bool:
    """Position i starts a sentence, a list item, a label's text or a table cell."""
    before = _CITE_TAIL.sub("", text[:i]).rstrip("  \"'‘“([")
    return (not re.search(r"[A-Za-z0-9]", before) or before[-1] in ".?!:;>\t•–—-|"
            or before.endswith((". (", ".)")))


# --- 1. capitalised common terms in running text -> lower case --------------------------

# Never lowered: eponyms, names, places, peoples, languages, brand-like words that also end in a
# common suffix. (Words the author never writes in lower case are left alone anyway, except in
# a full form directly before its abbreviation, where this list and the suffix test guard.)
PROPER = set("""
Doppler Parkinson Parkinsonian Alzheimer Hodgkin Gram Kaplan Meier Pearson Spearman Fisher Student Wilcoxon
Mann Whitney Kruskal Wallis Bonferroni Tukey Cox Chi Snellen Goldmann Humphrey Zeiss Heidelberg Bruch Descemet
Schlemm Tenon Müller Muller Bowman Henle Sjögren Sjogren Graves Behçet Behcet Vogt Koyanagi Harada Stevens
Johnson Coats Leber Stargardt Best Wilson Down Marfan Horner Adie Argyll Robertson Purkinje Willis Bell Guillain
Barré Barre Cushing Addison Crohn Hashimoto Kawasaki Raynaud Reiter Takayasu Wegener Whipple Bayesian Gaussian
Poisson Likert Hispanic Arabic Asiatic Atlantic Pacific Islamic Catholic Nordic Caucasian Asian African
American European Indian Chinese Japanese Korean Saudi Arab English British Latin Greek Roman Western Eastern
Northern Southern Christian Hindu Muslim Jewish Gulf Medicare Medicaid Ayurvedic Ayurveda Unani Siddha
Gaucher Fabry Tay Sachs Krabbe Niemann Pick Ehlers Danlos Stickler Usher Bardet Biedl Alport Fuchs Axenfeld
Rieger Peters Sturge Weber Hippel Lindau Recklinghausen Gorlin Goldenhar Treacher Collins Apert Crouzon
Pierre Robin Duane Brown Mobius Möbius Parinaud Terson Purtscher Eales Mooren Thygeson Haab Vossius Kayser
Fleischer Krukenberg Hudson Stähli Ferry Arlt Herbert Horner Trantas Dalen Fuchs Elschnig Siegrist Roth
Cotton Wool Hollenhorst Amsler Ishihara Hess Maddox Worth Bagolini Titmus Randot Lang Frisby Krimsky Hirschberg
Schirmer Seidel Jones Hertel Luedde Exophthalmometer Placido Scheimpflug Pentacam Orbscan Lasik LASIK Ahmed
Baerveldt Molteno Express Trabectome Kahook iStent Hydrus Xen Preserflo Lucentis Avastin Eylea Ozurdex
Iluvien Jetrea Lumigan Xalatan Travatan Vyzulta Rhopressa Combigan Cosopt Azopt Alphagan Timoptic Durysta
Kenalog Triesence Visudyne Macugen Beovu Vabysmo Syfovre Izervay Luxturna Restasis Xiidra Cequa Tyrvaya
Optovue Cirrus Spectralis Topcon Nidek Canon Optos Clarus Visucam Stratus Avanti Angiovue Angioplex Triton
Nikon Leica Olympus Alcon Bausch Lomb Allergan Novartis Roche Genentech Regeneron Bayer Pfizer Merck Sanofi
Excel Microsoft Windows Google Apple Android IBM SPSS Stata Python MATLAB Matlab GraphPad Prism MedCalc ImageJ
Fiji Epi OpenEpi Info Redcap REDCap Qualtrics Zoom WhatsApp Facebook Twitter YouTube Instagram ChatGPT
Covid COVID Ebola Zika Lyme Marburg Lassa Nipah Kyasanur Chandipura Japanese Rift Murray Ross West Nile
God Lord Ramadan Diwali Christmas January February March April May June July August September October
November December Monday Tuesday Wednesday Thursday Friday Saturday Sunday
""".split())

# A run of capitalised words holding one of these is a name (an organisation, a study, a named
# scale or system): left as written ("World Health Organization", "Visual Analogue Scale").
NAME_HINTS = set("""
Hospital Hospitals University Institute Institutes Institution College Department Dept Centre Center Centres
Centers Organization Organisation Society Association Council Ministry Foundation Academy Board Trust Clinic
Clinics Laboratory Laboratories Company Inc Ltd Limited Corporation Corp Study Studies Trial Trials Group Groups
Program Programme Survey Initiative Network Registry Consortium Agency Administration Service Services
Federation Union Bureau Office Commission Authority Agency Mission Scheme Project Collaboration Collaborative
Scale Score Scores Questionnaire Index Inventory Criteria Classification System Systems Grading Guidelines
Guideline Statement Checklist Tool Test Battery Profile Version Edition Software Package Model Protocol
Declaration Act Law Code Award Fund Journal Press Medical Sciences Science Research Health Healthcare
State States Republic Kingdom Nations District City Village Province Region County Street Road Nagar
Ophthalmic Eye Ophthalmology Optometry Dental Nursing Pharmacy Pharmaceuticals Pharma Biotech Diagnostics
""".split())

# Minor words allowed inside a run of capitalised words ("Institute of Medical Sciences").
_MINOR = {"of", "and", "the", "for", "in", "on", "to", "with", "&", "a", "an", "by", "at"}

# Common-noun / adjective endings: a capitalised word with one of these (and not in PROPER)
# is taken as a common term in a full form before its abbreviation ("Optical Coherence
# Tomography (OCT)").
_COMMON_SUFFIX = re.compile(
    r"(?:ography|ology|itis|osis|ance|ence|tion|sion|ment|ity|ive|ous|ness|ure|scopy|graphy|metry|"
    r"therapy|ectomy|otomy|opathy|emia|aemia|oma|ular|ary|ory|ical|al|ic|ing|ed|ism|ist|ase|ine|"
    r"cyte|cytes|gen|ogen|ics|ate|ated|ant|ent|ies|tions|ments|ities|cies|age|ide|ion|ions|ar)$")

_CAP_RUN = re.compile(
    r"(?<![\w\-’'])[A-Z][a-z]+(?:-[A-Za-z][a-z]+)*"
    r"(?:(?:\s+(?:of|and|the|for|in|on|to|with|&|a|an|by|at))*\s+[A-Z][a-z]+(?:-[A-Za-z][a-z]+)*)*(?![\w’'])")
_ABBR_AFTER = re.compile(r"\s*\(([A-Z][A-Za-z0-9\-]{1,9}s?)(?:[;,][^)]*)?\)")


def _initials_match(words: list[str], abbr: str) -> bool:
    """The full form's significant words spell the abbreviation ("Magnetic Resonance Imaging" ->
    MRI); hyphenated parts count ("Contrast-Enhanced Computed Tomography" -> CECT)."""
    letters = re.sub(r"[^A-Za-z]", "", abbr.rstrip("s") if abbr.endswith("s") and abbr[:-1].isupper() else abbr)
    parts = [x for w in words for x in w.split("-") if x.lower() not in _MINOR]
    initials = "".join(x[0] for x in parts).lower()
    return letters.lower() == initials or (len(letters) >= 2 and initials.startswith(letters.lower())
                                           and len(initials) == len(letters))


def _known_common(w: str, ctx: Context, suffix_ok: bool, need: int = 1) -> bool:
    if w in PROPER:
        return False
    if ctx.lower.get(w.lower(), 0) >= need:
        return True
    return suffix_ok and bool(_COMMON_SUFFIX.search(w.lower()))


def _title_like(text: str) -> bool:
    """A heading, a title or a label line: most words capitalised, or short with no full stop."""
    words = re.findall(r"[A-Za-z][\w\-’']*", text)
    if not words:
        return True
    caps = sum(w[0].isupper() for w in words)
    if not re.search(r"[.?!:]\W*$", text.strip()) and (len(words) <= 12 or caps / len(words) > 0.3):
        return True  # headings, titles, table and figure labels
    return len(words) >= 3 and caps / len(words) > 0.5


def find_lowercase(text: str, cfg: dict, ctx: Context) -> list:
    """"Computed Tomography", "Right Eye", "Vitreous" mid-sentence -> lower case.

    A run of capitalised words (minor words such as "of" inside) is lowered as a whole, and
    only when every word in it is a common term: written in lower case elsewhere in the
    manuscript, or, in a full form directly before its abbreviation ("Magnetic Resonance Imaging
    (MRI)"), with a common-noun ending. Runs naming an organisation, study, scale or place
    (NAME_HINTS), eponyms and names (PROPER), sentence starts, labels followed by a number or
    letter ("Group A", "Stage 2") and heading-like paragraphs are left alone."""
    if _title_like(text) or re.match(r"\s*AQ\s*\d*\s*:", text):
        return []
    out = []
    expansions = cfg.get("expansions", True)
    running = cfg.get("running_text", True)
    min_lower = cfg.get("min_lower", 2)
    suffix = cfg.get("suffix", False)
    runs = list(_CAP_RUN.finditer(text))
    for i, m in enumerate(runs):
        run = m.group(0)
        words = re.findall(r"[A-Za-z][a-z]*(?:-[A-Za-z][a-z]+)*|&", run)
        sig = [w for w in words if w.lower() not in _MINOR]
        first_at_start = _sentence_start(text, m.start())
        abbr = _ABBR_AFTER.match(text, m.end())
        is_def = bool(abbr) and _initials_match(sig, abbr.group(1))
        if is_def and not expansions or not is_def and not running:
            continue
        if _quoted(text, m.start()) or re.match(r"\s*:", text[m.end():]) and _sentence_start(text, m.start()):
            continue  # a quoted title or term; a run-in label ("Technical Note: ...")
        if re.search(r"(?:\b[A-Z]{2,}[\w/]*|\d)\s*$", text[:m.start()]) or re.match(r"\s+[A-Z]{2,}\b(?!\))", text[m.end():]):
            continue  # next to an acronym or a number: a name ("World PPH Day", "PCR Amplification Kit", "Type 2 Diabetic")
        chain = _chain(text, runs, i)
        if "&" in words or any(x in NAME_HINTS or x in PROPER for r in chain
                               for w in re.findall(r"[A-Za-z]+", r.group(0)) for x in (w,)):
            continue
        if not is_def and len(chain) > 2:
            continue  # a list of capitalised items: drugs, products, divisions ("Cornea, Cataract, and ...")
        if not is_def and re.match(r"(?:\.?\s+|-)(?:[0-9]|[A-Z](?![a-z])|[IVX]+\b)", text[m.end():]):
            continue  # labels: "Group A", "Visit 4", "Zone I"
        if not is_def and re.match(r"['’]s\b", text[m.end():]):
            continue
        need = 1 if is_def or len(sig) > 1 else min_lower
        if not all(_known_common(x, ctx, suffix and is_def, need) for w in sig for x in w.split("-")):
            continue
        # lower each capitalised word part, except a sentence-initial first word
        for wm in re.finditer(r"[A-Za-z][a-z]*", run):
            w = wm.group(0)
            s = m.start() + wm.start()
            if not w[0].isupper() or len(w) < 2 or not w[1:].islower():
                continue
            if s == m.start() and first_at_start:
                continue
            out.append((s, s + 1, w[0].lower()))
    return out


def _quoted(text: str, i: int) -> bool:
    """Position i is inside quotation marks (an article title, a named term)."""
    before = text[:i]
    return before.count("“") > before.count("”") or before.count("‘") > before.count("’") \
        or before.count('"') % 2 == 1


def _chain(text: str, runs: list, i: int) -> list:
    """The capitalised runs joined to run i by commas, "and", "or", "/" or a dash: one list or name."""
    sep = re.compile(r"\s*(?:,\s*(?:and\s+|or\s+)?|\s+(?:and|or|&)\s+|\s*[/–—-]\s*)$")
    lo = hi = i
    while lo > 0 and sep.match(text[runs[lo - 1].end():runs[lo].start()]):
        lo -= 1
    while hi + 1 < len(runs) and sep.match(text[runs[hi].end():runs[hi + 1].start()]):
        hi += 1
    return runs[lo:hi + 1]


# --- 2. prefixes closed up --------------------------------------------------------------

# Prefixes the copy editors close up. Measured on 158 articles (copy editor's text, all journals:
# closed / left hyphenated): non 151/221, post 76/103, pre 40/63, intra 6/2, peri 4/2, hyper 7/0; the
# others are mostly left: anti 2/32, multi 9/25, sub 7/19, under 8/14, semi 0/14, inter 4/12, re 4/10,
# ultra 2/10, co 10/10. "post" and "pre" close only before an adjective or adverb (postoperative,
# preoperatively): before a noun the editors keep the hyphen (post-surgery, post-transfusion).
PREFIXES = ["non", "post", "pre", "intra", "peri", "hyper", "hypo"]
_ADJECTIVAL = re.compile(r"(?:ive|ively|al|ally|ic|ical|ar|ary|ous|atal|partum)$")
# Bases the prefix stays hyphenated before: Latin phrases, open compounds, look-alikes.
KEEP_BASES = {"hoc", "small", "existing", "ex", "op", "ops", "test", "tests", "and", "or", "to", "vs", "a",
              "the", "only", "normal"}
# Prefix-base pairs that change meaning closed up ("re-cover", "re-sign", "co-op"), or read badly.
KEEP_PAIRS = {"re-cover", "re-sign", "re-form", "re-creation", "re-press", "re-treat", "re-treatment",
              "re-treated", "co-op", "co-author", "co-authors", "un-ionized", "multi-ethnic", "pre-eclampsia",
              "pre-eclamptic", "co-infection", "co-infected", "co-occurrence", "co-occurring", "co-operation",
              "co-operative", "co-ordination", "co-ordinate", "co-ordinated", "inter-rater", "intra-rater",
              "inter-observer", "intra-observer", "anti-inflammatory", "post-traumatic"}
# Spaced forms the editors close ("post operatively"): only with these bases.
SPACED_BASES = {"operative", "operatively", "operation", "partum", "natal", "nataly", "procedural",
                "menopausal", "menopause", "infective", "infectious", "inflammatory", "invasive", "surgical",
                "surgery", "treatment", "injection", "pubertal"}


def find_prefixes(text: str, cfg: dict, ctx: Context) -> list:
    """"non-invasive" -> "noninvasive", "post operatively" -> "postoperatively", and the reverse
    where a vowel meets itself: "intraarterial" -> "intra-arterial".

    The hyphen stays before a capital, a number or an abbreviation (non-ICU, post-COVID,
    pre-2000), where the prefix's last letter meets the same vowel (anti-inflammatory,
    semi-independent), before an already hyphenated or open compound (non-small cell,
    non-insulin-dependent), in KEEP_PAIRS, and for prefixes not in `text.prefixes.list`."""
    allowed = cfg.get("list") or PREFIXES
    alt = "|".join(sorted(allowed, key=len, reverse=True))
    out = []
    for m in re.finditer(r"(?<![\w\-])((?i:" + alt + r"))(-|\s)([a-z]{2,})(?![\w])(-)?", text):
        pre, sep, base, hy_after = m.group(1), m.group(2), m.group(3), m.group(4)
        if pre[1:] != pre[1:].lower():
            continue
        whole = f"{pre}-{base}".lower()
        if base.lower() in KEEP_BASES or whole in KEEP_PAIRS or hy_after:
            continue
        if pre[-1].lower() == base[0].lower() and pre[-1].lower() in "aeiou":
            continue  # same vowel meets: anti-inflammatory
        if pre.lower() == "re" and base[0].lower() == "e":
            continue
        if base.endswith("ly") and (pre.lower() == "non" or not base.endswith(("ively", "ally"))):
            continue  # "non-normally distributed": the prefix governs the phrase
        if pre.lower() in ("post", "pre") and not _ADJECTIVAL.search(base.lower()):
            continue
        nxt = text[m.end():m.end() + 12]
        if re.match(r"\s+(?:cell|lung)\b", nxt):
            continue  # "non-small cell": the prefix applies to an open compound
        if sep != "-":
            if not cfg.get("spaced", True) or base.lower() not in SPACED_BASES or pre.lower() not in (
                    "post", "pre", "intra", "peri", "non", "inter"):
                continue
            if sep != " ":
                continue
        out.append((m.start(2), m.end(2), ""))
    if cfg.get("same_vowel_hyphen", True):
        for m in re.finditer(r"(?<![\w\-])(intra|extra|supra|infra|ultra|anti|semi|multi)(a|i)([a-z]{3,})", text, re.I):
            if m.group(1)[-1].lower() == m.group(2).lower():
                out.append((m.end(1), m.end(1), "-"))
    return out


# --- 3. number words with units of time -------------------------------------------------

NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12"}
_TIME = r"(?:seconds?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?)"
_NW = "|".join(NUMBER_WORDS)
_NUMWORD = re.compile(
    r"(?<![\w\-])(" + _NW + r")((?:\s*(?:to|or|and|-|–)\s*(?:" + _NW + r"))?)(\s|-)(" + _TIME + r")\b", re.I)


def find_number_words(text: str, cfg: dict, ctx: Context) -> list:
    """"three months" -> "3 months", "a five-year history" -> "a 5-year history" (units of time).
    A number word that starts a sentence stays, and so does "one" used as a pronoun or in
    "one of"; "two to three weeks" becomes "2 to 3 weeks"."""
    out = []
    for m in _NUMWORD.finditer(text):
        if _sentence_start(text, m.start()):
            continue
        before = text[:m.start()].rstrip()
        if re.search(r"\b(?:" + _NW + r"|\d+)\s*(?:to|or|and|-|–)$", before, re.I):
            continue  # "1 or two weeks": the first half is handled with it or left as written
        w = m.group(1)
        if w.lower() == "one" and re.search(r"\b(?:than|the|this|that|any|each|every)$", before, re.I):
            continue
        out.append((m.start(1), m.end(1), NUMBER_WORDS[w.lower()]))
        if m.group(2):
            g2 = re.search(_NW, m.group(2), re.I)
            s = m.start(2) + g2.start()
            out.append((s, s + len(g2.group(0)), NUMBER_WORDS[g2.group(0).lower()]))
    return out


# --- 4. semicolons between groups of statistics -----------------------------------------

_STAT_GROUP = r"\([^()]*\b[pP]\s*[=<>≤≥]\s*[<>]?\s*0?\.\d+[^()]*\)"
_STAT_SEMI = re.compile(_STAT_GROUP + r"(,)(?=\s+(?:and\s+)?(?:at|in|on|during|after|by)\b"
                        r"(?:(?!\.\s)[^;()]){0,200}?" + _STAT_GROUP + ")")


def find_stat_semicolons(text: str, cfg: dict, ctx: Context) -> list:
    """"... (p = 0.065), at 3 months it was ... (p = 0.165)" -> "(p = 0.065); at 3 months": a
    comma between two clauses that each report a statistic in brackets becomes a semicolon."""
    return [(m.start(1), m.end(1), ";") for m in _STAT_SEMI.finditer(text)]


# --- 5. ranges in prose -----------------------------------------------------------------

_VAL = r"\d+(?:[.,]\d+)?"
_RANGE = re.compile(r"\b(between|from)(\s+(?:the\s+ages?\s+of\s+|ages?\s+|the\s+years?\s+)?)(" + _VAL + r")"
                    r"(\s*%?\s*)([-–—]\s*|\s+[-–—]\s*)(" + _VAL + r")(?![\d.,]*\s*[-–—/]\s*\d|[\d.,]*/)", re.I)


def find_ranges(text: str, cfg: dict, ctx: Context) -> list:
    """"between 1919- 1999" -> "between 1919 and 1999", "from 18-81 years" -> "from 18 to 81
    years": a dash range after "between" / "from" reads as words."""
    out = []
    for m in _RANGE.finditer(text):
        after = text[m.end():m.end() + 20]
        if re.match(r"\s*(?:to|and)\b", after):
            continue
        word = "and" if m.group(1).lower() == "between" else "to"
        out.append((m.start(4), m.end(5), f"{m.group(4).strip()} {word} "))
    return out


def find_compound_dash(text: str, cfg: dict, ctx: Context) -> list:
    """A compound word joined by an en dash that the article also writes with a hyphen: the hyphen ("oral
    health–related quality of life" beside "health-related" -> "health-related"; JEHP_73_26, QC: "Inconsistent en
    dash & hyphen use")."""
    return [(m.start() + m.group(0).index("–"), m.start() + m.group(0).index("–") + 1, "-")
            for m in _COMPOUND.finditer(text)
            if "–" in m.group(0) and m.group(0).replace("–", "-").lower() in ctx.hyphenated]


_AGED_OLD = re.compile(r"\baged\b[^.;:()]{0,40}?\d\s*(?:years?|yrs?)(\s+old)\b", re.I)


def find_aged_years_old(text: str, cfg: dict, ctx: Context) -> list:
    """"aged between 65 and 70 years old" -> "aged between 65 and 70 years": "aged" already says it (JEHP_73_26, QC:
    "Redundant, so removed"). "45 years old" with no "aged" is left."""
    return [(m.start(1), m.end(1), "") for m in _AGED_OLD.finditer(text)]


FINDERS = {
    "lowercase": find_lowercase,
    "prefixes": find_prefixes,
    "number_words": find_number_words,
    "stat_semicolons": find_stat_semicolons,
    "ranges": find_ranges,
    "compound_dash": find_compound_dash,
    "aged_years_old": find_aged_years_old,
}


def propose(text: str, cfg: dict, ctx: Context) -> list[tuple[int, int, str, str]]:
    """(start, end, new, rule) for every enabled rule, outside protected spans, without overlaps."""
    from .rules_guide import looks_like_reference

    if looks_like_reference(text):
        return []
    guard = _protected(text)
    out: list[tuple[int, int, str, str]] = []
    for name, finder in FINDERS.items():
        if not enabled(cfg, name):
            continue
        sub = cfg[name] if isinstance(cfg[name], dict) else {}
        for s, e, new in finder(text, sub, ctx):
            if text[s:e] == new or any(gs < e and s < ge or s == e and gs <= s < ge for gs, ge in guard):
                continue
            if any(o[0] < e and s < o[1] or s == e == o[0] == o[1] for o in out):
                continue
            out.append((s, e, new, name))
    return sorted(out)


# --- checker ----------------------------------------------------------------------------

def settings(style: JournalStyle) -> dict:
    return dict(style.get("text") or {})


def enabled(cfg: dict, name: str) -> bool:
    """A rule is on when its setting is true or a mapping with `enabled: true`."""
    sub = cfg.get(name)
    return bool(sub.get("enabled")) if isinstance(sub, dict) else bool(sub)


def paragraphs(ms: Manuscript, include_tables: bool) -> list[Paragraph]:
    from . import references
    from .rules import front_matter_ids

    end = references.find_heading(ms)
    paras = ms.paragraphs if end is None else ms.paragraphs[:end]
    front = front_matter_ids(ms)
    return [p for p in paras if p.text.strip() and p.id not in front and (include_tables or not p.in_table)
            and not p.text.lstrip().startswith("<H")]


def collect(ms: Manuscript, style: JournalStyle) -> list[Change]:
    from .copyedit.common import change

    cfg = settings(style)
    if not cfg.get("enabled", True) or not any(enabled(cfg, k) for k in FINDERS):
        return []
    paras = paragraphs(ms, bool(cfg.get("include_tables")))
    ctx = Context.from_texts(p.text for p in paras if not p.in_table)
    out = []
    for p in paras:
        for s, e, new, name in propose(p.text, cfg, ctx):
            out.append(change(p, s, e, new, "text", reason=f"text.{name}"))
    return out
