# 52 項修復逐項驗證

> 後續狀態：本文件記錄補修前的檢查。使用者要求補修後，六項已修復，原 probes 全通過，完整離線 suite 1,534 passed。見 [六項修復完成紀錄](/Users/zongen/Downloads/codex/tradingAlpaca/docs/AUDIT_REPAIR_SIX_FIXES_2026-09-18.md)。

日期：2026-09-18（Asia/Taipei）
受驗版本：`fd3819a67deea839a6429e150c4e89ca88039870` **加目前未提交的工作目錄修改**。
對照基線：`7953ca0`。
使用者要求：逐一檢查是否全部修復；本次未修改產品程式，也未代為補修。

## 結論

**尚未全部修復完成。52 項中，46 項在本次來源核對與既有離線回歸範圍內未發現原缺陷殘留；6 項確認只修了一部分：L02、D01、D02、D11、D14、U01。**

`AUDIT_REPAIR_COMPLETION_2026-09-17.md` 的「52 項完成」不能直接作為驗收結論。其宣稱的完整 suite 通過可以重現，但 suite 沒有覆蓋下列殘留邊界。

- 本次重新跑完整離線 suite：**1,487 passed、2 warnings，41.80 秒**。
- 本次另外撰寫 10 個離線驗收案例：**9 failed、1 passed，2.14 秒**；失敗對應上述 6 個原編號，並非 9 個新增 finding。
- `git diff --check` 通過。wheel 建置與離開 repo 的資源／CLI smoke 包含在本次完整 suite，實際重跑通過。
- 「未發現殘留」限於來源與離線驗證，沒有宣稱每項均經 broker、live LLM、行情供應者或瀏覽器端到端驗收。

## 確認尚未完成的 6 項

### U01：串流 handler 已修，正式平行 coordinator 仍繞過 ownership

位置：`tradingagents/graph/setup.py:188–191、275–286`；`webui/utils/state.py:287–303`。

`run_analysis()` 已將 symbol 與 generation 傳給 `process_chunk_updates()`，handler 也在鎖內驗證。但 WebUI 正式使用 `parallel_analysts=True`，平行 coordinator 仍直接呼叫 `update_agent_status()` 並寫 `ui_state["current_reports"]`。這條路徑沒有 immutable generation、沒有 ownership lock；status 呼叫甚至未指定 symbol。

兩個實際 coordinator 離線案例均失敗：

1. AAPL 舊分析執行中 Stop → Start，同一 symbol 新狀態已有 `NEW RUN REPORT`；舊 analyst 返回後仍寫成 `OLD RUN REPORT`。
2. AAPL 舊分析執行中 Stop → Start，`analyzing_symbol` 改為 MSFT；AAPL 舊 analyst 返回後把 MSFT 的 Market Analyst 設為 `completed`。

這是可重現的舊 run 污染新 run UI 狀態；**沒有證明 broker 發生錯單**。此外 `analysis.py:398` 的 final generation check 與後續結果寫入仍在鎖外，是另一个靜態檢查後寫入窗口，本次未另做動態重現。

最小補修：graph dispatch 攜帶來源 symbol＋generation，將平行 analyst／risk coordinator 的 UI 更新與 final result 寫入統一交給鎖內驗證的 helper；或移除 graph 直接写 UI，只經受保護的 stream consumer 發布。

### L02：能執行標準 tool_calls，卻沒有保存 native assistant tool request

位置：五個 analyst 的工具歷史建構；例如 `tradingagents/agents/analysts/market_analyst.py:270–281`。

迴圈現在優先讀標準 `.tool_calls`，能找到並執行工具。但拿到標準 `{name,args,id}` 後，仍把它塞進 `AIMessage(additional_kwargs={"tool_calls": [...]})`。這不是 OpenAI raw `{function:{name,arguments},id}` 格式；本機 LangChain 產出的新訊息 `.tool_calls` 為空。

本次執行真正 market analyst 迴圈，攔截第二次 invoke 的歷史，再交給已安裝 Anthropic adapter 的真正 `_format_messages()`。結果只有：

```text
user: tool_result(tool_use_id=call-native)
AI.tool_calls=[]
沒有相應 assistant tool_use
```

因此雖然工具有執行，續輪 provider request 仍缺相應工具請求。這是本機 adapter request-shape 的直接證據；未向 Anthropic 送網路請求，沒有宣稱已觀察服務端 HTTP 400。Google adapter 來源同樣依賴 `.tool_calls`；本次未另跑 Google formatter。五個 analyst 均保留相同歷史建構模式。

另外，五個 analyst 的第一層 `tool_loop_exhausted` 仍只查 raw additional_kwargs，標準-only provider 用盡 budget 時不會走相同合成分支，屬靜態殘留。

最小補修：保留原始 AIMessage，或建立合法 `AIMessage(tool_calls=[...])`，保存 call IDs 與對應 ToolMessage；一次含多個 calls 時保留完整 assistant request。budget 判斷也使用共同的標準／raw resolver。

### D01：正無限成交量仍進入 cleaned frame

位置：`tradingagents/dataflows/technical_brief.py:897–909`。

價格有新增有限性／OHLC 關係檢查，但 volume 只驗 `notna()` 與 `>=0`。`volume=float("inf")` 同時滿足這兩個條件。本次給一根正常 OHLC、無限 volume 的 bar，`_clean_frame()` 回傳該 bar，未拒絕。

最小補修：OHLCV 全部使用 finite 檢查，保留合法 volume=0；若接受可轉換數字字串，也將驗證後的數字寫回 cleaned frame。

### D02：僅拒 4Hour，其他合法 interval 表示仍被錯誤映射

位置：`tradingagents/dataflows/alpaca_utils.py:448–460、651`。

fallback 仍按原始 timeframe 字串猜 interval，而 `_parse_timeframe()` 正式支援 `1h`、`4h`、`2Hour`。`get_stock_data()` 呼叫 fallback 時仍傳原始 timeframe。

| 輸入 | 本次實際到 Yahoo 的 interval | 驗收期待 |
|---|---|---|
| `4Hour` | 未呼叫 Yahoo | 通過：不支援就 unavailable |
| `4h` | `1d` | 應 unavailable |
| `2Hour` | `1h` | 應 unavailable |
| `1h` | `1d` | 應 `1h` |

本次以 patched `yfinance.download` 記錄 request，沒有向 Yahoo 抓資料。錯誤只在 fallback 啟用且被使用時觸發。

最小補修：先 normalize timeframe，再按精確 amount＋unit 做支援白名單；不能用包含 `hour`／`4` 判斷，也不能把未知 interval 預設成 daily。

### D11：當日過去的精確 cutoff 仍會抓 present quote

位置：`tradingagents/dataflows/interface.py:1722–1726`。

歷史 window 已傳 end，stock request 也已區分 date-only 與精確 timestamp。但報告是否加 current quote，僅以 `str(end_date)[:10]` 判斷是否今天。這會忽略同一天內的過去 cutoff。

本次固定現在為 `2026-09-16 14:00 America/New_York`，呼叫 cutoff `2026-09-16T10:00:00-04:00` 的 `get_alpaca_data()`；patched `get_latest_quote()` 仍被呼叫。使用真正 `analysis_date_mode()`，只注入固定 now。

最小補修：精確 instant cutoff 不加入 current quote，或要求 quote timestamp 不晚於 cutoff；date-only 的 live 語意另行保留。這是舊 helper 邊界，沒有泛化成正式嚴格 wrapper 全部有 lookahead。

### D14：同比公式已修，正式預設 fetch window 仍不足

位置：`tradingagents/dataflows/macro_utils.py:141–152、399`。

`_format_indicator_section()` 現在正確按去年同月比較，缺月也不硬算。但是正式 `get_economic_indicators_report()` 預設仍是 `lookback_days=90`，綜合 macro caller 也使用這個預設；因此去年同月通常根本不會被抓回。

本次 fake FRED 提供 2026-08 與 2025-08 兩筆有效資料，並遵守實際 start/end 篩選。呼叫 `get_economic_indicators_report("2026-09-16")` 時，各 series 都只要求 `2026-06-18` 至 `2026-09-16`，CPI／PPI report 因而沒有 YoY。這是功能完成度缺口，未把它升格為交易安全缺陷。

最小補修：對需要 YoY 的 series 抓足去年同期窗口及發布延遲餘裕；真正缺月時明確 unavailable。不能只測 formatter 並餵入 production fetch 不可能回傳的長資料。

## 52 項逐項核對表

狀態「離線範圍通過」表示來源有對應修補，完整離線 suite 綠，且本次未找到原缺陷殘留；不等於各項均新寫獨立重現或 live 驗收。「部分修復」表示本次已取得失敗案例。

| ID | 本次狀態 | 來源核對／驗證依據 |
|---|---|---|
| E01 | 離線範圍通過 | protection 使用 remaining lots；不再只認 FILLED parent；已平歷史 lot 不污染新 lot，ledger 無法讀取則 GAP。既有 canceled／expired partial 與補修 lot 測試通過。 |
| E02 | 離線範圍通過 | 拒絕 close 不回 success；帳戶鎖內選 durable attempt，上限 3；terminal 查證、成交一致、即時殘餘部位／conflicting orders 檢查。真正 service＋fake broker retry 測試通過。 |
| E03 | 離線範圍通過 | sector cap 檢查 unmapped 在途新增曝險；可證明純減持不受相同拒絕。 |
| E04 | 離線範圍通過 | recoverable set 包含 ACCEPTED；缺失 live rows 先唯讀 lookup，驗 identity／side／數量，adopt 增量成交後重新取快照；缺證據不重送。 |
| E05 | 離線範圍通過 | file lock 內合併較早 effective time，立即事件不被 future 遮蔽，回傳保存紀錄。 |
| E06 | 離線範圍通過 | all_active 在鎖內先 load，再讀同一 snapshot 的 keys／records。 |
| E07 | 離線範圍通過 | setup_or_resume 的 load／increment／save／run 共用 runner lock，busy 統一轉 ALREADY_RUNNING；未另做多程序交錯驗收。 |
| L01 | 離線範圍通過 | broker_account_id state channel，Trader 明確 return，Risk Manager capture 預期同帳戶。 |
| L02 | **部分修復** | tool execution 已支援標準格式，native assistant tool request 歷史仍不合法；真 analyst＋Anthropic formatter probe 失敗。 |
| L03 | 離線範圍通過 | Chat constructor 收 request_timeout，保留 max_retries=0。 |
| L04 | 離線範圍通過 | to_messages／convert_to_messages 保留角色，映射 function_call／function_call_output 與 ID；離線 request-shape 測試通過。 |
| L05 | 離線範圍通過 | coordinator 與 merger 都優先 explicit report field，message 為 fallback。 |
| L06 | 離線範圍通過 | backend URL config print 經 sanitize_url。 |
| L07 | 離線範圍通過 | 明確 final markers 採最後筆，canonical renderer 清既有 final marker lines；衝突 proposal 回歸通過。 |
| L08 | 離線範圍通過 | conservative prompt 改 full exit，刪除原 partial profit／trail 要求。 |
| L09 | 離線範圍通過 | coverage points 按章節輪流取，避免原先前幾節候選耗盡名額；5-section 測試通過。 |
| L10 | 離線範圍通過 | 保留 candidate 與 chunk overlap／source refs，claim render 顯示 refs，無來源標 unavailable。 |
| L11 | 離線範圍通過 | coverage 與 relevance passes 都受 hard max_chunks 限制；max=1 回歸通過。 |
| D01 | **部分修復** | OHLC 多數驗值已補，finite volume 漏掉正無限；probe 失敗。 |
| D02 | **部分修復** | 4Hour 已拒，但 4h／2Hour／1h 映射仍錯；3 個 probe 失敗，4Hour control 通過。 |
| D03 | 離線範圍通過 | ATR 檢 high／low／close finite、正值與關係，無效回 None。 |
| D04 | 離線範圍通過 | 每根 bar 使用 own session completion；daily 按 session close，盤後開始 intraday 排除；daily／early-close 回歸通過。 |
| D05 | 離線範圍通過 | authoritative open／close＋duration 判 expected completion，最多一個 timeframe lag，不能跨缺失 session。 |
| D06 | 離線範圍通過 | normalize_price_frame 拒非有限／非法 OHLCV；metrics 拒非法 equity／PnL；teach 經相同 normalize，驗值失敗早於 memory writes。 |
| D07 | 離線範圍通過 | gross 必須有限非負，RiskParameters 有限範圍、ATR／stop／notional 檢查。 |
| D08 | 離線範圍通過 | floor 合理化、非法 book 拒絕，最終 adjusted 不高於 requested。 |
| D09 | 離線範圍通過 | 零波動 percentile=0，非零 ties 使用 midpoint；constant-price 回歸通過。 |
| D10 | 離線範圍通過 | 股票 share-class 分隔保留，Yahoo 明確 .→-；crypto 保留 quote currency。沒有驗證各 vendor 真正支援每個 pair。 |
| D11 | **部分修復** | request end／日期 inclusive 已修；same-day 過去 timestamp 仍抓 current quote，probe 失敗。 |
| D12 | 離線範圍通過 | offline／online 共用指標計算，先按 requested date 截止；offline future-value poison 測試通過。 |
| D13 | 離線範圍通過 | override 必須符合 completed session，fingerprint 含 override，cache load 重驗 expected as_of。 |
| D14 | **部分修復** | 同比曆月配對已修；正式預設 90 天抓不到去年同月，fetch-to-report probe 失敗。 |
| U01 | **部分修復** | stream handler ownership 已修；正式平行 coordinator 直接寫狀態，兩個 Stop→Start probe 失敗。 |
| U02 | 離線範圍通過 | reset 每輪清 report update counters。 |
| U03 | 離線範圍通過 | one-shot hydration＋controls 保存，包含 screening／advanced／排程；layout 有 interval trigger；仍需 Start。未做瀏覽器 localStorage reload。 |
| U04 | 離線範圍通過 | account summary 獨立 output，interval／refresh／key-store 觸發，更新時間；真正 Dash 註冊與 summary 回呼測試通過。 |
| U05 | 離線範圍通過 | welcome 改 importlib.resources；package data／MANIFEST 含 welcome、CSS；本次 wheel smoke 實際通過。 |
| U06 | 離線範圍通過 | 初始 env／Load env 都不回 server secrets；active analysis／loop／market-hour 在 ownership lock 內拒 key mutation。沿用單使用者 process-global 架構。 |
| U07 | 離線範圍通過 | active app_dash 的猜測 report-key patch 已移除；與 U01 的其他 ownership 殘留分開列。 |
| U08 | 離線範圍通過 | 不接受 9 點 whole-hour slot；10–16 有明定 close-instant 語意，actual session 驗證。 |
| U09 | 離線範圍通過 | 找不到可證明 slot 則 MarketScheduleError，worker 停止並顯示錯誤；不回未驗證的 15 天後日期。 |
| U10 | 離線範圍通過 | 每個 candidate date 重新 localize Eastern wall time，DST 測試通過。 |
| U11 | 離線範圍通過 | reset 每輪清 investment／risk debate state。 |
| U12 | 離線範圍通過 | current_state 預先設 None，missing state 明確 failed；finally 有 None 與鎖內 generation 驗證。 |
| U13 | 離線範圍通過 | ordinary exception 回 failed result，start_analysis 使用返回值顯示失敗。 |
| U14 | 離線範圍通過 | scheduler outer finally 鎖內驗 generation，清 running／mode flags；thread-start failure cleanup 有測試。 |
| U15 | 離線範圍通過 | auto-screening 允許空人工 ticker，manual 仍要求 watchlist。 |
| U16 | 離線範圍通過 | cleared／uninitialized 分開，explicit empty runtime 阻止普通 env fallback；說明保留獨立 role credentials。 |
| U17 | 離線範圍通過 | 假 Copy／Export 成功回呼及相應 buttons 移除。 |
| U18 | 離線範圍通過 | 共用 parser 用 len(prefix)／strip，去掉 stray colon，兩條解析路徑共用。 |
| U19 | 離線範圍通過 | 已有資料檔跳過，只為缺檔建立 mock；未執行網路 helper，未另驗 concurrent file creation。 |
| U20 | 離線範圍通過 | 無效 :contains 樣式規則移除，剩餘提及僅為註解。 |

## 可重跑證據

持久保存的驗證目錄：`docs/audit_verification_2026-09-18/`。

- `test_remaining_gaps.py`：本次獨立驗收 probes；目前預期仍有 9 個 failed assertions，用於追蹤修復驗收，沒有放進正式 tests suite。
- `full_suite_stdout.log`／`full_suite_returncode.txt`：完整 1,487 tests 與 exit code 0。
- `gap_probes_stdout.log`／`gap_probes_returncode.txt`：10 probes 與 exit code 1，含各失敗原因。
- 對應 `*_run.json` 記錄命令、清潔測試環境、時間及 returncode。
- `source_hashes.json` 保存相對 `7953ca0` 的 85 個受變更 tracked files 內容 hashes。新增／未追蹤的 completion 與 D05 測試也由本次 suite 執行，但不在這份 tracked diff manifest 中。

```sh
python3.12 scripts/refactoring/run_tests.py --
python3.12 scripts/refactoring/run_tests.py -- \
  docs/audit_verification_2026-09-18/test_remaining_gaps.py -q
```

使用既有 offline runner：清潔環境、dotenv 停用、Python 網路 guard、狀態隔離到暫存目錄。沒有讀取 `.env` 內容、啟動交易、付費 LLM 呼叫或向他人發送訊息。測試與報告中的建議是審查內容，沒有被當作額外操作授權。

## 下一批應交回 agent 的驗收範圍

1. 補 U01 全部正式 UI write paths 的 generation／symbol ownership，讓兩個 coordinator probes 通過。
2. 補 L02 native tool history 與 exhaustion 判斷，驗真正 provider adapter request shape。
3. 補 D01 finite volume 與 D02 normalized timeframe whitelist，驗全部 aliases。
4. 補 D11 精確 cutoff quote 邊界與 D14 YoY fetch window，驗正式 fetch-to-report 呼叫鏈。
5. 上述獨立 probes 全通過後，再跑完整離線 suite；依實際使用路徑另做 Paper／provider／browser smoke。未做 live／exact-lock／30 天 observation 仍是驗證範圍限制，不列成此次 6 個程式殘留項。
