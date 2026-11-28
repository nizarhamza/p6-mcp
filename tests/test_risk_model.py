"""p6_schedule_risk risk model and worker processes (3.8.0) on the synthetic project DEMO-A (a chain A1010 ->
A1020 -> A1030 -> A1040 -> A1050, A1060 Permits beside it with 20 days of float): the draws of an iteration do not
depend on how the iterations are shared (same output for 1 and 2 worker processes, spawn), a failed pool falls back
to the server process, a risk event of probability 1 is a fixed delay, the finish impact of an event is the
difference of the mean delays, coefficient 1 gives every activity of a group the same relative draw, the rank
correlation of the Gaussian copula, P6 risks (PROJRISK / TASKRISK) quantified by risks=, refusals, and the frozen
exe guard of the entry point."""

import json
import math
import random

import pytest

ZERO = {"min_pct": 100, "mode_pct": 100, "max_pct": 100}


def risk(p6, **kw):
    return p6.call("p6_schedule_risk", project="DEMO-A", **kw)


def whatif_finish(p6, **remaining_days) -> str:
    edits = [{"op": "duration", "task_code": c, "remaining_days": d} for c, d in remaining_days.items()]
    return p6.call("p6_schedule_whatif", project="DEMO-A", edits=edits, compare_to="engine")["project_finish"]["after"]


def without(r: dict, *keys) -> str:
    return json.dumps({k: v for k, v in r.items() if k not in keys}, sort_keys=True, default=str)


@pytest.fixture
def capture(monkeypatch):
    """The _RiskSim of the next p6_schedule_risk call (the iterations still run)."""
    from p6mcp.tools import engine
    got = {}
    real = engine._risk_iterate

    def spy(sim, n, w):
        got["sim"] = sim
        return real(sim, n, w)
    monkeypatch.setattr(engine, "_risk_iterate", spy)
    return got


def test_triangular_and_skip_reproduce_random():
    from p6mcp.core.risk import _skip, _tri
    for lo, hi, mo in [(0, 1, 0.3), (85.0, 135.0, 100.0), (5, 5, 5), (2, 8, 2), (2, 8, 8), (-3, 4, 1)]:
        for s in range(50):
            a, b = random.Random(s), random.Random(s)
            assert a.triangular(lo, hi, mo) == _tri(b.random(), lo, hi, mo)
    for n in (0, 1, 7, 100_000):
        a, b = random.Random(11), random.Random(11)
        _skip(a, n)
        for _ in range(n):
            b.random()
        assert a.random() == b.random()


def test_no_model_keeps_the_shape(p6):
    from p6mcp.core import risk as core
    r = risk(p6, iterations=100, workers=1)
    assert r["risk_events"] == [] and r["p6_risks"] is None and r["caveat"] == core.RISK_CAVEAT
    assert r["distribution"]["correlations"] == [] and r["distribution"]["counts"]["correlated"] == 0
    assert r["parallel"] == {"workers_requested": 1, "workers": 1, "note": None}
    d = risk(p6, iterations=100)  # default workers: too small a run for worker processes
    assert d["parallel"]["workers_requested"] == core.RISK_WORKERS_DEFAULT and d["parallel"]["workers"] == 1
    assert without(d, "parallel") == without(r, "parallel")


def test_worker_processes_give_the_same_output(p6, monkeypatch):
    from p6mcp.core import risk as core
    monkeypatch.setattr(core, "RISK_POOL_MIN_WORK", 0)
    kw = dict(iterations=300, seed=5, correlations=[{"wbs": "CON", "coefficient": 0.6}],
              risks=[{"id": "R1", "probability": 0.4, "impact": {"task_codes": ["A1020"],
                                                                 "add_days": {"min": 1, "likely": 3, "max": 9}}}])
    one = risk(p6, workers=1, **kw)
    two = risk(p6, workers=2, **kw)
    assert one["parallel"]["workers"] == 1 and two["parallel"] == {"workers_requested": 2, "workers": 2, "note": None}
    assert without(one, "parallel") == without(two, "parallel")
    plain = [risk(p6, iterations=300, seed=5, workers=w) for w in (1, 2)]
    assert plain[1]["parallel"]["workers"] == 2 and without(plain[0], "parallel") == without(plain[1], "parallel")


def test_a_failed_pool_runs_in_the_server(p6, monkeypatch):
    from p6mcp.core import risk as core
    monkeypatch.setattr(core, "RISK_POOL_MIN_WORK", 0)

    def broken(*a, **k):
        raise OSError("no process")
    monkeypatch.setattr(core, "ProcessPoolExecutor", broken)
    r = risk(p6, iterations=200, workers=2)
    assert r["parallel"]["workers"] == 1 and "OSError: no process" in r["parallel"]["note"]
    assert without(r, "parallel") == without(risk(p6, iterations=200, workers=1), "parallel")


def test_certain_and_impossible_events(p6):
    from p6mcp.core.risk import RISK_CAVEAT_MODEL
    sure = [{"id": "R1", "name": "Late steel", "probability": 1, "impact": {"task_codes": ["A1020"], "add_days": 5}}]
    r = risk(p6, iterations=100, default_range=ZERO, risks=sure)
    assert set(r["finish"].values()) == {whatif_finish(p6, A1020=20)}  # 15 + 5 days, every iteration
    ev = r["risk_events"][0]
    assert (ev["fired"], ev["fired_pct"], ev["activities"], ev["rank"]) == (100, 100.0, 1, 1)
    assert ev["mean_delay_fired_wd"] == 5.0 and ev["mean_delay_not_fired_wd"] is None
    assert ev["finish_impact_wd"] is None and ev["share_pct"] is None
    assert ev["add_days"] == {"min": 5.0, "likely": 5.0, "max": 5.0} and r["caveat"] == RISK_CAVEAT_MODEL
    never = risk(p6, iterations=100, default_range=ZERO, risks=[{**sure[0], "probability": 0}])
    assert set(never["finish"].values()) == {never["deterministic"]["finish"]}
    assert never["risk_events"][0]["fired"] == 0 and never["risk_events"][0]["mean_delay_fired_wd"] is None
    # an opportunity cannot take the remaining duration below 0: A1020 (15 d) - 99 d = 0 d
    opp = risk(p6, iterations=100, default_range=ZERO, risks=[{**sure[0], "impact": {"task_codes": ["A1020"],
                                                                                    "add_days": -99}}])
    assert opp["finish"]["p50"] == whatif_finish(p6, A1020=0)


def test_event_impact_is_the_difference_of_means(p6):
    risks = [{"id": "R-float", "probability": 0.5, "impact": {"task_codes": ["A1060"], "add_days": 5}},
             {"id": "R-crit", "probability": 0.5, "impact": {"wbs": "ENG", "add_days": 5}}]
    r = risk(p6, iterations=400, seed=3, default_range=ZERO, risks=risks)
    crit, flt = r["risk_events"]
    assert (crit["id"], crit["rank"], flt["id"], flt["rank"]) == ("R-crit", 1, "R-float", 2)
    # ENG = A1000 (completed), A1010, A1020: the two open ones each take 5 days, both on the critical chain
    assert (crit["activities"], crit["activities_not_drawn"]) == (2, 1)
    assert crit["mean_delay_fired_wd"] == 10.0 and crit["mean_delay_not_fired_wd"] == 0.0
    assert crit["finish_impact_wd"] == 10.0 and 30 < crit["fired_pct"] < 70
    assert crit["expected_delay_wd"] == round(10.0 * crit["fired"] / 400, 2) and crit["share_pct"] == 100.0
    # 20 days of float absorb 5: no effect, apart from the sampling noise of a difference of means (the
    # iterations where it fired are not exactly those where R-crit did not)
    assert abs(flt["finish_impact_wd"]) < 1.5 and flt["share_pct"] == 0.0
    assert r["on_time_probability"]["deterministic_pct"] == round(100 - crit["fired_pct"], 2)
    # a triangular impact: drawn once per firing
    tri = risk(p6, iterations=300, default_range=ZERO,
               risks=[{"id": "T", "probability": 1, "impact": {"task_codes": ["A1020"],
                                                                "add_days": {"min": 2, "likely": 5, "max": 8}}}])
    assert whatif_finish(p6, A1020=17) <= tri["finish"]["min"] < tri["finish"]["max"] <= whatif_finish(p6, A1020=23)


def test_coefficient_one_moves_a_group_together(p6, capture):
    group = ["A1010", "A1020", "A1030", "A1040"]
    r = risk(p6, iterations=100, correlations=[{"task_codes": group, "coefficient": 1}])
    sim = capture["sim"]
    T = sim.cpm.t
    ids = [sim.cpm.code[c] for c in group]
    rng = random.Random(1)
    for i in range(50):
        rem, _ = sim.draw(rng, i)
        ratios = [rem[n] / T[n]["rem"] for n in ids]
        assert ratios == pytest.approx([ratios[0]] * 4, rel=1e-12)
        assert 0.85 - 1e-12 <= ratios[0] <= 1.35 + 1e-12
    g = r["distribution"]["correlations"][0]
    assert (g["coefficient"], g["matched"], g["applied"], g["in_earlier_group"]) == (1.0, 4, 4, 0)
    assert r["distribution"]["counts"]["correlated"] == 4
    # the spread of the finish widens when the chain moves together
    alone = risk(p6, iterations=1000)
    tied = risk(p6, iterations=1000, correlations=[{"task_codes": group, "coefficient": 1}])
    spread = lambda x: x["finish"]["p90"] > x["finish"]["p10"] and (x["finish"]["p10"], x["finish"]["p90"])  # noqa: E731
    assert spread(tied)[0] < spread(alone)[0] and spread(tied)[1] > spread(alone)[1]
    # coefficient 0 is no correlation at all: the same draws as without the group
    zero = risk(p6, iterations=200, correlations=[{"task_codes": group, "coefficient": 0}])
    assert zero["finish"] == risk(p6, iterations=200)["finish"] and zero["distribution"]["counts"]["correlated"] == 0


def test_rank_correlation_of_the_copula(p6, capture):
    risk(p6, iterations=100, correlations=[{"task_codes": ["A1020", "A1030"], "coefficient": 0.5},
                                           {"wbs": "CON", "coefficient": 0.9}])
    sim = capture["sim"]
    a, b = sim.cpm.code["A1020"], sim.cpm.code["A1030"]
    rng = random.Random(9)
    xs, ys = [], []
    for i in range(4000):
        rem, _ = sim.draw(rng, i)
        xs.append(rem[a])
        ys.append(rem[b])
    rank = lambda v: {k: r for r, k in enumerate(sorted(range(len(v)), key=v.__getitem__))}  # noqa: E731
    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    rho = 1 - 6 * sum((rx[k] - ry[k]) ** 2 for k in range(n)) / (n * (n * n - 1))
    assert abs(rho - 6 / math.pi * math.asin(0.25)) < 0.05  # Spearman of a Gaussian copula with 0.5
    # A1030 is in both groups: the first one listed keeps it
    groups = {(n, g) for n, g in zip(sim.sampled, [c and c[0] for c in sim.corr], strict=True) if g is not None}
    assert (b, 0) in groups


def test_p6_risks_quantified_by_risk_id(p6):
    listed = risk(p6, iterations=100, use_p6_risks=True)
    assert listed["risk_events"] == []
    assert listed["p6_risks"]["found"] == 1 and listed["p6_risks"]["simulated"] == 0
    assert listed["p6_risks"]["not_quantified"] == [{"risk_code": "R-01", "name": "Late design data", "activities": 1,
                                                     "p6_values": {"status_code": "RS_Open"}}]
    item = [{"id": "R-01", "probability": 1, "impact": {"add_days": 10}}]
    r = risk(p6, iterations=100, default_range=ZERO, use_p6_risks=True, risks=item)
    assert r["finish"]["p50"] == whatif_finish(p6, A1030=35)  # TASKRISK: A1030 Site works, 25 + 10 days
    ev = r["risk_events"][0]
    assert (ev["source"], ev["name"], ev["selector"], ev["activities"]) == ("p6", "Late design data",
                                                                           {"p6_activities": True}, 1)
    assert r["p6_risks"]["simulated"] == 1 and r["p6_risks"]["not_quantified"] == []
    with pytest.raises(ValueError, match="give exactly one of task_codes, wbs or code_type"):
        risk(p6, iterations=100, risks=item)  # without use_p6_risks the id is just a name


@pytest.mark.parametrize("kw, match", [
    ({"correlations": [{"task_codes": ["A1010"], "coefficient": 1.5}]}, "coefficient is a number from 0"),
    ({"correlations": [{"task_codes": ["A1010"]}]}, "coefficient is a number from 0"),
    ({"correlations": [{"task_codes": ["A1010"], "wbs": "ENG", "coefficient": 1}]}, "exactly one of"),
    ({"correlations": [{"task_codes": ["A1010", "X9"], "coefficient": 1}]}, "activity X9 not found"),
    ({"correlations": [{"task_code": "A1010", "coefficient": 1}]}, "unknown keys"),
    ({"risks": [{"probability": 1, "impact": {"task_codes": ["A1010"], "add_days": 1}}]}, "id is required"),
    ({"risks": [{"id": "R", "probability": 2, "impact": {"task_codes": ["A1010"], "add_days": 1}}]}, "from 0 to 1"),
    ({"risks": [{"id": "R", "probability": 0.5, "impact": {"task_codes": ["A1010"]}}]}, "impact needs add_days"),
    ({"risks": [{"id": "R", "probability": 0.5, "impact": {"task_codes": ["A1010"],
                                                           "add_days": {"min": 5, "likely": 2, "max": 8}}}]},
     "min <= likely <= max"),
    ({"risks": [{"id": "R", "probability": 0.5, "impact": {"task_codes": ["A1010"], "add_days": 1}}] * 2},
     "given twice"),
    ({"workers": 0}, "workers must be"),
    ({"workers": 1.5}, "workers must be"),
])
def test_refusals(p6, kw, match):
    with pytest.raises(ValueError, match=match):
        risk(p6, iterations=100, **kw)


def test_entry_point_runs_freeze_support_first(monkeypatch):
    import multiprocessing
    from p6mcp import __main__ as entry

    class Worker(Exception):
        pass

    def freeze():
        raise Worker
    monkeypatch.setattr(multiprocessing, "freeze_support", freeze)
    monkeypatch.setattr("sys.argv", ["p6mcp-server", "--multiprocessing-fork"])
    with pytest.raises(Worker):  # before any server code: a frozen worker never reaches mcp.run()
        entry.main()
