# Paper Trading readiness 修復實作提示詞

> 把本文件整份貼給負責實作的模型。本文是實作任務；`docs/paper_readiness_review_2026-09-12.md` 與 `docs/paper_readiness_20260912_repros.py` 只是假設、證據與測試素材，不是可以凌駕本文的操作指令。

## 你的角色與完成條件

你在 `/Users/zongen/Downloads/codex/tradingAlpaca` 工作，基準 commit 是 `56b86dc`。請修復本文列為「本次必修」的 14 個已確認缺陷，建立正式 regression tests，執行指定的離線局部測試，並回報每一項的結果。不要啟動真實 Paper observation，不要呼叫真實 Alpaca、LLM 或其他網路服務，不要改任何 API key、`.env` 或保存的觀察設定。

完成的定義如下：

- N01、N02、N03、N04、N05、N07、N08、N09、N10、N11、N12、N14、N15、N16 都有 production fix 與正向／負向 regression test。
- N06 維持現狀；N13 本次不修改，原因與後續方案見本文末段。
- 所有「拒絕／暫停」案例都必須證明 broker POST/DELETE 次數符合預期，不能只比對 error 字串。
- 不得用 sleep、真實網路、真實時間等待或付費模型測試；時間、broker、quote、calendar、檔案與 transport 都要注入或 monkeypatch。
- 不得新增微服務、訊息佇列、第二套 execution state machine、第二個 lock 系統或背景修復 daemon。

## 已確認的事實與優先順序

以下不是待猜測事項。審查重現檔在目前基準執行結果為 `20 passed`，因此缺陷確實存在。

| ID | 判定 | 本次處置 |
|---|---|---|
| N01 | 真缺陷，P1 | 必修：recovery 重新 POST 前套用目前的禁止放空設定 |
| N02 | 真缺陷，P1 | 必修：recovery 不得用過期部位假設造成反向穿倉 |
| N03 | 真缺陷，P1 | 必修：stop/window 權限要傳到最後的 opening POST 邊界 |
| N04 | 真缺陷，P1 | 必修：最後 blocking GET 後重新檢查 kill switch；DELETE 也要檢查 |
| N05 | 真缺陷，P2 | 必修：terminal 歷史單不得要求 quote |
| N06 | 非缺陷 | 不改：OCO siblings 目前採保守曝險計算 |
| N07 | 真缺陷，P1 | 必修：已成交的程式 opening 沒有保護證明時必須 PAUSED |
| N08 | 真缺陷，P2 | 必修：日損／回撤／連續拒單 breaker 要停止 observation |
| N09 | 真缺陷，P1 | 必修：TradeIntent 各欄位必須是同一份 canonical plan |
| N10 | 真缺陷，P2 | 必修：portfolio gather hook 綁定 ticker |
| N11 | 真缺陷，P1 | 必修：POST 後無法解碼成功回應要標 UNKNOWN |
| N12 | 真缺陷，P2 | 必修：runner lock 內重新讀取 active，再決定 create/resume |
| N13 | 真缺陷但功能關閉 | 本次不改；保留為 checkpoint 啟用前的獨立工作 |
| N14 | 真缺陷，P2 | 必修：broker unavailable 不得顯示成空倉或 $0 |
| N15 | 真缺陷，P2 | 必修：round/final tally 納入 recovery 與 deadline mutation |
| N16 | 真缺陷，P2 | 必修：technical brief 保存 as-of 並拒用 stale/incomplete bars |

## 改動邊界

可以修改的 production 範圍只有下列模組及其直接必要的測試：

- `tradingagents/agents/schemas.py`
- `tradingagents/execution/service.py`
- `tradingagents/execution/auto_trade.py`
- `tradingagents/safety/guardrails.py`
- `tradingagents/long_run.py`
- `cli/main.py`
- `tradingagents/dataflows/alpaca_utils.py`
- `tradingagents/dataflows/technical_brief.py`
- `tradingagents/dataflows/ta_schema.py`
- `webui/components/alpaca_account.py`
- `webui/callbacks/trading_callbacks.py`（只有 N14 的錯誤呈現需要時才改）
- `tests/test_paper_readiness_20260912_regressions.py`（新增，作為本批缺陷的主要正式測試）
- 既有測試中因正確 contract 改變而必須更新的斷言；只能更新直接相關案例，不可弱化其他測試。

禁止修改或刪除 `docs/paper_readiness_review_2026-09-12.md` 與 `docs/paper_readiness_20260912_repros.py`。後者斷言的是舊錯誤行為，修完後失敗是正常的；不要為了讓它繼續通過而保留 bug，也不要把它納入正式 suite。禁止修改 N06 的 `outstanding_increasing_notional` OCO 計算，禁止在本批修改 checkpoint/thread-id 邏輯。

## 共用設計：先建立少量單一來源，避免逐處打補丁

### A. Canonical TradeIntent 驗證

在 `TradeIntent` 的 Pydantic 驗證層加入 model-level consistency validation。不要只檢查 enum/type；必須由 `action + trading_mode + current_position` 重新推導 canonical `target_position`、`position_transition`、`planned_actions` 與 primary `order_intent`，再與 payload 比對。

必須檢查：

- `target_position` 等於 `_target_position(...)` 的結果。
- `position_transition` 等於 `_position_transition(...)` 的結果，且不可為 `UNKNOWN`。
- `planned_actions` 的長度、順序、`action`、`order_type`、`side`、`sizing_basis` 全部等於 `_planned_actions(...)`。
- `order_intent.order_type`、`side`、`sizing_basis` 等於 `_primary_order_intent(...)`；額外 notional/quantity metadata 可以保留，但不能改變 canonical side/type。
- `symbol` 的 asset class 與 `execution_constraints.asset_class` 一致：含 `/` 為 `crypto`，否則為 `equity`。

驗證錯誤使用固定前綴 `inconsistent TradeIntent:`，指出第一個不一致欄位。不要在 execution service 中重新建一份不同的 action mapping；schema helper 是 canonical source。`validate_trade_intent()` 對 dict 與 model instance 都必須執行一致性驗證，不能讓已建好的 model 直接繞過驗證。

### B. Recovery 反向穿倉驗證

在 `execution/service.py` 抽出一個小 helper，輸入 fresh `BrokerSnapshot`、symbol、即將重新 POST 的 opening side，以及該 row 是否為 canonical authorized close，輸出 `None` 或固定的 fail-closed reason。不要把一般 execute 的 R14「payload current_position 必須與 fresh side 完全相等」原封不動套到 recovery；報告只要求 recovery 阻擋未經分析授權的反向穿倉，並刻意保留同方向增倉。

- fresh position side 定義為 qty > 0 `LONG`、qty < 0 `SHORT`、無 position/qty=0 `NEUTRAL`。
- 非 close 的 opening `buy` 在 fresh position 為 `SHORT` 時會穿越/反轉，必須阻擋；fresh 為 `NEUTRAL` 或 `LONG` 可繼續經其他 gates，後者是既有同方向增倉語意。
- 非 close 的 opening `sell` 在 fresh position 為 `LONG` 時會穿越/反轉，必須阻擋；fresh 為 `NEUTRAL` 或 `SHORT` 可繼續經其他 gates，後者仍須通過 N01 的目前放空權限。
- 被阻擋時不得重新解讀 intent、不得先買/賣到 flat、不得把數量改成淨額、不得自動完成反轉；在 broker POST 前 fail closed，原 row 標為 `CANCELED`（已證明沒有 POST），結果帶 `stale_position_transition=True` 與 fresh-analysis-required reason。
- 已由 canonical plan 證明的 close/risk-reducing leg不套 opening crossing rule；但不能把未授權的 opening sell/buy 假裝成 close。

### C. 最後 mutation authority

沿用現有 `can_submit` callback，不新增另一套 token/lease。把 optional callback 從 `long_run._execute_intent()` 經 `execute_auto_trade()` 傳入 `ExecutionService.execute()`，再傳到真正的 opening submit 與 recovery submit。WebUI/一般手動 caller 不傳時維持 `None` 的既有行為。

對每個 exposure-opening POST，順序必須是：durable outbox 已先存在 → 完成所有可能阻塞的 broker GET（包括 market clock）→ 驗證 snapshot/quote/entry policy → 呼叫 `can_submit` → 重新讀取 SafetyGuard/kill-switch → 確認 row 已進入 `SUBMITTING` → 立即呼叫唯一一次 `broker.submit_order()`。若現有 caller 已在進入 final helper 前將 row 轉成 `SUBMITTING`，可以保留該結構，但 `can_submit=False` 時必須以合法 transition 改成 `CANCELED`，POST=0，絕不可留下假裝仍可能在途的 `SUBMITTING`。recovery 的 `PENDING/UNKNOWN` 則應在 final checks 通過後才轉 `SUBMITTING`；authority 拒絕時保留原狀，且 recovery/account 必須保持 unresolved/PAUSED，不能報 CLEAN。

kill switch 的最後檢查必須在 clock GET 之後、POST 之前。對 `_cancel_protection_with_race_check()`，fresh snapshot/reconcile GET 完成且確認 child still live 後，要再次檢查 kill switch，然後才可 DELETE；若已生效，DELETE=0 並保留保護單。這只能關閉「檢查前」的 race；不要宣稱可以撤回已進入網路的 POST/DELETE。

### D. Broker submit outcome 分類

增加單一 helper（名稱可自訂）判定「可證實的 broker 拒絕」：只有 exception chain 中存在 structured HTTP 4xx，且不是 408，才是 definitive rejection。不要用 exception message 猜 HTTP status。

`broker.submit_order(request)` 被呼叫之後：

- structured 400–499（排除 408）可標 `REJECTED`。
- HTTP 408、任何 5xx、timeout/reset，以及沒有 structured definitive 4xx 的任何例外都標 `UNKNOWN`。
- HTTP 200 response 的 JSON decode error、schema/model validation error、缺必要欄位都屬 `UNKNOWN`。
- 空 response 或 response 缺 broker order identity 仍是 `UNKNOWN`。
- 一旦呼叫 submit，`broker_calls`/submit attempt 必須記為 1，即使 SDK 丟例外。
- request 在呼叫 submit 以前無法建構，才可以是本地 terminal failure；不得標成已呼叫 broker。

相同分類要套用 bracket/plain opening、recovery resubmit 與 `_liquidate_core`，避免同一個 HTTP 200 decode failure 在不同入口得到不同結果。UNKNOWN 必須保留同一 client order id 給既有 lookup/reconciliation 使用，並使 account/observation PAUSED。

## 各缺陷的實作要求與驗收條件

### N01 — recovery 遵守目前禁止放空設定

位置：`ExecutionService._resubmit_recovered()`。

從 `_get_execution_config()` 讀取「目前」`allow_shorts`，不可相信 persisted payload 內舊的 `execution_constraints.allow_shorts`。在已存在 broker order 的 adoption 路徑不要阻擋；只有即將重新 POST 的 opening 才檢查。equity symbol 的 opening sell/target SHORT 在 `allow_shorts=False` 時 POST=0；crypto opening short 永遠 POST=0。回補既有 short 的 canonical close buy 必須繼續允許。

Regression 至少包含：舊 SHORT PENDING + current false 被擋；current true 可按其他 gates 正常提交；SHORT position 的 close buy 在 false 時不被誤擋。

### N02 — recovery 不得反向穿倉

位置：`_resubmit_recovered()`，共用前述 recovery crossing helper。

若 persisted intent 認為 `NEUTRAL → LONG`，fresh broker 已為 SHORT，POST=0；不要把 buy 9 改成 buy 4 或 buy 5，也不要建立任何 protective child。錯誤結果必須清楚要求 fresh analysis。另測相同 persisted BUY 在 fresh broker 已為 LONG 時不被 crossing rule 單獨擋下，以及 persisted SHORT 在 fresh broker 已為 SHORT 時不被 crossing rule 單獨擋下（仍受目前 `allow_shorts`、caps、entry policy與 protection gates約束）。

### N03 — stop/window 最後邊界

位置：`long_run.py` 的 `_execute_intent`/`_execute_symbol`、`auto_trade.py`、`service.py` 的 final opening dispatch 與 recovery resubmit。

把 `lambda: _control_stop_reason(deps, ends_at) is None` 傳到底層。測試必須讓 callback 在 broker clock GET 執行期間由 true 變 false，分別覆蓋 stop、window ended、recovery stop；三案都要求 POST=0。callback 在所有 GET 完成後才被呼叫。不要以「外層已檢查一次」取代 final check。

### N04 — kill switch 最後邊界

位置：`_submit_one()`、`_resubmit_recovered()`、`_cancel_protection_with_race_check()`，並檢視 `_liquidate_core()` 的 submit。

測試讓 clock GET 寫入 kill-switch，opening POST 必須為 0；讓 fresh snapshot GET 後寫入 kill-switch，protection DELETE 必須為 0。保留既有政策：kill switch 已生效時，close 也被擋，既有 stop/target 不取消。不要改成「kill switch 仍允許平倉」。

### N05 — terminal order 不要求 quote

位置：`_evaluate_opening_caps()` 的 `needs_price`。

使用既有 `broker_status_to_local` 與 terminal set `FILLED/CANCELED/REJECTED/EXPIRED` 篩選。只有 live、remaining qty > 0、quantity-only 的 order 才加入 `needs_price`。不要另寫一份 Alpaca raw status 白名單。測試至少參數化 canceled/rejected/expired/filled；這些 DELISTED 歷史單都不得觸發 quote，新的 AAPL opening 可繼續。另保留一個 live DELISTED order 無 quote時 fail closed 的對照。

### N07 — 沒有 child facts 不得 CLEAN

位置：`_protection_coverage_gaps()` 與必要的 ledger/payload 讀取。

「是否有保護義務」不能再以「已登錄 protective child relation」作為唯一證據。對每個 live position，從 durable program-owned opening parent 及其 intent payload 判定：該 parent 是 opening leg、已填單/已形成 position，且 payload 有 required broker stop (`risk_controls.stop_loss_price`，並符合現有 protective entry contract)，就存在 expected protection obligation。即使 child 從未出現在任何 snapshot、從未登錄 relation，也要檢查 coverage；無 program-owned live reducing stop/close 覆蓋剩餘 qty時加入 `PROTECTION_GAP:` 並 PAUSED。

不要把所有 manual holdings 強制納管；沒有 durable program-owned protected opening 的 symbol 仍視為手動部位。不要在本批自動補掛 stop、猜 child id 或自動平倉。Regression 包含：filled parent + zero children => execute 與 restart recovery 都 PAUSED；child stop 足量 => CLEAN；position 已平 => CLEAN；純 manual position => 不因本規則 PAUSED；child 不足量/terminal => PAUSED。

### N08 — hard breaker 停止 observation

位置：`safety/guardrails.py`、execution result propagation、`long_run._check_execution_hard_stop()`。

為 `SafetyVerdict` 加入穩定 `reason_codes`（default empty list），並在各 check 失敗時加入下列固定 code：

| check | code | 是否停止整個 observation |
|---|---|---|
| kill switch | `KILL_SWITCH` | 是，沿用現有 stop path |
| per-order notional | `MAX_TRADE_NOTIONAL` | 否，只拒當筆 |
| concentration | `MAX_SYMBOL_CONCENTRATION` | 否，只拒當筆 |
| daily loss | `DAILY_LOSS_HALT` | 是 |
| drawdown | `MAX_DRAWDOWN_HALT` | 是 |
| rejection streak | `CONSECUTIVE_REJECTIONS_HALT` | 是 |

ExecutionService 的 early 與 authoritative safety block 都要把 codes 放入 result 的 `safety_reason_codes`；多個 verdict/result 合併時去重但保序。`summarize_execution_result()` 要保存這個欄位。`_check_execution_hard_stop()` 看 code，不解析英文 reason；遇三種 circuit breaker 時 raise `LongRunStop("SAFETY_CIRCUIT_BREAKER", detail)`。測試分別覆蓋三種 hard breaker；另證明 notional/concentration 仍只拒當筆、不停止 observation。

### N09 — canonical intent consistency

按「共用設計 A」完成。重現 payload（action=BUY，但 target/planned side=SHORT）必須在 schema boundary 被拒，broker GET/POST 都為 0。另對 builder 產生的 BUY、SELL、HOLD、LONG、SHORT、NEUTRAL 與兩種 reversal 做正向驗證，避免 validator 錯殺 canonical payload。

### N10 — portfolio gather 綁定 symbol

位置：`execution/auto_trade.py`。

將 `gather_state=gather_portfolio_state_via_alpaca` 改為明確綁定 ticker 的零參數 callable，例如 `lambda: gather_portfolio_state_via_alpaca(ticker)` 或 `functools.partial(...)`。不得改 `adjust_new_position_notional` 的 callable contract，也不得把既有「portfolio service 失敗時沿用原金額」策略改成 hard fail。測試要求 broker gather 確實收到 `AAPL`，且回傳的縮減金額傳給 `service.execute(dollar_amount=...)`；另保留 gather 失敗時原金額的對照。

### N11 — 200 decode/validation failure 是 UNKNOWN

按「共用設計 D」完成。正式測試可沿用重現檔的真實 Alpaca SDK + fake `requests.Response`，但 transport 必須被替換且 socket 全面封鎖。截斷 JSON 與缺 required fields 的 HTTP 200 都要求：request call=1、local order=`UNKNOWN`、`has_unknown=True`、account state=`PAUSED`、同 client id 可供 lookup、不得變 `REJECTED/CLEAN`。另測 structured 422 仍為 REJECTED，request 建構前錯誤仍為 0 calls。

### N12 — runner lock 保護 active 決策與寫入

位置：`cli/main.py:long_run()`。

不要在互動提示期間持有 lock。互動設定、read-only preflight、使用者 authorization 可先完成；一旦準備執行任何 recovery 或 active/run state mutation，就只取得一次 `runner_lock()`，並持有到 `run_observation_loop()` 結束。lock 內第一件事重新 `load_active_state()`：

- 初始就看到 active：取得 lock 後再讀一次，使用同一份 fresh active 做 resume；不要先 `with lock: pass` 再放鎖、改 restart_count、重新拿鎖。
- 初始沒有 active、提示期間另一 runner 建立 active：取得 lock 後看到 active 時，不做 recovery、不建 manifest、不寫 active；顯示 race/已有 observation，exit code 2。操作者再次執行即可走正常 resume。
- lock busy：不得修改 active/manifest/restart_count/recovery；顯示已有 runner，exit code 2。
- new path：lock 內 fresh active 仍為 None 才可 recovery → session list → manifest/snapshot/event → `save_active_state` → observation loop。

Regression 重現第二 runner race，要求既有 `run_id` 與整份 `active.json` byte-for-byte 不變；另測正常 new 與 resume 仍各只取得一次 lock，resume 的 restart_count 在 lock 內加一。

### N14 — UI 明確顯示 broker unavailable

位置：`alpaca_utils.py` 與 account UI render/callback。

採最小方案，不做 last-known-value cache：`get_positions_data()`、`get_recent_orders_page()`、`get_account_info()` 在 broker/API/解析失敗時拋出帶 context 的 exception，讓既有 renderer 的 error state 顯示。合法成功回應的空 list 與真實數值 0 仍正常顯示 empty/$0。刪除或更新會吞例外並回 `[]`/zero dict 的 wrapper；callback catch 必須回 `Unable to Load ...`，不可再呼叫 empty body renderer。

不得在 UI 顯示 secret、完整 request headers 或 credential 值；error text 最多保留 exception 類型與經過清理的訊息。Regression 分開測 outage 與合法空帳戶，前者含 `Unable to Load Positions/Orders/Account Summary`，後者才可含 `portfolio is currently empty`、0 orders、`$0.00`。

### N15 — recovery/deadline mutation 納入 journal 與 final report

位置：`ExecutionService` recovery telemetry、`long_run.new_round_journal()`、`run_daily_round()`、`aggregate_final_report()`。

請新增明確的 round-level `maintenance_execution`（或等價單一欄位），至少分 `recovery` 與 `deadline`。每筆 summary 必須包含：`broker_calls`（所有 POST/DELETE mutation attempts）、`submit_calls`、`cancel_calls`、`submitted_symbols`（排序後唯一 symbol list）、`has_unknown`、`paused`、`error`。不要用 execution DB row 數推估 broker calls；bracket parent 一次 POST 可能產生 parent + 2 child rows，仍是 submit_calls=1。

`startup_recover()` 與 `_recover_locked()` 要收集真正 submit attempt：在呼叫 `broker.submit_order` 的邊界計數，adopt existing broker order 不算 mutation。`enforce_exit_deadlines()` 已有 cancel/submit facts，請分開回傳並保留既有 `broker_calls` 總數。不要讓內層 public call 與 round journal重複計數同一 mutation。

fresh round 在 recovery 前若尚無 journal，先建立空的 PENDING journal並保存，使 recovery 後即使 screening 失敗，mutation evidence 仍存在；screening 成功後再補 symbols，不要因此把空 symbols 誤判為已完成。每次 recovery/deadline 回傳後、任何 raise/下一個步驟以前先保存 summary。

final aggregate 的 `broker_calls` 同時加總 symbol execution 與 round maintenance；`submitted_symbols` 依實際 `submit_calls>0` 的 symbol event計數，不能因 child ledger rows膨脹。Regression 要求 recovery 一次 bracket POST、DB 三 rows 時：report `broker_calls=1`、`submitted_symbols=1`；adoption-only 是 0；deadline close 的 cancel/POST 數與 report 完全相等。

### N16 — technical brief data as-of 與 freshness

位置：`technical_brief.py` 與 `ta_schema.py`。

增加 typed `TimeframeDataQuality`：`timeframe`、`status` (`fresh|stale|unavailable`)、`as_of`（UTC ISO，可為 null）、`reason`（可為 null）。`TechnicalBrief` 增加三個 timeframe 都必須出現的 `data_quality` list；只有 fresh timeframe 可進 `timeframes`/indicator/signal 計算。daily 不 fresh時 `raw_prices.last_close/prev_close/daily_change_pct` 全部為 `None`，不可用 0 假裝行情。

所有 frame 必須有可解析的 `timestamp`；用 `pd.to_datetime(..., utc=True, errors="coerce")`，拒絕無 timestamp、全無效、future bar。先移除相對 reference time 尚未完成的 bar，再判斷最後一根。production equity calendar 只用 `tradingagents.dataflows.market_calendar` 的 Alpaca authoritative helpers，calendar unavailable 時該 timeframe=`unavailable`，不得 fallback 到 2024–2027 static table。

採用以下已決定的 freshness 規則，不要另猜數值：

| asset/timeframe | fresh 規則 |
|---|---|
| US equity `1d` | last bar 的 ET session 必須等於 reference time 的 most recent completed authoritative session |
| US equity `1h` | 移除未完成 bar後，last bar 必須屬於當前 session（若已有 completed 1h bar）或緊鄰的上一個 completed session；不可跨過一個應有 session |
| US equity `4h` | 與 1h 相同；bar completion 為 `min(bar_start + 4h, 該 session 實際 close)`，所以 early close 也正確 |
| crypto `1h` | last completed bar 距 reference time不超過 2 小時 |
| crypto `4h` | 不超過 8 小時 |
| crypto `1d` | 不超過 36 小時 |

為了可測試，`build_technical_brief`/freshness helper 接受 optional `now`、`calendar_client`、`calendar_rows`；既有兩參數 caller 不必改。historical `curr_date` 必須維持 point-in-time：如果早於 now 的 ET 日期，以該日 authoritative close 作 reference，不得拿 2026 now 去判 2025 historical request stale。`generated_at` 改用 timezone-aware `datetime.now(timezone.utc)`。

Regression 至少包含：2024 bars + 2026 reference 三 timeframe 都 stale、brief JSON保留 2024 as_of、沒有 stale price/signal；正常當日 1h、前一 completed session 4h/daily；週末、假日、early close；future/incomplete bar被剔除；calendar outage是 unavailable；crypto 三個 TTL 的邊界值。

## 本次刻意不做的兩項

### N06 — 維持保守 OCO 曝險

不要修改 `tradingagents/risk/exposure.py` 目前 sibling 共用 reducing capacity 的算法。極端市場可能讓兩腿都成交，現行多算 headroom 是保守但合理的 policy。把 OCO siblings 合併會放寬風控並需要更完整的 broker relationship/競態證明，對首輪 Paper 是不必要的過度工程化。保留現有測試或新增一個 characterization test，明確斷言 9 股 position + linked stop/target 仍計入額外 US$900 increasing notional。

### N13 — checkpoint 啟用前另案處理

目前保存設定 `checkpoint_enabled=False`，本批禁止修改 `trading_graph.py` 與 `checkpointer.py`。後續啟用前應另開小變更：checkpoint identity 納入 compatible run scope（至少 ticker、trade date、analysis source/observation id），有未完成 checkpoint 時以 `graph.invoke(None, config=...)` 或等價 LangGraph resume API 恢復，不再傳 initial state；成功後才 clear。另案測試應證明第一節點只執行一次、失敗節點重跑一次、不同 observation/manual run 不共用 checkpoint。只把 `invoke(init_state)` 改成 `invoke(None)` 卻不隔離 run scope，屬不完整修復。

## 實作順序與局部驗證

1. 先新增正式 regression 檔，從 `docs/paper_readiness_20260912_repros.py` 複製必要 fixture，但反轉成「修正後行為」；正式測試不得 import docs 重現檔，也不得連網。
2. 完成 execution P1 與 N05：N01/N02/N03/N04/N07/N09/N11，執行 `tests/test_paper_readiness_20260912_regressions.py` 中對應案例及直接相關既有 execution tests。
3. 完成 operations/data P2：N08/N10/N12/N14/N15/N16，逐組執行新測試與直接相關既有測試。
4. 執行本文指定的 targeted set；修正所有 regression，不可用 skip/xfail、放寬 assertion 或吞 exception 讓測試變綠。
5. 最後檢查 `git diff --check`、`git status --short`，回報改檔、每個 ID 的修法、執行命令與結果、未執行的 full suite；不要 commit、push、啟動 Paper 或刪除使用者未追蹤檔。

建議 targeted command（依實際被你改動的既有測試補上精確 node id；不要先跑完整 suite）：

```bash
.venv-p2/bin/python -m pytest \
  tests/test_paper_readiness_20260912_regressions.py \
  tests/test_execution_safety_plan_a.py \
  tests/test_phase_b_caps.py \
  tests/test_runtime_reliability_plan_b.py \
  tests/test_phase_d_long_run.py \
  tests/test_phase_d_integration_fixes.py \
  tests/test_chaos_resilience.py \
  tests/test_strategy_consistency.py \
  -q --tb=short
```

如果一次處理太多造成失敗，按 issue group縮小 node id，不要改變 scope。預估實作與局部回歸 12–20 工時；N13 後續另估 2–4 工時。
