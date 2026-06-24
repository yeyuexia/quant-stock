"""
Risk management and portfolio analytics.

Key metrics:
  - Portfolio volatility and Sharpe ratio
  - Max drawdown
  - Value-at-Risk (VaR) and Conditional VaR
  - Position sizing via Kelly criterion (half-Kelly for safety)
  - Correlation matrix for diversification check
"""
import numpy as np
import pandas as pd
from scipy import stats


def portfolio_stats(returns: pd.DataFrame, weights: np.ndarray,
                    risk_free_rate: float = 0.05) -> dict:
    """Compute annualized portfolio statistics."""
    port_ret = (returns * weights).sum(axis=1)
    ann_ret = port_ret.mean() * 252
    ann_vol = port_ret.std() * np.sqrt(252)
    sharpe = (ann_ret - risk_free_rate) / ann_vol if ann_vol > 0 else 0

    # Max drawdown
    cum = (1 + port_ret).cumprod()
    peak = cum.cummax()
    dd = (cum - peak) / peak
    max_dd = dd.min()

    # VaR and CVaR at 95%
    var_95 = np.percentile(port_ret, 5)
    cvar_95 = port_ret[port_ret <= var_95].mean()

    return {
        "ann_return": ann_ret,
        "ann_volatility": ann_vol,
        "sharpe_ratio": sharpe,
        "max_drawdown": max_dd,
        "var_95_daily": var_95,
        "cvar_95_daily": cvar_95,
        "win_rate": (port_ret > 0).mean(),
        "best_day": port_ret.max(),
        "worst_day": port_ret.min(),
    }


def half_kelly(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """Half-Kelly position sizing. Returns fraction of capital to risk.

    Robust against near-zero avg_loss (which would divide-by-zero on exact
    equality but also produce absurd Kelly values for very-tiny losses).
    Also rejects negative/zero win_rate or negative avg_win as nonsensical.
    """
    if abs(avg_loss) < 1e-9 or avg_win <= 0 or win_rate <= 0:
        return 0.0
    b = avg_win / abs(avg_loss)
    p = win_rate
    kelly = p - (1 - p) / b
    return max(0.0, kelly / 2)  # half-Kelly for safety


def position_size(capital: float, weight: float, price: float,
                  max_pct: float = 0.25) -> int:
    """Calculate number of shares to buy, respecting max position size.
    Returns 0 on non-positive capital / weight / price (defensive)."""
    if capital <= 0 or weight <= 0 or price <= 0:
        return 0
    alloc = capital * min(weight, max_pct)
    return int(alloc / price)


def correlation_matrix(returns: pd.DataFrame) -> pd.DataFrame:
    """Return correlation matrix of asset returns."""
    return returns.corr()


def diversification_ratio(returns: pd.DataFrame, weights: np.ndarray) -> float:
    """Diversification ratio: weighted avg vol / portfolio vol.
    Higher = better diversified."""
    vols = returns.std() * np.sqrt(252)
    weighted_avg_vol = (vols * weights).sum()
    cov = returns.cov() * 252
    port_vol = np.sqrt(weights @ cov.values @ weights)
    return weighted_avg_vol / port_vol if port_vol > 0 else 1.0
