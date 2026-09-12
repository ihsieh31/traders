**結論：目前不建議直接啟動 30 日無人值守 Paper Trading。**

審查日期：2026-09-12（Asia/Taipei）。程式基準：`56b86dc`，包含先前 R01–R16 與 F-01/F-03/F-04 修復。本次確認 **15 項缺陷：7 項 P1、8 項 P2**，另有 **1 項非阻擋的風控計算口徑觀察**。P1 表示應於開始自動 Paper 交易前修復；P2 表示影響可靠性、資料或觀察結果，依啟用流程安排修復。

本次只做審查、離線故障注入及文件交付；未修改交易程式、未啟動 observation、未呼叫真實下單或取消 API、未使用付費 LLM，也未使用 Codex Security。

**驗證結果與界線**

| 驗證 | 結果 |
|---|---|
| 現有完整測試 `.venv-p2/bin/python -m pytest tests/ -q --tb=short` | 1060 passed、257 subtests passed，40.49 秒 |
| 新增離線缺陷證據 | 20 passed，2.19 秒；包括 15 項缺陷、1 項計算口徑、競態／SDK 變體與 1 個正常行為對照 |
| `.venv-p2/bin/python -m pip check` | 無相依套件衝突 |
| 真實 Alpaca、實際 LLM、長時間排程、真實網路中斷 | 未執行；不能用離線通過替代這些驗收 |
| 生產程式修改 | 無；只新增本報告與重現腳本 |

證據腳本：[paper_readiness_20260912_repros.py](/Users/zongen/Downloads/codex/tradingAlpaca/docs/paper_readiness_20260912_repros.py)。**這份腳本斷言的是目前錯誤行為，因此 passed 代表問題已重現，不代表問題已修好。** 修復後應把對應斷言改為正確結果，移入正式 regression tests。

腳本使用臨時 execution DB、帳戶鎖、long-run 目錄與真實 SafetyGuard；封鎖 socket 網路。N03/N15 跑實際 daily-round 路徑，N11 跑實際 Alpaca SDK 的 HTTP 回應解碼，N12 跑實際 CLI，N13 跑實際 LangGraph SQLite checkpoint，N14 跑實際 UI renderer；外部 transport、資料與 broker 成交由 fixture 注入。Fake broker 接受某請求，只證明程式會送出該請求，不能證明真實 Alpaca 必然接受。

**流程覆蓋**

| 流程 | 本次檢查重點 | 結果 |
|---|---|---|
| 啟動與設定 | Paper 設定、三角色 provider、preflight／授權順序、狀態檔、鎖 | N12；本機設定見後文 |
| 排程與停止 | 日曆、提前收盤、原窗口續跑、停止訊號、期限 | N03；未做 30 日實跑 |
| Screening／資料 | universe、session gate、Top20、資料品質與 stale 情境 | N16；未驗證真實 feed 資格與完整度 |
| 分析與決策 | LLM retry／usage、checkpoint、結構化 intent、部位調整 | N09、N10、N13 |
| 下單與曝險 | snapshot／quote 時效、entry policy、caps、outbox、唯一 client ID | N04、N05、N06、N11 |
| 恢復與對帳 | PENDING／UNKNOWN、broker lookup、重新送單、目前持倉 | N01、N02、N07；已知 F-02 另列 |
| 出場與保護 | bracket／OTO、child ownership、取消競態、deadline、liquidation | N07；既有 kill switch 保留保護單的對照通過 |
| 操作與觀察 | WebUI 停止／帳戶畫面、safety hard-stop、round journal、最終報告 | N08、N14、N15 |
| 執行環境 | 現有測試、lockfile／pip、Docker 持久化與 CI 設定閱讀 | 未重建 Docker image，也未做長時間跨程序壓測 |

審查已跨越主要呼叫鏈與失敗分支，但不宣稱窮舉所有 broker／作業系統／市場狀態。以下每項缺陷都有本次可重現證據，不以假設風險湊數。

**P1：開始自動 Paper 交易前應修復**

**N01 — recovery 沒有重驗目前「禁止放空」設定。** 位置：[service.py:2101](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:2101)，一般入口的放空檢查在 [service.py:1441](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:1441)。

重現：留下合法 SHORT 的 PENDING 訂單，再以 `allow_shorts=False` 恢復；仍送出 sell 9 股，fake broker 持倉變 −9，recovery 回報成功。使用者關閉放空後，舊單仍可重新建立空頭曝險。最小修法：對「真的要重新 POST」的開倉，依目前設定與實際 side 驗證放空權限；已存在 broker 的訂單照常認領，回補空頭不應被誤擋。證據：`test_N01_recovery_ignores_current_short_opt_out`。

**N02 — recovery 可把過期的開倉假設變成反向穿倉，保護數量也不吻合。** 位置：[service.py:2121](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:2121)；一般新單已有持倉方向檢查：[service.py:1616](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:1616)。

重現：PENDING intent 假設 NEUTRAL→BUY，但 broker 目前已是 SHORT −4；恢復仍送 buy 9 與 9 股 stop／target。接受此請求的 fake broker 最後為 LONG +5，保護單卻仍是 9 股，帳戶標成 CLEAN。真實 broker 可能拒絕，但本地不應依賴這點。最小修法：恢復前重驗目前部位方向，阻擋未經分析授權的反向穿倉；保留既有設計允許的同方向增倉，不必全面禁止 recovery。證據：`test_N02_recovery_crosses_changed_position_and_overprotects`。

**N03 — stop／觀察期限沒有守住最後一個下單邊界。** 位置：[long_run.py:1947](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:1947)、[service.py:2249](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:2249)、[service.py:2591](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:2591)。

重現實際 daily-round 的三種情境：一般單最後一次 broker clock GET 期間收到 stop、超過 ends_at，以及 recovery 的同類 stop。三者都已失去執行權限，仍 POST 一筆並買入 9 股。一般執行未將 stop/window callback 傳入 service；recovery 雖有 callback，檢查後又進行網路 GET。最小修法：沿現有呼叫鏈傳遞同一個執行權限檢查，放在全部可能阻塞的 GET 完成後、POST 前。證據：`test_N03_stop_or_window_during_final_get_still_posts` 三個參數案例。

**N04 — kill switch 在最後一次 GET 期間生效，仍會新增曝險。** 位置：[service.py:1862](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:1862)、[service.py:2591](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:2591)、[service.py:2781](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:2781)。

重現：真實 SafetyGuard 已通過，clock GET 期間寫入 kill-switch 檔案，回傳後仍 POST 買入 9 股。最小修法：最後一次網路讀取後、mutation 前重查 kill switch，檢查取消保護單的路徑是否也有同類缺口；保留現行 kill switch 同時限制出場的既有政策。這能消除已重現的 GET 時間窗，不能撤回已送上網路的請求。證據：`test_N04_kill_switch_during_final_clock_get_does_not_block_post`。對照：kill switch 若早已生效，出場會被阻擋、既有保護單不會被取消，此行為正確。

**N07 — 從未看到任何 child order，仍把已成交部位當成 CLEAN。** 位置：[service.py:845](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:845)，跳過點在第 889 行。

重現：broker 回傳已成交的保護式 entry parent，但所有讀取都沒有 child facts；帳上已有 9 股，ledger 僅一筆 parent，execute 與 startup recovery 仍回 CLEAN。coverage 檢查只有在曾登錄 child relationship 後才承認它是程式建立的受保護部位，因此「一開始就沒有證明」反而被當成手動持倉略過。最小修法：從 durable opening intent／payload 記錄預期保護義務；未證明有效 stop 或已平倉前維持 PAUSED。先不做自動補掛保護單系統。證據：`test_N07_filled_parent_without_any_protective_children_is_clean`。

**N09 — TradeIntent 型別驗證通過，不代表 action／target／broker side 一致。** 位置：[schemas.py:87](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/schemas.py:87)、[schemas.py:135](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/schemas.py:135)、[service.py:125](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:125)。

重現：將合法 SHORT payload 的 action 改成 BUY，保留 SHORT target／sell planned action，保留有效的 short stop；schema 驗證通過，`allow_shorts=False` 仍送 sell 9。常規 builder 不會主動建立這種 payload；問題是受支援的 dict/model 邊界對呼叫端錯誤或版本漂移缺少一致性保證。最小修法：驗證 action、目前部位、target、transition、planned actions 符合同一份 canonical plan；實際 side 也需符合曝險權限，不一致直接拒絕。證據：`test_N09_schema_accepts_contradictory_action_and_broker_side`。

**N11 — HTTP 200 的無法解析回應被當作明確拒單，遺失 UNKNOWN 狀態。** 位置：[service.py:89](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:89)、[service.py:2784](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:2784)，recovery 共用相同分類。

使用真實 Alpaca SDK，僅替換其 HTTP session：POST 回 200，但 body 分別為截斷 JSON、缺必要欄位 JSON。兩者都已進入 HTTP 請求一次，SDK decode／model validation 失敗後，本地卻標 REJECTED、帳戶 CLEAN。這不能證明 broker 沒收單，後續新 decision 可能再加倉。最小修法：明確區分送出前失敗與送出後不確定；POST 後無法理解成功回應應 UNKNOWN，以原 client ID 對帳，只有可證實拒絕才 terminal。證據：`test_N11_real_sdk_response_decode_failure_is_marked_rejected` 兩個案例。

**P2：可靠性、資料與觀察品質**

**N05 — 已取消的歷史訂單仍要求即時報價，阻擋無關的新單。** 位置：[service.py:464](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:464)。

曝險計算收集 `needs_price` 時未排除 terminal 訂單。重現：歷史中有 DELISTED 的 canceled、未成交 10 股單，該股已無 quote；新的 AAPL 開倉因此被拒，POST=0。若舊單長期留在查詢範圍，可能持續卡住帳戶。最小修法：收集必要 quote 時套用曝險計算相同的 live-order 判定。證據：`test_N05_terminal_unfilled_order_still_demands_quote`。

**N08 — 日損等硬性 breaker 擋住訂單，long-run 卻不結束觀察。** 位置：[long_run.py:1976](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:1976)。

真實 SafetyGuard 看到 equity 89,000、昨日 100,000，正確以日損 −11% 拒絕下單並回 `safety_blocked`；但 `_check_execution_hard_stop` 不拋出停止，只判斷 UNKNOWN、PAUSED、kill switch。影響是後續分析／排程與成本可能繼續，觀察狀態也不表達這個硬停止；本案例沒有繞過下單風控。最小修法：以穩定 reason code 區分日損／回撤／連續拒單 breaker 與一般單筆限制，前者進既有停止與結案路徑，後者維持普通拒單。證據：`test_N08_daily_loss_block_does_not_stop_observation`。

**N10 — 自動交易的 portfolio 調整缺少 symbol，啟用的新多單實際都跳過此層。** 位置：[auto_trade.py:76](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/auto_trade.py:76)、[portfolio/__init__.py:232](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/portfolio/__init__.py:232)、[portfolio/__init__.py:254](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/portfolio/__init__.py:254)。

caller 傳未綁定的 `gather_portfolio_state_via_alpaca`，callee 用 `gather_state()` 呼叫，但函式必須有 symbol。每次都 TypeError，被既有容錯吞掉後沿用原金額；測試中 US$1,000 原封不動送往 execution service，portfolio broker gather 根本沒開始。相關性與波動調整因此失效；其他獨立硬性 exposure caps 仍存在。最小修法：以 lambda／partial 綁定 ticker，補一個真實 hook 串接測試即可。證據：`test_N10_auto_trade_never_supplies_portfolio_gather_symbol`。

**N12 — 第二個 CLI 啟動程序在拿 runner lock 之前覆寫 active observation。** 位置：[cli/main.py:1638](/Users/zongen/Downloads/codex/tradingAlpaca/cli/main.py:1638)、[cli/main.py:1748](/Users/zongen/Downloads/codex/tradingAlpaca/cli/main.py:1748)、[cli/main.py:1752](/Users/zongen/Downloads/codex/tradingAlpaca/cli/main.py:1752)。

重現：兩個啟動程序起初都看不到 active state；第一個先跑起來，第二個還在設定階段。第二個之後先寫新的 RUNNING active.json，才拿鎖並失敗，原 observation 指標已被取代。下一次 resume 會找錯 run／窗口。最小修法：使用既有 runner lock 保護「重新讀取 active、決定 new/resume、啟動 recovery、寫狀態」；互動設定可先做，但拿鎖後必須重新決定，不能沿用拿鎖前判斷。不需新鎖服務。證據：`test_N12_new_cli_runner_overwrites_active_state_before_lock`。

**N13 — optional checkpoint 存在，但 propagate 重啟仍重跑已完成節點。** 位置：[trading_graph.py:553](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/graph/trading_graph.py:553)，`graph.invoke(init_agent_state)` 呼叫位於此方法內。

真實 LangGraph 兩節點測試：第一節點完成並落 SQLite，第二節點故障；重跑 propagate 時重新傳 initial state，第一節點又執行一次。會重付已完成分析的成本，恢復語意與預期不符。最小修法：同一個相容 run 的未完成 checkpoint 應走明確 resume；新分析才給 initial state，區分已完成 checkpoint。現行本機 `checkpoint_enabled=False`，因此不必為首輪 Paper 而啟用或優先重做此功能。證據：`test_N13_checkpoint_restart_reexecutes_completed_nodes`。

**N14 — WebUI 把 broker 讀取失敗畫成空倉、零餘額、零訂單。** 位置：[alpaca_utils.py:671](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/alpaca_utils.py:671)、[alpaca_utils.py:715](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/alpaca_utils.py:715)、[alpaca_utils.py:770](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/alpaca_utils.py:770)、[alpaca_account.py:100](/Users/zongen/Downloads/codex/tradingAlpaca/webui/components/alpaca_account.py:100)。

注入 broker unavailable 後，實際 renderer 顯示「Your portfolio is currently empty」，account 回 cash／buying power=0、orders 回空集合。操作員可能誤認已平倉；execution authority 自身的 fail-closed 機制沒有因此失效。最小修法：API unavailable 與合法空集合分開表示；畫面顯示讀取失敗，或保留最後成功值並標出時間與過期狀態。證據：`test_N14_webui_broker_outage_is_rendered_as_empty_portfolio`。

**N15 — round／最終 execution 統計漏記 recovery 下單。** 位置：[long_run.py:1479](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:1479)、[long_run.py:1962](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:1962)、[long_run.py:2427](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:2427)。

實際 daily round 恢復一筆 pending 並買入 9 股；最終報告卻顯示 `broker_calls=0`、`submitted_symbols=0`，execution DB 同時有 parent／children 共 3 筆。原因是 tally 只讀 per-symbol 正常 execution summary。前段 deadline enforcement 也未經這個記錄入口，需一併核對，但本次直接重現的是 recovery。最小修法：把 recovery／deadline 結果與實際 mutation 次數記入既有 round journal，統一 aggregate；區分 parent POST 與 child ledger rows，不要用 3 筆 rows 推算 3 次下單。證據：`test_N15_recovery_posts_absent_from_observation_execution_tally`。

**N16 — technical brief 未檢查最後一根 bar 的時效，也未保留資料日期。** 位置：[technical_brief.py:185](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/technical_brief.py:185)、[technical_brief.py:795](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/technical_brief.py:795)。

注入回傳 220 根 2024 年舊 bars 的資料來源，2026 年的 build_technical_brief 仍產生三個 timeframe、last_close=121.9，generated_at 卻是現在，而且輸出不含 bar 的資料日期。此測試證明對 stale upstream response 缺防護，並不表示真實 Alpaca 已回傳這種資料。正常停牌或落後的 feed 也需要清楚區分「現在產生」與「資料截至何時」。最小修法：每個 timeframe 保留實際 as-of，依交易日曆與已完成 bar 檢查 freshness；過期時明確 unavailable／stale，別讓生成時間代替資料時間。證據：`test_N16_technical_brief_accepts_old_bars_as_current`。

**N06：非阻擋，先確認風控計算口徑**

位置：[exposure.py:63](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/risk/exposure.py:63)。9 股多頭、市值 US$900，搭配各 9 股的 linked stop／target，目前會另外計入 US$900 outstanding increasing notional。原因是兩個 sibling 共用同一個可減倉數量，第二個被視為可能新增反向曝險。

這會較早用掉 gross headroom，但**不列為確認缺陷，也不要求為了 Paper 而放寬**。Alpaca 說明正常情況下一腿成交會取消另一腿，同時明列極端快速市場可能兩腿都成交；保守計算有其依據。若產品原意是按正常 OCO 群組計算，才考慮以已證實的 parent relationship 合併 siblings；不明或手動獨立訂單繼續分別計入。來源：[Alpaca bracket orders 說明](https://docs.alpaca.markets/us/docs/orders-at-alpaca)。證據：`test_N06_bracket_siblings_count_as_extra_opening_exposure`。

**本機啟動條件與既有限制**

以下為唯讀取得的保存 config，以及以它和 DEFAULT_CONFIG 組合出的 runtime；不是某個已運行程序的狀態。沒有讀出或附上任何 API secret。

| 項目 | 本次讀到的值 | 啟動前意義 |
|---|---|---|
| Paper | `.env` 中 `ALPACA_USE_PAPER=True` | 設定指向 Paper；本次未向 broker 證明憑證有效或帳戶實況 |
| 保存觀察設定 | 30 個日曆天、每日 11:00 ET、基準 US$1,000 | 尚無 active.json，沒有已啟動的 observation |
| 放空 | `allow_shorts=True` | 目前保存設定允許放空；N01 是之後關閉／換設定恢復時的缺口 |
| 三角色 | Analysis／Decision／Screening 均為 openai provider、ling-3.0-flash-fin | 本次未做連線與模型相容性 probe |
| Safety | 開啟；單筆上限 US$25,000、單股集中 25%、日損停止 10%、回撤停止 15%、連續拒單 5 次 | 如需調整，應是明確的觀察政策決定，不是這次審查偷偷改參數 |
| LLM 每日預算 | `daily_llm_token_budget=0` | 無上限；與 broker 金額限制分開 |
| Portfolio intelligence | 開啟 | 但 N10 使新多單的這層調整被跳過 |
| 額外 risk sizing | `risk_sizing_enabled=False` | 不代表 entry policy／其他硬性曝險 caps 全部關閉 |
| 保護單 | `protective_bracket_orders_enabled=True` | 需修 N07 並驗證真實 child facts |
| Sector | mapping 為空，預設 sector cap 30% | 缺 mapping 時 sector constraint 不活化，不能宣稱已具備產業分散保證 |
| Corporate actions | 預設為手動事件清單，無自動事件 feed | 覆蓋範圍僅限已提供事件；不列為新 bug |
| 通知 | `.env` 與預設設定未見 Telegram／webhook 管道 | 需配置並驗證一條可用管道，才能依賴無人值守通知 |
| Checkpoint | 關閉 | N13 可延後；不要為了首輪測試擴大功能範圍 |

README 已明列 **F-02：ACCEPTED／PARTIAL 的 recovery whitelist 維持 fail-closed**。本次不把它重複算成新缺陷，但它仍影響無人值守恢復的可用性，不能把「先前 Accepted」理解成所有正常 broker 狀態都可無人介入續跑。Paper 本身也可能出現部分成交：Alpaca 說明可成交訂單有 10% 機率先得到隨機大小的 partial fill，後續才再評估剩餘部分。因此實際演練必須涵蓋部分成交與重啟。來源：[Alpaca Paper Trading rules](https://docs.alpaca.markets/us/docs/paper-trading)。

既有 1060 項測試通過，以及 kill switch 已生效時保護單保留的負向對照，都是有效的已完成工作。本次 P1 多數落在「最後一次外部讀取後」「恢復舊意圖」「成功 HTTP 回應無法理解」「保護單第一次就不可見」等邊界，現有 happy-path／先設停止旗標測試沒有完整涵蓋。兩個既有測試 warning 屬第三方 deprecation；新 technical brief 案例另暴露 utcnow deprecation，均不列啟動阻擋。

**最小修復與驗收順序**

1. **先修同一條 execution 邊界，預估 6–10 工時。** 合併處理 N01／N02／N09 的 intent 與目前部位檢查、N03／N04 的最後 mutation 權限、N07 的預期保護、N11 的 ambiguous outcome；沿用現有 service、outbox、locks、reason codes，不新增微服務或另一套交易狀態機。
2. **補已啟用流程的 P2，預估 4–7 工時。** N05／N10 是直接且小的修正；N08／N12／N15 影響 30 日觀察，應於無人值守前完成；N14／N16 補操作與資料真實性。N13 在 checkpoint 關閉時可延後，N06 保持保守值即可。
3. **把缺陷證據改成正式 regression，預估 1–2 工時。** 驗收應明確斷言：撤銷權限後 POST=0；舊假設不穿倉；禁止放空有效；沒有 stop 證明不可 CLEAN；200 decode failure 留 UNKNOWN；第二 runner 不改 active；恢復實際 POST 與報告一致。跑完整 suite 與 pip check，不用把全部 UI／資料層重寫成測試框架。
4. **修復通過後才做有人值守的 Paper 演練，操作約 1–2 小時，再觀察至少一個完整美股交易時段。** 驗證真實帳戶／quote／模型 probe、entry 與 broker child orders、partial fill、取消與平倉、程序中斷續跑、stop／kill、通知與最終報告對帳。記錄 client ID、broker ID、持倉與 journal 的對應；以上通過後才開始 30 日觀察。這次審查沒有執行或代為授權這些交易。

上述是工程估時，核心修復與離線回歸合計約 **11–19 工時**，不含交易日等待。首輪不需要自動修復所有異常；對確定不了的 broker 狀態，清楚 PAUSED、保留證據、通知操作員，就比增加複雜恢復分支更符合目前範圍。
