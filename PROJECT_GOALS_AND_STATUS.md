# Traders：專案目標、執行計劃與目前狀況

版本：3.0

最後更新：2026-09-12

GitHub：[ihsieh31/traders](https://github.com/ihsieh31/traders)

本機：`/Users/zongen/Downloads/codex/tradingAlpaca`

上游：[huygiatrng/AlpacaTradingAgent](https://github.com/huygiatrng/AlpacaTradingAgent) @ `8d9d770da9ecc108d70fd8a97caae032c53caad0`

## 1. 唯一目標

> **保留 AlpacaTradingAgent 的研究與策略系統，只重做最後「TradeIntent → Alpaca Paper → broker 真實狀態」這一段。**

Phase A 完成後，系統必須能在單機、單 Alpaca Paper 帳戶上長時間自動執行。程式遇到重啟、重複 callback、部分成交、broker timeout、資料過期或本機與 broker 狀態不一致時，必須停止新增風險，不能猜測或盲目重送。

Phase D（30 日無人值守 Paper 觀察，`long-run`）是這個目標的驗證載具：單一 CLI 指令編排 30 個日曆天的排程、每日 round、crash/resume、hard-stop 與最終報告；它不新增任何 broker 路徑，所有下單仍走唯一 `ExecutionService` 入口。

## 2. 現階段範圍

### Completed：P1／Phase A — 安全可靠的 Paper execution

Phase A 已完成以下原計劃中真正必要的 P0＋P1 範圍；P2（Phase B）與 P3（Phase C）已分別於 2026-09-05 fresh acceptance **Accepted**（見下方各節）；P4（Phase D）30 日無人值守觀察已實作並完成 30 日測試前最終修復驗收（見 Phase D 節）：

1. Paper-only hard lock。
2. Strict `TradeIntent`，交易邊界 fail closed。
3. 兩層 idempotency：analysis decision/intent，以及 broker `client_order_id`。
4. SQLite execution ledger，以及先 commit、後送單的 durable outbox。
5. Order state machine、partial fill 與 `UNKNOWN` recovery。
6. 固定的 broker GET/POST timeout 與 retry policy。
7. BrokerSnapshot 作為 account、position、order、fill、cash 的唯一權威來源。
8. Startup、送單後及週期性 reconciliation。
9. Account、position、order、quote freshness gate。
10. Account-scoped single execution lock。

Phase A 完成且取得真實 Alpaca Paper E2E 證據後，才可開始長期 Paper run。

### Accepted：P2／Phase B — 策略與資料品質（2026-09-05 fresh re-acceptance）

原範圍全部保留：SEC filing／company IR primary sources、split／ticker change／delisting／non-tradable quarantine、sector exposure constraints，以及驗證調整既有 correlation、regime、memory/reflection（不重寫）。

加入 Analysis／Decision 兩角色 provider/model/endpoint 分離、LLM最多3次retry（共4次request）與耗盡停止、Trader與Risk Manager的fresh broker持股context，以及沿用既有symbol concentration cap的headroom裁切。仍用人工watchlist；observation 不阻擋離線驗收。

實作要點（2026-09-05，離線mock，無外部call）：
- B1：`tradingagents/llm_clients/roles.py` 六個role key解析（legacy保留、跨provider缺model為config error、role-specific key env、UI/CLI設定與顯示）。
- B2：`tradingagents/llm_clients/retry.py` 單一retry owner（SDK層固定0；openai/anthropic/google/azure四family transport計數證明4次上限、401立即停）；ProviderFailure向上傳播、parallel coordinators取消未開始工作、run log `stopped`、scheduler停止自動dispatch。
- B3：`tradingagents/execution/context.py` 共用strict持股context（Trader取得、Decision重取並驗證同一account；unknown/NaN/timeout一律stop不當flat）；`tradingagents/risk/exposure.py` symbol/sector/gross/cash headroom裁切含outstanding orders；flip只裁增加部分；recovery resubmit重新計算cap。
- B4：`tradingagents/dataflows/sec_ir.py`（官方CIK映射、submissions API、per-type freshness、bounded transport）接入fundamentals toolkit；`tradingagents/risk/corporate_actions.py` 持久quarantine（restart保留、operator+CLEAN才能解除、無TTL）；sector cap在提供sector_mapping後啟用，unknown拒增。
- B5：correlation/vol sizing hook（`adjust_new_position_notional`）接入auto-trade路徑（原先無caller）；regime scaling、memory/reflection既有測試與caller保留；Kelly維持關閉。

驗收結論（2026-09-05 fresh read-only 獨立驗收，remediation 後重跑）：B01–B24 全數 Pass；完整離線 suite `462 passed, 170 subtests passed`、`compileall` 與 `git diff --check` 綠；real-graph 與 execution 公開入口共 12 項 PoC 全過（記錄 LLM 請求數、下游節點數、intent rows、broker mutation 計數）；F1–F4 remediation 逐項複驗通過；外部 call=0；驗收前後 source diff hash 一致。驗收記錄兩項非阻擋 Minor（roles 模式 `llm_request_timeout_seconds` 未套用、改由 provider SDK 預設 per-request timeout 承擔——已於 env.sample／ARCHITECTURE 註明；`llm_retry_backoff_max_seconds` 為死設定 key）與一項行為觀察（flip 增加腿被 Phase A session guard 以 pre-close 持倉值保守拒絕，fail-closed、零超額風險），均不違反矩陣。

### Accepted：P3／Phase C — 自動選股（2026-09-05 fresh re-acceptance，含 M1 修復）

2026-09-05 fresh read-only 獨立驗收（F1 remediation 後全矩陣重跑）判定 **Accepted**：C01–C23 全數 Pass、對抗 PoC 與全 suite 綠、無 public bypass、驗收前後 diff/未追蹤檔 hash 一致。P2 Accepted 前置成立（同日 fresh re-acceptance）。

驗收證據要點：
- F1 重驗：`llm.py` ranks 1..N 連續檢查生效——11 種 LLM 輸出反例（19/21 筆、越界 symbol、重複 rank/symbol、rank-gap、extra field、NaN score、空/超長 reason、小寫 symbol）全部於 scan 階段 `SCREENING_INVALID_OUTPUT` 停止（每例恰 1 transport request、零 repair、零下游、零 cache 寫入）；retry 語義（transient×3+成功=4 requests、4 次無第 5 次、401 一次即停）經真實 `RetryingLLM` strict 驗證棧證明。
- 公式獨立手算 PoC：4-symbol fixture 全因子與 score 以 `statistics` 模組獨立重算（`rel_tol=1e-9`）全吻合；eligibility 臨界（4.99/5.00、ADV 19,999,999/20,000,000、60/61 bars、volume 0、NaN/Inf、未收盤 bar、缺日、DST/假日窗）全過；平手 tie-break、shuffle 不變性、n=1→p=0.5、Top40/不足候選全過。
- scheduler 真實入口 PoC：23-symbol 聯集（20+5 持股、2 重疊）、同日 cache 重用（第二輪 0 transport calls、持股每輪重取）、cache 五種失效（隔日/config/損毀/未來時間戳/篡改）自動重掃、人工 refresh 耗盡先刪 cache 不回退、holdings GET 失敗整輪停止、非交易日 held-review、雙執行者單次 scan（flock）、C16 headroom（第一股成交後同 symbol clip 至剩餘 headroom $5k、第三股重算 cash_limit 含在途單）、C20 outage 經真實 WebUI helper（恰 4 requests、helper 回 None、`stop_loop`+`stop_market_hour`+佇列清空）。
- C15 gate：內嵌於唯一 `ExecutionService.execute`（account lock 內）；偽造 cache、outsider BUY、holdings-only BUY 全部 `entry_gate_blocked` 零 broker calls；holdings-only SELL 走 verified Phase A 路徑。
- 指令結果：全 suite `529 passed, 172 subtests`、compileall/git diff --check 綠、外部 LLM/broker/webhook call=0（程序揭露：驗收初期一個場景誤用真實 helper 未注入 fakes，對 Alpaca **paper** 帳戶發出一次唯讀 GET positions；即時揭露後所有場景改以注入 fakes 重跑，判定證據全部來自注入後的乾淨執行）。`ponytail` skill 本環境不存在，以 C23 條款直接靜態/接線實測替代。
- 驗收記錄兩項非阻擋 Minor：M1（cache 無完整性密封，top40-內 member swap 篡改可通過重驗）與 M2（`ScreeningDeps.llm_factory` 死欄位）。

M1 修復（驗收後同日，最小修復）：`selection_store.py` 加自雜湊 integrity seal（`save()` 蓋 SHA-256 章、`load_valid()` 於其他檢查前驗章；docstring 明示為 tamper evidence 非 provenance）、`SCHEMA_VERSION` 1→2（舊格式 cache 自動失效重掃一次）、`pipeline.py` 引用 `SCHEMA_VERSION` 單一來源。新增 5 項永久測試含原攻擊路徑（top40-member swap）必須被拒。修復後：focused `71 passed`、全 suite `533 passed, 172 subtests`、compileall/git diff --check 綠；PoC 重放確認攻擊路徑已封、gate fail-closed、正常同日 reuse 不受影響。M2 留待一般維修。

R1–R4 生產整合 remediation（2026-09-05，同日 fresh read-only 獨立驗收判定 **Accepted**）：R1–R4 全數 Pass、無等效 bypass、全 suite `562 passed, 172 subtests`、compileall/git diff --check 綠、外部 call=0、驗收前後 source 一致。
- R1：`screening/universe.py` 新增 `enum_value`（`getattr(value,"value",value)` lower），universe 與 holdings `get_asset()` 路徑共用；真實 `AssetStatus.ACTIVE`/`AssetClass.US_EQUITY` 接受、`INACTIVE`/錯 class/`tradable=False` 排除、純字串回相容。
- R2：`execution/service.py:_resubmit_recovered` 在 POST 前重跑當日 `check_entry_allowed`（交易日＋當日 validated Top20），阻擋轉 `CANCELED`（durable、不重試）；broker 已有單直接 adopt 不 POST；verified reducing exit 與 crypto 走 Phase A 原路徑；`client_order_id` 不變、無重試迴圈。
- R3：`dataflows/market_calendar.py` 新增 Alpaca `TradingClient.get_calendar(GetCalendarRequest)` 權威 adapter（`*_auth`＋bounded cache），`sessions.py`/`metrics.py`/`pipeline.py`/`gate.py`/`selection_store.py`/WebUI 全切權威路徑；2024–2027 靜態表僅留 legacy/test fixture；early close 按實際 close、2028+、DST、61-session、缺 final bar 不回退 `as_of`、calendar 不可用/incomplete 一律 fail-closed。
- R4：`screening/metrics.py:fetch_daily_bars_batch` 固定 `DataFeed.SIP`、無 IEX fallback（失敗 raise `SIP consolidated bars unavailable (feed=sip)`）；`selection_store.py:SCHEMA_VERSION` 2→3＋`data_feed=sip` 綁入 payload/fingerprint，舊 IEX cache 永不復用；公式不變（ADV20/volume_ratio 獨立手算吻合）。
- 測試：新增 `tests/test_phase_c_remediation.py` 29 項（R1 真 enum、R2 P1–P5＋loop、R3 P1–P9＋`GetCalendarRequest`、R4 P1–P6），另更新 `test_phase_c_screening.py`/`test_market_hours.py` 走權威路徑。
- 驗收記錄一項非阻擋觀察：WebUI `is_market_open` 在 close 缺失的畸形列上回退 `16:00`（正常 early close 與完全不可用皆正確 fail-closed；不影響 execution gate 權威路徑）。

未覆蓋限制：真實Alpaca universe/bars資料品質、真實Screening vendor輸出品質、與長期Paper observation皆未驗證；離線驗收僅證明mock鏈路與fail-closed語義。**Accepted** 僅代表離線P3功能；真實資料與小額Paper observation另經授權執行，不自行啟動無人值守Paper run。

### 歷史紀錄：P3／Phase C — F1 remediation 與首次驗收（2026-09-05）

首次獨立驗收判定 **Not Accepted — code/test defect**：C01–C23 中 22 項 Pass，唯 C09 Fail（F1）。**F1**：`tradingagents/screening/llm.py` 的 Screening 輸出契約只驗 rank 唯一、筆數與 membership，未驗 1..20 連續；rank `[1..19, 25]` 的輸出在 scan 階段被接受並存入 cache，當輪照常分析（浪費一輪全量 LLM 分析）。防禦縱深有效：`selection_store.load_valid` 的 `rank != index` 檢查拒絕該 payload，entry gate 對當日所有新 entry fail-closed，零未授權 broker 暴露。

F1 remediation（同日，最小修復）：`llm.py` `invoke_screening_structured` 於筆數檢查後新增 ranks == 1..N 唯一連續檢查（fail `SCREENING_INVALID_OUTPUT`，零 repair、零下游）；補 3 項測試（rank-gap 反例一次請求即停、合法 rank 排列仍接受、store 級 tampered-rank-gap payload 拒絕）；另修正 `ScreeningOutput` docstring 過時引用。修復後：focused Phase C `67 passed`、完整離線 suite `529 passed, 172 subtests passed`、`compileall` 與 `git diff --check` 綠、外部 call=0。閉環 PoC（PoC-5 v2，fake transport 經真實 `run_screening_invocation` 驗證棧）：rank-gap 回合於 scan 階段停止（`SCREENING_INVALID_OUTPUT`、恰 1 次請求、無 cache 寫入、entry gate fail-closed）；合法 rank 排列仍接受且 cache 可重驗。

### Implemented（歷史紀錄，已由 Accepted 節取代）：P3／Phase C — 自動選股（2026-09-05）

C1–C5 全部實作完成（離線mock，無外部call；離線suite `526 passed, 172 subtests passed`、`compileall` 與 `git diff --check` 綠）：

- **C1**：`tradingagents/screening/` 新套件。`llm.py` 第三固定角色 `screening_provider/model/backend_url`（啟用時必填、不繼承Analysis、`SCREENING_<PROVIDER>_API_KEY` env、停用時不建client；P2 retry owner 重用、`ProviderFailure` 一路stop）。`universe.py` 全量ACTIVE US_EQUITY（消費分頁/迭代器、只留tradable、失敗即 `UNIVERSE_ERROR` 不用fallback名單）。
- **C2**：`dataflows/market_calendar.py` 共用假日表（webui market_hours 改引用）；`screening/sessions.py` as_of＝最近已完成session（pytz US/Eastern、DST、16:00收盤、假日）；`metrics.py` bars驗證（61根完整、丟棄未收盤bar、缺交易日/重複/NaN/Inf/負量即排除、單一明示adjustment policy、P2 quarantine排除）＋精確門檻（≥$5、adv20≥$20M，臨界相等合格）＋r5/r20/r60/adv20/vol20(ddof=1,sqrt252)/volume_ratio/trend＋ascending average-rank percentile公式（n=1→0.5）＋score降序、symbol tie-break取Top40；<20候選即 `INSUFFICIENT_CANDIDATES`。
- **C3**：`prompt.py`＋`prompts/templates/screening/screening_selection.md` compact table（symbol/price/adv20/r5/r20/r60/vol20/volume_ratio/trend/score/sector，附單位；無持倉/cash/新聞；禁BUY/SELL/股數/權重；reason限table因子）。`llm.py` strict schema（extra=forbid、恰20筆、rank 1..20唯一連續、symbol∈input、finite 0..100、reason≤300字）；成功但不合法→ `SCREENING_INVALID_OUTPUT`（零repair請求、零下游）；sector diversity全員有sector才套用（capacity `sum(min(count,5))<20`→ `INSUFFICIENT_SECTOR_CAPACITY`；缺漏明示 `sector_diversity_applied=false`；P2 execution sector cap不受影響）。
- **C4**：`selection_store.py` 小檔案cache（atomic replace、stdlib flock防雙執行者、schema/日期/config fingerprint/角色/未來時間戳/成員資格重驗；隔日/損毀/config變更無效；重啟可重用）。`pipeline.py` 每交易日一次scan＋一次Screening logical invocation，之後輪次用cache；持倉每輪重取（失敗整輪 `HOLDINGS_UNAVAILABLE` 停止、不當空集合）；`Top20 ∪ holdings` 去重（Top20按rank、額外持股按symbol）；非交易日held-review only（不scan、不entry）；人工refresh先失效cache再scan，失敗不回退。`gate.py` entry gate接在 `ExecutionService.execute` 內（recovery之後、opening legs）——auto模式只有當日validated Top20可新增曝險，holdings-only只能HOLD/SELL/verified exit；直接caller與resume不能繞過；HOLD與verified reducing exit不受影響；手動watchlist模式完全不走gate。
- **C5**：WebUI（config panel screening開關/三欄位/refresh按鈕/狀態面板；scheduler三種模式接 `prepare_screening_round`，screening stop即 `_halt_scheduling_for_screening_stop` 清佇列停排程）；CLI（Step 5c screening設定、run_analysis改為對deep set逐symbol跑既有流程、Top20表格與理由/stopped原因輸出）；env.sample註記（role config走UI/CLI，env僅 `SCREENING_<PROVIDER>_API_KEY`）。
- **測試**：`tests/test_phase_c_screening.py` 64項——三角色獨立解析與key env、universe分頁/過濾/無fallback、as_of盤中/收盤後/週末/假日/DST跨接、61/60 bars、4.99/5.00、$19,999,999/$20,000,000、未收盤bar丟棄、stale/重複/缺日/NaN/Inf/負量、公式獨立重算（statistics模組）、平手平均rank與shuffle不變性、n=1→score 50、Top40、不足候選、retry語義（transient×3→4次成功、4次無第5次、401一次、invalid output不repair）、sector容量/違規/降級、聯集順序去重、持倉失敗/隔離/非tradable blocked review、非交易日、cache重用/隔日/config變更/損毀/未來時間戳/篡改成員、refresh失敗不回退、雙執行者單次scan、entry gate（手動模式/無selection/非交易日/非Top20/成員/crypto）與真實ExecutionService整合（外部持股BUY零broker call、Top20成員可下單、holdings SELL不受gate）、webui scheduler stop傳播。

未覆蓋限制：真實Alpaca universe/bars資料品質、真實Screening vendor輸出品質、與長期Paper observation皆未驗證；離線驗收僅證明mock鏈路與fail-closed語義。（歷史紀錄：本節當時狀態為 Implemented — pending independent acceptance，已由上方 Accepted 節取代。）

### Planned：P3／Phase C — 自動選股（歷史規劃，已由上節實作取代）

```text
ACTIVE tradable US equities → 完整daily bars與eligibility
→ deterministic Top40 → Screening Provider structured Top20
→ Top20 UNION fresh current_positions
→ Analysis Provider → Decision Provider → 既有Phase A execution
```

Screening／Analysis／Decision各自可設定provider、model、endpoint與credential。Top20只定義新機會；額外持股用於HOLD／verified reducing exit。每天一份有效selection，人工refresh失敗即停止，不能回退舊名單。實作與验收細節見第11節與四份提示詞。

### Current：P4／Phase D — 30 日無人值守 Paper 觀察（2026-09-06 實作，歷經多輪審查修復；30 日測試前最終修復已於 2026-09-12 獨立驗收 **Accepted**）

單一指令 `python -m cli.main long-run` 編排整個 30 日觀察（`tradingagents/long_run.py`，約 3,000 行，單一模組擁有完整生命週期）：

- **設定與授權**：非機密設定存 `~/.tradingagents/long_run/config.json`（祕密僅寫入當前目錄 `.env`，config 寫入前拒絕任何疑似祕密內容）；互動精靈詢問 Analysis/Decision/Screening 三角色 provider/model/endpoint、選配 Analysis fallback、每日 notional、每日執行時刻（09:30–16:00 ET）、分析師、深度與語言。唯讀 preflight（config schema、無人值守安全 gate、每條 role 路徑一次 LLM transport probe（至多 4 條）、Alpaca 唯讀帳戶/持倉/權威日曆證明）之後，需一次明確的 Paper-test 授權；**授權前零 recovery mutation**，授權後、建立觀察前才執行唯一一次 post-authorization recovery（要求 `CLEAN`）。
- **執行模式**：強制 `auto_screening_enabled=True`、`safety_enabled=True`、paper-only（apply 後重驗，違反即 `SAFETY_DISABLED`/`CONFIG_APPLY_FAILED`）；本次 runtime 在任何 broker mutation 前安裝為全域執行設定（R01）；`allow_shorts` 為逐觀察 opt-in（false = investment BUY/HOLD/SELL，true = trading LONG/NEUTRAL/SHORT；SHORT 另受 execution 層 deterministic guard——crypto 一律拒絕）。
- **每日 round**（`run_daily_round`）：journal gate（COMPLETED 冪等；MISSED/STOPPED 拒絕重跑 `SESSION_SETTLED`；損毀即 `STATE_CORRUPT`）→ 停止/到期 precheck（R02 layer 1，recovery 的每個 resubmit POST 前再查一次 layer 2）→ `startup_recover(can_submit=...)` → `enforce_exit_deadlines` → pre/post-round sanitized 帳戶快照（NaN/缺失即 `SNAPSHOT_UNAVAILABLE`，絕不 coercion 成 0/空持倉）→ 每日 LLM 預算 gate → Phase C screening（停止即 `SCREENING_STOPPED`；執行-only resume 不重入 screening，F19）→ 逐 symbol 序列分析（crash-safe ANALYZING 標記、run-log 意圖回收；ProviderFailure 即 `PROVIDER_FAILURE`）→ 逐 symbol 經共用 auto-trade 進 `ExecutionService`（開倉必須 broker-side stop-loss、exposure cap 裁切含在途單、Top20 entry gate、最後 POST 前重驗市場時鐘/快照/報價，R05）→ 模糊結果 `EXECUTION_AMBIGUOUS`、帳戶暫停 `ACCOUNT_PAUSED`、kill switch `KILL_SWITCH` 皆整體停止。
- **排程器**：sweep 把 process 離線期間錯過的 session 結案 `MISSED_PROCESS_DOWN`（絕不以過期分析/補單回填）；收盤證明未開始的 session 結案 MISSED、絕不逾期補跑；**F-03 bounded retry**：calendar/scheduler 暫時性例外至多重試 3 次（間隔 5.0 秒，經 injected sleep），`LongRunStop` 永不重試，耗盡後 fail-closed `CALENDAR_UNAVAILABLE`；**F-04 fence**：round 內普通例外不再裸逃，整個觀察 finalize 為 `STOPPED/UNEXPECTED_ROUND_ERROR` 並留下 journal 證據（KeyboardInterrupt/SystemExit 仍向外傳播）。
- **狀態機**：SIGTERM/Ctrl-C → `INTERRUPTED`，重跑同一指令即恢復原窗口（`runner.lock` 單一執行者；`active.json` 損毀即 `ACTIVE_STATE_CORRUPT` 拒絕建立新觀察）；窗口自然到期 → `COMPLETED`（未跑 session 結案 MISSED）；硬停 → `STOPPED`（terminal，重跑=全新 30 日窗口）。日誌：`runs/<run_id>/{manifest.json, events.jsonl, account_snapshots.jsonl, rounds/<date>.json, daily_reports/, final_report.md+json}`。
- **最終報告**（確定性 Markdown+JSON，僅由 persisted 證據彙編）：coverage（完成/MISSED/STOPPED/重啟數）、帳戶結果（equity 序列；期末值只取觀察結束當下的 fresh `phase=final` 快照，取不到就明示 unavailable，絕不拿 stale 值充數）、決策與訊號統計、screening 掃描/換股、safety 事件、execution 統計、LLM 成本（按觀察 run-id 範圍歸戶，F13）、reliability（round 時長、schedule adjustments）。報告明列 `return_kind=unadjusted_account_equity_change` 與四條限制（未調整入出金、可能含既有部位、非純策略歸因、非淨獲利證明）——**operational observation，不是 profitability proof**。

**審查與修復紀錄**（每輪皆附專屬 regression 測試檔）：

1. 2026-09-06 實作（`feb011e`）＋ preflight 機密邊界/per-role probe/fallback 持久化修復（`a6a60d0`）。
2. 2026-09-08 全量審查（`docs/tradingAlpaca-full-review-2026-09-08.md`，19 項）→ Phase 1+2 remediation（`7c5d4d5`，獨立驗收通過）＋ 最終標靶修復 F15/F19/NEW-R1（`0804814`）＋ CI hermetic 與 LLM token 成本歸戶（`1e79e19`/`617ba3b`）。
3. 2026-09-09 paper-production remediation（`64b84aa`：損毀狀態 fail-closed、無人值守安全 gate、bounded HTTP、`requirements.lock` 可重現依賴）＋ long-run state 缺口（`266d2fc`）。
4. 2026-09-10 LLM 強化：OpenCode Zen/Agnes failover/Gemini embedding（`b76c57d`，本地 .env 配置非程式預設）、tool_choice 降級（`7dfb20e`）、failover 擴及 Decision+Screening（`6c3102a`）、Cloudflare 5xx 歸類暫時性（`735d019`）；**short exposure opt-in**（`3a34e93`）。
5. 2026-09-11 無人值守安全修復 F01–F10（`3c8d5b9`/`9dbc338`/`a8c5371`，56 項回歸）。
6. 2026-09-11 paper-readiness 審查（`docs/paper_readiness_review_2026-09-11.md`，16 項 R01–R16，結論「尚不可進入無人值守 Paper」）→ Plan-A（`bc06dac`：R03/R04/R05/R06/R07/R09/R14 broker/execution 安全）＋ Plan-B（`050b820`：R01/R02/R08/R10–R13/R15/R16 runtime/scheduler/state/reporting）＋ 最終 blockers（`00496c8`：R05 clock TOCTOU、R01 設定先於 recovery、R02 no-POST 狀態語意）＋ R01 WebUI runtime 完整性（`486aa74`）。
7. 2026-09-12 **30 日測試前最小修復 F-01/F-03/F-04**（`48f2419`）——fresh read-only 獨立驗收 **Accepted**：F-03 bounded scheduler retry（3 attempts/5.0s/injected sleep/LongRunStop 不重試/耗盡 `CALENDAR_UNAVAILABLE`）、F-04 ordinary-exception fence（`UNEXPECTED_ROUND_ERROR` finalize、journal STOPPED 證據、BaseException 不捕）、F-01 explicit gated baseline rebase（`Reconciler.rebase_baseline`→`ExecutionStore.rebase_account_state`：非空 reason、fresh snapshot、既有 account state、先 normal reconcile、reasons 恰為 broker/local position mismatch、無未解決本地單、無 live broker 單、rebase 後驗證 CLEAN；任何失敗 baseline 不動）。驗收以驗收者自寫 18 條獨立 reproduction（驅動真實 `run_observation_loop`/`ExecutionStore`/`Reconciler`）全數通過；全 suite `1060 passed, 0 failed, 257 subtests`；`git diff --check` 綠；無越界修改（service.py/WebUI 零 diff）、無自動 rebase call site、無新依賴/schema。**F-02（recovery whitelist / `ACCEPTED` / `PARTIAL` recovery 行為）依規格明確不在本輪處理**，為已知未修項目。

## 3. 不做的事情

- Live trading、真實資金或 paper/live 切換設定。
- PostgreSQL、DB roles、ORM、message queue 或 distributed lock。
- 完整 immutable evidence platform、security master、symbol-lineage graph。
- 20+ contracts、source authority hierarchy 或多輪 Risk rejection/resubmission。
- 重寫 LangGraph、Agent debate、LLM provider、WebUI、CLI、backtest 或 alerts。
- 強化 ChromaDB memory、Kelly sizing、correlation 或 regime intelligence。
- Phase A 期間建立 SEC/IR ingestion framework 或 automatic universe。

需要多機、多帳戶或量測證明 SQLite 不足時，才重新評估儲存與分散式架構。

## 4. 現有架構與保留項目

### 現有流程

```text
WebUI / CLI
  -> Market / Social / News / Fundamentals / Macro analysts
  -> Bull / Bear -> Research Manager
  -> Trader -> Risky / Safe / Neutral -> Risk Manager
  -> typed TradeIntent
  -> sizing + safety guardrails
  -> Alpaca order API
```

### 直接沿用

- typed `TradeIntent` / `OrderIntent`。
- broker-side bracket/OTO orders。
- ATR、單筆 notional、單股 concentration、gross exposure 限制。
- daily loss、drawdown、rejection breakers 與 kill switch。
- broker position lookup 失敗時停止交易。
- LangGraph SQLite checkpoint、decision log、Chroma memory、run audit log。
- portfolio correlation、volatility/regime scaling 的現有實作。
- backtest、chaos tests、CI、daily report、Telegram/webhook alerts。

Kelly 已存在但 `risk_sizing_enabled=False`；Phase A 保持關閉，不刪除也不強化。AI confidence 不是已校準勝率，不能把它直接當 Kelly edge。

## 5. Phase A 目標架構

```text
Risk Manager
    |
    v
strict TradeIntent -- invalid --> NO_TRADE
    |
    v
decision_id unique
    |
    v
SQLite transaction:
  persist execution_intent + order row (PENDING)
  COMMIT                         <- durable outbox boundary
    |
    v
account-scoped execution lock
    |
    v
fresh BrokerSnapshot + reconciliation == CLEAN
    |
    v
existing deterministic safety checks
    |
    v
SUBMITTING -> Alpaca Paper with deterministic client_order_id
    |
    +-- response --> ACCEPTED / PARTIAL / FILLED / terminal failure
    |
    +-- timeout --> UNKNOWN -> query client_order_id before any retry
    |
    v
post-order reconciliation -> authoritative local ledger
```

WebUI、CLI、scheduler、liquidation 與 protective orders 必須進入同一 execution service。其他模組不得直接呼叫 `submit_order()` 或 `close_position()`。

## 6. 最小 execution data model

只使用 Python stdlib `sqlite3`。`execution_intents` 的 pending row 同時就是 durable outbox，不另外建立 queue 或 outbox framework。現行 schema（`SCHEMA_VERSION=3`）為四張表：原三張表之外新增 `protective_children`（order_id ↔ parent_order_id，bracket/OTO 保護腿關聯）；DB↔account 綁定與每帳戶 `CLEAN`/`PAUSED` 狀態（含 reconciliation baseline）以保留列 `symbol='__ACCOUNT__'` 存於 `execution_intents`，不另立第二套持久化。baseline 凍結由 `save_account_state()` 保證；只有明確的人工維護 API `rebase_account_state()` 可替換（見 Phase D 節 F-01）。

### `execution_intents`

- `intent_id` primary key。
- `decision_id` unique：同一份分析結果只能建立一次 execution intent。
- `run_id`、`symbol`、`action`、`target_position`。
- `payload_json`：驗證完成的 `TradeIntent`。
- `state`、`created_at`、`updated_at`。

### `orders`

- `order_id` primary key。
- `intent_id` foreign key。
- `client_order_id` unique：同一 logical order 永遠使用相同 ID。
- `broker_order_id` nullable unique。
- `symbol`、`side`、`quantity` 或 `notional`。
- `status`、`filled_qty`、`created_at`、`updated_at`。

### `fills`

- `execution_id` primary key：重複同步同一 fill 不會重複入帳。
- `order_id`、`qty`、`price`、`filled_at`。

必要 index 與 unique constraints 直接寫在 SQLite schema；Phase A 不使用 ORM 或 migration framework。

## 7. 不可協商的規則

### 7.1 Paper-only

- 交易 client 固定 `paper=True`。
- production code、設定、sample env、CLI 與 WebUI 移除 live 選項與宣稱。
- 偵測到非 paper endpoint 或無法確認 endpoint 時 startup failed，broker call 為 0。

### 7.2 Strict execution boundary

- Analyst、Research Manager 與 Trader 仍可 free-text fallback。
- Phase A 既有行為：Risk Manager structured bind、invoke、validation、timeout、429 或 provider failure，一律阻止交易。P2 精確區分：成功回覆但 schema/bind/validation 不合法仍 `INVALID/NO_TRADE`；provider 存取失敗依 LLM policy 後整輪 `STOPPED`，不以正常 NO_TRADE 隱藏故障。兩者都不能建立可執行 intent。
- 不從 free text、Markdown 或 legacy signal 猜 `BUY/SELL/LONG/SHORT`。
- 只有 schema-valid `TradeIntent` 能建立 `execution_intent`。

### 7.3 兩層 idempotency

```text
analysis decision_id (unique)
        -> execution intent_id
        -> one or more logical orders
        -> deterministic client_order_id (unique)
```

- callback、scheduler 或 process 重複處理同一 `decision_id` 時，只回傳既有 intent。
- 重啟後沿用原 `intent_id` 與 `client_order_id`，不能重新產生。
- protective child order 也必須可關聯至原 intent 與 broker parent order。

### 7.4 Durable outbox

- 必須先在一個 SQLite transaction 內寫入 intent 與 `PENDING` order，commit 成功後 executor 才能呼叫 Alpaca。
- DB commit 失敗：零 broker call。
- commit 後、submit 前 crash：startup recovery 重新取得該 row 並安全處理。
- submit 成功、回寫 DB 前 crash：依既有 `client_order_id` 查 broker 並 adopt，不建立新 logical order。

### 7.5 Order state machine

```text
PENDING -> SUBMITTING -> ACCEPTED -> PARTIAL -> FILLED
                         |           |
                         +-----------+-> CANCELED / REJECTED / EXPIRED

SUBMITTING -- ambiguous outcome --> UNKNOWN
UNKNOWN -- broker lookup --> ACCEPTED / PARTIAL / FILLED / terminal state
```

- 非法狀態轉移拒絕並寫 audit。
- `PARTIAL` 更新 `filled_qty`，不自動建立補單。
- `UNKNOWN` 未解決前，該帳戶保持 `PAUSED` 且禁止新增風險。

### 7.6 固定 retry policy

- Idempotent GET failure：最多 3 次、短暫 exponential backoff；之後 fail closed。
- POST validation/rejection：不 retry，直接記錄 terminal state。
- POST submit timeout/connection loss：不直接 retry，先標記 `UNKNOWN`。
- `UNKNOWN` 以同一 `client_order_id` 做 bounded lookup；找到即 adopt。
- broker 明確回覆不存在後，才可用**同一個** `client_order_id` 再 submit；仍不確定則保持 `PAUSED`。

### 7.7 Broker authority 與 reconciliation

`BrokerSnapshot` 至少包含：

- `observed_at`、account ID、equity、cash、buying power。
- positions、open/recent orders、today fills、gross exposure。

執行前 sizing、safety 與送單使用同一份 snapshot。Agent memory、checkpoint、audit log 與 local ledger 都不能覆寫 broker facts。

只有 reconciliation=`CLEAN` 才能新增風險。以下全部轉為 `PAUSED`：

- position mismatch。
- unknown order 或 duplicate `client_order_id`。
- cash/equity/account unavailable。
- unresolved partial fill。
- broker timeout 尚未解決。
- snapshot 不完整、account ID 不符或過期。

Startup、每次 order action 後及 scheduler 每輪開始前都要 reconciliation。風險降低型 exit 只有在 broker position 已即時確認，且沒有矛盾 open order 時才能執行；不能根據 stale local state 猜測。

### 7.8 Freshness

Phase A 只 gate execution 必要資料：account、positions、orders、fills 與 quote。TTL 使用少量明確預設值並集中設定；缺時戳、未來時間或超過 TTL 都是 `NO_TRADE`。News/SEC/IR freshness 留到 Phase B。

### 7.9 Single execution lock

- 使用 SQLite transaction 或 stdlib file lock，以 Alpaca account ID 為 key。
- 第二個 process 取不到 lock 時立即退出 execution，不送單。
- crash 後可恢復；Phase A 不做 distributed lease。

## 8. 實作順序與完成 Gate

| Gate | 工作 | 必須通過的證據 | 狀態 |
|---|---|---|---|
| A0 基準 | clone、架構盤點、計劃、離線 suite | upstream SHA；`298 passed, 158 subtests passed` | 完成 |
| A1 封死邊界 | Paper-only、strict TradeIntent、集中 execution entry | live config 無法送單；所有 structured failure 零 broker call | Accepted |
| A2 Durable intent | 三表 SQLite、兩層 ID、commit-before-submit | duplicate callback 與三個 crash points 都不重複送單 | Accepted |
| A3 Order recovery | state machine、partial fill、UNKNOWN、固定 retry | timeout/restart/partial/terminal transition tests | Accepted |
| A4 Broker authority | BrokerSnapshot、startup/post-order reconciliation | mismatch/unavailable/duplicate/unknown 全部 `PAUSED` | Accepted |
| A5 無人值守 | freshness、single lock、periodic reconciliation | stale snapshot 與雙 process 都是零新增曝險 | Accepted |
| A6 Paper E2E | 真實 Alpaca Paper sandbox | submit、partial/cancel、timeout recovery、restart、reconcile 證據 | 完成（submit→fill→adopt→reconcile→verified close→flat 已在真實 Paper 驗證；partial/timeout branch 由 deterministic mock/chaos 證據覆蓋） |
| D0 Phase D 實作 | 30 日觀察編排（config/state/lock/preflight/round/loop/report） | 離線確定性 suite 全綠；無新 broker 路徑，全部走 `ExecutionService` | 完成（2026-09-06） |
| D1 審查與修復 | 全量審查 19 項 → Phase 1+2 remediation（獨立驗收通過）＋ F15/F19/NEW-R1；安全修復 F01–F10 | 每輪專屬 regression suite 全綠 | 完成（2026-09-08~11） |
| D2 Paper-readiness 修復 | 審查 16 項（R01–R16）→ Plan-A/Plan-B/最終 blockers 修復 | 修復後全 suite 綠；R02 no-POST 語意、R05 最後 POST 前重驗、R01 設定先於 recovery 落地 | 完成（2026-09-11~12） |
| D3 30 日測試前最終修復 | F-01/F-03/F-04（retry/fence/rebase） | fresh read-only 獨立驗收 **Accepted**（2026-09-12；18/18 獨立 reproduction、全 suite `1060 passed, 0 failed`） | **Accepted** |
| D4 30 日 Paper observation | 真實 30 日無人值守觀察（小 notional） | 操作觀察報告（帳戶權益變化），非 profitability proof | 未開始（需使用者明確授權） |

規則：一次只做一個 Gate。每個 Gate 要有 focused regression、完整離線 suite 與乾淨工作樹；mock evidence 與真實 Alpaca Paper evidence 分開報告。A6 已於 fresh acceptance 通過；長期無人值守前仍需一段穩定 Paper observation。

## 9. 提示詞與執行順序

P1＝Phase A（A.1/A.2）；P2＝Phase B；P3＝Phase C。P1四份、P2/P3四份舊提示詞均已依使用者要求刪除（P2/P3 於 2026-09-05 兩階段 Accepted 後移除），歷史內容保留於Git；A0–A6完成紀錄與本文件安全規則保留。

執行順序（歷史紀錄）：P2實作→P2獨立驗收→P3實作→P3獨立驗收；每階段實作只能回報Implemented/pending acceptance，只有新的獨立acceptance task能給Accepted。驗收不得修檔；修復後重新fresh acceptance。提示詞完成不表示階段已實作，也不授權外部provider或broker calls。

## 10. Phase A 驗收矩陣

至少覆蓋以下情境：

| 情境 | 預期結果 |
|---|---|
| `ALPACA_USE_PAPER=False` 或非 paper endpoint | startup failed；零 broker call |
| Risk Manager schema/timeout/429/provider failure | Phase A 為 `NO_TRADE`；P2 access failure 改為可辨識 `STOPPED`；均零 order/close call |
| 同一 `decision_id` 觸發兩次 | 回傳同一 intent；只產生一組 logical orders |
| DB commit 前 crash | 無 broker order |
| DB commit 後、submit 前 crash | restart 從 `PENDING` 恢復 |
| submit 後、ACK 寫 DB 前 crash | 以 `client_order_id` adopt broker order |
| POST timeout | `UNKNOWN`；lookup 前零 retry |
| partial fill | 更新同一 order；不自動補單 |
| broker/local position mismatch | `PAUSED`；禁止新增風險 |
| account/cash/equity unavailable | `PAUSED` |
| stale/invalid timestamp | `NO_TRADE` |
| 兩個 executor 同時啟動 | 只有 lock owner 可執行 |
| verified risk-reducing exit | 依明確 policy 執行並立即 reconcile |

## 11. P2／P3詳細計劃與進入條件

### P2／Phase B

A6 通過後即可實作；Paper observation 不屬於 P2／P3 實作與離線驗收的前置條件，啟用長期無人值守 Paper 自動交易前才須完成。每項提供直接功能或風控fixture證據，不宣稱提高收益。

| 工作項 | 修改範圍與做法 | 完成證據 |
|---|---|---|
| B1 固定角色 | 重用factory；Analysis全研究節點共用一model，Risk Manager獨立Decision；保留legacy quick/deep；逐角色解析key/URL/kwargs，接CLI/UI | fake role注入、legacy/cross-provider config、secret隔離 |
| B2 Retry/stop | `llm_max_retries=3`共4次HTTP；SDK單層有界retry；transient重試、permanent立即停、schema失敗NO_TRADE；所有catch與scheduler傳播stop | 四adapter transport計數、structured/tool路徑、parallel故障零下游dispatch |
| B3 持股與上限 | Trader取fresh snapshot，Decision重新取，execution自己再驗；quantity/value/entry/P&L/weight/equity/cash/buying power/gross%；沿用`max_symbol_concentration_pct`，扣持股與pending headroom | 18%+5%在20%cap僅容許2%；unknown/stale不能當flat；verified exit保留 |
| B4 資料／sector | SEC/官方IR最小接入與source/url/published/retrieved；corporate-action持久quarantine、reconcile、alert；sector cap與unknown拒增風險 | dataflow fixture、事件/restart/解除測試、sector headroom整合 |
| B5 原模組驗證 | correlation/regime/memory/reflection沿用，只修有證據缺口；Kelly仍關閉 | focused與完整離線suite、實際caller證據 |

新role只需固定設定，不建立router。沒有新role設定就保持legacy quick/deep；明確跨provider卻缺model為config error。Provider耗盡後停止本輪及自動dispatch，需明確重啟，不fallback或用舊checkpoint結果。非Risk內容fallback不能吞provider存取錯誤。Broker GET/POST policy與LLM retry獨立。

持股只直接注入Trader與Decision，不給前端Analysts/Research/Screening。沿用既有25%單股預設，不新增同義`max_single_position_pct`；sector cap規劃預設30%，均非收益建議。unknown sector/facts無法證明headroom時拒絕新增曝險；verified reducing exit仍依Phase A。保留SEC/IR缺漏及corporate-action feed覆蓋限制，不宣稱已建全市場自動事件平台。

### P3／Phase C

前提是 P2 fresh Accepted；不要求先有人工 watchlist 長期穩定運作或 Paper observation 證據。使用者已要求自動選股；本次僅完成計劃，不跳過前置直接執行。

| 工作項 | 修改範圍與做法 | 完成證據 |
|---|---|---|
| C1 Universe與第三角色 | Alpaca完整ACTIVE US_EQUITY/tradable清單、batch bars；獨立screening provider/model/URL/key，沿B2 retry | 完整分頁fixture、手動模式零Screening、三角色隔離 |
| C2 Eligibility/Top40 | close>=5、adv20>=20M、61完整bars支持60D return、最近完整session、quarantine排除；固定rank公式 | 獨立手算、threshold/tie/missing/stale/holiday tests |
| C3 Top20 | Top40 compact features一次structured rerank；exact20、unique/input-only、rank/score/reason；sector資料完整時最多5/sector | schema/sector反例零下游，不修補、不fallback |
| C4 持股/cache/scheduler | Top20聯集fresh持股；額外持股只減風險；每日selection cache、explicit refresh；任一provider失敗stop | 20+5−2=23、held-only entry拒絕、refresh失败不能沿用、雙執行者 |
| C5 端到端與文件 | 沿現有graph/dispatch/UI，無新scheduler/DB/optimizer | mock全鏈路、P1/P2回歸、完整離線suite |

確定性score固定為 `100*(0.20*p_adv20 + 0.25*p_r20 + 0.25*p_r60 + 0.15*(1-p_vol20) + 0.15*p_volume_ratio)`；percentile採ascending average rank、`(rank-1)/(n-1)`，n=1為0.5；同分按symbol。vol20為20個simple daily returns的sample std乘sqrt(252)；volume_ratio為5日/20日平均量。這些係數是本次將討論具體化的baseline，不是既有程式或已驗證策略績效。

20–39合格候選全部交Screening，少於20整輪停止；成功必須exact20。Sector資料完整且容量不足20也停止；資料不完整則明示不做selection diversity，但P2交易sector cap不放寬。每天一次scan，cache只接受本交易日/相同config與已驗證內容；人工refresh失敗使舊selection失效。日期、公式、schema、配置與每條驗收方法以對應implementation prompt為完整契約。

## 12. 目前狀況

### 已完成

- [x] 完整 clone 上游 Git history；`upstream` 保留原 repo。
- [x] 公開 GitHub repo `ihsieh31/traders`；`origin/main` 已同步。
- [x] 現有 execution、structured output、risk、safety、WebUI/CLI 路徑盤點。
- [x] Python `compileall` 通過。
- [x] Python 3.12 隔離環境：`298 passed, 158 subtests passed`（21.38 秒）；4 warnings 均來自第三方套件。
- [x] tracked secrets 與大於 50 MB 檔案檢查未發現候選項目。
- [x] 計劃重整為 Phase A/B/C；durable outbox、fail-closed matrix、兩層 idempotency 與 retry policy 已明文化。
- [x] Phase A 曾拆成四份提示詞並完成實作／驗收；2026-09-05依使用者要求刪除舊提示詞，歷史可由Git查回。
- [x] 2026-09-05完成P2/P3更新計劃與四份詳細implementation/independent acceptance prompts（文件完成，程式未開始）。
- [x] Phase A.1 實作（A1–A3，約 45%）：paper-only hard lock、strict TradeIntent、唯一 execution entry、三表 SQLite durable outbox、兩層 idempotency、order state machine 與最小 UNKNOWN lookup/adopt — Accepted（fresh read-only acceptance 通過：`321 passed, 158 subtests passed`，對抗 PoC 9/9，mock only，無外部 call）。
- [x] Phase A.2 A4/A5 實作：immutable `BrokerSnapshot`、固定 GET/POST policy、startup/post-order/periodic reconciliation、durable `CLEAN`/`PAUSED` reasons、UTC freshness gate、account-scoped OS lock 與 verified risk-reducing exit — Accepted（fresh read-only acceptance 2026-09-04）。
- [x] Phase A.2 驗收後 remediation（A4/A5 code review findings）：P2 liquidate 預設 decision_id 改為 per-call（重複平倉不再被靜默 dedup；重複/併發安全仍由 `_verified_reducing_exit` 的 broker 驗證把關）、P3 統一 broker status mapper 至 authority（刪除 service 重複實作，未知狀態維持 fail-safe ACCEPTED）、P3 `_submit_one` 外層 except 的 `broker_calls` 提前初始化並如實回報 POST 計數。補 4 項 regression tests；完整離線 suite `347 passed, 158 subtests passed`。修復內容已納入 2026-09-04 fresh acceptance 範圍。
- [x] Phase A.2 fresh read-only 驗收（2026-09-04）：驗收矩陣全數 Pass、對抗 PoC 12/12、focused A1/A2 `49 passed`、完整離線 suite `347 passed, 158 subtests passed`、`compileall` 與 `git diff --check` 全綠；source review 無 mutation bypass 或 stale authority path — **Accepted**。
- [x] Phase A.2 A6 真實 Alpaca Paper E2E（2026-09-04， disposable paper account、經明確授權）：paper endpoint 驗證（`BaseURL.TRADING_PAPER`）、durable outbox commit→submit 時序驗證、真實 submit `F` notional $10 → broker fill 0.687164671 @ 14.538 → 本地 adopt 同一 broker_order_id → reconcile `CLEAN` → verified close（sell 0.687164671 @ 14.562）→ 帳戶 flat、零 open orders、最終狀態 `CLEAN`。無 LLM/provider call、無 secret 洩漏。

### 尚未完成

- [ ] 長期 Paper observation（小 notional、人工 watchlist）尚未開始；不阻擋 P2/P3 實作與離線驗收，啟用長期無人值守 Paper 自動交易前須完成。
- [x] P2／Phase B實作（2026-09-05）：B1–B5全部實作完成，離線suite `457 passed, 170 subtests passed`、`compileall` 與 `git diff --check` 通過；**Implemented — pending independent acceptance**。
- [x] P2／Phase B獨立驗收（2026-09-05）：**Not Accepted — code/test defect**。B01–B24 中 B01/B06/B07/B24 Fail。Critical finding F1：`RetryingLLM`/`RetryingRunnable` 非 LangChain `Runnable`，五個 analyst 的 `prompt | llm.bind_tools(tools)` chain 建構即 `TypeError`（零 LLM 請求），per-analyst catch 吞成空 report 後 run 照常完成；另有 F2（env.sample 六個死 role env key）、F3（quarantine gate 記憶體快取不見他處 release）、F4（首次 reconcile 前 `account_status` 不顯示 quarantine）。完整矩陣證據見 2026-09-05 驗收報告。
- [x] P2／Phase B驗收後 remediation（2026-09-05，最小修復）：F1 `retry.py` 兩個 wrapper 改繼承 `langchain_core.runnables.Runnable`（新增 LCEL 組合與真實 market analyst 節點回歸測試）；F3 `QuarantineStore` 讀寫前 `reload()`（跨實例 release/quarantine 立即生效，回歸測試）；F4 `account_status()` 兩條 return 路徑都附 `quarantined_symbols`（回歸測試）；F2 env.sample 修正為「role 設定走 UI/CLI config，env 僅 role-specific API key」。修復後 focused Phase B suites 全綠。**Remediated — pending fresh acceptance**（B01/B06/B07/B24 需重驗）。
- [x] P2／Phase B fresh re-acceptance（2026-09-05）：**Accepted**。B01–B24 全 Pass；完整離線 suite `462 passed, 170 subtests passed`、`compileall` 與 `git diff --check` 綠；real-graph／execution 公開入口 PoC 12 項全過（LLM 請求數／下游節點數／intent rows／broker mutation 計數）、外部 call=0；F1–F4 remediation 逐項複驗通過；驗收前後 source diff hash 一致。兩項非阻擋 Minor 已記錄並處理（roles 模式 timeout 改註明由 SDK 預設承擔；`llm_retry_backoff_max_seconds` 死鍵留待一般維修）。
- [x] P3／Phase C實作（2026-09-05）：C1–C5全部實作完成，離線suite `526 passed, 172 subtests passed`、`compileall` 與 `git diff --check` 通過；**Implemented — pending independent acceptance**。
- [x] P3／Phase C獨立驗收（2026-09-05）：**Not Accepted — code/test defect**。C01–C23 中 22 項 Pass，C09 Fail（F1 rank-gap 未於 scan 階段拒絕；防禦縱深使金錢風險為零）。PoC-1～PoC-4 共 70+ 檢查；focused regression `288 passed`、全 suite `526 passed, 172 subtests`、compileall/git diff --check 綠、外部 call=0。完整矩陣證據見 2026-09-05 驗收報告。
- [x] P3／Phase C驗收後 F1 remediation（2026-09-05，最小修復）：`llm.py` 新增 ranks 1..N 連續檢查＋3 項反例/防禦縱深測試＋docstring 修正。修復後 focused `67 passed`、全 suite `529 passed, 172 subtests passed`、compileall 與 `git diff --check` 綠；PoC-5 v2（真實驗證棧）證明 rank-gap 回合 scan 階段即停、零下游、零 cache。**Remediated — pending fresh acceptance**（C09 及相關回歸重驗後方可 Accepted）。
- [x] P3／Phase C fresh re-acceptance（2026-09-05）：**Accepted**。新獨立read-only task全矩陣重驗 **C01–C23 全 Pass**（未縮減為僅 C09/C11）：PoC-1 獨立手算公式/eligibility ~60 checks、PoC-2/2b/2c scheduler+execution 真實入口對抗場景（23-symbol 聯集、11 種 LLM 輸出反例經真實 retry owner＋strict 驗證棧、refresh 耗盡先刪 cache、holdings 失敗整輪停、headroom 串行重裁、雙執行者單次 scan、gate bypass 全擋）、PoC-3（真實 WebUI helper screening outage、三角色獨立 client、手動模式零 client、Ponytail 靜態檢查）全過；focused `67 passed`、P2+A1/A2 `231 passed`、全 suite `529 passed, 172 subtests`、compileall/git diff --check 綠；F1 修復複驗通過（rank-gap 於 scan 階段即停、零下游、零 cache）。程序揭露：驗收初期一個場景誤用真實 helper 未注入 fakes，對 Alpaca paper 帳戶發出一次唯讀 GET positions（零 mutation、零訂單）；矩陣證據全部取自注入 fakes 後之乾淨重跑。
- [x] P3／Phase C 驗收後 M1 remediation（2026-09-05，最小修復）：`selection_store.py` 自雜湊 integrity seal（`save()` 蓋章、`load_valid()` 優先驗章、不符即重掃；docstring 明示 tamper evidence 非 provenance）＋`SCHEMA_VERSION` 1→2（舊格式 cache 自動失效重掃一次）＋`pipeline.py` 引用 `SCHEMA_VERSION` 單一來源＋5 項測試（top40-member swap 原攻擊路徑必須被拒、pre-seal/空 seal 拒絕、stale-seal 編輯拒絕、seal roundtrip 同日 reuse 正向）。修復後 focused `71 passed`、全 suite `533 passed, 172 subtests`、compileall 與 `git diff --check` 綠；M1 攻擊路徑重放 PoC 全過（攻擊被封、gate fail-closed、正常 reuse 零 transport call）。M2（`ScreeningDeps.llm_factory` 死欄位）與 `zero_volume_baseline` 死路徑標籤留待一般維修。
- [x] 2026-09-06：Phase C 後續——Analysis-only provider failover（共享重試預算，不回彈、永久錯誤不 failover）；**Phase D 30 日無人值守 Paper 觀察實作**（`long_run.py`、`python -m cli.main long-run`）；Phase D preflight 機密邊界/per-role probe/fallback 持久化修復；README 改寫為繁體中文。
- [x] 2026-09-07~08：DVI-1 決策完整性與 `last_equity` daily-loss 接線、誠實績效呈現；DMC-1 辯論最小一致性修復；CI 驗收測試 hermetic 化；無歸戶 LLM token 成本修正。2026-09-08 全量審查（19 項，`docs/tradingAlpaca-full-review-2026-09-08.md`）→ **Phase 1+2 remediation 獨立驗收通過**（`7c5d4d5`）＋ 最終標靶修復 F15（nested LangChain usage 少計）/F19（resume 重入 screening 繞過預算）/NEW-R1（protected SHORT close 數量符號）（`0804814`）。
- [x] 2026-09-09：paper-production remediation（`64b84aa`：corrupt-state fail-closed `ACTIVE_STATE_CORRUPT`、無人值守安全 gate `SAFETY_DISABLED`、bounded HTTP/timeout、`requirements.lock` 222 pins 可重現依賴；CI/Docker 改用 lock 安裝＋`pip check`）；long-run state/dependency lock 缺口收尾（`266d2fc`）。
- [x] 2026-09-10：provider 面強化——OpenCode Zen 主 LLM、Agnes failover、Gemini embedding（本地 .env 配置）；tool_choice 對 auto-only endpoint 降級 auto；failover 擴及 Decision 與 Screening 角色（共享 `analysis_fallback` 路由與預算）；**allow_shorts opt-in**（逐觀察設定，execution 層 deterministic SHORT guard：crypto 一律拒絕）；Cloudflare 5xx（520-527/529/530）與任意其他 5xx 歸類暫時性錯誤；deep 角色單次輸出上限 16000 tokens。
- [x] 2026-09-11：無人值守安全修復 F01–F10（`3c8d5b9`）＋ F02/F04/F06（`9dbc338`）＋ F02 殘餘 race（`a8c5371`：dispatch-time generation 捕捉、stale screening 不能停新 run），新增 56 項回歸（`tests/test_unattended_safety_regressions.py`）。同日 paper-readiness 獨立審查（基準 `a8c5371`）確認 16 項問題 R01–R16（11 P1、5 P2），結論「尚不可進入無人值守 Paper Trading」（`docs/paper_readiness_review_2026-09-11.md`）。
- [x] 2026-09-11~12：R01–R16 修復——Plan-A broker/execution 安全（R03 bracket 連動取消、R04 反向 close-only、R06 `done_for_day`、R07 5xx 模糊語意、R09 保護缺口 gap-check、R14 stale-position precondition）、Plan-B runtime/scheduler/state/reporting（R01/R02/R08/R10–R13/R15/R16）、最終 blockers（R05 最後 POST 前重驗時鐘/快照/報價、R01 runtime 先於 recovery 安裝、R02 no-POST 狀態語意）、R01 WebUI runtime 完整性（本 run 的 allow_shorts/trading_mode 先於 recovery 套用）。每輪附專屬 regression 檔（`test_execution_safety_plan_a.py`、`test_runtime_reliability_plan_b.py`、`test_final_paper_readiness_blockers.py`）。
- [x] 2026-09-12：**30 日測試前最小修復 F-01/F-03/F-04 獨立驗收 Accepted**（`48f2419`）。驗收者自寫 18 條 reproduction（驅動真實 `run_observation_loop`/`ExecutionStore`/`Reconciler`）18/18 通過：F-03 retry 恰 3 attempts/5.0s injected sleep/`LongRunStop` 不重試/耗盡 `CALENDAR_UNAVAILABLE`、F-04 普通例外 finalize `STOPPED/UNEXPECTED_ROUND_ERROR` + journal STOPPED 證據/KeyboardInterrupt 仍傳播、F-01 freeze 保留 + gated rebase 九項動態情境（mismatch-only rebase→CLEAN；PARTIAL/未解決單/live broker 單/stale snapshot/空 reason/缺 account state 全拒；baseline 不動）。邊界稽核：修改僅限 `long_run.py`/`store.py`/`authority.py`，F-02/WebUI 零觸碰、無自動 rebase、無新依賴/schema/擴充。全 suite `1060 passed, 0 failed, 257 subtests`、`git diff --check` 綠。
- [x] 2026-09-13：剩餘問題最小修復——A2/A3/A4b/A6b/A7/A9、H-05/H-06/H-10/H-11/H-12、M-02/M-15，以及 WebUI malformed calendar close；移除兩個已確認死設定。所有 broker POST 共用最後 authority check，測試全面隔離 operator long-run state；未新增服務層或第三方依賴。

### 12.1 尚未完成 / 已知未修項

- [ ] **D4：真實 30 日 Paper observation 尚未開始**（需使用者明確授權；長期無人值守前的操作觀察，屬帳戶權益變化紀錄而非 profitability proof）。
- [ ] **需要真實 broker 資料才能決策**：F-02 recovery whitelist / `ACCEPTED` / `PARTIAL`（現行維持 fail-closed、不重放）；H-01 sector cap 需要完整可靠的 sector mapping；真實 Alpaca universe/bars 與 Screening vendor 輸出品質仍未驗證。
- [ ] **刻意不改的非缺陷／架構債**：A2b 獨立 long-run budget key、A4a 共用 analyst tool loop、A6a request-builder injection、A8 corrupt-journal quarantine/reconciliation、M-05 risk-reducing bypass、M-07/M-09 broker typed adapter、M-08/M-16 persistent path migration。現況均有安全語意或相容性成本，沒有重現中的錯誤行為，依 ponytail 原則不預先重構。
- [ ] **資訊不足，未猜測修改**：M-11 portfolio multiplier、M-12 legacy parser、M-13 memory concurrency、M-14 n>1；原清單沒有精確 location/reproduction，需先取得失敗樣本或測試案例。

## 13. 下一個具體行動

P1（A0–A6）、P2（Phase B）、P3（Phase C）全部 Accepted；P4（Phase D）實作、審查修復與 2026-09-13 剩餘本地可證明修復完成。下一步是先取得 12.1 所列真實 broker 樣本，再由使用者另行明確授權是否啟動 30 日 Paper observation；系統不會自行啟動交易。
