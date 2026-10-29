import json
import threading

from edit_os import spend


def test_ledger_prices_and_parts(tmp_path):
    with spend.track() as led:
        spend.add_usage("copy_edit", "claude-opus-5-5", {"input_tokens": 1_000_000, "output_tokens": 100_000})
        spend.add("reference_web_search", "claude-haiku-5-5", input_tokens=10, cost=0.02, searches=2)
    spend.add("copy_edit", "claude-opus-5-5", input_tokens=10**9)  # outside any article: not counted
    d = led.to_dict()
    assert d["parts"]["copy_edit"]["usd"] == 6.0  # 1M in at $4 + 0.1M out at $20
    assert d["parts"]["reference_web_search"] == {"calls": 1, "input_tokens": 10, "searches": 2, "usd": 0.02}
    assert d["usd"] == 6.02
    rec = spend.record(str(tmp_path), "edit", led, words=3000, journal="IJO")
    assert rec["pages"] == 10.0 and rec["usd_per_page"] == 0.602
    with spend.track() as led2:
        spend.add_usage("autopilot_check", "claude-haiku-5-5", {"input_tokens": 1_000_000})
    rec = spend.record(str(tmp_path), "autopilot", led2)
    assert [s["step"] for s in rec["steps"]] == ["edit", "autopilot"] and rec["usd"] == 6.12
    assert json.load(open(tmp_path / spend.FILE))["words"] == 3000


def test_parallel_articles_keep_their_own_ledgers():
    out = {}

    def job(name, n):
        with spend.track() as led:
            for _ in range(n):
                spend.add("copy_edit", "claude-haiku-5-5", input_tokens=1_000_000)
            out[name] = round(led.total(), 4)

    ts = [threading.Thread(target=job, args=(f"a{i}", i + 1)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert out == {"a0": 0.1, "a1": 0.2, "a2": 0.3, "a3": 0.4}


def test_unknown_model_priced_as_the_dearest():
    assert spend.usd("claude-mystery", output_tokens=1_000_000) == 20.0
