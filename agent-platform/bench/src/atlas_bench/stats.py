"""统计：平均值 + bootstrap 95% 置信区间；两组配对比较（doc/rag-eval-design.html §13）。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["Estimate", "estimate", "paired_diff"]

_RESAMPLES = 2000
_SEED = 20261012


@dataclass(frozen=True)
class Estimate:
    mean: float
    low: float
    high: float
    n: int

    def fmt(self, pct: bool = True) -> str:
        if self.n == 0:
            return "—"
        if pct:
            return f"{self.mean * 100:.1f}%（{self.low * 100:.1f}–{self.high * 100:.1f}）"
        return f"{self.mean:.3f}（{self.low:.3f}–{self.high:.3f}）"


def estimate(values: list[float]) -> Estimate:
    vals = np.asarray([v for v in values if v is not None and not np.isnan(v)], dtype=float)
    if vals.size == 0:
        return Estimate(float("nan"), float("nan"), float("nan"), 0)
    rng = np.random.default_rng(_SEED)
    means = rng.choice(vals, size=(_RESAMPLES, vals.size), replace=True).mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return Estimate(float(vals.mean()), float(lo), float(hi), int(vals.size))


def paired_diff(a: dict[str, float], b: dict[str, float]) -> tuple[Estimate, bool]:
    """同一批题上 b − a 的差值及置信区间；区间不含 0 视为显著。"""
    keys = [k for k in a if k in b and a[k] is not None and b[k] is not None]
    est = estimate([b[k] - a[k] for k in keys])
    significant = est.n > 0 and (est.low > 0 or est.high < 0)
    return est, significant
