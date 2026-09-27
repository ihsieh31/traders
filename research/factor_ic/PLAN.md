# 因子 IC 研究 — 實作計畫（交接契約）

> **本文件是其他 agent 接手的唯一權威規格。** 實作前請完整讀完。
> 文件版本：1.1 ｜ 建立日期：2026-09-27 ｜ 最後更新：2026-09-27 ｜ 狀態：**實作完成，資料已審查，待跑 10.7 年主研究**

### 版本 1.1 變更摘要

相對 1.0 的實作結果，修正了 **1 個會讓 t 值虛高的統計錯誤**（§3.3 的 NW lag 單位），並新增資料審查章節（§3.7）與已得的 3.7 年結果（§14）。其餘章節為現況同步。

---

## 0. 一句話

用**完全決定式、零 LLM、零券商**的方法，量測生產篩選分數及其每一個成分因子，在 1/3/5/10/20/40/60 日持有期上是否具有**符號正確且統計顯著**的前瞻預測力。

---

## 1. 這是什麼 / 這不是什麼

### 是

一個**因子資訊係數（IC）量測工具**。輸入：歷史日線 + 生產因子計算函式。輸出：每個因子在每個持有期的 rank IC、t-stat、方向正確率與報酬分布。

### 不是（scope 邊界，硬性）

- **不是** portfolio backtest engine
- **不是** 策略模擬器
- **不是** 執行模擬器（不模擬滑價、部位 sizing、bracket 保護單、訂單類型）
- **不是** P&L 計算器
- **不會** 呼叫任何 LLM
- **不會** 對券商帳戶做任何讀寫

**為什麼先做這個而不是 backtest**：IC 是一個便宜的守門員。如果因子在實際持有期上 IC ≈ 0，那麼 backtest 出來一定是雜訊，先做 backtest 是浪費時間。反之 IC 顯著才值得投入 backtest 工程。

**範圍膨脹的處置**：若實作過程中發現需要部位 sizing、成本模型或組合建構才能回答核心問題，**停下來回報，不要自行擴充**。那代表需要先重新決策。

---

## 2. 核心研究問題

> 生產篩選分數（`metrics.py` 的 `score = 100 × (0.20·p_adv20 + 0.25·p_r20 + 0.25·p_r60 + 0.15·(1−p_vol20) + 0.15·p_volume_ratio)`）在 **5–15 個交易日的實際持有期**上，是否具有正確符號的預測力？

支撐這個問題的背景事實（已由接手者核實）：

1. **期限可能錯配**。分數有 50% 權重壓在 `r20`/`r60`（1 個月 / 3 個月動能），而系統 prompt 模板宣告的持有期為 5–15 個交易日（2026-09-27 由原 2–10 統一調整）。橫斷面動能效應在 3–12 個月區間有文獻支持；在 1–3 週區間，文獻中最強的效應是**短期反轉**。因此分數在實際持有期上**可能符號為負**。**這個期限調整只是消除內部不一致，沒有解決期限與因子的實質錯配——那正是本研究要量測的。**
2. **`r5` 與 `trend` 已計算但權重為零**。而 `trend`（價格 vs 20 日均線）正是教科書式的 1 個月反轉訊號。本研究的價值之一就是確認這個權重設定是否合理。
3. **市值篩選排除了約 70% 的宇宙**（本地實測：13,500 中 9,399 因市值缺失或 <$300M 被排除）。動能溢價歷史上在小盤股較強，因此生產設計只捕捉了效應較弱的大盤區段。

---

## 3. 已知偏差與限制（**必須在最終報告中逐條列出，不可隱瞞**）

### 3.1 存活者偏差（survivorship bias）— 無法在本地解決

研究宇宙是「**今天** ACTIVE 且 tradable 的美股」。已退市、已被併購的標的不在內。

- **方向**：偏向樂觀。動能文獻通常使用含退市報酬的 CRSP 資料，本研究排除死亡樣本會**高估**因子表現。
- **量級估計**：3 年視窗約 5–10% 的原始宇宙消失。此期間越長越嚴重。**注意：這是外部經驗值，不是本研究的實測結果。** §3.7 已確認此占比**無法從現有資料集量測**（缺席的標的連痕跡都不留），需 CRSP/Compustat 歷史成分股清單才能量化。引用時不可寫成「本研究測得」。
- **處置**：記錄為限制。**不要**嘗試用未驗證方法修正。
- 若未來取得歷史成分股名單（CRSP/Compustat），可重跑比較。

### 3.2 市值非 point-in-time — 本階段刻意略過

生產的 `universe.py` 透過 Nasdaq screener 取得**當日**市值，cache 在 `app_home()/cache/market_cap/nasdaq_<date>.json`。本地實測只有 **5 天**（2026-09-23 ~ 09-27）。

- 本階段**不使用市值篩選**，因為它無法 point-in-time 重建，用今日市值篩歷史日會造成 look-ahead bias。
- 保留的門檻（皆可由日線 point-in-time 計算）：`price >= 5.0`、`ADV20 >= 20,000,000`、`61 根完整日線`。
- **後果**：研究宇宙 ≠ 生產宇宙（多了市值 <$300M 但流動性達標的標的）。因此**本研究的 IC 結果不能直接外推到完整生產管線**。這是刻意的 scope cut，必須在報告中聲明。

### 3.3 重疊窗口造成的 t-stat 膨脹（**最重要的統計陷阱**）

因子使用 61 根 bar 的窗口，因此 `r60` 在相鄰交易日之間有 60 天重疊。若每日取樣計算 IC 並用一般 t-stat，**統計顯著性會被嚴重高估**（autocorrelated overlapping observations）。

**強制處置**（兩者都要做，缺一不可）：
1. **Newey-West 調整 t-stat**，lag 以**取樣單位（sample units）**計算：

   ```
   nw_lag = max(1, ceil(horizon / date_step))
   ```

   理由：IC 序列是**每 `date_step` 個 session 取樣一次**的，所以它的自相關結構是以取樣間隔為單位。`date_step=5`、horizon=60 時，正確的 lag 是 `ceil(60/5)=12`，**不是 60**。

   **⚠️ 這個錯誤在 1.0 版被寫錯了（原寫「lag = horizon」），實作時造成 |t| 虛高 0.3–1.5，足以把雜訊推過 |t|=2 的門檻。已修正。** 若你看到 `nw_lag = horizon`，那是舊的錯的。

2. **non-overlapping 子樣本穩健性檢查**：另取稀疏間隔的子集重算，結果必須與主報告方向一致才採信。

若實作只做一般 t-stat，或 NW lag 用錯單位，**該研究視為無效**。

**已知限制：此檢查統計效力不足。** 61-bar 因子窗口下，10.7 年（2,680 session）以 `date_step=5` 主取樣只有約 160 個點；非重疊取樣要拉到 `step=61` 才只剩約 8 個點，8 個點無法支撐任何推論。**因此該檢查只能證明「沒有明顯反向」，不能證明「穩健」。** 報告中必須如此標示（`ic_report.md` 已自動加上此警告）。

### 3.4 資料來源（2026-09-27 實測結論）

**`DataFeed.SIP` 可用，且歷史深度 10.7 年。** 已用生產函式 `fetch_daily_bars_batch`（`metrics.py:530`）實測確認。驗證方式：同一日 AAPL 的 IEX 量能 390,603 股 vs SIP 50,080,539 股（比值 62 倍），且收盤價也不同（193.99 vs 194.03，因 IEX 的 OHLC 只聚合 IEX 成交）。

**歷史最深 2016-01-04**（實測 AAPL 2,680 根日線），2015 年及更早的請求回空。這是 Alpaca 產品限制，不是帳戶權限問題。已抓滿 10.7 年：12,427 檔、19,225,174 列、2,687 個交易日。

**`yfinance` 不可用（已實測並排除）。** 曾實作為無權限的備案：

| 指標 | SIP | yfinance |
|---|---:|---:|
| 日曆交易日缺口率 | **1.14%**（幾乎全來自權證） | **22.11%** |
| 完全無缺口的標的 | 260 / 264 | **0 / 99** |

22% 的缺口會讓 `validate_and_clean_bars` 正確地 fail closed，導致大多數日期的橫斷面歸零（實測 39 個日期中 30 個橫斷面 = 0）。程式碼保留 `--source yfinance` 僅為無權限環境備用，**不建議使用**。

**調整政策：兩邊都已分割調整，不要重複處理。** 實測 AMZN（2022-06-06 執行 20:1 分割）：

| 日期 | yfinance Close | Alpaca SIP Close |
|---|---:|---:|
| 2016-01-04 | 31.8495 | 31.8500 |
| 2022-06-03 | 121.68 | 121.68 |

yfinance 的 `Close` 與 `Volume` 交付時就已是分割調整後的值，與 `Adjustment.SPLIT` 一致。**本專案早期版本曾對 yfinance 輸出再套一次 `split_only_adjust`，造成每個分割前日期 19 倍的假誤差。該函式已移除，勿再引入。** 成交量兩邊口徑略有差異（Alpaca 合併量能比 Yahoo 高 2.5–5.4%），屬已知差異。

**SDK 陷阱**：`alpaca-py==0.44.0` 已將 `StockBarsRequest` 欄位由 `symbol_or` 改為 `symbol_or_symbols`。生產碼已遷移（`metrics.py:581`），collector 必須比照。

**日曆快取必須驗證涵蓋範圍（已踩過的坑）**：`load_calendar_rows` 會把交易日曆快取到 `data/calendar.json`。若快取涵蓋範圍小於請求範圍就**必須重抓**，否則所有覆蓋率檢查會拿錯的 session 集合去比，而且完全不會報錯。

實測踩到兩次：

1. 第一次跑 10.7 年審查時，沿用了 3.7 年那次留下的 **927-session** 日曆快取，卻以為在檢查 2,689 個 session。已修：快取範圍不足即重抓。
2. 修完之後**反而弄壞 smoke test**：smoke test 寫入的合成日曆剛好涵蓋 `start..end`，但新的檢查連 ±7 天緩衝都要求，於是它判定失效、**靜默改用網路抓真實日曆**並覆蓋掉合成日曆，導致取樣網格與 numpy 參考實作對不上（1 日期 IC 差 0.055）。最終修法：**驗收比對的是未加緩衝的 `[start, end]`**；±7 天只是抓取時的緩衝，不是覆蓋要求。

smoke test 現已加上**禁止網路的硬性斷言**（`common.fetch_trading_calendar` 被換成會拋例外 的版本），使這類「靜默退回網路」的 bug 無法再藏身。

### 3.5 資料品質驗證（`verify_data.py`，2026-09-27）

```bash
python -m research.factor_ic.src.verify_data --start 2016-01-01 --end 2026-09-01 --cross-check 400
```

10 項檢查，結果寫入 `out/data_quality.json` 與 `out/trusted_symbols.txt`。

| 檢查 | 結果 |
|---|---|
| schema | ✅ 欄位齊全 |
| symbol_identity | ⚠️ 2,078 列空 ticker（0.011%），已回報並丟棄 |
| instrument_mix | ✅ 12,427 檔分類：普通股 11,957、優先股 358、權證 54、單位 45、權利 13 |
| duplicate_bars | ✅ 無重複 |
| monotonic_timestamps | ✅ 全部嚴格遞增 |
| value_sanity | ✅ 19,225,174 列全數通過（OHLC 正值、高低一致、量非負） |
| split_adjustment_smoke | ✅ 僅 0.0365% 的單日變動超過 \|log\| 0.60 |
| calendar_coverage | ⚠️ 中位覆蓋率 1.0000；239 檔低於 0.98 |
| extreme_moves | ⚠️ 7,007 次單日 \|log\| > 0.6，跨 2,144 檔 |
| cross_source | ✅ 389 檔中 **P50 誤差 2.37e-08、P90 僅 1.44e-05**；10 檔（2.6%）超過 2% |

**交叉驗證的方法論陷阱（已修正，勿回退）**：
1. 早期版本對 yfinance 輸出**重複做分割調整**，導致 AMZN 顯示 19 倍假誤差。
2. 早期版本把全部標的**混成一個分布**，讓 10 個壞檔看起來像全面資料失敗。

現行為：**逐標的統計**，`Close` 直接對比不做調整。10 檔不符者的誤差是**精確整數倍**（29x、9x、7x、4x、3x），即**未處理的反向分割**特徵。

**可信標的集（`out/trusted_symbols.txt`）：9,753 檔**，排除 2,674 檔：

| 排除原因 | 檔數 | 理由 |
|---|---:|---|
| `extreme_move` | 2,144 | 未解釋的 >82% 單日跳動＝反向分割漏處理 / 權證 / **ticker 被重複使用**。三者都會把兩個標的接成一條序列，汙染該檔的 r60 與 vol20 |
| `instrument` | 470 | 非普通股（權證／優先股／單位／權利），價格動態不同，不應進入橫斷面百分位 |
| `coverage` | 239 | 在自身首末區間內有缺口 |
| `cross_source_disagreement` | 10 | 獨立來源比對不符 |

可信集在實務上產生的橫斷面為 1,662–2,544 檔，與生產實測的 ~2,060 相符。

研究時**必須**以 `--symbols-file out/trusted_symbols.txt` 執行。

### 3.6 其他

- **分紅調整**：沿用生產的 `adjustment="split"`（`metrics.py:530` 預設）。對 5–15 日持有期影響可忽略（季度股息約 0.5%，15 日折合約 0.25%）。**但對 20/40/60 日視窗是實質的**——解讀長期限 IC 時必須註明此偏誤方向為高估。
- **無 point-in-time 新聞/基本面**：本研究不使用這些輸入（它們屬於 LLM 層），故無此偏差。
- **交易日曆**：使用 Alpaca 交易日曆，單次抓取覆蓋整個研究期間後注入（見 §5.2），不做 per-date 網路呼叫。**快取必須驗證涵蓋範圍**，見 §3.4 末段。

### 3.7 資料深度審查（`audit_data.py` / `audit_report.py`，2026-09-27）

`verify_data.py`（§3.5）是**閘門**：通過或不通過，產出可信清單。`audit_data.py` 是**調查**：描述這份資料實際是什麼，讓讀者判斷該把結論信任到什麼程度。兩者用途不同，不要互相取代。

```bash
python -m research.factor_ic.src.audit_data --start 2016-01-01 --end 2026-09-01 \
    --trusted research/factor_ic/out/trusted_symbols.txt
python -m research.factor_ic.src.audit_report   # 只排版，不重算
```

輸出 `out/data_audit.json`（原始數字）與 `out/data_audit_report.md`（人讀）。

**結論：阻擋性問題 0 項；無法解決的限制 2 項。**

#### 完整性

| 指標 | 值 |
|---|---:|
| 標的數（可信集） | 9,753 |
| 覆蓋率 100% 的標的 | **9,723** |
| 覆蓋率低於 0.98 / 低於 0.90 | **各 0 檔** |
| 2,689 個交易日中，有任一標的缺資料的 | **僅 58 日** |
| 有缺漏的日期，中位影響幾檔 | **1 檔** |

缺漏是零星且個別的，這是**停牌**該有的樣子，不是資料源中斷的樣子。若曾有 >5% 標的同日缺資料，才需判定為 feed 端問題（`audit_data.py` 會自動區分並改寫判定文字）。

#### 可信度：統計特徵

| 指標 | 實測 | 意義 |
|---|---:|---|
| 超額峰度 | 36.9 | 厚尾，符合股票 |
| 日報酬 lag-1 自我相關 | **−0.0549** | 教科書上的買賣價差回彈值 |
| 年化波動 P10/P50/P90 | 5.4% / 25.4% / 75.8% | 大盤到微股的合理梯度 |
| 單日波動 >5% / >10% 比例 | 4.83% / 0.97% | 含微股時的合理值 |
| 平均橫斷面兩兩相關 | +0.0552 | 正值；偏低是因為宇宙含大量微股 |

#### 可信度：市場重建（最強證據）

以 9,753 檔的每日對數報酬取等權平均重建市場：

| 最差月 | 等權報酬 | | 最好月 | 等權報酬 |
|---|---:|---|---|---:|
| 2020-03 | **−23.26%** | | 2020-11 | **+12.46%** |
| 2022-09 | −9.93% | | 2020-04 | +10.79% |
| 2018-12 | −8.98% | | 2019-01 | +8.50% |
| 2022-06 | −8.77% | | 2023-01 | +8.36% |
| 2022-04 | −8.19% | | 2023-11 | +7.20% |

2020 年 3 月 −23.3% 與真實歷史一致（S&P 500 當月約 −20%，等權全市場含微股跌幅更深）。**被打亂、抽樣或填補過的資料不可能同時重現崩盤、反彈與熊市的幅度與時序。**

另有一項不依賴任何外部參考值的內部一致性檢查：等權市場年化波動 **16.0%**（落在 10–35% 寬帶內），且單日變動 >1% 的天數占比 **20.2%** 必須**低於**由同一波動率推出的高斯預期值 **32.1%** —— 股票厚尾，身體部位應比高斯更集中。此檢查只依賴兩個統計量互相一致，不依賴記憶。

#### 兩項必須隨結論揭露的限制

1. **存活者偏差（§3.1）**：宇宙為「今日仍交易」的公司。期間內退市／被併購者**完全不在資料中**，占比**無法從本資料集量測**（需 CRSP/Compustat）。方向**偏樂觀**——動能研究裡暴漲後被併購的股票是贏者，刪掉會低估動能效應。緩解：結論標示「僅限存續股票」並說明方向，**不用未驗證方法修正**。
2. **無 point-in-time 市值（§3.2）**：研究宇宙不等於生產宇宙。

#### 審查過程中移除的三個不可靠控制組（記錄以免重蹈）

這個章節的價值有一半來自記錄**被移除的檢查**。三者都犯同一種錯：**拿自己記不住的市場細節當控制組**。

| 被移除的檢查 | 為何無效 |
|---|---|
| 逐日已知事件對照（手寫「2020-02-19 應為跌」之類的清單，6 個方向） | 6 個手寫方向有 **3 個是錯的**——2020-02-19 實際是創高日。控制組比被驗證對象更不可靠，故刪除 |
| 「單日變動 >1% 應佔 3–12% 天數」 | 那是 **S&P 500** 的統計；本序列是含大量微股的**等權全市場**，20.2% 才是對的。**是我的區間錯，不是資料錯** |
| 池化的橫斷面相關估計 | 公式倒置且未處理 panel 缺口數（2016 年 3.6k 檔、2026 年 9.7k 檔），得到 −1877 的無意義值。已改為**逐日估計**並取中位數 |

**通則：控制組的預期值必須能不用「我記得」就驗證。** 記不住就別拿來當控制組，改用可推導的內部一致性檢查。

---

## 4. 方法論決策（逐項，勿自行更改）

### 4.1 前瞻報酬定義 — **本計畫最關鍵的選擇**

生產行為：決策在 `as_of` 當日盤中/盤後做出（long-run 預設 `run_time_et="11:00"`），市價單送出，因此**最早只能在下一個交易日開盤成交**。

**誠實定義（主報告使用）**：

```
entry  = open[as_of + 1]            # 下一交易日開盤進場
exit   = close[as_of + 1 + (N-1)]   # 進場後第 N 個交易日收盤出場
forward_return(N) = exit / entry - 1
```

此慣例與現有 backtest 一致（`backtest/engine.py:136-137` 註解：「sizing uses the signal bar's close but **fills at the next open**」）。

**對照定義（報告中並列輸出，但標記為有偏誤）**：

```
close_to_close(N) = close[as_of + N] / close[as_of] - 1
```

此定義隱含「能以 `as_of` 收盤價成交」的假設，屬 **look-ahead bias**，**不可**作為結論依據。它存在的唯一目的���方便與學術文獻（通常用收盤對收盤）對照。

**符號慣例**：正的 `forward_return` = 上漲。正的 IC = 分數越高、未來報酬越高。

### 4.2 IC 計算

- **Spearman rank correlation**，於**每個日期橫斷面**（在當日 eligible 宇宙內）。
- 樣本數不足時（當日 eligible < 20）**跳過該日**，不計入。
- 逐日 IC 序列再對時間彙總（mean、median、std、Newey-West t-stat、正號比例）。

### 4.3 必須分因子計算

**這是整個研究的核心價值。** 不可只算 composite。

七個因子全部獨立計算 IC：

| 因子 | 生產權重 | 備註 |
|---|---|---|
| `adv20` | 0.20 | 流動性 |
| `r20` | 0.25 | 1 個月動能 |
| `r60` | 0.25 | 3 個月動能 |
| `vol20` | 0.15（反向，`SCORE_WEIGHTS["vol20_inverse"]`） | 本研究直接對 `vol20` **原始值**算 IC，**不做反向**。故此表 IC 的符號須乘以 −1 才對應生產權重方向 |
| `volume_ratio` | 0.15 | 量能擴張 |
| `r5` | **0.00** | 權重為零，測試是否應有權重 |
| `trend` | **0.00** | 權重為零，疑似反轉因子，測試是否應有權重 |

另需計算 composite score 的 IC 作為對照基準。

### 4.4 成本

**IC 本身不加成本**，因為 IC 是無成本的統計量。成本屬於 backtest 階段。

但報告**必須**輸出各持有期的 `forward_return` 分布（p5/p25/median/p75/p95、以及中位數與中位絕對值的比值），讓讀者能自行估算成本侵蝕幅度。

### 4.5 期間與取樣

- **區間**：**2016-01-01 起至資料可得之最新完整交易日**（Alpaca SIP 歷史深度 10.7 年，2016-01-04 起）。涵蓋 2 次熊市（2018Q4、2022）與 1 次崩盤（2020-03），已由 §3.7 的市場重建驗證。
- **主取樣**：`date_step=5`（每 5 個 session 一次，約 160 個 session）。較密會讓 NW autocovariance 在 lag 上不夠估計。
- **穩健性取樣**：`step=61`（非重疊）。**注意：此檢查僅約 8 個點，統計效力不足，只能證明「沒有明顯反向」。** 見 §3.3 末段。
- 若實際可取得的資料少於 1 年，**在報告中明確標示**並下調結論強度。

---

## 5. 重用的生產程式碼（**不要重寫這些邏輯**）

### 5.1 純函式 — 直接 import

```python
from tradingagents.screening.metrics import (
    compute_features,        # metrics.py:256 — 純函式，無 IO
    ascending_percentiles,   # metrics.py:321
    select_top_k,            # metrics.py:374
    EligibilityThresholds,   # metrics.py:54（frozen dataclass）
    SymbolFeatures,          # metrics.py:91
    FACTOR_KEYS,             # metrics.py:41 — ("adv20","r5","r20","r60","vol20","volume_ratio","trend")
    SCORE_WEIGHTS,           # metrics.py:45 — 生產權重，务必直接引用而非硬编码
)
```

`compute_features(symbol, window, *, thresholds) -> (SymbolFeatures|None, exclusion_reason|None)`
回傳全部 7 個因子，門檻邏輯（price / ADV20 / r5,r20,r60 可用性 / zero_volume_baseline）已內建。**這就是生產的 factor 計算，確保研究與生產一致。**

`SymbolFeatures.factor_row()`（:114）直接回傳 7 因子的 dict，可直接餵給 IC 計算。`score` 欄位（:108）由呼叫端填入，本研究應以 `SCORE_WEIGHTS` 自行計算以保持可審計。

### ⚠️ 關鍵：不要呼叫 `scan_universe`

`scan_universe`（`metrics.py:464`）**會強制檢查市值並因此排除標的**。本階段刻意不做市值篩選（見 §3.2），所以：

- ✅ 直接用 `validate_and_clean_bars` + `compute_features` 逐檔處理
- ❌ **不要**呼叫 `scan_universe` 或 `prepare_screening_round`

已驗證 `compute_features`（`metrics.py:256-315`）**完全不讀取 `min_market_cap_usd`**，它只檢查 price、ADV20、r5/r20/r60 的 base close、zero_volume_baseline。因此 `EligibilityThresholds` 實例可以安全攜帶 300M 這個值而不影響結果。

### 5.2 需要注入日曆的函式 — 直接 import

```python
from tradingagents.screening.metrics import validate_and_clean_bars  # metrics.py:181
```

`validate_and_clean_bars(symbol, frame, *, as_of, thresholds, calendar_client, calendar_rows)`

- 傳入整段歷史 frame，函式會切出「截至 `as_of` 的最近 `required_bars`(61) 個權威 session」窗口。
- **`calendar_rows` 一次注入整段期間的交易日曆**（避免 per-date 網路呼叫）。
- 已經處理：NaN/Inf、`close<=0`、`volume<0`、重複 session、缺 session、stale last bar、未收盤 bar。**絕不 forward-fill。**
- 回傳 `(clean_window_df, None)` 或 `(None, exclusion_reason)`。

### 5.3 不可重用、需自寫的部分

`fetch_daily_bars_batch`（`metrics.py:530`）**不能直接用**：它的 `lookback_calendar_days=130` 設計為單一 `as_of`，不適合多年拉取。

自寫的抓取器必須**鏡像它的參數**以確保資料語意一致：

```python
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import Adjustment, DataFeed

# 必須一致：
DataFeed.SIP                      # 不可用 IEX，不可 fallback
Adjustment.SPLIT                  # 對應 adjustment="split"
timeframe = TimeFrame.Day
```

並沿用 `alpaca_utils` 的既有重試與逾時工具：
- `get_alpaca_stock_client()`
- `fetch_with_bounded_retry`
- `_apply_read_timeout`（研究路徑可用更寬鬆逾時）

### 5.4 宇宙來源

```python
from tradingagents.screening.universe import fetch_us_equity_universe  # universe.py:268
```

回傳今日 ACTIVE + tradable + US_EQUITY 的完整清單（已驗證 `universe.py:286-308` 的三重過濾與去重）。

**注意**：`fetch_us_equity_universe` 在 `:309` 會無條件呼叫 `fetch_nasdaq_market_caps()` 並把 `market_cap` 掛到每筆結果上。本階段**忽略 `market_cap` 欄位即可**。該下載有每日 cache（`app_home()/cache/market_cap/nasdaq_<date>.json`），失敗時回 `{}`（fail-closed），不影響我們的流程。

不要為了「省掉 Nasdaq 呼叫」而自行重寫宇宙邏輯——生產的過濾語意（active / us_equity / tradable / 去重 / 排序）必須一致，否則存活者偏差的量級會偏離本文件的記錄。

### 5.5 日曆來源

```python
from tradingagents.dataflows.market_calendar import (
    fetch_trading_calendar,       # :143  fetch_trading_calendar(start, end, client) -> List[row]
    _calendar_date_set,           # :250  rows -> Set[date]（僅供參考／除錯）
    session_dates_ending_at_auth, # :328  權威 session 推導
)
```

**單次**抓取覆蓋 `[研究起始 - 7 天, 研究結束 + 7 天]`，把回傳的 `rows` 原樣注入 `validate_and_clean_bars(..., calendar_rows=rows)`。不要 per-date 呼叫網路。

---

## 6. 模組結構

```
research/factor_ic/
├── PLAN.md                  # 本文件（交接契約，進版控）
├── .gitignore               # data/ 不進版控；out/ 進版控（見下方說明）
├── src/
│   ├── __init__.py
│   ├── common.py            # 路徑、env 隔離、日曆（驗涵蓋範圍）、CSV.gz 快取、Spearman、Newey-West
│   ├── collect_bars.py      # 長歷史日線抓取 → data/bars/batch_*.csv.gz（可續跑）
│   ├── verify_data.py       # 資料品質閘門：10 項檢查 → out/data_quality.json + out/trusted_symbols.txt
│   ├── run_ic_study.py      # 主研究：切窗口 → 算因子 → 前瞻報酬 → IC → 報告
│   ├── audit_data.py        # 資料深度審查（調查，非閘門）→ out/data_audit.json
│   ├── audit_report.py      # 把 data_audit.json 排版成 out/data_audit_report.md
│   └── smoke_test.py        # 端對端自我測試（合成數據，不需金鑰與網路）
├── data/                    # gitignored：bars/（275 MB）、calendar.json
└── out/                     # 進版控：報告、可信清單、研究結果
```

**`data/` 與 `out/` 的版控取捨**：`data/` 是 275 MB 可在 20 分鐘內重新生成的原始行情，不進版控。`out/` 雖然也可重新生成，但它是**證據**——報告與可信清單必須與其產出的程式碼同版本存在，否則日後無法稽核「當時是根據什麼資料下的結論」。故 `out/` 進版控。

### 6.0 執行順序（實測可用的完整流程）

```bash
cd /Users/zongen/Downloads/codex/tradingBuffett
source .venv-p2/bin/activate

# 0. 自我測試（不需金鑰、不需網路，約 20 秒）。通過才值得抓真實資料。
python -m research.factor_ic.src.smoke_test

# 1. 抓資料（可續跑，重複執行只補缺批次）。10.7 年約 20 分鐘。
python -m research.factor_ic.src.collect_bars --start 2016-01-01 --end 2026-09-01

# 2. 品質閘門 → 產出可信清單（9,753 檔）
python -m research.factor_ic.src.verify_data --start 2016-01-01 --end 2026-09-01 --cross-check 400

# 3. 深度審查（可選，但結論引用前必做）
python -m research.factor_ic.src.audit_data --start 2016-01-01 --end 2026-09-01 \
    --trusted research/factor_ic/out/trusted_symbols.txt
python -m research.factor_ic.src.audit_report

# 4. 主研究（約 75 分鐘）
python -m research.factor_ic.src.run_ic_study \
    --start 2016-01-01 --end 2026-09-01 --date-step 5 \
    --symbols-file research/factor_ic/out/trusted_symbols.txt
```

**步驟 4 的 `--symbols-file` 不可省略。** 少了它會把 2,674 檔已知有問題的標的放進橫斷面（見 §3.5 的排除表）。

### 6.0.1 `smoke_test.py` 驗證了什麼（交接者必讀）

這個測試存在的理由：IC 研究有兩種「跑完了但答案是錯的」失敗模式，兩種都不會被「腳本沒報錯」抓到。

1. **對自動相關的 IC 序列用普通 t 檢定** → 顯著性被系統性高估
2. **進場慣例或索引錯誤** → 符號整個翻轉

測試做法：合成一段日線，注入一個**已知符號與量級**的 r20 → 前瞻報酬關係，跑真實 study，斷言它能還原。

已驗證通過的項目：

| 項目 | 結果 |
|---|---|
| Newey-West 公式 | 對 `[1,2,3,4]`, lag=1 手算精確吻合（t = 4.0）；lag=0 退化為 `t_textbook × sqrt(n/(n-1))`（NW 用 1/n 正規化，非 1/(n-1)） |
| NW 對強自相關序列 | \|t\| 確實被壓縮（這是 §3.3 的核心性質） |
| Spearman | 完全正相關 = +1、完全負相關 = −1、常數序列 = NaN |
| **有害因子偵測** | 注入均值回復（kappa=−0.03）後，r20 IC 在 1/5/20 日皆為負（−0.569 / −0.479 / −0.476），t = −104.6 / −94.8 / −90.2 |
| **雙向符號處理** | kappa=−0.03 → IC −0.479；kappa=+0.03 → IC +0.028。單調反應，無隱藏的絕對值或符號 bug |
| **獨立交叉驗證** | 與一份**完全不使用本研究程式碼**的純 numpy 實作比對：1d 差 0.0004、5d 差 0.0005、20d 差 0.0000 |
| 橫斷面穩定性 | 300/300 標的全期維持資格（OU 拉回防止價格跌破 $5 門檻） |
| 產出健全性 | 前瞻報酬分布 n=20400、中位數非退化、Newey-West t 為有限值 |
| **禁止網路** | `common.fetch_trading_calendar` 被換成拋例外版本，任何靜默退回真實日曆都會立即失敗 |

**修改 `common.py` 或 `run_ic_study.py` 之後必須重跑這個測試。** 若 study 與獨立實作不再吻合，是 study 出錯，不是實作出錯。

**這個測試真的抓到過 bug。** 修正 §3.4 的日曆快取涵蓋範圍時，驗收條件連 ±7 天緩衝都要求，導致 smoke test 的合成日曆被判失效、**靜默改用網路抓真實日曆**，取樣網格偏移，1 日期的 IC 與參考實作差 0.055 而失敗。修好後已加上禁止網路的斷言，使同類 bug 無法再藏身。

### 6.1 `common.py` 職責

- `RESEARCH_ROOT` / `DATA_DIR` / `OUT_DIR` 解析（皆在本目錄下）
- `load_calendar_rows(start, end) -> list` — 單次抓取並快取到 `data/calendar.json`，**快取範圍不足即重抓**（見 §3.4 末段）
- `load_bars() / save_batch()` — **CSV.gz 分批快取**（非 Parquet；本環境無 pyarrow）
- `spearman_ic(x, y)` / `newey_west(series, lag)` — 本地實作，本環境無 scipy/statsmodels
- `get_thresholds() -> EligibilityThresholds` — 用**生產預設值**建構，門檻在此集中宣告（見 §8）
- `log()` — 統一輸出格式

### 6.2 `collect_bars.py` 職責

1. 取得當日 ACTIVE tradable US_EQUITY 宇宙 — `fetch_us_equity_universe()`（§5.4），忽略其 `market_cap` 欄位
2. 依 §5.3 參數分批抓取全期間日線
3. **逐批寫入 `data/bars/batch_NNNNN.csv.gz` 並可續跑**（中斷可繼續，不要重抓）
4. 輸出覆蓋報告：總檔數、日期範圍、每檔平均 bar 數、失敗清單

**實測**：10.7 年、12,427 檔、19,225,174 列、2,689 個交易日、136 批、**0 失敗**、約 20 分鐘。

### 6.3 `run_ic_study.py` 職責

1. 載入日線 + 日曆
2. 枚舉每個 `as_of`（每個交易日，或 non-overlapping 間隔）
3. 對每個標的：`validate_and_clean_bars` → `compute_features`
4. 計算前瞻報酬（§4.1 兩種定義）
5. 逐日橫斷面算 Spearman IC（composite + 7 因子）
6. 彙總：mean、median、std、Newey-West t-stat、正號比例
7. 產出報告

---

## 7. 輸出格式

### 7.1 `out/ic_summary.json`

```json
{
  "meta": {
    "as_of_range": ["2023-01-03", "2026-09-25"],
    "n_symbols": 412,
    "n_dates_used": 940,
    "n_dates_skipped_insufficient": 12,
    "thresholds": { "min_price": 5.0, "min_adv20_usd": 20000000.0, "required_bars": 61 },
    "adjustment": "split",
    "data_feed": "sip",
    "market_cap_filter_applied": false,
    "entry_convention": "next_open",
    "t_stat_method": "newey_west",
    "generated_at": "<iso>"
  },
  "factors": {
    "score": {
      "1":  { "ic_mean": 0.0, "ic_t": 0.0, "ic_positive_ratio": 0.0, "n": 0 },
      "3":  { },
      "5":  { },
      "10": { },
      "20": { },
      "40": { },
      "60": { }
    },
    "r5": { }, "r20": { }, "r60": { }, "vol20": { },
    "volume_ratio": { }, "adv20": { }, "trend": { }
  },
  "return_distribution": {
    "5":  { "p5": 0, "p25": 0, "median": 0, "p75": 0, "p95": 0, "median_abs": 0 },
    "10": { }, "20": { }, "60": { }
  },
  "limitations": ["survivorship_bias", "no_point_in_time_market_cap", "split_only_adjustment"]
}
```

### 7.2 `out/ic_report.md`

人類可讀版本，必須包含：

1. 期間、樣本數、覆蓋率
2. **IC 熱點表**：因子 × 持有期的 IC 與 t-stat
3. **符號正確率表**
4. 前瞻報酬分布（供成本侵蝕估算）
5. **§3 全部已知偏差逐條列出**
6. 明確的「本研究不回答什麼」
7. non-overlapping 穩健性檢查結果
8. close-to-close 對照結果（標記為有偏誤）

### 7.3 `out/ic_timeseries.csv`

逐日 IC 明細，供後續進階統計（分 regime 檢查等）。

---

## 8. 門檻值來源（重要）

`get_thresholds()` **必須使用生產預設值**，不得為研究目的調整。

**正確寫法**：

```python
from tradingagents.screening.metrics import EligibilityThresholds

def get_thresholds() -> EligibilityThresholds:
    return EligibilityThresholds.from_config({})   # 全部生產預設值
```

`from_config({})` 會得到：`min_price=5.0`、`min_adv20_usd=20_000_000.0`、`min_market_cap_usd=300_000_000.0`、`required_bars=61`、`vol_window=20`、`vol_mean_window=20`、`ratio_recent_window=5`（已於 `metrics.py:56-62, 76-88` 驗證）。

### ⚠️ 常見錯誤：不要傳 `min_market_cap_usd=None`

`EligibilityThresholds` 是 `@dataclass(frozen=True)`，其 `__post_init__`（`metrics.py:64-73`）**強制要求 `min_market_cap_usd` 為正且有限值**，傳 `None` 或 `0` 會直接 `raise ValueError`。

**不需要這樣做**。已驗證 `compute_features` 從不讀取 `min_market_cap_usd`（見 §5.1），所以攜帶生產預設的 300M 完全無害——它只是永遠不被查到。市值篩選的略過是靠**不呼叫 `scan_universe`** 達成的，不是靠改門檻值。

**理由**：若研究門檻與生產不同，IC 結論無法外推回生產管線。保持一致是這份研究唯一的 validity 前提。

---

## 9. 執行方式

完整可用的四步驟流程見 **§6.0**。環境需求見 §9.1，唯讀限制見 §9.2。

### 9.1 環境需求

- 需要 `TRADINGBUFFETT_ALPACA_API_KEY` / `SECRET_KEY`（只讀歷史行情）
- 需要 `TRADINGBUFFETT_ALPACA_USE_PAPER=True`（沿用專案 hard lock）
- **必須設定** `TRADINGBUFFETT_RESULTS_DIR`、`TRADINGBUFFETT_CACHE_DIR`、`TRADINGBUFFETT_MEMORY_LOG_PATH`、`TRADINGBUFFETT_AGENT_MEMORY_DIR`、`TRADINGBUFFETT_EXECUTION_DB`、`TRADINGBUFFETT_LONG_RUN_DIR` 指向本目錄的 `data/`，**避免寫入 operator 的 `~/.tradingbuffett/`**
- 僅 `smoke_test.py` 不需要上述任何一項（它用 `TRADINGBUFFETT_RESEARCH_DATA_DIR` 導向臨時目錄）

### 9.2 副作用限制（硬性）

本研究**唯讀**。不得：

- 建立或修改任何 execution ledger
- 寫入 `~/.tradingbuffett/safety/`（會影響 kill switch / HWM）
- 寫入 `~/.tradingbuffett/long_run/`
- 觸發任何 broker mutation
- 呼叫 LLM

理由：operator 的 safety state 與 execution ledger 是不可再生的持久狀態，研究程式污染它會造成實際損失。

---

## 10. 結果判讀規則（預先寫定，避免事後合理化）

在拿到資料**之前**先定好判準：

| 觀察結果 | 意義 | 建議動作 |
|---|---|---|
| 所有因子在 5/10 日 IC 的 \|t\| < 2 | 生產因子在实际持有期上無統計顯著預測力 | **停止策略調參。** 重新設計因子或放棄波段定位 |
| `r20`/`r60` 在 5/10 日 IC 顯著為負 | 期限錯配導致**反向** | 檢驗 IC@20/40/60；若長期限為正 → 延長持有期；若仍為負 → 翻轉權重 |
| `r20`/`r60` 在 20/40/60 日顯著為正，5/10 日不顯著 | **因子對，期限錯**（最可能） | 把持有期拉長至與因子期限匹配，或改用 `r5`/`trend` |
| `r5` 或 `trend` 在 5/10 日顯著為正 | 零權重因子其實有效 | 重新配置權重（需謹防過度擬合） |
| `vol20` 顯著為正 | 生產使用反向權重是錯的 | 重新檢視 |
| 因子在所有期限都不顯著 | 純雜訊 | 停止。這是重要的負面結論，同樣有價值 |
| non-overlapping 子樣本與主報告方向不一致 | 結果不穩健 | **不採信**，回報為無結論 |

**禁止事項**：不得因為結果不符期待而事後調整門檻、期間、因子定義後重跑並只報有利結果。所有跑過的版本都要保留在 `out/`。

---

## 11. 明確不做的事

1. **不寫 backtest engine** — 那是 Gate 1b，且需要先解決 point-in-time 資料來源
2. **不做 LLM 消融實驗**（Gate 2）— 需要先有 IC 結論
3. **不碰 30 天 Paper run** — 統計上無法回答問題（$500 notional on $100k，30 日 SE ≈ $120）
4. **不調整任何生產參數** — 本階段只觀察
5. **不修改 `tradingagents/` 任何一行** — 發現生產 bug 請回報，不要順手改

---

## 12. 已知的生產程式碼不一致（觀察紀錄，本階段不修）

這些在閱讀時發現，與本研究無直接關係，**列出僅供記錄，不要在本階段修**：

| 項目 | 位置 | 說明 |
|---|---|---|
| `store.py` docstring 宣稱 schema v2 / 三張表 | `store.py:1-6` | 實際為 `SCHEMA_VERSION = 3`、四張網域表 |
| `_is_ambiguous_error` 無生產呼叫者 | `service.py:68`, `requests.py:53` | 唯一呼叫者是測試。其名稱暗示文字比對，但生產規則是結構化 4xx，易誤導 |
| `adopt_broker_order` 無生產呼叫者 | `store.py:668` | 死碼 |
| 7 個 prompt 模板無引用 | `prompts/templates/` | 5 個 `analysts/*_final_recommendation` + `shared/analyst_final_recommendation` + `trader/trader_fallback_plan` |
| `error_diagnostics.py` 無生產引用 | 289 行 | 死碼 |
| `max_symbol_concentration_pct=0` 語義矛盾 | `guardrails.py:496` vs `exposure.py:284` | 前者視為 uncapped、後者硬拒絕。方向安全（fail-closed） |
| PARTIAL row 阻塞全部 recovery | `recovery.py:537-552` 缺 `PARTIAL` | 已記錄為 F-02，刻意延期。操作員可在券商端取消掛單解除 |

---

## 13. 交接檢查清單

接手者動手前確認：

- [ ] 已完整讀過本文件
- [ ] 已讀 `metrics.py` 的 `compute_features`（:256）與 `validate_and_clean_bars`（:181）實際簽章
- [ ] 已讀 `metrics.py:56-88` 理解 `EligibilityThresholds` 的 `__post_init__` 限制，**不會傳 `None` 市值**
- [ ] 已理解**不呼叫 `scan_universe`** 的原因（§5.1）
- [ ] 已讀 `universe.py:268-312` 理解 `fetch_us_equity_universe` 的過濾語意與 Nasdaq 呼叫
- [ ] 已讀 `market_calendar.py:143` 的 `fetch_trading_calendar` 簽章
- [ ] 已讀 `backtest/engine.py:100-150` 確認 next-open 成交慣例
- [ ] 已理解 §3.3 的 Newey-West 要求（**這是本計畫最容易做錯的地方**）
- [ ] 已理解 §4.1 的 next-open 進場定義（**這是第二容易做錯的地方**）
- [ ] 已理解 §9.2 的唯讀限制
- [ ] 已設定 §9.1 的環境變數隔離
- [ ] 已理解 §10 的判讀規則是**預先寫定**的，不得事後修改
- [ ] 已跑過 `python -m research.factor_ic.src.smoke_test` 並通過
- [ ] 已知悉：改動 `common.py` / `run_ic_study.py` 後必須重跑 smoke test
- [ ] 已知悉：若 study 與 smoke test 內的獨立 numpy 實作不再吻合，是 **study** 出錯
- [ ] 已知悉：本環境**沒有** pyarrow / scipy / statsmodels，Spearman 與 Newey-West 為本地實作
- [ ] 已知悉：NW 的長期變異數用 `1/n` 正規化（NW 慣例），與普通 t 檢定的 `1/(n-1)` 差 `sqrt(n/(n-1))`
- [ ] 已知悉：`collect_bars.py` **不能**重用 `fetch_daily_bars_batch`（其 130 天 lookback 只適用單一 as_of）
- [ ] 已知悉：合成 fixture 必須讓 `open != close`，否則 horizon 1 的同日報酬恆為 0、IC 全為 NaN
- [ ] 已知悉：**NW lag 以取樣單位計**（`ceil(horizon/date_step)`），不是 horizon（§3.3）。看到 `nw_lag = horizon` 就是舊的錯的
- [ ] 已知悉：日曆快取範圍不足會重抓；`smoke_test` 已禁止網路，不要移除那條斷言（§3.4 末段）
- [ ] 已知悉：主研究必須帶 `--symbols-file out/trusted_symbols.txt`
- [ ] 已知悉：`audit_data.py` 與 `verify_data.py` 用途不同（調查 vs 閘門），不可互相取代
- [ ] 已讀 §3.7 知道引用結論時必須附帶的兩項揭露（存活者偏差、無 point-in-time 市值）
- [ ] 已讀 §14 知道 3.7 年已得的結果，避免重跑已知的無效實驗
- [ ] 已讀 §15 知道因子庫掃描已執行完畢，且**沒有**因子通過多重檢定
- [ ] 已理解 §15.0 的三分期設計與 §15.7 為何拒絕 `tail_ratio_120` @20日那個「發現」
- [ ] 已知悉：修改 `factor_lib.py` 後必須重跑 `factor_report.py` 以重生 `FACTORS.md`，否則文件與實作不一致

---

## 15. 因子庫掃描（2026-09-27，第二階段）

### 15.0 為什麼有這一段

§14.2 的結論是「生產那 7 個因子在 5–15 日沒有優勢」。操作員的要求是：**不限因子數，盡可能多找，把找到的量化成公式，最後一起跑。**

照做的同時必須解決一個方法學問題，否則產出必定是假的：

> 在 N 個候選因子中挑出最好的，並用**挑選它的那批資料**證明它有效 —— 這在數學上保證會得到漂亮結果。`|t|>2` 的門檻下，純隨機因子約有 4.6% 會通過，掃 102 個就預期 ~5 個假陽性。

更嚴重的是，這 102 個**不是 102 個獨立檢定**：`hv_20` 與 `hv_60` 幾乎是同一個數字。

**因此本階段強制三分期：**

| 期間 | 範圍 | 用途 |
|---|---|---|
| **探索期 discovery** | 2016-01-01 .. 2021-01-01 | 在此**找**因子 |
| **驗證期 validation** | 2021-01-01 .. 2026-08-31 | **完全不參與挑選**，只驗證 |
| 合併 combined | 全期間 | 頭條數字，但**已被探索期污染**，必須標明 |

**通過標準：驗證期 `|t|` 通過族校正門檻，且與探索期同號。** 只在探索期顯著者一律標為「僅探索期顯著」，不採信。

### 15.1 有效獨立檢定數

校正門檻不該用因子數 102（太嚴），也不該假裝獨立（太鬆）。本階段由 **IC 序列相關矩陣的特徵值**推估有效檢定數：

```
N_eff = n² / Σλ²        （λ 為相關矩陣特徵值，因為對角線為 1 故 Σλ = n）
```

完全獨立的因子給出 `N_eff = n`，完全重合的因子給出 `N_eff = 1`。已用這兩個極值與「5 組 × 10 重複」的中間案例驗證實作正確。

### 15.2 模組

| 檔案 | 職責 |
|---|---|
| `src/factor_lib.py` | 94 個候選因子，每個都是明確公式，**文件由此自動產生**故不會與實作漂移 |
| `src/run_factor_scan.py` | 掃描驅動器：探索/驗證/合併三期 + 前置閘控 |
| `src/factor_report.py` | 產生 `FACTORS.md` 與 `out/factor_report.md`（含多重檢定帳） |

因子涵蓋 8 個家族：短期反轉、動能、波動率（含 Parkinson / Garman-Klass / Yang-Zhang）、流動性（含 Amihud）、量能、盤中結構、市場相對風險（beta / idio vol）、高階矩。**多數是生產系統完全沒用的家族。**

### 15.3 前置閘控（掃描前必須通過，否則拒絕執行）

1. **快速資格路徑 vs 生產逐檔判斷**：以 8 個橫跨全期間的日期（涵蓋分割兩側）比對，必須 **8/8 完全一致**（成員差異 = 0）。這是效能優化，不是重新定義——條件全部來自 `validate_and_clean_bars` + `compute_features` 的向量化重述。
2. **快速因子路徑 vs 生產輸出**：同樣 8 個日期逐欄比對 `adv20/r5/r20/r60/vol20/volume_ratio/trend/score`，最大相對誤差須 < 1e-9。實測 **5.55e-16**（機器精度）。
3. **`spearman_fast` vs `spearman_ic`**：於 `smoke_test` 中以 200 組含並列、NaN、洗牌 的輸入斷言一致至 1e-12。實測最大差 5.55e-17。
4. **因子拋例外必須報出**：`compute_all` 回傳 `errors` 清單。任何因子拋例外都會被記錄，**不會**靜默變成 NaN——那會讓一個 crash 看起來像「沒有 IC」的乾淨負面結果。

### 15.4 實作期間抓到的四個自身錯誤（記錄以免重蹈）

| 錯誤 | 後果 | 修法 |
|---|---|---|
| 視窗轉置成 `(時間, 標的)` | **因子庫把橫斷面當成時間軸**，全部因子讀錯軸 | 移除 `.T`；`Ctx` 的形狀約定寫進註解 |
| `yzvol_60` 對陣列用內建 `max()` | 拋 `truth value is ambiguous`，被 `except` 吞成 NaN | 改 `np.maximum` |
| `mfi_14` 把 `flow[:, 1:]` 與長度 14 的 `d` 相乘 | 廣播失敗，同樣被吞成 NaN | 兩者本就等長，移除切片 |
| dense 網格用 float32 | 因子相對誤差 5e-7，**高於 1e-9 等價門檻** | 改 float64（1.05 GB，可接受）。**沒有放寬門檻來遷就網格** |

後兩者若沒有「拋例外必須報出」這條規則，會以「此因子無 IC」的形式安靜地出現在報告裡。

### 15.5 執行

```bash
python -m research.factor_ic.src.run_factor_scan \
    --start 2016-01-01 --end 2026-09-01 --split 2021-01-01 --date-step 5 \
    --verify-fast-dates 8 \
    --symbols-file research/factor_ic/out/trusted_symbols.txt
python -m research.factor_ic.src.factor_report
```

`--symbols-file` 不可省略（同 §3.5）。`--verify-fast-dates 0` 會停用快速路徑與其驗證，改用生產逐檔呼叫（正確但慢數小時）。

### 15.6 結果（已執行）

掃描 476 個取樣日、9,753 檔可信集、橫斷面中位數 1,673，耗時 204 秒。

**有效獨立檢定數 N_eff ≈ 5.7**（由 5 日 IC 序列相關矩陣特徵值推估）——101 個因子高度重疊，實際只有約 6 個獨立方向。**族校正後門檻 |t| ≥ 2.62。**

| 期限 | 探索期達標 | 驗證期達標 | 兩期同號且都達標 |
|---:|---:|---:|---:|
| 1 | 0 | 6 | 0 |
| **5（主軸）** | **3** | **0** | **0** |
| 10 | 2 | 0 | 0 |
| 20 | 2 | 1 | 1 |
| 40 | 1 | 2 | 0 |
| 60 | 1 | 7 | 0 |

**結論：沒有任何因子在未參與挑選的資料上通過族校正門檻。** 生產 `score` 合併期 IC = 0.0025、t = 0.32。

仍值得記錄的三點：

1. **短期反轉方向最穩**：`mom_1`/`mom_2`/`mom_3`/`mom_5` 兩期**全部同號為負**。四個獨立持有期給出同方向結果，比單一因子 |t|=2.4 更值得注意。
2. **流動性溢價同向**：`adv_5`/`adv_20`/`adv_60` 全為正，與 §14.2 的發現一致。
3. **因子庫沒找到生產系統完全沒有的新結構**：主成分分析顯示變異集中在波動/回撤（成分 1）與流動性（成分 5），正是生產權重最大的兩個方向。

### 15.7 拒絕的一個「發現」

若容許在 6 個持有期上挑選，會找到 `tail_ratio_120` @20 日（探索 −2.78 / 驗證 −2.73，兩期同號）。**本報告不採用。**

理由：那是 606 次檢定而非 101 次，N_eff 會從 5.7 升到約 34，門檻也隨之從 2.62 升到 3.18。而 606 次檢定中偶然出現 9 個左右達標組合本來就是機率應有的樣子（實測恰好 9 個）。**這正是 §10 禁止的事後挑選。**

### 15.8 因子庫正確性的獨立佐證

庫中有三個因子在數學上等同生產因子，其 IC 必須逐位相同：

| 因子庫 | 生產 | 最大 IC 差 |
|---|---|---:|
| `mom_5` | `r5` | 0.00e+00 |
| `adv_20` | `adv20` | 0.00e+00 |
| `ma_dist_20` | `trend` | 0.00e+00 |

**完全相同。** 這是獨立於其他所有檢查的驗證：資料切窗、橫斷面對齊、秩計算全部與生產一致。

### 15.9 本階段不做的事

- **不調整任何生產權重**。找到有效因子**不等於**應該換掉現有權重；那是另一個決策，需要先討論交易成本與容量。
- **不做多重檢定後再回頭調因子**。§10 的預先寫定原則同樣適用於此。
- **不把驗證期結果回頭用來挑選因子**。那會把驗證期變成第二個探索期，這正是本階段要避免的。

---

## 14. 已完成的實作與已得結果（2026-09-27）
### 14.1 交付狀態

| 項目 | 狀態 |
|---|---|
| `smoke_test.py` | ✅ 通過（含禁止網路斷言） |
| `collect_bars.py` 10.7 年抓取 | ✅ 12,427 檔、19,225,174 列、0 失敗 |
| `verify_data.py` 品質閘門 | ✅ 10 項檢查，可信集 9,753 檔 |
| `audit_data.py` 深度審查 | ✅ 阻擋性問題 0 項（見 §3.7） |
| **10.7 年主研究** | **⏳ 尚未執行（下一步）** |

### 14.2 3.7 年先導研究的結果（**已過時，僅供對照**）

在 2023-01 ~ 2026-09（12,396 檔、162 個日期、橫斷面中位數 2,099）上跑過一次，結論是**篩選因子在 5–15 日持有期沒有可測得的優勢**：

| 因子 | 權重 | 1/5/20 日 IC 的 \|t\| | 判定 |
|---|---:|---|---|
| `score`（composite） | 1.00 | **≤ 1.3（所有期限）** | 雜訊 |
| `r20` + `r60` | 0.50 合計 | **≤ 0.9** | 雜訊（佔一半權重） |
| `adv20` | 0.20 | 2.1 / 2.1 / 2.6 | 唯一顯著，但只是流動性溢價 |
| `r5`、`trend`、`vol20`、`volume_ratio` | 0.00 / 0.15 | 不顯著 | — |

量級對照：最佳因子的毛邊際約 **0.7%/年**，而交易成本約 **3.9%/年**。

**方法學註記**：此結果是在 NW lag 錯誤（`lag = horizon`）修正**之前**跑出的，因此 |t| 偏高 0.3–1.5。修正後只會**更弱**。3.7 年樣本也只涵蓋 1 次熊市尾段，不足以支撐跨 regime 的結論。

**下一步**：以 10.7 年 + 可信標的集重跑同一個研究（§6.0 步驟 4，約 75 分鐘），與本表對照。若仍無優勢，結論可升級為「**10.7 年、2 次熊市 + 1 次崩盤，無優勢**」。

**不要**因為結果不符期待而調權重重跑。§10 的判讀規則是預先寫定的。
