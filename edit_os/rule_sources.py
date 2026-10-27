"""The stylesheet or pre-editing guide line behind each style suggestion ("Why" on the review screen).

The repository holds only a small mapping from a rule's reason (`Change.reason`) to search keywords: which
words the line it implements must contain, and which words rank it. The quotes themselves are resolved at
runtime from the journal's stylesheet and the guide (`edit_os/stylesheets.py`: EDITOS_STYLESHEETS,
EDITOS_PREEDIT_GUIDE), once per journal and file version. With no stylesheet configured, or no line that
matches, the rule's plain description is all there is (`quote` None).

    rule_for(journal, reason, provenance=None) -> {"key", "label", "description", "quote", "where", "line"}
    key_for(reason, provenance=None)           -> the group key ("front_matter:title", "claude.grammar", ...)
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass

from . import style as style_mod
from .stylesheets import RuleIndex, _words, find_stylesheet, load_rules

ENV_SHEETS = "EDITOS_STYLESHEETS"
ENV_GUIDE = "EDITOS_PREEDIT_GUIDE"
MAX_QUOTE = 360


@dataclass(frozen=True)
class Locator:
    label: str
    description: str = ""
    sheet: tuple[str, ...] = ()  # regexes a stylesheet line must all match (case-insensitive)
    guide: tuple[str, ...] = ()  # regexes a guide line must all match
    terms: str = ""              # words that rank the matching lines
    first: str = "sheet"         # "sheet" | "guide": which to quote when both have a line


L = Locator
_REF = r"reference|bibliograph"
_NOT_ABSTRACT = r"\A(?![\s\S]*structured\s+abstract)"

RULES: dict[str, Locator] = {
    # front matter
    "front_matter": L("Front matter", "Title page rebuilt in the journal's order and style",
                      (r"article\s+type|title|author|affiliation|correspondence",), (r"running|title|author",)),
    "front_matter:running_head": L("Running head", "Surnames, a colon and a short title in sentence case",
                                   (r"running\s+(?:title|head)",), (r"running\s+head|surname",),
                                   "surname colon short title sentence case"),
    "front_matter:article_type": L("Article type", "The manuscript type as the submission gives it",
                                   (r"(?:article|manuscript)\s+type",), (r"manuscript\s+type",), "bold title case left"),
    "front_matter:title": L("Article title", "Title in the journal's case, bold, no end period",
                            (r"article\s+title|^title\b",), (r"title",), "sentence case bold period title"),
    "front_matter:authors": L("Author names", "Author names as the journal prints them, comma separated",
                              (r"author",), (r"author",), "author names comma delimiter bold initials"),
    "front_matter:affiliations": L("Affiliations", "Affiliations complete, run on, country at the end",
                                   (r"affiliation",), (r"affiliation",), "affiliation department institution country"),
    "front_matter:correspondence": L("Correspondence", "Corresponding author's name, address and e-mail",
                                     (r"correspondence",), (r"correspond",), "correspondence address email name expanded"),
    "front_matter:dates": L("History dates", "Received, revised and accepted dates from the submission",
                            (r"history|received",), (r"history|received",), "history dates format received accepted"),
    "front_matter:cite": L("How to cite", "The cite line built from the authors and title",
                           (r"how\s+to\s+cite|cite\s+this",), (r"how\s+to\s+cite|cite",), "cite article authors et al"),
    "front_matter:label": L("Title-page label", "A label with no content of its own is removed",
                            (r"title\s*page",), (), "label delete"),
    "figure_insert": L("Figure inserted", "A figure sent as a separate file placed above its legend",
                       (r"image|figure",), (r"figure|image",), "image present first caption below figure"),
    # no stylesheet line: the journal's editors add the block (sections.back_matter.first_author)
    "first_author": L("First author", "A \"First author\" block after the references: the first author's full name "
                      "and affiliation, from the front matter"),
    # submission material
    "submission": L("Submission material", "Parts of the submission that are not the article are removed",
                    (r"cover\s+letter|copyright",), (r"cover\s+letter|copyright",), "delete cover letter copyright"),
    "submission_copyright_notice": L("Copyright notice", "The portal's copyright notice is queried",
                                     (r"copyright",), (r"copyright",), "copyright form signed authors query"),
    # headings and sections
    # body headings: never the structured-abstract line (its "Title case" is about the abstract's sub-heads)
    "heading_tags": L("Heading level", "Headings tagged with their level (<H1>, <H2>, <H3>)",
                      (r"\bH[123]\b|heading", _NOT_ABSTRACT), (r"heading",), "h1 h2 h3 heading level caps bold roman left aligned"),
    "sections_headings": L("Heading", "Section headings renamed and cased as the journal has them",
                           (r"\bH1\b", _NOT_ABSTRACT), (r"heading",), "h1 caps case bold roman left aligned"),
    "subheadings": L("Sub-heading", "Short own-line heads inside a section become <H2> / <H3>",
                     (r"\bH[23]\b",), (r"sub-?heading|heading",), "h2 h3 sentence case bold"),
    "sections_abstract": L("Abstract", "Abstract heading and structured sub-headings",
                           (r"abstract",), (r"abstract",), "abstract heading structured sub-headings bold colon"),
    "sections_keywords": L("Keywords", "Keywords label, order and case",
                           (r"key\s*words?",), (r"key\s*words?",), "keywords alphabetical order case colon"),
    "sections_back_matter": L("Back matter", "The journal's back-matter sections, headings and wording",
                              (r"^(?!.*consent)", r"financial|conflict|acknowledg"),
                              (r"^(?!.*consent)", r"financial|conflict|acknowledg"),
                              "financial support sponsorship conflicts interest acknowledgments nil"),
    "sections_back_matter.consent_added": L("Patient consent added", "The standard consent statement where patient "
                                            "images appear without one", (r"consent",), (r"consent",),
                                            "declaration patient consent statement add standard"),
    "sections_consent": L("Patient consent", "Declaration of patient consent heading", (r"consent",), (r"consent",),
                          "declaration patient consent heading"),
    "sections_contributions": L("Author contributions", "Authors' contributions as a section",
                                (r"contribution",), (r"contribution",), "author contribution add"),
    "sections_salutation": L("Salutation", "Letter salutation in the journal's form", (r"salutation|dear\s+(?:sir|editor)",),
                             (r"salutation|dear\s+(?:sir|editor)",), "letter salutation dear sir editor"),
    # citations and references
    "author_citations": L("Citation placement", "A citation moved next to the study it names",
                          (r"citation",), (r"citation",), "citation author name after"),
    "author_year": L("Surname, year citation", "An author-year citation as its reference number",
                     (r"surname", r"year"), (r"citation",), "surname year format numbers reference"),
    "citations": L("Citation style", "Citations in the journal's brackets, position and delimiters",
                   (r"citation",), (r"citation",), "citation square bracket superscript punctuation comma range"),
    "renumber": L("Reference renumbering", "References numbered in order of first citation",
                  (r"number|order",), (r"number|order",), "references numbered consecutively order first mentioned "
                  "renumbering chronological"),
    "fig_renumber": L("Table / figure renumbering", "Tables and figures numbered in order of first citation",
                      (r"(?:figure|table)", r"chronological|order"), (r"(?:figure|table)", r"order"),
                      "figure table citations chronological order"),
    "fig_citations": L("Figure / table citation", "Figure and table citations in the journal's form",
                       (r"(?:figure|table|fig)", r"citation|cite"), (r"(?:figure|table)", r"citation|cite"),
                       "table figure citation bracket order"),
    "fig_captions": L("Figure / table caption", "Caption labels and end punctuation",
                      (r"caption|label|legend",), (r"caption|legend|label",), "caption label bold period table figure"),
    "fig_legends_move": L("Figure legends at the end", "Figure legends moved after the references, before the tables",
                          (r"caption|legend",), (r"caption", r"end\s+of\s+document"),
                          "caption placed end document before table", first="guide"),
    # no stylesheet line: the journal's editors' heading over the legends
    "fig_legends_heading": L("Figure Legends heading", "A heading over the figure legends"),
    # no stylesheet line: the journal's editors put the caption in a merged first row of the table
    "table_caption_row": L("Table caption in the table", "The table caption in a merged first row of its table"),
    # the guide's TABLE section (owner, 2026-10-27: follow the guide)
    "tables_move": L("Tables at the end", "Tables moved to the end of the document, after the figure captions",
                     (r"table", r"end\s+of\s+document"), (r"table", r"end\s+of\s+document"),
                     "table placed end document after figure captions", first="guide"),
    "tables.indent": L("Table indentation", "Indentation in the first column as 5 spaces per level",
                       (r"indentation",), (r"indentation",), "indentation removed 5 spaces increment headings rows"),
    "table_cell_blanks": L("Empty lines in table cells", "Empty paragraphs inside table cells deleted",
                           (r"paragraphs", r"rows"), (r"paragraphs", r"rows"), "paragraphs content separate rows deleted",
                           first="guide"),
    "fmt_table_caption": L("Table caption bold", "The table caption, label and text, in bold",
                           (r"label", r"bold"), (r"label", r"bold"), "label table caption content bold style"),
    "ref_lookup": L("Reference rebuilt from PubMed / CrossRef", "Entry rebuilt from the verified record in the journal's "
                    "reference style", (r"pubmed|index medicus",), (r"pubmed",), "update pubmed volume page number"),
    "ref_other": L("Reference format", "Books, websites and reports in the journal's template",
                   (r"^(?:books?|url|edited|websites?)\b",), (r"book|website|url",), "books edition publisher available from"),
    "ref_journal": L("Journal abbreviation", "Journal names abbreviated as in Index Medicus (NLM)",
                     (r"index\s+medicus|abbreviat",), (r"abbreviat|index medicus",),
                     "titles journals abbreviated index medicus"),
    "ref_et_al": L("et al. in references", "More than six authors: the first six, then et al.",
                   (r"et\s+al",), (r"et\s+al",), "six authors et al references"),
    "ref_pages": L("Page range", "Repeated digits dropped from the last page",
                   (r"page",), (r"page",), "page range repeated digits last page deleted"),
    "ref_initials": L("Author initials", "Initials without periods", (r"^journals?\s*\|",), (r"initial",),
                      "journals"),
    "ref_author_case": L("Author surname case", "A surname in capitals as a name (\"ALIZADEH GJ\" -> \"Alizadeh GJ\")",
                         (r"^journals?\s*\|",), (r"author",), "journals"),
    "ref_year": L("Year and volume", "Journal name, then year;volume", (r"^journals?\s*\|",), (), "journals"),
    "ref_issue": L("Issue number", "Issue numbers dropped", (r"^journals?\s*\|",), (r"issue",), "journals"),
    "ref_title_case": L("Reference title case", "Capital after a colon in the title", (r"^journals?\s*\|",),
                        (r"colon|case",), "journals"),
    # statistics, formatting, the guide's table
    "statistics": L("Statistics spacing", "Spaces around operators in statistics", (r"operator",),
                    (r"operator",), "space either side operators"),
    "fmt_p_value": L("P value", "The probability P in capital italics", (r"\bp[\s-]*values?\b",),
                     (r"probabilit",), "p value italic capital"),
    "fmt_n": L("n in italics", "The variable n in italics", (r"\bn\b.*italic|italic.*\bn\b",), (r"variable",),
               "italics variable n"),
    "fmt_et_al": L("et al. italic", "et al. in italics", (r"et\s+al", r"italic"), (r"et\s+al", r"italic"),
                   "et al italics period", "guide"),
    "fmt_latin": L("Latin terms italic", "in vivo, in vitro, in situ in italics", (r"in\s+vi(?:vo|tro)|latin",),
                   (r"in\s+vi(?:vo|tro)",), "italics in vivo in vitro"),
    "fmt_citations": L("Superscript citations", "Citation numbers superscript", (r"citation", r"superscript"),
                       (r"citation", r"superscript"), "superscript citation bracket"),
    "fmt_color": L("Font colour removed", "The author's coloured text set to automatic", (r"colou?r",),
                   (r"colou?r",), "color font text"),
    "fmt_table_head": L("Table column heads", "Column heads in bold", (r"column\s+heads?",), (r"column\s+head",),
                        "column heads bold sentence case"),
    "fmt_table_body": L("Table body", "Table body text not bold", (r"table\s+body",), (r"table\s+body|bold",),
                        "table body bold"),
    "fmt_label": L("Label bold", "Table and figure labels in bold", (r"label",), (r"label",),
                   "label table figure bold caption"),
    "fmt_heading_bold": L("Heading bold", "Headings in bold", (r"\bH[123]\s*\|", r"bold"), (r"heading", r"bold"),
                          "heading bold"),
    "fmt_heading_underline": L("Heading not underlined", "Headings without underline", (r"heading|\bH[123]\b", r"underline"),
                               (r"heading", r"underline"), "heading underline"),
    # owner's decisions (2026-10-26): no stylesheet line
    "fmt_symbol": L("Symbol font", "Greek letters, ±, ≤, ≥ and the minus sign in the Symbol font"),
    "fmt_heading_unbold": L("Heading not bold", "Third-level headings in italics, not bold"),
    "fmt_body_bold": L("Bold removed", "Stray bold in body text", (r"bold",), (r"bold",), "text should not be bold"),
    "guide.p_value_capital": L("P value", "The probability p as capital P", (r"\bp[\s-]*values?\b",), (r"probabilit",),
                               "p value capital", first="guide"),
    "guide.et_al_period": L("et al.", "et al. with a period", (r"et\s+al",), (r"et\s+al",), "et al period", "guide"),
    "guide.ampersand": L("& to and", "Ampersand spelled out outside references", (r"ampersand|&",), (r"&",),
                         "replace and", "guide"),
    "guide.operator_spacing": L("Operator spacing", "Spaces either side of operators", (r"operator",),
                                (r"operator",), "space either side operators", "guide"),
    "guide.percent_space": L("Percent sign", "No space before %", (r"percent|%",), (r"percent",),
                             "delete space before percentage", "guide"),
    "guide.value_unit_space": L("Value and unit", "A space between a value and its unit", (r"value", r"unit"),
                                (r"value", r"unit", r"add\s+space"), "add space between value and unit", "guide"),
    "guide.value_bracket_space": L("Value and bracket", "A space between a value and an opening bracket",
                                   (r"value", r"opening\s+bracket"), (r"value", r"opening\s+bracket", r"add\s+space"),
                                   "space value opening bracket",
                                   "guide"),
    "guide.bracket_inner_space": L("Spaces inside brackets", "No spaces just inside brackets", (r"bracket", r"space"),
                                   (r"bracket", r"delete\s+space"), "delete space opening closing bracket", "guide"),
    "guide.double_period": L("Double period", "A second period deleted", (r"period",), (r"second\s+period",),
                             "delete second period", "guide"),
    "guide.degree_space": L("Degree sign", "No space before °C", (r"degree|°",), (r"°|degree",),
                            "delete space value unit", "guide"),
    "guide.multiplication": L("Multiplication sign", "x as ×", (r"multiplication",), (r"multiplication",),
                              "multiplication symbol", "guide"),
    "guide.technetium": L("Isotope", "No space in 99mTc", (r"\bmtc\b|technetium",), (r"mtc",), "space", "guide"),
    "guide.nbsp": L("Non-breaking space", "Non-breaking spaces made normal spaces", (r"non-?\s*breakable|non-?\s*breaking",),
                    (r"non-?\s*breakable|non-?\s*breaking",), "replace non breakable space keyboard space", "guide"),
    "guide.unary_minus": L("Minus sign", "Hyphen as minus for a negative value", (r"minus",), (r"minus",),
                           "hyphen minus unary operator", "guide"),
    "guide.comparison_symbol": L("≤ and ≥", "<= and >= as the ≤ and ≥ symbols", (r"greater|less",),
                                 (r"less\s+than\s+and\s+equal|greater\s+than\s+or\s+equal",),
                                 "less than equal greater than or equal symbol", "guide"),
    "guide.unary_comparison": L("Comparison sign", "No space after > or < before a value", (r"unary|greater|lesser",),
                                (r"unary", r"greater|lesser|delete"), "delete space unary operator greater lesser",
                                "guide"),
    "guide.range_dash": L("Number range", "En dash in numeric ranges", (r"numer(?:ic|al)\s+range|range\s+of\s+values",),
                          (r"numer(?:ic|al)\s+range|range\s+of\s+values",),
                          "range en dash hyphen", "guide"),
    "guide.trailing_space": L("Trailing space", "Spaces at the end of a paragraph deleted", (r"space",),
                              (r"space", r"beginning|end"), "space beginning end deleted", "guide"),
    "guide.leading_space": L("Leading space", "Spaces at the start of a paragraph deleted", (r"space",),
                             (r"space", r"beginning|end"), "space beginning end deleted", "guide"),
    "guide.double_space": L("Double space", "Double spaces made single", (r"double\s+space|multiple\s+spaces",), (r"double\s+space|multiple\s+spaces",),
                            "double space single", "guide"),
    "guide.smart_quotes": L("Quotes", "Straight quotes made curly", (r"quot",), (r"quot",), "quotes curly single double",
                            "guide"),
    "guide.abbreviation_colon": L("Abbreviation list", "Abbreviations as term: definition", (r"abbreviation",),
                                  (r"abbreviation",), "abbreviations term definition colon", "guide"),
    "guide": L("Pre-editing guide", "A fix from the pre-editing guide's search-and-replace table", (), (), "",
               "guide"),
    # copy edit
    "text": L("Copy-edit text rule", "A copy editors' convention for this journal", (), (), ""),
    "text.prefixes": L("Prefixes closed up", "Prefixes written closed (noninvasive, postoperatively)",
                       (r"prefix", r"hyphen|close"), (r"prefix", r"hyphen|close"),
                       "prefix hyphen closed", "guide"),
    "text.ranges": L("Ranges in words", "between X and Y, from X to Y", (r"\bbetween\s+\d|\bfrom\s+\d",),
                     (r"\bbetween\s+\d|\bfrom\s+\d",),
                     "between and from to range"),
    "text.number_words": L("Numbers with units", "Numerals with units of time", (r"numer(?:al|ic)|number", r"word|spell"),
                           (r"numer(?:al|ic)|number", r"word|spell"), "numbers numerals words units"),
    "text.stat_semicolons": L("Statistics semicolons", "Semicolons between statistics", (r"semicolon",),
                              (r"semicolon",), "semicolon statistics"),
    "text.lowercase": L("Lower case", "Common terms in lower case", (r"lower\s*case|capital",),
                        (r"lower\s*case|capital",), "lower case capitalization"),
    "spelling": L("Spelling convention", "One spelling convention (British or American) throughout",
                  (r"american|british|\b(?:us|uk)\s+english",), (r"american|british|\b(?:us|uk)\s+english",),
                  "spelling american british"),
    "languagetool": L("Grammar and typography (LanguageTool)", "A LanguageTool rule", (r"grammar|punctuation",),
                      (r"grammar|punctuation",), "grammar punctuation"),
    "abbreviations": L("Abbreviations", "Abbreviations defined at first use and used after", (r"abbreviation",),
                       (r"abbreviation",), "abbreviations expanded first instance parentheses"),
    "claude": L("Claude copy edit", "Grammar, punctuation, articles and word choice; checked in code",
                (r"grammar|subject-verb|punctuation",), (r"grammar|punctuation",), "grammar punctuation agreement"),
    "editor_edit": L("Your edit", "Typed into the paragraph by an editor"),
}

# Reasons whose wording differs from the key ("spelling (american)", "languagetool:EN_A_VS_AN", "claude.grammar (...)").
_ALIASES = [(re.compile(r"^(?:expand abbreviation|use abbreviation|abbreviation )"), "abbreviations"),
            (re.compile(r"^fm\d"), "front_matter")]


def key_for(reason: str, provenance: dict | None = None) -> str:
    """The group key of a suggestion: its reason without a free-text tail, with the front-matter role."""
    r = (reason or "").strip()
    m = re.match(r"^front_matter\s*:\s*(\w+)", r)
    if m:
        return f"front_matter:{m.group(1)}"
    if r == "front_matter" and provenance and provenance.get("role"):
        return f"front_matter:{provenance['role']}"
    r = re.sub(r"\s*\(.*\)\s*$", "", r)  # "claude.grammar (subject-verb)" -> "claude.grammar"
    for rx, key in _ALIASES:
        if rx.match(r):
            return key
    return r


def locator(key: str) -> tuple[str, Locator] | None:
    """The mapping entry for a key, or for the nearest shorter key ("guide.x" -> "guide", "languagetool:ID" -> ...)."""
    k = key
    while k:
        if k in RULES:
            return k, RULES[k]
        cut = max(k.rfind("."), k.rfind(":"))
        if cut <= 0:
            break
        k = k[:cut]
    m = re.match(r"^(ref|fmt|sections|guide|text|submission)_", key)
    if m and m.group(1) in RULES:
        return m.group(1), RULES[m.group(1)]
    if key.startswith("submission_"):
        return "submission", RULES["submission"]
    return None


# --- the files -----------------------------------------------------------------------------------

class _Source:
    """One stylesheet or the guide: rule lines (stylesheets.load_rules) plus the raw lines for continuations."""

    def __init__(self, path: str, prefix: str):
        self.path = path
        self.rules = load_rules(path, prefix)
        self.index = RuleIndex(self.rules)
        with open(path, encoding="utf-8", errors="replace") as f:
            self.lines = [" ".join(x.split()) for x in f]

    def find(self, patterns: tuple[str, ...], terms: str) -> tuple[str, int] | None:
        if not patterns:
            return None
        rx = [re.compile(p, re.I | re.M) for p in patterns]

        def ok(text: str) -> bool:
            flat = text.replace(" / ", " ")  # " / " joins sub-rules and wrapped cells in the files
            return all(r.search(flat) for r in rx)

        hits = [r for r in self.rules if ok(r.text)]
        if not hits:
            return None
        ranked = self.index.search(_words(terms), len(self.rules)) if terms.strip() else []
        order = {id(r): i for i, r in enumerate(ranked)}
        best = min(hits, key=lambda r: (order.get(id(r), 10 ** 6), r.line))
        return self._quote(best, rx[0]), best.line

    def _quote(self, rule, focus=None) -> str:
        text = rule.text
        # a table row wrapped onto a second line in the file (the cell text continues in lower case)
        if rule.line and not re.search(r"[.!?:)\]]$", text) and rule.line < len(self.lines):
            nxt = self.lines[rule.line]
            if nxt and len(nxt) < 160 and not re.match(r"^\d+\s*\|", nxt) and " | " not in nxt and nxt[:1].islower():
                text = f"{text} {nxt}"
        row = re.match(r"^\d+\s*\|\s*(.+)$", text)
        if row:  # the guide's search-and-replace table: "5 | p | P | Probability ..." -> "p → P: Probability ..."
            cells = [c.strip() for c in row.group(1).split(" | ")]
            cells[-1] = cells[-1].replace(" / ", " ")
            text = f"{cells[0]} → {cells[1]}: {cells[2]}" if len(cells) == 3 else ": ".join(cells)
        if len(text) <= MAX_QUOTE:
            return text
        m = focus.search(text) if focus is not None else None
        if m is None or m.end() < MAX_QUOTE - 40:
            return text[:MAX_QUOTE - 1].rstrip() + "…"
        # a long line: the sentence holding the match
        start = max(text.rfind(". ", 0, m.start()), text.rfind(" / ", 0, m.start()))
        start = start + 2 if start >= 0 else max(0, m.start() - 80)
        piece = text[start:].lstrip("/ ")
        return "…" + (piece if len(piece) <= MAX_QUOTE else piece[:MAX_QUOTE - 1].rstrip() + "…")


class Resolver:
    """Rule quotes for one journal, memoised per key."""

    def __init__(self, journal: str, sheet_path: str | None, guide_path: str | None):
        self.journal = style_mod.resolve(journal)
        self.sheet = _Source(sheet_path, "S") if sheet_path else None
        self.guide = _Source(guide_path, "G") if guide_path else None
        self.memo: dict[str, dict] = {}
        self.lock = threading.Lock()

    @property
    def sheet_name(self) -> str:
        return f"{self.journal.upper()} stylesheet"

    def rule(self, key: str) -> dict:
        with self.lock:
            if key not in self.memo:
                self.memo[key] = self._resolve(key)
            return self.memo[key]

    def _resolve(self, key: str) -> dict:
        hit = locator(key)
        out = {"key": key, "label": None, "description": None, "quote": None, "where": None, "line": None}
        if hit is None:
            return out
        _, loc = hit
        out.update(label=loc.label, description=loc.description or None)
        tries = [("sheet", self.sheet, loc.sheet), ("guide", self.guide, loc.guide)]
        if loc.first == "guide":
            tries.reverse()
        for kind, src, patterns in tries:
            if src is None:
                continue
            found = src.find(patterns, loc.terms)
            if found:
                out.update(quote=found[0], line=found[1],
                           where=self.sheet_name if kind == "sheet" else "Pre-editing guide")
                break
        return out


_CACHE: dict[tuple, Resolver] = {}
_CACHE_LOCK = threading.Lock()


def _stamp(path: str | None) -> float:
    try:
        return os.path.getmtime(path) if path else 0.0
    except OSError:
        return 0.0


def resolver(journal: str, sheet_dir: str | None = None, guide_path: str | None = None) -> Resolver:
    """The cached resolver for a journal (per stylesheet / guide file and version). Directories default to the
    environment (EDITOS_STYLESHEETS, EDITOS_PREEDIT_GUIDE)."""
    sheet_dir = os.environ.get(ENV_SHEETS, "") if sheet_dir is None else sheet_dir
    guide_path = os.environ.get(ENV_GUIDE, "") if guide_path is None else guide_path
    sheet = find_stylesheet(journal, sheet_dir) if sheet_dir else None
    guide = guide_path if guide_path and os.path.isfile(guide_path) else None
    key = (style_mod.resolve(journal), sheet, _stamp(sheet), guide, _stamp(guide))
    with _CACHE_LOCK:
        r = _CACHE.get(key)
        if r is None:
            r = _CACHE[key] = Resolver(journal, sheet, guide)
        return r


def configured() -> dict:
    """What the server can quote from (for the review screen's note when nothing is configured)."""
    d = os.environ.get(ENV_SHEETS, "")
    g = os.environ.get(ENV_GUIDE, "")
    return {"stylesheets": bool(d and os.path.isdir(d)), "guide": bool(g and os.path.isfile(g))}


def rule_for(journal: str, reason: str, provenance: dict | None = None) -> dict:
    return resolver(journal).rule(key_for(reason, provenance))
