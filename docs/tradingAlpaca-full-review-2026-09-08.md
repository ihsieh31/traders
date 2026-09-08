目前不建議啟動 30 日無人值守 Paper 測試。這次審查整理出 19 項問題：10 項 P1、9 項 P2。P1 會阻斷主要流程、影響下單／持倉保護，或破壞恢復與停止控制；P2 會使特定設定失效、資料錯誤或評估失真。

審查基準：`2dccbc0`，日期：2026-09-08。涵蓋 CLI、WebUI、選股、分析與決策、行情資料、風控、下單與對帳、狀態保存、排程／續跑、記憶、回測、成本／報告，以及啟動與封裝設定。203 個受版控 Python 檔案均通過語法解析；功能審查聚焦資料和決策如何流入實際執行流程，不將語法通過視為功能正確。

所有重現都在專案複本或暫存目錄，以假的券商／LLM 傳輸進行；下述金額、股數和送單次數都是離線案例結果。未修改專案程式、設定或測試，未使用子代理或 Codex Security。未讀取實際金鑰、未呼叫真實券商或付費 LLM、未開始 30 日測試。

**F01 · P1：預設 GPT-5 系列轉接器遺失結構化輸出 schema，選股與風控無法取得有效決策。**

`with_structured_output(ScreeningOutput/RiskDecision)` 經由 `bind_tools` 傳入 Pydantic 類別，但工具轉換只找 `.name` 或 `.func.__name__`，因此沒有把這兩個 schema 放進請求，也沒有保留強制工具選擇設定。使用實際轉接器與 LangChain parser、只模擬 OpenAI 回應時，兩者結果均為 `None`；請求沒有 `tools`。預設模型走此路徑時，新選股會以 `SCREENING_INVALID_OUTPUT` 停止；決策端也無法產生有效交易意圖。只要求模型回答 OK 的 preflight 不會偵測到此問題。

位置：[gpt5_llm.py:410](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/utils/gpt5_llm.py:410)、[gpt5_llm.py:548](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/utils/gpt5_llm.py:548)、[screening/llm.py:263](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/screening/llm.py:263)。驗證：離線重現。

**F02 · P1：多筆待恢復訂單重複使用同一份可用額度，合計超過曝險上限。**

恢復迴圈對每筆 PENDING／UNKNOWN 訂單使用同一份 broker snapshot，直到整個迴圈結束才更新；前一筆已送出的委託沒有先占用下一筆的額度。離線設定帳戶權益 $100,000、總曝險上限 3%，放入兩筆各 $2,500 的待恢復意圖，實際服務各送出 24 股、參考價 $100，合計約 $4,800，超過 $3,000 上限。單筆檢查均通過。

位置：[execution/service.py:1607](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:1607)、[execution/service.py:1625](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:1625)。驗證：實際 ExecutionService 搭配假券商重現。

**F03 · P1：計算其他股票的未成交曝險時，錯用目前候選股票的價格。**

未提供 notional、以股數下單的委託，會使用共用的 `reference_price` 估值；計算整個帳戶時傳入的卻是目前候選股票報價。離線案例中，另一股票 10 股、每股 $1,000 的待成交委託，因新候選股報價只有 $10，被計成 $100，而非 $10,000，並被標示為已完整估算；後續新單因而獲准。這使總曝險上限失去可靠性。

位置：[risk/exposure.py:93](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/risk/exposure.py:93)、[risk/exposure.py:221](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/risk/exposure.py:221)。驗證：實際 snapshot／曝險計算流程離線重現。

**F04 · P1：先取消停損保護，平倉失敗後持倉仍在，帳戶卻可維持 CLEAN。**

全數出場先取消本程式的保護單，之後才提交平倉意圖與券商訂單。若平倉被拒絕或本地提交失敗，沒有補回保護，也沒有因缺少保護而必然暫停。離線先建立 9 股與停損單，再令平倉送單被拒絕：結果是 `REJECTED`、仍持有 9 股、停損已取消，帳戶狀態仍為 `CLEAN`，沒有 reconciliation reason。自動出場與手動清倉共用這段取消流程。

位置：[execution/service.py:481](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:481)、[execution/service.py:1904](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:1904)、[execution/service.py:2030](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:2030)。驗證：實際服務與持倉／保護單 fixture 重現。

**F05 · P1：WebUI 清倉識別碼跨頁面重載重複，可顯示成功卻沒有平掉新持倉。**

清倉 decision ID 只包含股票與頁面點擊次數。重新載入頁面後計數重置；同股票重新建倉，再第一次清倉，會命中舊清倉的去重紀錄。服務還會先取消新持倉的保護單，再回傳舊操作的成功結果。離線依序買入、清倉、重新買入、重用 UI 清倉 ID，回傳 `success=True, deduped=True`，但 9 股持倉未減少，存活保護單為 0。

位置：[trading_callbacks.py:129](/Users/zongen/Downloads/codex/tradingAlpaca/webui/callbacks/trading_callbacks.py:129)、[execution/service.py:2038](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:2038)。驗證：離線重現。

**F06 · P1：標示為唯讀的 preflight，會在使用者確認開始 30 日測試之前重送委託。**

CLI 先執行 preflight，之後才顯示本次觀察的確認問題；preflight 內部卻執行 `startup_recover()`。若執行資料庫存在可恢復、券商查無的待送訂單，恢復流程就會送出。離線只執行 `run_preflight`，預先放入一筆待恢復意圖，即記錄到 1 次 `submit_order`，preflight 並回傳成功。因此取消最後的確認，不代表這次前置檢查沒有產生券商操作。

位置：[cli/main.py:1668](/Users/zongen/Downloads/codex/tradingAlpaca/cli/main.py:1668)、[cli/main.py:1692](/Users/zongen/Downloads/codex/tradingAlpaca/cli/main.py:1692)、[long_run.py:923](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:923)。驗證：實際 preflight 與恢復服務搭配假券商重現。

**F07 · P1：選股所用行情 HTTP 請求沒有 timeout，網路停滯可能卡住整輪排程。**

股票及加密貨幣 HistoricalDataClient 未套用 TradingClient 已有的請求 timeout。實際 SDK 的 requests 傳輸被離線攔截時，兩種行情請求都沒有 `timeout` 參數。選股批次同步呼叫這個客戶端，因此網路連線若一直不回應，排程不能進入後續股票、停止檢查或 30 日結束處理。宏觀資料的 FRED `requests.get` 也沒有 timeout。

位置：[alpaca_utils.py:165](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/alpaca_utils.py:165)、[screening/metrics.py:450](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/screening/metrics.py:450)、[macro_utils.py:46](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/macro_utils.py:46)。驗證：行情 SDK 請求參數離線攔截；FRED 為靜態確認。

**F09 · P1：只讀最近 500 筆訂單，較舊但仍有效的保護單會被判定遺失，恢復卡在 PAUSED。**

帳戶快照只取 `ALL + limit=500 + DESC`，沒有分頁或另外補齊舊的有效委託；調節先把舊 ACCEPTED 訂單判為 unresolved，恢復流程又在逐筆查詢之前直接返回。離線保留一筆仍有效的舊停損單，讓最近列表包含 500 筆更新的取消單，即得到 `PAUSED`；雖然單筆查詢仍能找到舊停損，實際查詢次數為 0。已有交易歷史或長時間累積委託的帳戶會遇到，不需要真的遺失保護單。

位置：[execution/authority.py:209](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/authority.py:209)、[execution/service.py:1598](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:1598)。驗證：離線重現；訂單查詢參數另參照 [Alpaca 官方 GetOrdersRequest 文件](https://alpaca.markets/sdks/python/api_reference/trading/requests.html)。

**F10 · P1：停止訊號與觀察截止時間只在外層檢查，當前整輪仍可繼續下單。**

SIGTERM handler 只設定旗標；逐股票分析與送單之前沒有檢查旗標，也沒有檢查 `ends_at`。離線在第一檔分析途中設定停止旗標，第一檔及第二檔都繼續進入執行函式。若一輪跨過觀察截止時間，也仍可能繼續交易。WebUI 有同類問題：Stop 將狀態改成已停止，但目前分析完成後仍會依 `trade_enabled` 交易；單次模式的剩餘佇列也不檢查停止狀態。

位置：[long_run.py:1417](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:1417)、[long_run.py:1204](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:1204)、[long_run.py:1312](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:1312)、[control_callbacks.py:1082](/Users/zongen/Downloads/codex/tradingAlpaca/webui/callbacks/control_callbacks.py:1082)、[analysis.py:375](/Users/zongen/Downloads/codex/tradingAlpaca/webui/components/analysis.py:375)、[control_callbacks.py:1431](/Users/zongen/Downloads/codex/tradingAlpaca/webui/callbacks/control_callbacks.py:1431)。驗證：CLI 停止旗標以實際 round 流程重現；截止時間與 WebUI 為靜態確認。

**F12 · P1：崩潰續跑可能採用同股票、同日期的其他分析決策。**

恢復 ANALYZING 狀態時只用股票與日期尋找最新 completed run，沒有綁定本次 observation、分析開始時間或確切 run ID。若當天曾手動分析，或另一個觀察留下仍在有效期限內的決策，本次尚未完成的分析就可能被替換成該決策並進入交易。離線放入本次 HOLD 與較新的外部手動 BUY 紀錄，恢復函式選到外部 BUY。此函式還固定使用預設結果目錄，沒有使用本次設定的 `results_dir`。

位置：[long_run.py:1017](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:1017)、[long_run.py:1252](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:1252)、[run_logger.py:435](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/run_logger.py:435)。驗證：恢復紀錄選取離線重現，後續執行鏈靜態確認。

**F08 · P2：`llm_request_timeout_seconds` 在角色模式與 GPT-5 工廠失效。**

timeout 只加到 legacy 的參數集合，Analysis／Decision 角色另建客戶端時沒有帶入，Screening 建構也沒有帶入；GPT-5 工廠與 `bind_tools` 即使收到 timeout，也沒有保留。離線傳入 `timeout=7`，模型的 timeout 仍為 `None`，SDK 建構參數也沒有 timeout。結果依賴 SDK 自己的預設等待時間；「最多重試三次」只能限制次數，無法維持設定的請求時間上限。

位置：[trading_graph.py:130](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/graph/trading_graph.py:130)、[trading_graph.py:259](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/graph/trading_graph.py:259)、[screening/llm.py:178](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/screening/llm.py:178)、[gpt5_llm.py:603](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/utils/gpt5_llm.py:603)。驗證：GPT-5 工廠離線重現，角色參數路徑靜態確認。

**F11 · P2：公司事件隔離檔損壞或不可讀時，會被當成沒有隔離紀錄。**

QuarantineStore 遇到 JSON 解析或讀檔錯誤就清空紀錄，並沒有保留「隔離狀態未知」的阻擋結果。離線先建立有效的拆股隔離，確認交易受阻，再讓同一檔案成為損壞 JSON，重新檢查後便不再阻擋。這會在隔離狀態無法讀取時反而開放交易。

位置：[risk/corporate_actions.py:75](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/risk/corporate_actions.py:75)。驗證：實際 QuarantineStore 離線重現。

**F13 · P2：30 日報告的成本混入其他觀察與歷史紀錄，自訂執行資料庫也可能讀錯。**

最終成本報告掃描整個 `results_dir`，沒有依 observation 的分析紀錄或時間窗口過濾。離線把 2025 年的 1,000,000 tokens 放入目錄，新觀察報告的 tokens 立即包含這筆舊資料。另一路執行統計直接以 `execution_db_path` 或硬編預設路徑開資料庫，沒有沿用 ExecutionService 的 `TRADINGAGENTS_EXECUTION_DB`；使用環境變數自訂資料庫時，報告可能讀取／建立另一個空資料庫。這會誤導 30 日運作成本與執行狀況的判斷。

位置：[long_run.py:1761](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:1761)、[long_run.py:1777](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:1777)、[execution/service.py:44](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/service.py:44)。驗證：成本混入離線重現；自訂資料庫路徑為靜態確認。

**F14 · P2：報告的最終資產與持倉，實際上是最後一輪分析後的快照。**

排程到達觀察終點後，finalize 沒有重新取得帳戶快照；`ending_equity` 與 `ending_positions` 直接取最後保存的 snapshot／post_round。最後一輪結束到窗口終點之間的價格變動、停損成交或其他成交，都可能不在最終損益與持倉裡。如果最後一輪在上午、窗口在晚間結束，報告終值就可能落後數小時；若最後幾天沒有完成 round，差距更大。

位置：[long_run.py:1663](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:1663)、[long_run.py:1674](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:1674)、[long_run.py:2180](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:2180)。驗證：靜態確認。

**F15 · P2：選股與部分模型請求漏記 token，成本與每日預算使用量被低估。**

計費事件必須先找到 active run 才會保存及呼叫 `record_llm_tokens`，但每日選股在各股票 `start_run` 之前執行。離線模擬一筆回傳 100 tokens 的實際 GPT-5 轉接器呼叫：沒有 active run 時，預算記錄次數為 0；建立 active run 後，同樣呼叫才記錄 1 次。一般 ChatOpenAI／Google／Anthropic 轉接器與直接呼叫 OpenAI 的資料工具也沒有接到這條共同 usage 記錄路徑。其影響是漏計使用量，不只是某些模型缺少價格表。

位置：[long_run.py:1139](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:1139)、[trading_graph.py:536](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/graph/trading_graph.py:536)、[run_logger.py:227](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/run_logger.py:227)、[run_logger.py:275](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/run_logger.py:275)、[llm_clients/openai_client.py:25](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/llm_clients/openai_client.py:25)。驗證：active run 漏計離線重現，其餘 usage 路徑靜態確認。

**F19 · P2：30 日 CLI 沒有套用每日 LLM 預算阻擋，即使已知額度用完仍繼續分析。**

`daily_llm_token_budget` 的拒絕新分析檢查只出現在 WebUI `start_analysis`；long-run 直接呼叫 graph，沒有對應檢查。離線設定預算 1 token、已使用 2 tokens，實際 SafetyGuard 已回傳 `allowed=False`，但 `run_daily_round` 仍分析 AAA、BBB，整輪狀態為 `COMPLETED`。這與 F15 漏計是兩個獨立問題：即使使用量完全記錄，CLI 仍沒有執行阻擋。預設 0 是無上限；本問題在使用者設定正數上限時觸發。

位置：[default_config.py:110](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/default_config.py:110)、[long_run.py:1274](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:1274)、[analysis.py:450](/Users/zongen/Downloads/codex/tradingAlpaca/webui/components/analysis.py:450)。驗證：實際 round 與 SafetyGuard 搭配假 graph／券商離線重現。

**F16 · P2：宏觀殖利率利差單位差 100 倍，曲線分類錯誤。**

FRED 殖利率以百分比數值保存，兩者相減得到的是百分點，程式直接標示為 basis points，並拿來與 50 比較。離線輸入 10 年期 4%、2 年期 3%，輸出是「1.00 basis points、FLAT」，正確差額應為 100 個基點；依程式自己的 50 基點門檻應落在 NORMAL。啟用 Macro Analyst 時，這段錯誤文字會進入分析證據。

位置：[macro_utils.py:120](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/macro_utils.py:120)。驗證：實際報告函式離線重現。

**F17 · P2：本機日期與美東日期不一致，台灣午夜後的即時分析會被當成未來日期。**

WebUI 用沒有指定時區的 `datetime.now()` 建立分析日，資料入口卻用 America/New_York 判斷是否為未來日期。在台灣午夜、美東仍為前一日交易時段時，WebUI 送入隔日日期而被拒絕。離線固定美東日期為 9 月 8 日，送入台灣已是 9 月 9 日的日期，得到 `analysis date is in the future`。一般 CLI 的預設日期也使用本地日期。long-run 自己採美東排程，因此這項主要影響本機 WebUI／一般 CLI，不是該排程的日期算法。

位置：[analysis.py:214](/Users/zongen/Downloads/codex/tradingAlpaca/webui/components/analysis.py:214)、[cli/main.py:437](/Users/zongen/Downloads/codex/tradingAlpaca/cli/main.py:437)、[interface_utils.py:32](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/interface_utils.py:32)。驗證：日期驗證離線重現。

**F18 · P2：套件提供的 Web 啟動指令指向不存在的模組。**

安裝入口 `tradingagents-web=web_ui:main` 指向專案中不存在的 `web_ui`。離線 `find_spec('web_ui')` 為 `None`，因此使用這個正式 entry point 會在啟動時發生匯入錯誤。直接執行現有的 `run_webui_dash.py` 屬於另一個啟動入口，不受此項影響。

位置：[setup.py:45](/Users/zongen/Downloads/codex/tradingAlpaca/setup.py:45)。驗證：封裝設定與離線模組解析確認。

驗證結果：既有測試 **790 passed、240 subtests passed、2 warnings**，執行時間 17.49 秒；警告來自第三方 websockets／LangGraph。測試是在受版控檔案複本中執行，封鎖外部 socket 連線，並隔離使用者狀態目錄。測試通過表示既有案例通過；上述重現顯示，真實轉接器、混合訂單恢復、停止時點和報告資料歸屬等整合情境仍有缺口。

本報告沒有加入架構重寫、效能微調或樣式整理建議。尚未驗證真實 Alpaca 回應、外部供應商實際延遲、容器建置及真正連續 30 日執行。
