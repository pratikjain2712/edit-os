"""Editing agent (prototype): Claude reads the whole manuscript and proposes copy edits and author queries.

Unlike the chunked Claude pass (copyedit/claude_pass.py: tiny edits, one chunk at a time), the agent sees the
whole manuscript, the journal's stylesheet, a summary of the journal style and real copy-editor edits from
training articles, and works through tools (edit_os/agent/tools.py):

  read_paragraphs / search_text   look at the text again (exact current wording, other uses of a term)
  propose_edits                   copy edits; each is checked in code before it is kept (below)
  add_queries                     author queries anchored on a phrase of the paragraph
  check_reference                 the code-verified lookup of reference n (refengine.resolve: PubMed / CrossRef
                                  record matched on title, authors and year) and where it is cited

Safeguards, all in code: an edit's `old` must occur exactly once in its paragraph and becomes exact offsets
(Change.old carries the text); the change is trimmed to the words that change and refused when it touches digits,
citations, symbols, acronyms, capitalised names, URLs or hidden content (claude_pass.verify / unsafe), spans more
than one sentence, adds a sentence or much new text, rewords text that holds numbers, changes a capitalised word
that may be a name, is only spacing, is longer than a clause (agent.max_words / max_chars), overlaps an edit
already kept, or is in a paragraph that is not running text (headings, tables, the reference list). A query that
names a reference number is refused until check_reference has been called for it, and one saying a work is not in
the reference list is refused when the list holds the name it is anchored on, so no query about a reference rests
on model output alone. Every kept edit is a Change with source AI and status suggested: editors decide. Edits
Claude marks "likely" are held back unless agent.min_confidence is "likely" (on the tuning articles they agreed
with the copy editor 39% of the time, "certain" ones 68%).

Cost: each API call is priced as it happens; the loop stops at agent.budget_usd (default $2) or agent.max_turns.
Responses are cached on disk keyed by the full request, so a rerun of the same article is free.

Settings: `copyedit.agent` in the journal style (off unless enabled: true); needs EDITOS_ANTHROPIC_API_KEY.
EDITOS_AGENT_MODEL, EDITOS_AGENT_EFFORT, EDITOS_AGENT_BUDGET override; examples come from EDITOS_CE_EXAMPLES and
the stylesheet from EDITOS_STYLESHEETS.
"""

from .loop import AgentResult, collect, enabled, run, settings  # noqa: F401
