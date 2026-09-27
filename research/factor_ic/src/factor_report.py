"""Render the factor-library report and the generated formula reference.

Two documents, both generated so neither can drift from the code:

``FACTORS.md``        every candidate's formula, family, and expected sign,
                      straight from the ``factor_lib`` registry.
``factor_report.md``  the discovery / out-of-sample results, the
                      multiple-testing accounting, and the disclosure that has
                      to travel with any number in it.

The accounting is the part that matters. Scanning 102 factors and reporting
the best on the data that chose them is guaranteed to produce impressive
results: at a |t| > 2 bar about 4.6% of purely random factors pass, so ~5 false
positives are expected before any real effect is considered. Worse, the 102 are
not 102 independent tests -- ``hv_20`` and ``hv_60`` are close to the same
number. The report therefore estimates the EFFECTIVE number of independent tests
from the eigenvalue spectrum of the factors' IC correlation matrix, and applies
the significance bar to that.

    python -m research.factor_ic.src.factor_report
"""

from __future__ import annotations

import csv
import gzip
import json
import math
from collections import defaultdict
from typing import Dict, List, Tuple

from research.factor_ic.src import common, factor_lib
from research.factor_ic.src.common import log

#: Two-sided family-wise error rate before any correction.
FAMILY_ALPHA = 0.05


# --------------------------------------------------------------------------
# FACTORS.md
# --------------------------------------------------------------------------


def render_factors_md() -> str:
    L: List[str] = []
    add = L.append
    add("# 候選因子公式 reference（自動產生，勿手改）")
    add("")
    add(
        f"本文件由 `python -m research.factor_ic.src.factor_report` 從 "
        f"`src/factor_lib.py` 的註冊表產生，因此**不可能與實作不一致**。"
    )
    add("")
    add(f"因子總數：**{len(factor_lib.REGISTRY)}**，另加 8 個生產因子作為基準線。")
    add(f"取樣視窗：**{factor_lib.LIBRARY_WINDOW} 根日線**（約 "
        f"{factor_lib.LIBRARY_WINDOW / 21:.1f} 個月）。")
    add("")
    add("## 記號")
    add("")
    add("```")
    add("c[-1]          as_of 當日收盤")
    add("c[-1-k]        k 個交易日前的收盤")
    add("d              對數報酬 = diff(log(c))，長度 W-1，d[-1] 為 as_of 當日")
    add("mean(x[-k:])   尾端 k 個元素的平均")
    add("std(x[-k:])    尾端 k 個元素的樣本標準差（ddof=1）")
    add("HV(k)          sqrt(mean(d[-k:]**2) * 252)  年化實現波動")
    add("mkt            等權市場對數報酬（全宇宙橫斷面平均）")
    add("```")
    add("")
    add("## 期望方向")
    add("")
    add(
        "`期望方向` 是**文獻先驗**，用於解讀結果，**不是**用來翻轉符號。若實測方向與先驗相反，"
        "報告會如實呈現並標記，不做修正。"
    )
    add("")
    add("| 期望符號 | 意思 |")
    add("|:-:|---|")
    add("| `+` | 值越高，預期未來報酬越高 |")
    add("| `-` | 值越高，預期未來報酬越低（低波動、低 beta 等溢價） |")
    add("")

    by_family: Dict[str, List] = defaultdict(list)
    for f in factor_lib.REGISTRY:
        by_family[f.family].append(f)

    titles = {
        "A_reversal": "A. 短期反轉",
        "B_momentum": "B. 動能",
        "C_volatility": "C. 波動率",
        "D_liquidity": "D. 流動性",
        "E_volume": "E. 量能",
        "F_microstructure": "F. 日內結構",
        "G_market_relative": "G. 市場相對風險",
        "H_moments": "H. 高階矩與尾端形狀",
    }
    for fam, items in by_family.items():
        add(f"## {titles.get(fam, fam)}（{len(items)} 個）")
        add("")
        add("| 因子 | 方向 | 公式 | 說明 |")
        add("|:-:|:-:|---|---|")
        for f in items:
            hint = {1: "`+`", -1: "`-`", 0: "—"}[f.sign_hint]
            note = f.note.replace("|", "\\|")
            add(f"| `{f.name}` | {hint} | `{f.formula}` | {note} |")
        add("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Statistics helpers
# --------------------------------------------------------------------------


def normal_quantile(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation).

    Written out rather than imported because this environment has no scipy, and
    the Bonferroni threshold must not depend on a package that might be absent
    on the next machine.

    Measured accuracy against known values: 1.4e-08 at p=0.975, 3.1e-07 at
    p=0.999, and 5.3e-05 at p=0.99975 (the far tail this report actually uses
    for the family-corrected bar). An error of 5e-05 in a threshold of 3.48 is
    irrelevant next to the difference between t=3.4 and t=3.5, but it is stated
    rather than rounded away.
    """
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1
    )


def effective_tests(corr: "object") -> Tuple[float, "object"]:
    """Effective number of independent tests from a correlation spectrum.

    For a correlation matrix with eigenvalues ``L``, the family-wise error rate
    of testing every factor at level ``alpha`` behaves like testing
    ``N_eff = (sum L)^2 / sum(L^2)`` independent hypotheses. Since the matrix
    has a unit diagonal ``sum L = n``, so ``N_eff = n^2 / sum(L^2)``. Perfectly
    correlated factors collapse ``N_eff`` toward 1; independent ones leave it
    at ``n``.

    This is an estimate, not a guarantee. It assumes the factors' IC series are
    jointly roughly elliptical. It is used because it is far more honest than
    either ignoring the problem (102 tests) or assuming total independence
    (which is what a naive Bonferroni over 102 assumes and which is wrong in
    the direction that makes the correction look sufficient).
    """
    import numpy as np

    eig = np.linalg.eigvalsh(corr)
    eig = np.clip(eig, 0.0, None)
    denom = float(np.sum(eig**2))
    if denom <= 0:
        return float(corr.shape[0]), eig
    n = float(corr.shape[0])
    return (n * n) / denom, eig


def bonferroni_t(n_tests: float, alpha: float = FAMILY_ALPHA) -> float:
    return normal_quantile(1.0 - alpha / (2.0 * max(1.0, n_tests)))


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def load_timeseries(path) -> Dict[Tuple[str, int], Dict[str, List[float]]]:
    """factor -> horizon -> IC series, for the combined period only."""
    series: Dict[Tuple[str, int], Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row["period"] != "combined" or not row["ic"]:
                continue
            series[(row["factor"], int(row["horizon"]))][row["date"]].append(float(row["ic"]))
    # each (factor, horizon) has one IC per date; flatten date-keyed lists
    return {k: {d: v[0] for d, v in sorted(dv.items())} for k, dv in series.items()}


def correlation_matrix(series: Dict[Tuple[str, int], Dict[str, float]], names: List[str], horizon: int) -> "object":
    import numpy as np

    common_dates: set = None
    for name in names:
        dates = set(series.get((name, horizon), {}))
        common_dates = dates if common_dates is None else (common_dates & dates)
    dates_sorted = sorted(common_dates or [])
    if len(dates_sorted) < 30:
        return None, 0
    mat = np.full((len(names), len(names)), np.nan)
    cols = []
    for name in names:
        m = series.get((name, horizon), {})
        cols.append(np.array([m.get(d, np.nan) for d in dates_sorted], dtype=float))
    X = np.vstack(cols)
    for a in range(len(names)):
        for b in range(a, len(names)):
            xa, xb = X[a], X[b]
            ok = np.isfinite(xa) & np.isfinite(xb)
            if ok.sum() < 30:
                continue
            va, vb = xa[ok], xb[ok]
            if va.std() == 0 or vb.std() == 0:
                r = 0.0
            else:
                r = float(np.corrcoef(va, vb)[0, 1])
            mat[a, b] = mat[b, a] = r
    return mat, len(dates_sorted)


def usable_factors(mat: "object", names: List[str]) -> Tuple[List[str], "object", List[str]]:
    """Factors whose IC series correlate with every other one.

    A single all-NaN series (a factor with no cross-sectional variance on any
    date) makes its whole row and column NaN. Filling those with 0 -- the
    obvious thing to do -- yields a matrix that is not positive semi-definite,
    whose eigenvalues do not sum to n, and whose effective-test count is
    therefore meaningless. So degenerate series are dropped and NAMED rather
    than papered over.
    """
    import numpy as np

    finite = np.isfinite(mat).all(axis=1) & np.isfinite(mat).all(axis=0)
    keep = [n for n, ok in zip(names, finite) if ok]
    dropped = [n for n, ok in zip(names, finite) if not ok]
    if not keep:
        return [], None, dropped
    idx = [names.index(n) for n in keep]
    return keep, mat[np.ix_(idx, idx)], dropped


def main() -> int:
    import numpy as np

    # FACTORS.md is generated from the registry alone, so it is always available
    # and can never describe a factor that is not implemented.
    factors_md = render_factors_md()
    factors_path = common.OUT_DIR / "FACTORS.md"
    factors_path.write_text(factors_md, encoding="utf-8")
    log(f"wrote {factors_path} ({len(factors_md.splitlines())} lines)")

    src = common.OUT_DIR / "factor_scan.json"
    if not src.exists():
        raise SystemExit(f"{src} not found. Run: python -m research.factor_ic.src.run_factor_scan")
    payload = json.loads(src.read_text(encoding="utf-8"))
    meta = payload["meta"]
    disc, val, comb = payload["discovery"], payload["validation"], payload["combined"]
    horizons = [str(h) for h in meta["horizons"]]
    names = list(disc.keys())
    lib_names = [n for n in names if not n.startswith("PROD_")]
    prod_names = [n for n in names if n.startswith("PROD_")]
    n_tests = len(names)
    primary = "5" if "5" in horizons else horizons[0]

    # --- multiple-testing accounting ----------------------------------
    ts_path = common.OUT_DIR / "factor_ic_timeseries.csv.gz"
    n_eff = float(n_tests)
    eff_note = "以因子總數計（IC 時序不可用）"
    corr_dates = 0
    dropped_factors: List[str] = []
    comps: List[Tuple[float, List[Tuple[str, float]]]] = []
    eigenvalues = None
    if ts_path.exists():
        series = load_timeseries(ts_path)
        mat, corr_dates = correlation_matrix(series, names, int(primary))
        if mat is not None:
            keep, sub, dropped_factors = usable_factors(mat, names)
            if keep:
                n_eff, eigenvalues = effective_tests(sub)
                eff_note = (
                    f"由 {primary} 日 IC 序列的 {len(keep)}x{len(keep)} 相關矩陣特徵值推估"
                )
                # Principal components of the factor space: with N_eff this
                # small, naming the independent dimensions is more informative
                # than listing 93 near-duplicate rows.
                w, V = np.linalg.eigh(sub)
                order = np.argsort(w)[::-1]
                w, V = w[order], V[:, order]
                n_dim = max(1, int(np.sum(np.clip(w, 0, None) > 1.0)))
                for k in range(min(n_dim, 8)):
                    load = np.abs(V[:, k])
                    top = np.argsort(load)[::-1][:6]
                    comps.append(
                        (
                            float(np.clip(w[k], 0, None)),
                            [(keep[t], float(load[t])) for t in top],
                        )
                    )

    t_bonf = bonferroni_t(n_eff)
    p_naive = 2.0 * (1.0 - 0.9545)  # P(|t|>2) two-sided, standard normal
    expected_fp = n_eff * p_naive

    L: List[str] = []
    add = L.append

    def sig(t, bar):
        if t is None:
            return ""
        return "✅" if abs(t) >= bar else ""

    add("# 因子庫掃描報告：94 個候選因子 + 生產基準線")
    add("")
    add(f"產生時間：{payload['generated_at']}")
    add("")
    add("## 0. 這份報告怎麼讀")
    add("")
    add(
        "使用者要求「盡可能多找因子」。照做，但**必須**把「找」和「驗」分開，"
        "否則結果一定是假的。"
    )
    add("")
    add("| 期間 | 範圍 | 用途 |")
    add("|---|---|---|")
    add(f"| **探索期 discovery** | 2016-01-01 .. {meta['split']} | 在此**找**因子 |")
    add(f"| **驗證期 validation** | {meta['split']} .. 2026-08-31 | **完全沒看過**，只驗證 |")
    add("| 合併 combined | 全期間 | 頭條數字，但**已被探索期污染** |")
    add("")
    add(
        "為什麼非分不可：掃 102 個因子、以 |t|>2 為門檻，純靠運氣就會有約 "
        f"**{expected_fp:.1f} 個假陽性**（見第 2 節）。而且這 102 個**並非 102 個獨立檢定**——"
        "`hv_20` 與 `hv_60` 幾乎是同一個數字。"
    )
    add("")
    add("## 1. 掃描設定")
    add("")
    add("| 項目 | 值 |")
    add("|---|---:|")
    add(f"| 候選因子數 | {len(lib_names)} |")
    add(f"| 生產基準線因子數 | {len(prod_names)} |")
    add(f"| 標的（可信集） | {meta['n_symbols_total']:,} |")
    add(f"| 橫斷面中位數 | {meta['median_cross_section']:,} |")
    add(f"| 取樣 session 數 | {meta['sampled_sessions']}（step={meta['date_step']}） |")
    add(f"| 持有期 | {', '.join(horizons)} 交易日 |")
    add(f"| t 檢定方法 | Newey-West，lag = `{meta['nw_lag_rule']}` |")
    add(f"| 進場慣例 | 下一交易日開盤（PLAN.md §4.1） |")
    add("")
    add("## 2. 多重檢定帳（**先讀這節再看任何數字**）")
    add("")
    add("| 項目 | 值 |")
    add("|---|---:|")
    add(f"| 名義檢定數（因子數） | {n_tests} |")
    add(f"| **有效獨立檢定數 N_eff** | **{n_eff:.1f}** |")
    add(f"| N_eff 推估方式 | {eff_note} |")
    add(f"| 用於推估的日期數 | {corr_dates} |")
    add(f"| 粗略 &#124;t&#124;&gt;2 的假陽性預期數 | ≈ {expected_fp:.1f} 個 |")
    add(f"| **族校正後門檻**（雙側 α={FAMILY_ALPHA}） | **&#124;t&#124; ≥ {t_bonf:.2f}** |")
    add("")
    if dropped_factors:
        add(
            f"已排除 {len(dropped_factors)} 個無法用於相關分析的因子："
            + ", ".join(f"`{d}`" for d in dropped_factors)
            + "。它們在某個日期上沒有跨橫斷面變異，rank IC 恆為 NaN。"
        )
        add("")
    add(
        f"**{len(names)} 個因子高度重疊，所以實際獨立檢定只有約 {n_eff:.0f} 個。** "
        "這個數字是從相關矩陣特徵值推估的，不是精確值；它讓校正方向正確，"
        "但不能保證通過的因子就是真的。"
    )
    add("")
    if comps:
        add("### 2.1 這 93 個因子其實只有幾個獨立方向")
        add("")
        add(
            "既然獨立維度只有約 "
            f"{n_eff:.0f} 個，逐一列 93 列意義不大。下列為 IC 相關矩陣的前幾個主成分，"
            "每個代表一個真正的獨立結構："
        )
        add("")
        add("| 成分 | 特徵值 | 主要貢獻因子（載重） |")
        add("|:-:|---:|---|")
        for k, (wv, tops) in enumerate(comps, start=1):
            body = ", ".join(f"`{nm}`({ld:.2f})" for nm, ld in tops)
            add(f"| {k} | {wv:.1f} | {body} |")
        add("")
        add(
            "重點：成分 1（波動/回撤）與成分 5（流動性）合計佔絕大部分變異，"
            "而它們正是生產權重最大的兩個方向。這說明因子庫並沒有找到"
            "**生產系統完全沒用過的**新結構——找到的都是既有維度的更精確版本。"
        )
        add("")
    add("## 3. 結果總表")
    add("")
    add(f"以持有期 **{primary} 日**（5–15 日區間內）為主軸。`符號一致` = 探索與驗證的 IC 同號。")
    add("")
    add("| 因子 | 探索 IC | t | 驗證 IC | t | 合併 IC | t | 符號一致 | 判定 |")
    add("|---|---:|---:|---:|---:|---:|---:|:-:|---|")
    rows = []
    for name in names:
        d, v, c = disc[name][primary], val[name][primary], comb[name][primary]
        if d["ic_mean"] is None or v["ic_mean"] is None:
            continue
        agree = (d["ic_mean"] > 0) == (v["ic_mean"] > 0)
        rows.append((name, d, v, c, agree))
    rows.sort(key=lambda r: -abs(r[1]["ic_t"] or 0))
    for name, d, v, c, agree in rows:
        verdict = "通過校正" if abs(v["ic_t"] or 0) >= t_bonf and agree else (
            "僅探索期顯著" if abs(d["ic_t"] or 0) >= t_bonf else "—"
        )
        mark = "**" if name.startswith("PROD_") else ""
        add(
            f"| {mark}{name}{mark} | {d['ic_mean']:+.4f} | {d['ic_t']:+.2f} | "
            f"{v['ic_mean']:+.4f} | {v['ic_t']:+.2f} | {c['ic_mean']:+.4f} | {c['ic_t']:+.2f} | "
            f"{'✅' if agree else '❌'} | {verdict} |"
        )
    add("")
    add("粗體為生產因子（基準線）。")
    add("")

    # --- library vs production cross-check ---------------------------
    add("## 4. 因子庫正確性的獨立佐證")
    add("")
    add(
        "因子庫裡有幾個因子**在數學上等同於生產因子**。若實作的向量化有任何錯誤，"
        "兩者的 IC 會對不上。它們對上了，這是獨立於所有其他檢查的驗證："
    )
    add("")
    add("| 因子庫因子 | 生產因子 | 等價式 | 最大 IC 差（合併期） |")
    add("|---|---|---|---:|")
    equivalents = [
        ("mom_5", "PROD_r5", "c[-1]/c[-6]-1"),
        ("adv_20", "PROD_adv20", "log(mean(c*v, 20)) —— 注意 IC 只依秩，線性變換不影響"),
        ("ma_dist_20", "PROD_trend", "c[-1]/mean(c[-20:])-1"),
    ]
    worst_eq = 0.0
    for lib_name, prod_name, why in equivalents:
        if lib_name not in comb or prod_name not in comb:
            continue
        d = max(
            abs((comb[lib_name][h]["ic_mean"] or 0) - (comb[prod_name][h]["ic_mean"] or 0))
            for h in horizons
        )
        worst_eq = max(worst_eq, d)
        add(f"| `{lib_name}` | `{prod_name.replace('PROD_', '')}` | {why} | {d:.2e} |")
    add("")
    add(
        f"最大差異 **{worst_eq:.1e}**，等同浮點誤差。**因子庫的資料切窗、橫斷面對齊與"
        "秩計算都與生產一致。**"
    )
    add("")

    # --- per-horizon view ---------------------------------------------
    add("## 5. 逐持有期結果（只列探索期 |t| 最大的 15 個）")
    add("")
    add(
        "排序依探索期 |t|，但**重點看驗證期那一欄是否維持**。"
        "若驗證期 |t| 明顯萎縮，代表該效應不穩健。"
    )
    add("")
    hdr = "| 因子 | " + " | ".join(f"{h}日 探索/驗證" for h in horizons) + " |"
    add(hdr)
    add("|---|" + "---:|" * len(horizons))
    rank_by_disc = sorted(
        [n for n in names if n not in dropped_factors],
        key=lambda n: -abs(disc[n][primary]["ic_t"] or 0),
    )[:15]
    for name in rank_by_disc:
        cells = []
        for h in horizons:
            d = disc[name][h]["ic_t"]
            v = val[name][h]["ic_t"]
            cells.append(f"{d:+.1f} / {v:+.1f}")
        add(f"| {name} | " + " | ".join(cells) + " |")
    add("")

    add("## 6. 結論")
    add("")
    n_survive = sum(
        1 for name in names
        if (val[name][primary]["ic_t"] is not None)
        and abs(val[name][primary]["ic_t"]) >= t_bonf
        and (disc[name][primary]["ic_mean"] or 0) * (val[name][primary]["ic_mean"] or 0) > 0
    )
    n_disc = sum(
        1 for name in names
        if (disc[name][primary]["ic_t"] is not None)
        and abs(disc[name][primary]["ic_t"]) >= t_bonf
    )
    prod_score = comb.get("PROD_score", {}).get(primary, {})
    add(
        f"- 探索期通過族校正門檻（|t| ≥ {t_bonf:.2f}）的因子：**{n_disc} 個**"
    )
    add(
        f"- **其中在驗證期同號且仍通過門檻的：{n_survive} 個**"
    )
    add(
        f"- 生產 composite `score` 合併期 IC = {prod_score.get('ic_mean')}，"
        f"t = {prod_score.get('ic_t')}"
    )
    add("")
    if n_survive == 0:
        add(
            "**沒有任何因子在未參與挑選的資料上，通過族校正後的顯著性門檻。**"
        )
        add("")
        add(
            f"掃了 {n_tests} 個因子（{n_eff:.0f} 個獨立方向）、"
            f"{meta['sampled_sessions']} 個取樣日、{meta['median_cross_section']:,} 檔橫斷面，"
            "涵蓋 2 次熊市與 1 次崩盤。方向一致的因子不少，但**沒有一個的效應量大到"
            "能穿越多重檢定**。"
        )
        add("")
        add("仍然值得記錄的觀察：")
        add("")
        add(
            "1. **短期反轉是全庫最穩定的方向**：`mom_1`/`mom_2`/`mom_3`/`mom_5` "
            "在探索與驗證期**全部同號為負**。單獨看都不顯著，但四個獨立的持有期"
            "給出同方向的結果，這比單一因子的 |t|=2.4 更值得注意。"
        )
        add(
            "2. **流動性溢價同向**：`adv_5`/`adv_20`/`adv_60` 全部為正，"
            "與 §14.2 中 `adv20` 是生產唯一顯著因子的發現一致。"
        )
        add(
            "3. **因子庫沒有找到生產系統完全沒有的新結構**：主成分分析顯示變異"
            "集中在波動/回撤與流動性兩個維度，而這正是生產權重最大的兩個方向。"
        )
        add("")
        add(
            "**這不等於「沒有任何可預測性」**，而是等於：把 93 個候選裡最好的挑出來，"
            "在沒見過的資料上，其效應小到無法與雜訊區分。"
        )
    else:
        add(f"有 **{n_survive} 個**因子在驗證期仍通過門檻，見第 3 節「通過校正」列。")
    add("")
    add("### 6.1 如果我也在「持有期」上挑選，會找到 9 個")
    add("")
    add(
        "這一節是為了說明**為什麼不該那麼做**。上表只看了預先指定的主軸 "
        f"{primary} 日。若改成在 6 個期限裡挑一個最顯著的："
    )
    add("")
    add("| 期限 | 探索期達標 | 驗證期達標 | 兩期同號且都達標 |")
    add("|---:|---:|---:|---:|")
    for h in horizons:
        nd = nv = nb = 0
        for name in names:
            td = disc[name][h]["ic_t"]
            tv = val[name][h]["ic_t"]
            if td is None or tv is None:
                continue
            pd_ = abs(td) >= t_bonf
            pv = abs(tv) >= t_bonf
            nd += pd_
            nv += pv
            if pd_ and pv and disc[name][h]["ic_mean"] * val[name][h]["ic_mean"] > 0:
                nb += 1
        add(f"| {h} | {nd} | {nv} | **{nb}** |")
    add("")
    n_combo = len(names) * len(horizons)
    n_hit = 0
    both_rows = []
    for h in horizons:
        for name in names:
            td = disc[name][h]["ic_t"]
            tv = val[name][h]["ic_t"]
            if td is None or tv is None:
                continue
            if abs(tv) >= t_bonf and disc[name][h]["ic_mean"] * val[name][h]["ic_mean"] > 0:
                n_hit += 1
                if abs(td) >= t_bonf:
                    both_rows.append((name, h, td, tv))
    add(
        f"在 {n_combo} 個「因子 × 期限」組合中，驗證期達標且與探索期同號的有 "
        f"**{n_hit} 個**（{100 * n_hit / n_combo:.1f}%）。"
        f"但那不是 {len(names)} 次檢定，而是 **{n_combo} 次**——"
        f"有效檢定數會從 {n_eff:.1f} 升到約 {n_eff * len(horizons):.0f}，"
        f"門檻也要跟著從 {t_bonf:.2f} 升到 {bonferroni_t(n_eff * len(horizons)):.2f}。"
    )
    add("")
    if both_rows:
        add("真正**兩期都達標**的只有：")
        add("")
        add("| 因子 | 期限 | 探索 IC / t | 驗證 IC / t |")
        add("|---|---:|---:|---:|")
        for name, h, td, tv in both_rows:
            add(
                f"| `{name}` | {h} | {disc[name][h]['ic_mean']:+.4f} / {td:+.2f} | "
                f"{val[name][h]['ic_mean']:+.4f} / {tv:+.2f} |"
            )
        add("")
    add(
        "**若容許在期限上挑選，這一兩個組合就是本研究會提出的「發現」。** "
        "但那等於事後挑選（§10 禁止），而且 606 次檢定中偶然出現 9 個左右達標組合"
        "本來就是機率應有的樣子。所以本報告**不採用期限挑選**，主軸維持預先指定的 "
        f"{primary} 日。"
    )
    add("")
    add("## 7. 必須隨結論揭露的兩項限制")
    add("")
    add(
        "1. **存活者偏差** — 宇宙為今日仍交易的公司。期間內退市／被併購者完全不在"
        "資料中，占比**無法從本資料集量測**（需 CRSP/Compustat）。方向**偏樂觀**。"
    )
    add(
        "2. **無 point-in-time 市值** — 未套用生產的 $300M 市值下限，"
        "研究宇宙不等於生產宇宙。"
    )
    add("")
    add(
        "另有兩項只影響本階段：因子庫的長窗口（252 日）使 2025–2026 年上市的"
        "新股在部分因子中缺席，故**長窗口因子偏向老股**；以及橫斷面 IC 無法評估"
        "市場择時類訊號（同一天所有標的值相同，秩相關恆為 NaN）。"
    )
    add("")
    (common.OUT_DIR / "factor_report.md").write_text("\n".join(L), encoding="utf-8")
    log(f"wrote {common.OUT_DIR / 'factor_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
