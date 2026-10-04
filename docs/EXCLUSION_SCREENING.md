# 報告公式接入主程式

來源：`research/out/20260928-exclusion-screen/REPORT.md` §7（2026-09-28）。
本次接入的是該報告公式，未採用後續研究的 efficient60 等替代公式。

啟用 `auto_screening_enabled=True`，`screening_method="auto"` 且
`allow_shorts=False` 時使用新流程；手動 ticker 模式仍由原本入口控制。
CLI 自動篩選預設選公式模式，長跑設定預設也為 `auto`。

## 計算與名單

保留 SIP、完整 61 sessions、價格 ≥5 美元、ADV20 ≥2,000 萬美元、
有效市值 ≥3 億美元、corporate-action quarantine 等原有資格檢查。
在所有合格股票上再要求：

```python
vol20 <= 0.28   # 20 個日報酬的 sample std (ddof=1) * sqrt(252)
trend > 1e-12  # close / MA20 - 1；容差見下
r60 >= -0.25   # close / close[-61] - 1
```

**trend 容差（公式版本 `exclusion-20261004-1`）**：十進位價格在二進位下不精確，
收盤價**恰等於** 20 日均價時，`trend` 會算成 ±1e-16，舊版 `trend > 0` 等於由捨入誤差
決定去留（債券型標的常見）。以分為單位的價格，真實非零 trend 最小約 7e-10
（即使股價 70 萬美元），所以 `TREND_EPSILON = 1e-12` 只吸收捨入誤差。
版本號變更會使舊快取的指紋失效並重掃一次。

僅在存活池計算百分位 `p = (平均名次-1)/(N-1)`，單一股票為 0.5：

```python
exclusion_score = 100 * (0.50*p(trend) + 0.30*p(r60) + 0.20*(1-p(vol20)))
```

依分數降序，同分依 ticker 升序，直接選最多 20 隻。既有 `score`
保留為舊公式分數；新公式使用獨立 `exclusion_score`，輸出
`screening_score` 為新公式分數。公式選股不呼叫 Screening LLM。

### 不足 20 檔：頻率與為什麼不補

`research/out/20261004-screen-diagnosis/`（2026-10-04）在**只含股票**的研究宇宙
（與生產一致；見下方 ETF 一節）每個交易日重算本公式：

| 期間 | 資格池 ≥ 20 但存活 < 20 的交易日 | 最少存活 |
|---|---:|---:|
| 2016–2020 | 68 天（約 5.4%） | 0 |
| 2021–2025 | 22 天（約 1.8%） | 3 |

先前文件引用的「9／5 天」是在混入 ETF 的宇宙上量的，低估約 6 倍。生產另有
$300M 市值門檻，實際頻率只會更高。這些日子集中在全市場崩跌（2018-02、2020-03、
2022-06），主要是 `trend > 0` 與 `vol20` 被全市場同時打穿。

事先寫定並只測一次的補位規則（從未過 vol20／trend、但過 r60 的股票中，用同一公式
補到 20）在這些日子的 20 日大跌率是 **26.2%**，高於舊分數公式的 22.5%，而當日存活者
只有 **11.1%**。因此維持「最多 20 檔、不足不補」：門檻在崩跌日最有效，硬補會引入
較差的候選。有效樣本只有 13 個不重疊 20 日區塊，這是方向性證據，不是精確估計。

### ETF 只是被間接排除

研究用的可信集約一半是 ETF；混 ETF 時本公式平均 12–15% 的名額給了 ETF
（某期 20 檔全是債券 ETF，另一期含 3 倍槓桿 ETF）。生產端市值來自 Nasdaq **股票**
screener，ETF 拿不到市值，被 `missing_market_cap` 剔除——這是資料來源的副作用，
不是明確的資產類型規則。若改用會回傳 ETF 市值的來源，ETF 會重新進入名單。

### 產業約束

有完整產業分類時保留既有每產業最多 5 隻約束，依分數掃過整個
存活池補名額；分類缺少時明確記錄缺失，execution 原有產業曝險閘門
仍獨立執行。門檻／產業容量不足可以只選 1–19 隻，不降低標準補數；
零存活者不呼叫 Screening LLM，只審查持倉。

每輪 freshly verified、可安全分析的美股持倉先占分析名額，候選補滿。
`screening_analysis_limit` 預設 20，所有自動篩選模式不能大於 20；
`screening_select_n` 在所有自動篩選模式也不能大於 20。非交易日同樣受持倉分析
名額限制。超額持倉返回 `HELD_REVIEW_BUDGET_EXCEEDED` 並停止本輪，
不靜默捨棄持倉。新候選被擠出時列入 `deferred_candidates`。
此限制計算股票數；每隻股票的多 agent / retry 呼叫仍受既有 token 預算約束。

## 設定與相容性

| 設定 | 預設 | 用途 |
|---|---|---|
| `screening_method` | `auto` | 做多用公式；開做空用舊雙向 LLM 流程 |
| `screening_max_vol20` | `0.28` | 年化波動上限 |
| `screening_min_r60` | `-0.25` | 60-session 報酬下限 |
| `screening_analysis_limit` | `20` | 每帳戶每輪最多分析股票數，含 Screening 與持倉 |

`screening_method="legacy"` 保留舊價格公式與 Screening LLM，
仍須獨立 provider/model/key；舊雙向做空流程不適用做多報告的 trend>0
門檻。`screening_method="exclusion"` 明確要求公式，加 `allow_shorts=True`
會拒絕啟動。所有模式都共用 20 檔上限。Legacy 在呼叫篩選 LLM 前先查持倉，最多送出剩餘名額的候選，雙向候選共用同一額度。產業容量不足時減少選取數；沒有名額不呼叫篩選 LLM。篩選後持倉新增造成當輪已看過的股票聯集超額時，返回 `SCREENING_ANALYSIS_BUDGET_EXCEEDED`，不繼續深度分析。每日快取重用未新增篩選 LLM 呼叫，但持倉仍會重新核對並預留深度分析名額。

`scan_universe` 的逐檔行情驗證改為 numpy 向量運算，交易日窗口每次掃描只算一次；
合成 3,000 檔的掃描從 15.3 秒降到 4.1 秒。新舊實作以 20,000 組隨機與異常行情做差分
測試，排除理由、輸出窗口與 dtype、證據 hash、因子值全部相同。

快取 schema 升為 6，舊快取失效後重新掃描；指紋綁定方法、公式版本、
門檻與所有模式的分析上限。公式模式的 `top40` 欄位沿用舊欄位名稱，但內容為
**完整存活池**，不會送給 LLM；保留完整因子，可在讀取時重算百分位、
排序及產業選股。每日 cache、entry gate、frozen A/B artifact 共用驗證，
不把自行重算的 SHA seal 當作公式正確性的證明。

長跑每輪保存分析集合和因子日期；重啟使用凍結集合，不重掃擴大同一輪
名單。A/B 各帳戶依自身持倉預留名額：只有雙邊皆有分析名額的候選形成
配對；無配對的新候選不分析／交易，既有持倉仍走自身風險審查。
`budget_unpaired_symbols` 和 `AB_ANALYSIS_BUDGET_UNPAIRED` 留下原因。

Risk Manager 接收凍結的 `vol20 / trend / r60 / adv20` 等測量及 as-of；
未出現在存活池的持倉明確標示篩選數值不可得，不假定它通過門檻。
分數不直接決定下單、倉位大小或通過 execution 風控。

## 驗證

`tests/test_exclusion_screening.py` 覆蓋手算分數、邊界、存活池百分位、
0/9/20 候選、產業容量、持倉占額、零 Screening LLM 呼叫、每日／凍結
快取、重新封印後仍無效的錯誤公式與 entry gate。
長跑 integration tests 同時覆蓋公式模式只 probe Analysis/Decision、
設定傳遞、重啟名單凍結、因子傳入 graph、超額名單拒絕恢復。
所有驗證使用離線 fake broker / LLM，沒有發出真實交易或模型請求。

這些測試證明實作與限制行為，並非新策略的樣本外收益驗證，也不能把
價格風險排除等同於已驗證企業品質。

2026-09-29 本機驗證（Python 3.12）：

```bash
TRADINGBUFFETT_ALPACA_READ_ONLY=false .venv-p2/bin/python -m pytest tests/ -q --tb=short
```

結果：**1,969 passed，335 subtests passed**，86.61 秒。
另外通過 CLI `--help`、修改模組 `compileall` 和 `git diff --check`。
唯一 warning 是既有 `websockets.legacy` 棄用提示。
本輪長跑 characterization 的完整新快照只改 schema-6 配置指紋；
還原 schema-5 指紋後精確匹配上版完整 SHA-256，交易／復原 trace
與報表內容沒有差異。新增共用預算測試核對篩選 LLM 看過的股票與
深度分析股票聯集，涵蓋雙向候選、持倉、零名額及篩選後持倉變動。
完整記錄見 `research/out/20260929-data-repair-pilot/full_tests_final.log`。

舊的執行中 observation 受既有 code/config drift guard 保護，不直接
跨版本恢復；新觀察使用新公式和 schema，既有 execution ledger 保留。
