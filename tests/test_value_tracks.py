import value_tracks as vt
from value_fundamentals import Fundamentals


def _A(**kw):
    base = dict(ticker="A", market_cap=5e9, is_profitable=True, pe=14.0, peg=0.8,
                ev_ebitda=9.0, ps=2.0, rev_growth=0.2, eps_growth=0.15, gross_margin=0.4,
                op_margin=0.1, debt_equity=0.5, current_ratio=2.0, fcf=4e8, total_cash=1e9)
    base.update(kw); return Fundamentals(**base)


def _B(**kw):
    base = dict(ticker="B", market_cap=2e9, is_profitable=False, pe=None, peg=None,
                ev_ebitda=None, ps=4.0, rev_growth=0.4, eps_growth=None, gross_margin=0.5,
                op_margin=-0.1, debt_equity=0.3, current_ratio=None, fcf=-2e8, total_cash=5e9)
    base.update(kw); return Fundamentals(**base)


def test_classify_routes_by_profitability_and_cap():
    assert vt.classify(_A()) == "A"
    assert vt.classify(_B()) == "B"
    assert vt.classify(_A(market_cap=1e8)) is None    # below cap floor


def test_track_a_accepts_and_each_gate_rejects():
    assert vt.passes(_A(), "A") is True
    assert vt.passes(_A(peg=1.5), "A") is False
    assert vt.passes(_A(pe=30), "A") is False
    assert vt.passes(_A(rev_growth=0.05), "A") is False
    assert vt.passes(_A(eps_growth=0.0), "A") is False
    assert vt.passes(_A(gross_margin=0.2), "A") is False
    assert vt.passes(_A(debt_equity=2.0), "A") is False
    assert vt.passes(_A(current_ratio=1.0), "A") is False
    assert vt.passes(_A(fcf=-1.0), "A") is False


def test_track_b_accepts_and_each_gate_rejects():
    assert vt.passes(_B(), "B") is True                 # fcf -2e8, cash 5e9 → runway 100q
    assert vt.passes(_B(ps=8.0), "B") is False
    assert vt.passes(_B(rev_growth=0.1), "B") is False
    assert vt.passes(_B(gross_margin=0.3), "B") is False
    assert vt.passes(_B(debt_equity=2.0), "B") is False
    assert vt.passes(_B(total_cash=1e7), "B") is False  # runway < 6q


def test_failopen_requires_a_signal_of_each_class():
    # all-None shell: no cheap/growth/solvency signal present → reject
    shell = Fundamentals(ticker="X", market_cap=5e9, is_profitable=True, pe=None, peg=None,
                         ev_ebitda=None, ps=None, rev_growth=None, eps_growth=None,
                         gross_margin=None, op_margin=None, debt_equity=None,
                         current_ratio=None, fcf=None, total_cash=None)
    assert vt.passes(shell, "A") is False


def test_score_orders_cheaper_higher_quality_first():
    cheap = _A(pe=8.0, peg=0.5, rev_growth=0.3, gross_margin=0.5)
    rich = _A(pe=19.0, peg=0.95, rev_growth=0.16, gross_margin=0.31)
    assert vt.score(cheap, "A") > vt.score(rich, "A")


def test_score_rewards_cashflow_positive_over_barely_burning():
    # cash-flow positive (inf runway) must not score BELOW a barely-burning peer
    base = dict(ticker="B", market_cap=2e9, is_profitable=False, pe=None, peg=None,
                ev_ebitda=None, ps=4.0, rev_growth=0.4, eps_growth=None, gross_margin=0.5,
                op_margin=None, debt_equity=0.3, current_ratio=None)
    positive = Fundamentals(**base, fcf=1e8, total_cash=5e9)   # inf runway
    barely = Fundamentals(**base, fcf=-1.0, total_cash=5e9)    # huge finite runway → 1.0
    assert vt.score(positive, "B") >= vt.score(barely, "B")


def test_partial_data_without_growth_or_liquidity_signal_rejected():
    # cheap (peg) + margin + leverage present, but NO real growth metric and NO
    # current_ratio/fcf → the growth & liquidity gates were never verified, so a
    # partial-.info name must NOT slip through Track A.
    f = Fundamentals(ticker="X", market_cap=5e9, is_profitable=True, pe=None, peg=0.5,
                     ev_ebitda=None, ps=None, rev_growth=None, eps_growth=None,
                     gross_margin=0.5, op_margin=None, debt_equity=0.2,
                     current_ratio=None, fcf=None, total_cash=None)
    assert vt.classify(f) == "A"
    assert vt.passes(f, "A") is False
