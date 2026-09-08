# 策略一致性修復計畫

本輪目的：修正可由規格與數學確定的問題，建立可判讀的研究結果；不聲稱證明收益，不改選股因子權重，也不啟動 broker 或 LLM。

1. 交易契約：明確的 READY/WAIT、可接受價格區間、進場截止時間、退出截止時間、數字停損與帳戶風險預算。未核實的條件不得變成市價單。恢復重送同樣驗證契約與保護單。
2. 分析：修正 Wilder 指標與 SMA200 暖機資料；補 SEC 工具與時間過濾、FRED vintage；證據分數標示為啟發式閱讀優先度，不視為可信度或勝率。
3. 學習：預設不讓未驗證的反思影響決策；固定次日開盤至完整 N 根 bar 的標籤、保留方向語意與持倉，區分假設報酬與實際損益；記憶按可取得時間過濾。
4. 績效：隔離測試紀錄、排除事後重跑、保留同日首次合格決策；修正 NEUTRAL；分窗結果標示為診斷。完整策略獲利只由前瞻帳戶淨值與成本支持，單股訊號重播不冒充完整 portfolio backtest。
5. 驗證：只做相關離線回歸；記錄尚需市場資料才能回答的問題。原始歷史檔案不刪除，不自動再訓練。

後續市場研究：固定版本與風險預算，同期比較被動持有、固定因子、單模型、辯論、記憶。3–6 個月是初步觀察時間，不是統計保證。所有研究費用與交易摩擦都需計入。

## 已完成與行為變更

- Risk Manager 接收實際 Trader 計畫及 UTC 決策時間。TradeIntent v2 保留進場與持有條件；READY、有效期限、價格區間、絕對停損價缺一不可。新契約不會替舊決策自動補上授權。
- 開倉、同方向加碼與重啟重送都受帳戶風險與集中度限制。以真實停損及授權區間最不利價格限制金額，再向下取整為可附保護單的股數。數字停損不再從「2 ATR」「8%」或多個目標中猜測。沒有校準資料就不用 Kelly；可選 sizing 模組失敗時停止開倉。
- 股權開倉必須有券商 OTO/bracket 停損。停損拒絕、空白回覆或無法建單不會轉送裸市價單。SQLite v3 新增 protective_children 關聯，僅接納券商母單證明的子單，使停損成交正確抵銷持倉。
- 一般退出會取消已確認屬於本系統的保護單，再重新核對持倉；取消仍在進行中時不再送平倉。到期退出使用實際成交重建剩餘持倉，受相同安全開關限制。新舊加碼共用同一股票部位，最早到期會退出該股票全部可核實的部位。
- SMA200 日線資料改為 420 個曆日；缺值保留 null。修正 Wilder ADX、ATR、RSI。SEC 使用 acceptance time／保守的日期可得時間，排除截止時間後資料；歷史日不抓現在的 IR 網頁。FRED 使用當日 vintage。SEC 目前仍是文件來源與時間資訊，不代表已驗證財報數字。
- 報告來源與新鮮度分數只作閱讀排序；角色數量不當作獨立來源數，無日期或未來事件不冒充新近發布。
- 記憶檢索與自動 outcome reflection 預設關閉。明確啟用後，仍只能讀取分析日期前已寫入的記憶。固定完整持有期的資產變動／假設部位報酬，不再冒充券商損益或生成單次成敗的因果教訓。
- 評估紀錄遵守 results_dir，寫入不會退回固定的 eval_results；初始化也不再擅自改寫其他程序可能正在使用的 running logs。訊號診斷排除無模型／工具事件的 fixtures、跨日期重跑及標記的歷史模式，同日取首次合格決策。

## 仍然需要市場驗證的部分

1. 辯論是否勝過單模型或固定規則、選股是否有超額收益、LLM 信心是否可校準，仍須固定版本的前瞻對照；本輪沒有調整因子權重來追逐既有結果。
2. 現有 backtest 是單股訊號診斷，未重現完整組合、保護單、全部交易與研究成本。帳戶淨值還需校正入出金並扣除未反映的費用，才能評估策略淨收益。
3. READY 的非價格確認目前由 Risk Manager 根據報告填入，並非逐項資料來源的確定性驗證。報告評分與來源 metadata 不能保證內容正確；SEC 完整財務 facts 與全資料來源的逐時點快照仍需另行建置。
4. 市價單的價格區間檢查只約束送單前報價；跳空、滑價與停損穿價仍可能超出計畫風險。退出期限依排程檢查，離線或休市不保證準時成交。持有保護單的反手交易需先退出、取得新決策後再開相反部位。
5. 目前新增保護契約適用美股；crypto 新增曝險會拒絕，既有部位退出不受此開倉限制。此輪未啟動程式、LLM、市場實驗或真實券商操作。

## 當前狀態補充（DVI-1）

- Top40 權重是 research baseline，不是 validated alpha；本輪未做任何 alpha 統計或因子驗證。
- 30 天無人值守 observation 是 operational observation（未調整的帳戶權益變化，`return_kind: unadjusted_account_equity_change`），不是 profitability proof：未調整入出金、可能含觀察期前既有部位、非純策略歸因，且未計入全部研究與交易成本。
- Analyst 只輸出研究報告（as-of、證據、多空啟示、缺口、horizon），不再產生 BUY/HOLD/SELL 等 final action；selected analyst 缺報告即 fail closed。Broker `last_equity` 為 daily-loss baseline，仍可能受入出金影響；未實作 cash-flow adjusted TWR。
- 既有歷史驗收紀錄保留原樣，不刪除也不重寫。

## 本輪驗證結果

2026-09-07：23 個直接相關測試檔案，共 476 tests passed、114 subtests passed。涵蓋分析指標、來源時點、記憶隔離、訊號語意、保護單／到期退出、重啟恢復、曝險上限及排程整合。3 個警告均來自既有第三方套件的棄用通知。所有修改 Python 檔案 AST 語法檢查通過；依既有 CRLF 檔案慣例檢查 Git 差異，沒有新增空白格式錯誤。這些檢查驗證程式契約，不是策略收益實驗。

## DMC-1 實作說明與限制（2026-09-08）

本節依 `docs/05_DEBATE_MINIMAL_IMPLEMENTATION_PROMPT.md` 追加，記錄辯論與交易計畫最小一致性修復的實作範圍、驗證結果與仍未解決的限制。不改寫歷史章節。

### 已實作（A–D）

- **A（排序只作排序）**：`report_context.py` 渲染層改為 `Heuristic Claim Reading Guide:`／`Heuristic Claim Priority Matrix`；移除 rendered text 的 `Net: ... (confidence)` 聚合、bull/bear 聚合分數與 manager guidance 值；欄名改為 `priority=/date_hint=/numeric_hint=/overlap_hint=`；`Claims by source` 移除報告級 `[Bullish/Bearish/Mixed]` 總結；memory context 移除聚合方向段落。新增 direction labels 與 overlap hints 的否定說明。`_initial_claim_scores`、`_finalize_claim_score`、`_apply_contradiction_scores`、`_side_score`、`_confidence_label`、`_build_evidence_scoreboard` 的計算與 JSON keys（含 `net_direction`、`net_confidence`、`manager_guidance`）全部保留於 structured metadata，未當成新校準結果。researchers/risk/managers/trader_context 模板同步改名 `Heuristic claim priority matrix`、`Analyst reports or retrieved excerpts`，移除「依高分採信」「scoreboard mixed 降信心」「矛盾分數高等待」句子，改為依原始證據與實際衝突判斷。
- **B（Trader 補寫有界且保留上下文）**：刪除首次回覆後因長度不足而以 `trader_fallback_plan` 無上下文補寫的整段。首次結果必須為 non-empty string，否則 `ValueError("Trader returned empty or invalid analysis")`。缺 final action 時改用既有 `extract_recommendation` 判斷；取得到則直接 `ensure_final_transaction_proposal`（零額外呼叫）；取不到才允許一次 contextual completion，傳入原 messages 拷貝 + assistant 原分析 + user 補全要求（`trader_final_decision.md` 新文案）；補全後仍無 action 則 `ValueError("Trader final action unavailable after one contextual completion")`，不默認 NEUTRAL/HOLD。ProviderFailure 任一處原型別向上傳播。正常 1 次、缺 action 最多 2 次 logical calls。
- **C（持倉能力誠實界定）**：trader_context/investment/trading 模板不再要求系統 trail stops，明示 pipeline 不自動 trail/replace 保護單；trader_context 與 risk_manager 加入相同能力界線段（maintain 不加減碼/不更新保護單/不重置期限；advisory stop/target 不送單；未知舊停損/目標/論點/期限標記 unavailable、不由未實現損益推測；本路徑僅支援 maintain 或 full exit，partial resize 不可執行；full exit 仍受既有執行檢查）。position_logic 兩處「有利價位平倉」改為「請求完整平倉並受既有執行檢查」；trading 模式 NEUTRAL 說明改為本標的平倉或保持空手；新增 protected reversal 說明（需先核實退出再 fresh decision）；investment BUY/SELL 說明同方向維持、SELL 為完整退出。schemas/execution 無任何 diff。
- **D（風險辯論失敗不可偽裝完成）**：三個 risk debator 在組字前檢查 LLM content 為 non-empty string，否則 `ValueError("<Role> risk analyst returned empty or invalid content")`；驗證通過才 append history/messages 並 count+1。`_create_parallel_risk_round_one_coordinator` 兩層 catch 修正：worker 一般例外改設 UI status `pending` 後 bare raise，不再回傳 local_state；`future.result()` 一般例外取消未開始 futures 後 bare raise，不再把 deepcopy(state) 併入 completed_results。ProviderFailure 仍原型別傳播，UI 同樣不標 completed。任何失敗不會走到 merged return；成功回合維持 Risky→Safe→Neutral 合併順序、count+3、latest=Neutral。

### DMC-1 驗證結果

- 指令：`.venv-p2/bin/python -B -m pytest -p no:cacheprovider tests/test_debate_minimal_consistency.py tests/test_report_context_scoring.py tests/test_prompt_templates.py tests/test_structured_decisions.py tests/test_phase_b_retry_stop.py tests/test_phase_b_position_context.py tests/test_decision_validation_integrity.py tests/test_strategy_consistency.py tests/test_trading_graph_mocked.py -q`
- 結果：139 passed、167 subtests passed（2 個既有第三方套件棄用警告），退出碼 0。`git diff --check` 退出碼 0；改動 Python 全數通過 stdlib `ast.parse`（未產生 pyc）。
- 新增 `tests/test_debate_minimal_consistency.py`（B01–B05、C02、D01 及 Trader 失敗的 graph integration：真實 Trader node 經真實 StateGraph 擲出 ValueError 時，Risky/Safe/Neutral/Risk Judge/execution spy 計數皆為 0）；A01/A02 併入 `test_report_context_scoring.py`；A03/C01 併入 `test_prompt_templates.py`；D02/D03 併入 `test_phase_b_retry_stop.py`。測試全部離線：fake LLM/memory、patch prompt capture、臨時隔離、socket blocker 保底；移除 `TRADINGAGENTS_PROMPT_DIR` override 以使用內建模板，原有 override 測試保留。

### DMC-1 仍未解決的限制

- 本輪不聲稱提高收益、資料已逐項查證、trailing 已實作，或已通過獨立驗收（驗收由 `docs/06_DEBATE_MINIMAL_ACCEPTANCE_PROMPT.md` 另行執行）。
- READY 的非價格條件仍由 Risk Manager 依報告認證，不是逐項資料驗證；啟發式 priority score 仍不是來源驗證、機率或勝率。
- Trader 對既有持倉的原始論點/停損/目標/期限若不在 supplied context 中，僅誠實標記 unavailable；完整持倉論點追蹤（thesis ledger/memory 接線）未實作。
- 辯論結構（同模型、角色分工、輪數、路由）與因子權重/序列配置為可比較策略選擇，本輪未改動、未驗證其績效影響。
- coordinator 取消只停止未開始的 futures；已開始的同輪模型呼叫可能完成，但任何失敗都會使該回合整體向上拋出、不會合併成成功決策。
