**審查結論：目前不建議進入自動／無人值守 Paper Trading。**

本輪確認 16 項問題：11 項 P1、5 項 P2。P1 涉及不該送出的委託、保護單／平倉流程或帳戶曝險；P2 涉及特定操作情境、隔離設定、成本紀錄、卡住與報告準確性。這些問題有離線重現證據，不是把「可能需要更多功能」列為缺陷。

審查基準：`a8c537191343169633fc09e3ac1204e203fcf5b9`，2026-09-11。未使用 Codex Security。未修改 production code，未啟動實際 Paper 觀察，也未透過本次重現向真實券商或 LLM 發送交易／推論請求。

**已完成驗證。** 現有測試 `975 passed`、`255 subtests passed`，31.41 秒；`pip check` 回報 `No broken requirements found`。另外新增 17 個離線故障案例，確認下列 16 項問題；R02 分別測試停止旗標與觀察期限。

第一次完整測試的 95 個失敗涉及沙盒禁止寫入使用者目錄的帳戶鎖、測試狀態等操作；經允許在沙盒外重跑後全部通過，因此沒有把第一輪失敗列為產品缺陷。兩個剩餘 warning 來自 websockets／LangGraph 相依套件棄用提示。

重現檔：[paper_readiness_20260911_repros.py](paper_readiness_20260911_repros.py)。執行方式：

```bash
.venv-p2/bin/python -m pytest tests/ -q --tb=short
.venv-p2/bin/python -m pytest docs/paper_readiness_20260911_repros.py -q -s --tb=short
.venv-p2/bin/python -m pip check
```

第二個檔案刻意放在 `docs/`：其斷言確認「目前錯誤確實存在」，17 項通過不表示程式已修好。修復時應將對應案例改成斷言正確行為，再納入正式 regression suite。重現使用獨立暫存 DB、模擬券商、模擬 LLM 邊界與禁止網路的 socket；沒有放寬 production gate。

| 編號 | 優先度 | 確認問題 | 主要影響路徑 |
|---|---|---|---|
| R01 | P1 | long-run 恢復委託早於執行設定套用，可能略過 Top20 gate | 啟動／恢復 |
| R02 | P1 | 停止／期限檢查晚於會下單的 recovery | long-run |
| R03 | P1 | bracket 連動取消後再取消另一子單，平倉中斷 | 手動、明確退出、期限退出 |
| R04 | P1 | 反向決策的 close-only 階段仍被開倉條件與自有保護單擋住 | LONG↔SHORT |
| R05 | P1 | 最後一次 POST 前沒有重驗報價、快照與決策期限 | 初次送單／恢復 |
| R06 | P1 | `done_for_day` 被曝險計算當成終止 | 未成交曝險上限 |
| R07 | P1 | HTTP 500 可能被當成確定拒單，帳戶繼續 CLEAN | 初次送單／恢復／清倉 |
| R08 | P2 | 文件中的 split 隔離範例會被默默忽略 | 公司事件隔離 |
| R09 | P1 | 保護單消失／取消後，未平倉部位仍可 CLEAN | 對帳／持倉保護 |
| R10 | P2 | 多程序 SafetyGuard 可互相覆寫 token／風控狀態 | CLI＋WebUI 同時使用 |
| R11 | P1 | 初始圖表載入期間 Stop→Start，舊分析可取得新 generation 並交易 | WebUI |
| R12 | P2 | 舊 generation 的 ProviderFailure 會清掉新佇列 | WebUI |
| R13 | P1 | 收盤後仍排當日分析，執行層未證明市場開盤，GTC 可能排到隔日 | 排程／委託有效期 |
| R14 | P1 | 真實持倉方向改變後，仍沿用舊的開倉計畫與保護數量 | 決策→執行 |
| R15 | P2 | memory embedding 未套用 timeout，SDK 每次可等待 600 秒並重試 | 分析／記憶 |
| R16 | P2 | 報告快照把無效／缺失的資產資料變成 0 或空持倉 | Preflight／最終報告 |

**R01 · P1 — long-run 恢復時的 Top20 gate 使用錯誤設定。**

`build_runtime_config()` 強制 `auto_screening_enabled=True`，但它只回傳 dict。真正呼叫 `set_config()` 在 `_build_graph_config()`，發生於 recovery 與 screening 之後。`ExecutionService` 的 gate 讀取全域 `get_config()`；新程序的預設值仍是 False。CLI 的 post-authorization recovery 也沒有接收 runtime。

重現：持久化一筆仍有效的 PENDING BUY，long-run runtime 為 True，全域設定為 False，selection 不存在。進入正常 `run_daily_round()` 後，模擬券商已收到 1 次 POST，才進入 screening；在 screening 邊界同時觀察到 `(runtime=True, global=False, posts=1)`。這不是純粹設定顯示不同，確實改變恢復下單的 gate。

最小修法：在第一個可能產生 mutation 的 recovery 前套用並驗證本次 runtime；CLI 新啟動、resume、WebUI 啟動都遵循相同順序。恢復只能使用當前有效 selection，不因尚未掃描就降級成 manual mode。不要新增另一套 selection authority。

定位：[long_run.py:1282](../tradingagents/long_run.py#L1282)、[long_run.py:1407](../tradingagents/long_run.py#L1407)、[cli/main.py:1725](../cli/main.py#L1725)、[gate.py:80](../tradingagents/screening/gate.py#L80)。證據：`test_R01_long_run_recovery_uses_manual_global_config`。

**R02 · P1 — recovery 可以在停止後／觀察到期後送出新單。**

`run_daily_round()` 先執行 `startup_recover()` 和 `enforce_exit_deadlines()`，到後面才呼叫 `_control_stop_reason()`。recovery 並非唯讀：它可以重新提交 PENDING／UNKNOWN。外層 loop 的檢查不足以涵蓋進入 round 前後或等待券商時發生的停止訊號。

重現兩種情況：停止旗標已為 True，或 `ends_at` 已過期 1 秒；兩者均在返回前送出 1 筆 opening order。這裡重現的是恢復開倉，不是風險降低的例外處理。

最小修法：停止／時間檢查放在 recovery 前，並讓恢復的 mutation 邊界也能檢查同一控制狀態。已有券商訂單的唯讀查詢／adopt 可以繼續；需要重新 POST 的分支必須重新取得有效執行許可。沿用既有旗標與 deadline，不需要新排程架構。

定位：[long_run.py:1282](../tradingagents/long_run.py#L1282)、[long_run.py:1306](../tradingagents/long_run.py#L1306)、[service.py:1981](../tradingagents/execution/service.py#L1981)。證據：`test_R02_recovery_precedes_round_stop_and_window_check`，stop／expired 兩案例。

**R03 · P1 — bracket 子單連動取消會中斷平倉，留下沒有保護的部位。**

程式取得兩個 live 子單後，依原始清單逐一 DELETE。取消第一個子單時，券商可能已連帶取消另一個；第二次 DELETE 若回覆「已取消」便直接跳到外層 exception，後續 refresh、close POST、protection-gap 檢查都未執行。Alpaca 明確記載，取消 bracket 組中任一訂單會取消組內其他未完成訂單。[Alpaca bracket 規則](https://docs.alpaca.markets/us/docs/orders-at-alpaca)

重現：正常開出 9 股並有 stop＋target；取消第一個子單連帶取消兩者，第二個 DELETE 回 422。結果保留 9 股，close POST=0，兩個保護單均 canceled；回傳卻是 `broker_calls=0`。close outbox 雖已持久化，當次退出並未完成，需後續恢復／人工介入。

最小修法：取消一個關聯子單後，以 fresh broker facts 判斷剩餘子單；「已取消」只能在重新 GET 證實後視為完成。任何取消已發生的 exception 路徑都要保存實際 mutation 數、執行對帳與保護缺口檢查。不要 catch 422 後盲目忽略全部錯誤。

定位：[service.py:608](../tradingagents/execution/service.py#L608)、[service.py:826](../tradingagents/execution/service.py#L826)、[service.py:2507](../tradingagents/execution/service.py#L2507)。證據：`test_R03_bracket_cancel_cascade_aborts_liquidation`。

**R04 · P1 — 已改成 close-only 的反向交易，仍無法退出正常的受保護持倉。**

反向決策包含 close＋open，故 `opening=True`。保護單取消流程只在 `closing_specs and not opening` 執行；接著 `_verified_reducing_exit()` 又將原有 stop／target 判定為衝突。正常受保護部位的反向決策因此完全不能送 close。另由原始開倉規劃決定的 entry policy、Top20、quarantine 與曝險檢查，也可能在 close-only 階段提前拒絕安全退出。

重現：LONG 9 股、有效 stop＋target，產生有效 `REVERSE_TO_SHORT`，`allow_shorts=True`。回傳 no conflicting close order 相關拒絕，沒有取消、沒有 close POST，仍持有 LONG。

最小修法：在執行 gate 前，把本次 reversal 明確縮減為「已授權、可驗證的 close phase」，共用現有 commit-before-cancel 平倉流程。之後維持現行要求：重新分析與確認 flat 才能反向開倉。不要把 reversal 改回同次呼叫 close 後立即 open。

定位：[service.py:994](../tradingagents/execution/service.py#L994)、[service.py:1054](../tradingagents/execution/service.py#L1054)、[service.py:1519](../tradingagents/execution/service.py#L1519)。證據：`test_R04_protected_reversal_cannot_execute_its_close`。

**R05 · P1 — 報價與決策有效期不是在真正送單前驗證。**

fresh quote／snapshot 的檢查發生在前段。之後還有風險計算、其他商品報價、SQLite commit、guard 與可能的程序暫停；`_submit_one()` 直接 POST。恢復分支也在取得 quote 後繼續做其他工作，沒有送單前最後一次 freshness／expiry 檢查。

重現：前段全部合法，在 outbox commit 完成後將時鐘推進兩小時，模擬電腦休眠／程序暫停。此時 entry policy 已過期，quote 亦遠超 TTL；仍送出 1 個 protected opening POST。事後對帳失敗不能撤回已送出的委託。

最小修法：在最後 POST 邊界重新檢查既有 quote／snapshot 的 age、entry expiry 和控制狀態。過期就拒絕並要求 fresh facts，不能只延長 TTL。需要更新 facts 時重新計算受影響的 size／caps，不要拿新時間戳包裝舊資料。

定位：[service.py:1092](../tradingagents/execution/service.py#L1092)、[service.py:1461](../tradingagents/execution/service.py#L1461)、[service.py:2306](../tradingagents/execution/service.py#L2306)。證據：`test_R05_expired_quote_and_decision_can_reach_submit`。

**R06 · P1 — `done_for_day` 的未成交委託被漏計曝險。**

authority 將 `done_for_day` 保留為 live，但 `risk/exposure.py` 將它放入終止狀態排除清單，兩個模組的狀態語義不同。Alpaca 對此狀態的定義是當天不再執行，隔一交易日仍可能更新，不等於 canceled／expired。[Alpaca order lifecycle](https://docs.alpaca.markets/us/docs/orders-at-alpaca)

重現：100 股 BUY、價格 $100、尚未成交、狀態 `done_for_day`。曝險計算回傳 `$0` 且 `fully_estimated=True`，可少計 $10,000 的待成交曝險。若再配置新單，symbol／sector／gross 上限可能被低估。

最小修法：統一使用明確的 terminal 狀態定義，保留 `done_for_day` 的 remaining quantity 曝險。無需新增狀態機。

定位：[exposure.py:32](../tradingagents/risk/exposure.py#L32)、[authority.py:708](../tradingagents/execution/authority.py#L708)。證據：`test_R06_done_for_day_exposure_is_ignored`。

**R07 · P1 — HTTP 500 沒有被當成不確定的送單結果。**

`_is_ambiguous_error()` 只比對少量字串，例如 timeout、connection reset；真實 Alpaca `APIError` 的 HTTP status 沒有參與判斷。`500 Internal Server Error` 會走 REJECTED，而不是 UNKNOWN。HTTP 500 本身無法證明 broker 絕對未接受委託。

重現使用實際 `alpaca.common.exceptions.APIError`，附帶 HTTP 500 response。結果 POST=1、local=REJECTED、account=CLEAN，沒有 `has_unknown`。targeted recovery 因本地已 terminal 而不會查這筆；後續一般快照仍可能找到晚到的券商紀錄，但中間已失去保守的等待限制。

最小修法：只有有明確拒絕語義的回應才標 terminal；HTTP 5xx、不能解析的送單回覆與傳輸不確定性走 UNKNOWN，沿用相同 client_order_id 查詢／adopt，禁止即時再送一筆。初次、recovery、liquidation 共用此判斷。

定位：[service.py:59](../tradingagents/execution/service.py#L59)、[service.py:2309](../tradingagents/execution/service.py#L2309)、[service.py:1983](../tradingagents/execution/service.py#L1983)。證據：`test_R07_server_error_is_terminal_rejection`。

**R08 · P2 — 依官方專案範例配置 split，實際不會隔離。**

`default_config.py` 的 `corporate_action_events` 範例包含頂層 `ratio`。但 `QuarantineStore.quarantine()` 不接收 ratio，初始化使用 `self.quarantine(**event)` 會拋 TypeError，隨即被 `except ...: continue` 靜默忽略。現行隔離本來就沒有自動事件 feed，這會直接破壞人工輸入的主要路徑。

重現：原樣使用 TSLA、split、ratio、effective_at、source 範例；到生效日之後，`is_quarantined()` 仍為 False，無有效紀錄。

最小修法：把 ratio 放在既有 `details`，同步修正範例；無效事件需在啟動時回報可讀錯誤並阻擋相關自動執行，不能當作沒有事件。無需建自動 corporate-action ingestion 系統。

定位：[default_config.py:224](../tradingagents/default_config.py#L224)、[corporate_actions.py:74](../tradingagents/risk/corporate_actions.py#L74)。證據：`test_R08_documented_quarantine_example_is_silently_dropped`。

**R09 · P1 — CLEAN 只證明帳本一致，尚未證明應有的持倉保護仍存在。**

protection-gap 檢查主要依賴「這次是本程式取消保護」或既有 `PROTECTION_GAP` reason。若券商／交易所取消孩子，或有人在 Alpaca 介面取消子單，後續對帳會同步為 CANCELED；positions 與 fills 若相符，帳戶即可 CLEAN。缺少重新核對 filled parent 對應保護是否仍有效的規則。

重現：正常 protected BUY 成交後，把兩個子單改為 canceled，保留 9 股。`startup_recover()` 回傳 `success=True, account_execution_state=CLEAN`。

相關邊界也需要覆蓋：bracket 的退出腿要等 entry 完全成交才啟動，不能把部分成交時的 held 子單當成已有效保護。[Alpaca bracket activation](https://docs.alpaca.markets/us/docs/orders-at-alpaca)

最小修法：在現有 reconcile 中，利用 durable parent／child 關係、remaining fills 與當前券商狀態驗證保護。保護缺失要 PAUSED 並發出原因；合法的待完成 close 可依既有規則處理。不需要自動重建 stop 或擴張成訂單管理平台。

定位：[service.py:639](../tradingagents/execution/service.py#L639)、[service.py:694](../tradingagents/execution/service.py#L694)、[service.py:1993](../tradingagents/execution/service.py#L1993)、[authority.py:556](../tradingagents/execution/authority.py#L556)。證據：`test_R09_missing_protective_orders_can_be_clean`。

**R10 · P2 — 多程序安全狀態會互相覆寫。**

SafetyGuard 初始化時讀取一次 state，之後在自身 RLock 內更新並 atomic replace。RLock 不跨程序；atomic replace 只保證 JSON 不被寫一半，不保證 read-modify-write 合併。long-run 與 WebUI 同時存在時，即使券商 POST 有帳戶鎖，LLM 記帳與 HWM 更新仍可能來自不同程序的舊 state。

重現：兩個 instance 先讀同一空 state；A 記 80 tokens，B 記 30 tokens；磁碟最後只有 30，而非 110。相同整份 state 覆寫機制也會影響 HWM／rejection streak。

最小修法：若第一輪只准單一程序，先明確限制 CLI／WebUI 不可同時操作同一狀態；若保留並行，對同一 JSON 的 reload→update→save 加一個跨程序檔案鎖。不要引進 Redis 或第二套資料庫。

定位：[guardrails.py:165](../tradingagents/safety/guardrails.py#L165)、[guardrails.py:189](../tradingagents/safety/guardrails.py#L189)、[guardrails.py:263](../tradingagents/safety/guardrails.py#L263)。證據：`test_R10_safety_state_lost_update_between_instances`。

**R11 · P1 — 初始圖表完成後，舊分析會被誤認成新一輪。**

`start_analysis()` 先同步 `create_chart()`，之後才呼叫 `run_analysis()`。generation 在後者才讀取。若圖表載入時使用者 Stop→Start，舊的 start_analysis 醒來後會讀到新 generation，通過後續 stale 檢查並執行交易。最新 scheduler dispatch 修補沒有把 token 傳遞到這個內層入口。

重現：第一個 chart 呼叫內把 generation 加 1 並清除 stop flag；舊呼叫繼續完整的 `start_analysis()`／`run_analysis()` 路徑，trade dispatch=1。模型與圖表 transport 使用 mock；generation 與分支使用真實 production code。

最小修法：將 scheduler 建立的 generation 沿 `start_analysis`→`run_analysis`→執行準備一路傳遞，任何慢操作之後重新檢查同一 token，禁止重新讀取最新 global 值作為舊工作身分。

定位：[analysis.py:521](../webui/components/analysis.py#L521)、[analysis.py:211](../webui/components/analysis.py#L211)。證據：`test_R11_stop_start_during_initial_chart_revives_old_analysis`。

**R12 · P2 — 舊模型請求失敗會停止新排程。**

`run_analysis()` 的 ProviderFailure handler 未先驗證 generation，就呼叫 `mark_provider_stop()`；後者改寫全域 provider_stop_reason 並清空 analysis_queue。成功回傳的 stale result 有防護，失敗回傳沒有。

重現：舊 stream 等待期間切換 generation，新佇列含 NEW-RUN；舊 stream 拋 ProviderFailure 後，新佇列被清空且設定停止原因。

最小修法：exception handler 使用同一 generation 檢查；舊請求可以記自己的失敗紀錄，但不改新排程狀態。並檢查同類全域結果／audit 更新是否使用工作自己的 run_id。

定位：[analysis.py:21](../webui/components/analysis.py#L21)、[analysis.py:431](../webui/components/analysis.py#L431)。證據：`test_R12_stale_provider_failure_clears_new_run_queue`。

**R13 · P1 — 收盤後可送出隔日才執行的過期策略委託。**

`next_due_session()` 對當天只比較排定時間是否已到；17:00 ET 的未完成當日仍標 due。ExecutionService 沒有使用 broker clock 證明 regular session 開盤，`requires_open_market` 僅為 metadata。即使 after-hours quote 新鮮，送出的仍是 GTC market bracket。Alpaca 表示不符合延長交易時段的收盤後委託，會排到下一交易日。[Alpaca eligible trading hours](https://docs.alpaca.markets/us/docs/orders-at-alpaca)

重現：排 11:00 的 round，17:00 ET 呼叫仍 due=True；模擬券商 clock 明確為 closed，執行入口仍 POST，clock GET=0。另從程式確認，`expires_at` 只在 POST 前做本地判斷，沒有券商端對應到期取消機制。隔日 broker 自行成交不會再經過本地 entry gate。

最小修法：對 exposure-adding POST 加 regular-session clock gate，並以 session close 約束 round；收盤後未執行部分保留明確原因，不繼續提交新 entry。已 accepted 但未完成的 entry 到期也必須有明確取消／停機處理，不能讓 GTC 自動延長決策許可。不要順手實作 extended-hours 策略。

定位：[long_run.py:585](../tradingagents/long_run.py#L585)、[service.py:260](../tradingagents/execution/service.py#L260)、[service.py:2306](../tradingagents/execution/service.py#L2306)、[policy.py:19](../tradingagents/execution/policy.py#L19)。證據：`test_R13_closed_session_still_due_and_execution_has_no_clock_gate`。

**R14 · P1 — 新鮮持倉沒有使舊 transition 計畫失效。**

執行時雖有 fresh snapshot，`_planned_order_specs()` 仍依決策時的 planned_actions；新的 `verified_position` 沒有用來拒絕不再成立的 position transition。close quantity 會更新，opening action 的語義卻未重新驗證。

重現：決策是 FLAT→LONG；送單時帳戶已 SHORT 5。系統送 BUY 9 並附 SELL 9 的 stop／target。模擬券商接受後帳戶為 LONG 4，保護單卻是 SELL 9，reconcile 仍 CLEAN。真實 Alpaca 可能直接拒絕此不一致組合；本次未聲稱真實券商必定接受或必定超賣。確定的問題是程式會發出錯誤的計畫，並缺少相應驗證。

最小修法：送單前比對計畫預期的 position side 與 fresh broker side；方向／transition 不再成立就要求重新分析。針對同方向加碼保留現有 cap 行為，不必全面重寫 planner。保護數量還需與真正增加的持倉吻合。

定位：[service.py:1095](../tradingagents/execution/service.py#L1095)、[service.py:1270](../tradingagents/execution/service.py#L1270)、[service.py:1360](../tradingagents/execution/service.py#L1360)。證據：`test_R14_stale_position_plan_can_create_oversized_opposite_stop`。

**R15 · P2 — embedding 路徑可能讓整輪卡住數十分鐘。**

memory 直接建立 `OpenAI(**client_config)`，沒有傳 timeout／max_retries，沒有使用本專案的 request timeout。現行預設已啟用 memory retrieval／reflection，因此這是日常路徑，不只是未使用的功能。

重現本機已安裝 SDK：設定 `llm_request_timeout_seconds=7`，memory client 仍為 `read=600` 秒、`max_retries=2`。同一邏輯請求若每次都 read timeout，等待可能接近 30 分鐘再加 backoff；多個 memory instance 可能各自遇到同樣問題。這是 timeout 設定與程式路徑的驗證，沒有真的等待或呼叫 embedding API。

最小修法：建立 embedding client 時傳入明確、較短的 timeout 與有限 retries；失敗沿用現有停用記憶、繼續本輪的行為。preflight 至少顯示 embedding endpoint 是否配置，若要測試連線也要有同樣時間上限。無需改記憶資料模型。

定位：[memory.py:17](../tradingagents/agents/utils/memory.py#L17)、[memory.py:50](../tradingagents/agents/utils/memory.py#L50)、[default_config.py:15](../tradingagents/default_config.py#L15)。證據：`test_R15_embedding_client_ignores_configured_request_timeout`。

**R16 · P2 — 報告把 unavailable 變成真實的零資產／零部位。**

執行 authority 有嚴格數字檢查，但報告用的 `capture_account_snapshot()` 不同：非有限 equity、缺失 cash 均 `or 0.0`；`get_all_positions() is None` 變成空列表。finalize 只要此函式不拋錯，就記成新鮮 final snapshot。

重現：equity="NaN"、cash=None、positions=None → equity=0、cash=0、positions=[]。這可使最終報告呈現假性的 -100% 報酬與已清空持倉，而非 unavailable。這個缺陷不會繞過執行層的嚴格資產驗證，但會誤導審核結果。

最小修法：報告快照也驗證必要欄位；無法證明時拋出可處理錯誤或保留 None，由既有 `final_snapshot_unavailable` 流程呈現。不要補 0，不要再維護另一份虛構帳戶狀態。

定位：[long_run.py:844](../tradingagents/long_run.py#L844)、[long_run.py:863](../tradingagents/long_run.py#L863)、[long_run.py:2615](../tradingagents/long_run.py#L2615)。證據：`test_R16_report_snapshot_fabricates_zero_on_malformed_broker_facts`。

**完整流程的檢查範圍與判讀。**

| 流程 | 本輪確認的保留項目 | 尚未可宣告通過的原因 |
|---|---|---|
| 安裝／啟動 | CLI、WebUI entrypoint、閉合 lockfile、現有套件煙霧測試、pip check | 本輪未重新建立 Docker image 或 Python 3.11 環境；Windows `fcntl` 相容性也未驗證 |
| Paper 隔離 | 生產 client 固定 paper，拒絕 live 設定與非 paper endpoint；legacy raw signal entry 關閉 | 未實際向券商驗證目前 key／account entitlement |
| 日曆／排程 | authoritative Alpaca calendar、休市／提早收盤、跨日 missed journal、每日去重 | R02、R13；同一天收盤後續跑仍有缺口 |
| Screening | ACTIVE US equity、SIP consolidated bars、完整 61 session window、NaN／缺日拒絕、Top40→Top20 strict schema、cache seal與當日 gate | R01、R08；SIP 權限與當下資料可用性需實際 read-only preflight |
| 分析／決策 | 結構化 RiskDecision、錯誤輸出 NO_TRADE、跨 mode action 檢查、historical/as-of 回歸 | R14、R15；offline tests 不能證明模型策略具正期望值 |
| 風險／Sizing | deterministic symbol／sector／gross／cash cap、remaining orders、stop distance、whole-share rounding | R05、R06、R10；sector_mapping 空時 sector cap 實際不啟用 |
| 下單／恢復 | SQLite outbox、client_order_id、account binding、同主機 account lock、UNKNOWN lookup、SUBMITTING 不盲送、逐筆 refresh | R01、R02、R05、R07 |
| 持倉／平倉 | durable fills、累計成交均價轉增量成本、期限 lots、commit-before-cancel、SHORT 符號修補 | R03、R04、R09、R14 |
| WebUI | scheduler generation dispatch、成功 stale result 抑制、單筆手動 liquidation 使用新 identity | R11、R12；顯示「Successfully liquidated」目前仍可能只代表 close order accepted，需人工核對成交 |
| 記憶／回測 | point-in-time memory cutoff、固定完整 horizon、標示 forward asset move 非 broker P&L、單標的回測 scope | R15；反思文字仍有判斷決策對錯的舊提示，第一輪結果不宜以自我反思取代成交績效 |
| 觀察報告／通知 | observation-scoped LLM 成本、結束時 fresh snapshot、明確 STOPPED／MISSED 記錄 | R16；本機沒有設定有效通知 channel |

現有舊修補的測試均通過，包含 account binding、累計 fill economics、SHORT deadline、SUBMITTING queue stop、initial snapshot age、nested usage accounting、execution-only resume 不重做 screening。這些措施應保留；本輪問題主要在它們的交界，不需要重建整個系統。

**本機進入 Paper 前仍需處理的操作設定。**

讀取到的保存設定是 30 天、每日 11:00 ET、每筆基準 $1,000、五個 analysts、深度 3，`allow_shorts=True`；三個角色使用 `ling-3.0-flash-fin`。`.env` 的 `ALPACA_USE_PAPER=True`，Alpaca key／secret 與 SEC User-Agent 有配置；未輸出任何 secret。沒有 active observation file。

目前 Telegram／webhook 通知沒有在 `.env` 配置；僅 `alerts_enabled=True` 不代表收得到通知。現行預設 daily token budget=0，代表不限額；該模型沒有出現在內建 pricing table，未知價格會列為 unpriced，不能把美元顯示視為完整成本。建議先填入自己的供應商價格與可接受日額，維持單一執行程序，並確認異常能在幾分鐘內被看到。這些是設定與操作準備，不要求新監控平台。

同時應清楚理解目前範圍：entry price range 是送單時的報價約束，不保證 market fill；stop 也不是跳空損失保證。`exit_by` 設計上是到期後第一次排程檢查才退出，不是精準定時執行。Kill switch 阻止後續 API order flow，並不自動撤銷已在券商端的 GTC／保護單。第一輪測試應直接核對 broker orders／positions，不能單靠 UI 的 success 或分析文字。

**避免過度工程化的修補順序。**

1. 修正保護與退出：R03、R04、R09、R14，共用現有 parent／child、outbox、reconcile，逐一驗證 LONG／SHORT、部分成交、取消競態。
2. 修正執行許可的時機：R01、R02、R05、R13，加上 WebUI 的 R11、R12；沿用同一 runtime、generation、expiry、broker clock，不新建 scheduler。
3. 修正明確的狀態／輸入邊界：R06、R07、R08，集中處理 terminal status、HTTP 5xx 與非法隔離事件。
4. 補上 R10、R15、R16 的小範圍修正及通知／預算設定；單程序操作可以先降低 R10 風險，無需分散式儲存。
5. 將重現轉為正式 regression、跑完整 suite，再做有人值守的 Paper 小額端到端演練：確認 entry、stop／target、取消、close、crash／resume 與最終報告一致後，才開始 30 日觀察。

修補與回歸可按 1–2 個工作天安排；真實 Paper 演練另保留一個開市時段。此次完成的是審查與重現證據，production 行為尚未修復。沒有把 mock 通過誤認成真實券商端到端驗收，也沒有承諾已窮盡所有交易所／網路行為。
