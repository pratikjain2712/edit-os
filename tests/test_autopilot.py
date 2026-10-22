"""Safe autopilot: which pending suggestions it accepts, the second Claude check, the API and undo."""

from edit_os import autopilot, style
from edit_os.changes import Change, Layer, Source, Status
from edit_os.frontmatter import NewParagraph

from test_api import KEY, client, upload, wait  # noqa: F401  (fixture)


def ch(id, reason, source=Source.RULE, layer=Layer.HOUSE_STYLE, status=Status.SUGGESTED):
    return Change(id=id, para_id="p0001", start=0, end=3, old="abc", new="abd", layer=layer, source=source,
                  reason=reason, status=status)


def test_plan_accepts_rules_and_judged_ai_edits():
    st = style.load("ijo")
    changes = [ch("r1", "citations"), ch("r2", "ref_lookup", layer=Layer.REFERENCES),
               ch("r3", "spelling (american)", layer=Layer.LANGUAGE), ch("r4", "guide.dash", status=Status.REJECTED),
               ch("a1", "claude.article (missing article)", Source.AI, Layer.LANGUAGE),
               ch("a2", "claude.word_choice (better word)", Source.AI, Layer.LANGUAGE),
               ch("a3", "claude.punctuation", Source.AI, Layer.LANGUAGE)]
    inserts = [NewParagraph(id="fm1", anchor="p0001", where="before", runs=[("Original Article", {})], role="type")]
    seen = []

    def judge(items):
        seen.extend(c.id for c in items)
        return {"a1": ("accept", "plain error"), "a2": ("review", "a matter of taste")}

    p = autopilot.plan(changes, inserts, st, judge=judge)
    assert seen == ["a1", "a2", "a3"]
    assert p.accept == ["r1", "r2", "r3", "fm1", "a1"]
    left = {d["id"]: d["why"] for d in p.left}
    assert left == {"a2": "second check: a matter of taste", "a3": "second check: no answer from the second check"}
    assert p.categories["r2"] == "references_verified" and p.categories["fm1"] == "front_matter"
    assert p.judge == {"used": True, "asked": 3, "agreed": 1}


def test_jehp_holds_the_rules_its_editors_confirm():
    # JEHP: stylesheet / guide rules the copy editors mostly skip stay suggestions; Claude's pass is off and held.
    st = style.load("jehp")
    assert st.get("copyedit.claude.enabled") is False
    changes = [ch("h1", "use abbreviation after its definition"), ch("h2", "sections_abstract.rename:methods"),
               ch("h3", "sections_back_matter.added"), ch("h4", "guide.unary_comparison"),
               ch("h7", "claude.grammar (agreement)", Source.AI, Layer.LANGUAGE), ch("k5", "fig_captions.case"),
               ch("k1", "sections_abstract.rename:conclusion"), ch("k2", "fig_captions.label"),
               ch("k3", "fig_captions.end_period"), ch("k4", "ref_year", layer=Layer.REFERENCES)]
    p = autopilot.plan(changes, [], st, judge=lambda items: {c.id: ("accept", "") for c in items})
    assert p.accept == ["k5", "k1", "k2", "k3", "k4"]
    assert {d["id"] for d in p.left} == {"h1", "h2", "h3", "h4", "h7"}


def test_plan_holds_and_needs_a_key_for_ai_edits():
    st = style.load("ijo")
    st.settings = {**st.settings, "autopilot": {**st.settings["autopilot"], "hold": ["renumber", "guide.dash"]}}
    changes = [ch("r1", "renumber"), ch("r2", "guide.dash"), ch("r3", "guide.dashes_more"),
               ch("a1", "claude.grammar", Source.AI, Layer.LANGUAGE)]
    p = autopilot.plan(changes, [], st)  # no Claude key in tests: AI edits stay pending
    assert p.accept == ["r3"]
    assert [d["id"] for d in p.left] == ["r1", "r2", "a1"]
    assert "no Claude key" in p.left[-1]["why"] and not p.judge["used"]


def test_judge_parses_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "_cache", autopilot._Cache(str(tmp_path / "ap.sqlite")))
    c = Change(id="ai1", para_id="p1", start=9, end=12, old="was", new="were", layer=Layer.LANGUAGE,
               source=Source.AI, reason="claude.agreement")
    texts = {"p1": "The data was analysed. " + "The results were clear. " * 30}
    calls = []

    def call(model, payload):
        calls.append(payload)
        return {"verdicts": [{"id": "ai1", "verdict": "accept", "why": "agreement"}, {"bad": 1}]}

    assert autopilot.judge_edits([c], {}, texts, call) == {"ai1": ("accept", "agreement")}
    assert autopilot.judge_edits([c], {}, texts, call) == {"ai1": ("accept", "agreement")}
    assert len(calls) == 1 and "[[was => were]]" in calls[0] and len(calls[0]) < len(texts["p1"])

    def broken(model, payload):
        raise RuntimeError("down")

    other = Change(id="ai2", para_id="p1", start=0, end=3, old="The", new="Our", layer=Layer.LANGUAGE,
                   source=Source.AI, reason="claude.other")
    assert autopilot.judge_edits([other], {}, texts, broken)["ai2"][0] == "review"


def test_autopilot_endpoint_and_undo(client, tmp_path):
    job_id = upload(client, tmp_path).json()["id"]
    assert wait(client, job_id)["status"] == "done"
    assert client.get(f"/api/jobs/{job_id}/autopilot", headers=KEY).status_code == 404
    items = client.get(f"/api/jobs/{job_id}/changes", headers=KEY).json()["items"]
    keep = items[0]["id"]  # an editor's earlier decision is left alone
    client.post(f"/api/jobs/{job_id}/decisions", headers=KEY, json={"ids": [keep], "status": "rejected"})

    r = client.post(f"/api/jobs/{job_id}/autopilot", headers=KEY)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["accepted"] == len(items) - 1 - rep["left"] and rep["accepted"] > 0
    assert rep["qc"]["passed"] in (True, False) and rep["by_category"]
    after = {i["id"]: i["status"] for i in client.get(f"/api/jobs/{job_id}/changes", headers=KEY).json()["items"]}
    assert after[keep] == "rejected"
    assert sum(s == "accepted" for s in after.values()) == rep["accepted"]
    assert client.get(f"/api/jobs/{job_id}/autopilot", headers=KEY).json()["accepted"] == rep["accepted"]

    u = client.post(f"/api/jobs/{job_id}/autopilot/undo", headers=KEY).json()
    assert u["undone"] == rep["accepted"]
    after = {i["id"]: i["status"] for i in client.get(f"/api/jobs/{job_id}/changes", headers=KEY).json()["items"]}
    assert after[keep] == "rejected" and sum(s == "accepted" for s in after.values()) == 0
    assert client.get(f"/api/jobs/{job_id}/autopilot", headers=KEY).status_code == 404
    again = client.post(f"/api/jobs/{job_id}/autopilot", headers=KEY).json()  # after an undo it runs again
    assert again["accepted"] == rep["accepted"]
