# 30 日交易與 A/B 最終深度審查

審查日期：2026-09-22（Asia/Taipei）。結論：**目前不宜啟動正式 30 日雙帳戶 A/B；存在 8 項 P1、8 項 P2 問題。** P1 代表會破壞實驗有效性、執行狀態判斷或恢復能力，應在正式收集樣本前修正；P2 是需修正的可靠性、排程及驗證缺口。這不是對交易收益的判斷。

本次依 ponytail 原則審查：優先調整既有邊界、重用現有持久化／執行／日曆機制，不建議重寫系統、增加服務或引入新依賴。未使用 Codex Security。只新增審查資料，未修改交易程式，也未啟動交易或以新增案例呼叫真實模型／券商。

## 審查基準與驗證結果

基準為 HEAD `8fa5837f30fced397f14144cff1a4016a4d5c229` 加上當時工作目錄的未提交修改，**不是只看 HEAD**。開始時已有 20 個修改檔案，予以保留。旁附 `source_snapshot.json` 記錄審查完成時程式與測試檔案雜湊、版本及 Git 狀態，供後續比對。

| 驗證 | 結果 | 解讀 |
|---|---|---|
| 原有 `tests/` 測試套件 | 1414 passed、139 skipped、294 subtests passed、2 warnings | 既有回歸通過，不等於 A/B 全流程正確 |
| 本次新增跨模組契約案例 | **17 failed**，對應 R01–R15；R08 有三組輸入 | 是實際行為不符合必要契約，並非測試初始化錯誤；故意保留紅燈供修正驗收 |
| 解除錯誤 WebUI skip 的指定核心測試 | **56 passed**、2 warnings | R16 證實原有 skip 過寬；沒有把所有舊 WebUI 測試強行啟用 |
| `git diff --check` | 通過 | 只驗證 diff 格式，不代表功能正確 |

證據目錄：[review_2026_09_22](/Users/zongen/Downloads/codex/tradingBuffett/docs/review_2026_09_22)。內含 `baseline_pytest.log`、`contract_failures.log`、`rescued_core_checks.log`、`rescued_core_nodeids.json`、兩個可執行檢查檔。

新增案例使用假的模型／券商邊界與暫存資料夾，保留真實 runner、SafetyGuard、evidence validator、Berkshire coordinator 或 SQLite ExecutionStore 等被驗證邏輯；並禁止 socket connect。它們驗證狀態轉移與隔離契約，不代表已做真實成交測試。R10 是持久化呼叫檢查，未進行實體斷電測試。

```bash
PYTHONPATH=. .venv-p2/bin/python -m pytest -q
PYTHONPATH=. .venv-p2/bin/python -m pytest docs/review_2026_09_22/test_final_review_contracts.py -q --tb=short
PYTHONPATH=. .venv-p2/bin/python docs/review_2026_09_22/run_skipped_core_checks.py
```

第二條目前預期 exit 1；修正後應轉為通過。案例留在審查目錄，未混入正常 `tests/` 發現範圍。兩個警告來自既有 websockets／LangGraph 依賴的棄用提示。

## 確認問題總表

| ID | 優先級 | 問題 | 直接後果 |
|---|---|---|---|
| R01 | P1 | 分析切換 A/B 時沒有切換 Safety singleton | token 與額度狀態寫入錯臂 |
| R02 | P1 | paper notional 未納入 campaign／pair 固定條件 | 同一配對可用 $500 對 $5,000 |
| R03 | P1 | `success=True, paused=True` 被當成完成 | 已暫停帳戶仍留下成功配對 |
| R04 | P1 | 無效 Risk 輸出被當成有效 shadow HOLD | 無效決策污染一致率與完成率 |
| R05 | P1 | 總結只讀成功的 `pair_summary.json` | 失敗樣本及成本消失，形成存活偏差 |
| R08 | P1 | 資料來源的錯誤文字／空物件通過 completeness | 沒有可用資料也可建立「完整」凍結證據 |
| R09 | P1 | 未完成配對不阻擋 campaign 往後推進 | 單臂先交易，下一日繼續改變帳戶／記憶 |
| R13 | P1 | 已成交 replay 先遇新單規則，才到去重 | 已成功交易被報為失敗，恢復流程中止 |
| R06 | P2 | 成功 CLI 回傳 exit 1 | 排程器誤判失敗並重試或停機 |
| R07 | P2 | Berkshire 包裝例外導致暫時故障變永久故障 | 兩臂 retry 規則不對稱 |
| R10 | P2 | A/B JSON 原子替換缺少 fsync | 電源／系統崩潰後不能保證恢復檔耐久性 |
| R11 | P2 | 第三次分析成功後，恢復仍先檢查 attempts 上限 | 已存下的決策不能繼續執行恢復 |
| R12 | P2 | 固定的是原始 config，而非解析後的實際路由 | 同 campaign 可悄悄更換模型 endpoint |
| R14 | P2 | A/B 分析入口未執行 LLM budget gate | 額度已耗盡仍開始新分析 |
| R15 | P2 | 排程時間剛好等於提早收盤時間未調整 | 該交易日直接錯過執行 |
| R16 | P2 | WebUI skip 以整個 class 原始碼比對 | 大量仍有效的核心回歸測試被跳過 |

## P1：正式 30 日實驗前必須處理

### R01 — Safety 狀態不是實際隔離

位置：[runner 分析入口](/Users/zongen/Downloads/codex/tradingBuffett/scripts/run_analysis_ab.py:464)、[singleton](/Users/zongen/Downloads/codex/tradingBuffett/tradingagents/safety/guardrails.py:586)。

`build_ab_configs()` 確實給兩臂不同 Safety 路徑，但 graph 的 `set_config()` 不會重建已存在的 SafetyGuard。`_preserve_runtime_config()` 到整個 runner 結束才 reset；paper 路徑雖然 reset，卻是在該臂分析完成、進入執行時才做。

**重現：** R01 在兩臂分析內取得真正的 SafetyGuard 並記錄 token，兩次 `state_path` 都指向 `_profiles/traders/safety/state.json`。執行順序反過來時，問題會依第一個初始化的 guard 改變；也可能沿用前置 evidence 工作初始化的 guard。

**影響：** shadow 下兩臂的 token 合併；paper 下後一臂的分析可能算到前一臂。不同目錄的配置不足以證明執行時隔離。這不等於已證實券商訂單跨帳戶，但足以使額度／成本隔離失真。

**最小修正：** 在每一臂分析開始前安裝該臂 config 並 reset guard；把共同 evidence 收集的使用量明確記在共同階段。重用既有 `replace_config`／`reset_safety_guard`，不用新增依賴注入框架。

### R02 — 恢復時可改變單臂的交易額度

位置：[campaign 建立](/Users/zongen/Downloads/codex/tradingBuffett/scripts/run_analysis_ab.py:926)、[執行參數](/Users/zongen/Downloads/codex/tradingBuffett/scripts/run_analysis_ab.py:1010)。

`paper_notional_usd` 是 runner 獨立參數，沒有成為 campaign fingerprint 或 durable pair 的固定條件。先完成一臂、另一臂 timeout 後，重新傳入另一個額度會被接受。

**重現：** R02 記錄執行參數，得到 `[('traders', 500.0), ('berkshire', 5000.0)]`，兩者仍屬同一配對與 campaign。最後 summary 使用恢復時的 $5,000，不能如實表示第一臂的原始額度。

**最小修正：** 將額度寫入 campaign 固定配置及 pair 狀態；恢復必須一致，或使用已保存的值。不要只在 summary 補欄位，因為那無法阻止條件漂移。

### R03 — 執行後暫停仍被標記成功

位置：[runner 成功判斷](/Users/zongen/Downloads/codex/tradingBuffett/scripts/run_analysis_ab.py:1013)、[執行後對帳](/Users/zongen/Downloads/codex/tradingBuffett/tradingagents/execution/service.py:876)。

ExecutionService 在送單成功後，如果對帳／保護覆蓋檢查不乾淨，可以保留 `success=True` 並追加 `paused=True`。這是「送單結果」與「可否繼續」兩種不同事實。A/B runner 只看 success，會把它寫成 completed。

**重現：** R03 傳回 `success=True, paused=True`，兩臂仍完成並產出成功 summary。現有 `_execute_paper_arm_inner()` 也會轉送這兩個旗標，因此不是不可能發生的 mock 形狀。

**最小修正：** 完成判斷必須同時處理 paused／unknown，保存對帳理由並阻止 campaign 繼續。重用 long-run 的 hard-stop 語義；不能把 UNKNOWN 直接改成成功。案例使用 terminal 狀態作為最小可接受的停止行為，若採明確的可恢復 PAUSED 狀態，也必須保持不計為完成。

### R04 — 失敗的 Risk 決策被統計成 HOLD

位置：[分析結果封裝](/Users/zongen/Downloads/codex/tradingBuffett/scripts/run_analysis_ab.py:469)、[Risk 無效分支](/Users/zongen/Downloads/codex/tradingBuffett/tradingagents/agents/managers/risk_manager.py:214)。

Risk 在 schema／結構化回應失敗時，安全地產生 `final_trade_intent=None` 與 `risk_invalid_reason`，並回到不交易訊號。但 `_run_one()` 只要 `propagate()` 回傳就標成 completed，且只保存 state keys，未保存無效理由。paper 路徑還有執行端檢查；shadow 沒有。

**重現：** R04 的兩臂都回傳無效 intent 與 HOLD，runner 仍產出成功配對；此時相同 HOLD 也會被視為一致。歷史日期缺少歷史 portfolio context 的分支同樣需要區分。

**最小修正：** 保存 decision validity 與 reason，將無效決策列為失敗／無效觀察，從有效訊號一致率分母分開。維持 Risk 拒絕交易的保護，不要靠放寬 schema 修補統計。

### R05 — 總結遺漏失敗觀察，完成率失去意義

位置：[載入樣本](/Users/zongen/Downloads/codex/tradingBuffett/scripts/summarize_analysis_ab.py:25)、[統計迴圈](/Users/zongen/Downloads/codex/tradingBuffett/scripts/summarize_analysis_ab.py:127)。

`pair_summary.json` 只在兩臂完成後產生，summarizer 又只掃這些檔案，且要求兩臂 status 均為 completed。因而後面的 `failed += 1` 正常情況下不可達；PARTIAL／FAILED_TERMINAL 的 `pair_state.json` 不進入分母。對目前被納入的有效 summary，`completed_rate` 必然為 1。

**重現：** R05 建立一個成功配對與一個失敗配對，總結仍回傳 failed=0。只讀最終 run log 也遺漏先前 retry 的成本；缺少／損壞 run log 被轉為空物件，使用量則成為 0。

**最小修正：** 以 pair state 為觀察清冊，再連結 summary 和所有 attempt logs。分別報告嘗試、有效完成、無效、未完成與失敗；缺失 telemetry 應標為未知。保留完整配對的 signal comparison，不讓它取代 campaign 完成率。現有 JSON 足夠，不需要分析資料庫。

### R08 — 凍結的是資料位元組，不保證是有效證據

位置：[來源狀態](/Users/zongen/Downloads/codex/tradingBuffett/tradingagents/experiments/evidence_snapshot.py:138)、[完整性 gate](/Users/zongen/Downloads/codex/tradingBuffett/tradingagents/experiments/evidence_snapshot.py:303)。

目前只排除 `None`、空字串與空 list；工具回傳的錯誤文字、空 dict、error dict 都可被標為 available。工具 wrapper 確實存在將 timeout 等錯誤轉成文字回傳的路徑。

**重現：** R08 三組值 `Error: Tool timed out`、`{}`、`{'error': 'unavailable'}` 全部通過 completeness。只要每個 section 都有這類內容，就能以有效 hash 進入兩臂。hash 只能證明兩臂拿到相同內容，不能證明內容可用。

**最小修正：** 在來源 adapter 保留明確成功／失敗狀態，拒絕空結構與已知錯誤 envelope，再由 completeness 使用這些狀態。不要建立龐大的文字猜測系統。另應在發布可重用 evidence 檔前通過 completeness；現在先保存後拒絕，重試會反覆載入同一個不完整 packet。

### R09 — 一日的未完成配對不會阻擋下一日

位置：[只載入當前 pair](/Users/zongen/Downloads/codex/tradingBuffett/scripts/run_analysis_ab.py:947)。

runner 只檢查本次 symbol/date 的狀態；同 campaign 的其他未完成 pair 不構成 gate。paper 模式下，一臂可能已經成交，另一臂仍 PARTIAL，操作者／外部排程仍可直接跑下一日。新一日又改變帳戶及持久記憶，之後補跑舊 pair 就不再是原本的配對條件；舊日期的 paper preflight 也可能使補跑無法進行。

**重現：** R09 先建立 2026-09-21 的單臂完成／另一臂 timeout 狀態，再以同 campaign 執行 2026-09-22，仍完成兩臂。測試隔離券商 preflight，直接驗證缺少 campaign 層 gate；不是實際跨日送單。

**最小修正：** 啟動後續日期前檢查尚未結束的 pair；要求先恢復，或以明確的 campaign 終止／缺樣本規則收束。對會改變持久狀態的配對，不應默默越過。只需掃現有 state，不必另建工作佇列。terminal 後「換 results-root」也不是充分修復，因為帳戶可能已有持倉，無法重新符合平倉起跑條件。

### R13 — 已成交交易的恢復會在去重前被拒絕

位置：[持倉變化檢查](/Users/zongen/Downloads/codex/tradingBuffett/tradingagents/execution/intent_execution.py:253)、[outbox／去重](/Users/zongen/Downloads/codex/tradingBuffett/tradingagents/execution/intent_execution.py:354)、[entry policy](/Users/zongen/Downloads/codex/tradingBuffett/tradingagents/execution/service.py:668)。

A/B 在 broker work 前保存 `analysis_completed`，預期崩潰後能重播同一 decision id。但在 broker 已成交、pair 尚未更新的窗口中，舊 intent 假設 NEUTRAL，broker 現在 LONG，執行核心會先回 `stale_position_transition`，未走到已存在訂單的 dedup。外層也會在確認既有 intent 前檢查 entry expiry。

**重現：** R13 使用真實 SQLite outbox，將相同 decision 的訂單轉至 FILLED，再給 LONG 的 broker snapshot，得到 success=false／stale_position_transition，而不是辨識已完成的相同決策。

**影響：** 本次證據證明錯誤拒絕與恢復失敗，沒有證明重複送單。這個拒絕本身能阻止新單，但不能作為恢復正確的證據。

**最小修正：** 在帳戶鎖內先查既有 decision、核對 payload 並依 durable 狀態／新鮮券商事實處理 replay；只有新的、尚未送出的 opening leg 才套新單條件。不能為了提早 dedup 而將 UNKNOWN 或 PENDING 無條件當成完成。

## P2：可靠性與驗證缺口

| ID | 來源與重現 | 最小修正與驗收 |
|---|---|---|
| R06 | `scripts/run_analysis_ab.py:1140`：成功回傳 shape 是頂層 traders／berkshire，但 main 尋找 status／arms。R06 的真正成功 summary 使 main 回傳 1。 | 統一成功 return shape 或讓 main 正確辨識既有 shape；成功與已完成 replay 必須 exit 0，PARTIAL／terminal 非零。 |
| R07 | `tradingagents/analysis_backends/berkshire/coordinator.py:89` 捕捉角色例外，包裝為 BerkshireAnalysisError；runner `:481` 以類名判斷 retry。R07 的 TimeoutError 變為 failed_terminal；Traders 的同類 timeout 可 retry。 | 基礎設施例外保留原型或明確分類；語意／schema 錯誤保持 terminal。亦須讀 ProviderFailure 的分類，不能把永久設定錯誤一律 retry。 |
| R10 | `scripts/run_analysis_ab.py:697` 以 write_text + replace 保存 state，未 fsync；evidence／manifest 也有相同持久化問題。R10 捕捉到 0 次 fsync。 | 重用 `long_run_support/state.py` 已有的 file + parent directory fsync 原子寫入。rename 已提供原子可見性；此問題專指系統崩潰／斷電後的耐久性，不宣稱普通程序退出必然遺失檔案。 |
| R11 | runner `:974` 先檢查 attempts>=3，才看 analysis_completed。R11 前兩次 timeout、第三次分析完成而執行被中斷，resume 立即變 terminal。 | 上限只限制新分析；已保存的 analysis_completed 仍可進入執行恢復，且不得再消耗 LLM。 |
| R12 | runner `:299` fingerprint 原始 config；實際模型／embedding 路由會再讀環境變數。R12 切換 localhost endpoint，resolved role 已變，但同 campaign 接受。 | 固定解析後的非秘密 provider、URL、模型與 embedding 設定；API key 不寫 manifest。依賴版本另記錄供重現。不需要另建設定系統。 |
| R14 | runner 的分析呼叫沒有 budget precheck。R14 在兩臂持久 Safety 狀態各記 11 tokens、budget=10，確認 check_llm_budget 拒絕後，兩臂仍開始分析並完成。 | 每次新分析／retry 前調用現有 budget gate；共同收集與 reflection 的使用量也應有明確歸屬。預設無上限和「有設定上限卻不執行」要分開。 |
| R15 | `tradingagents/long_run_support/sessions.py:38` 只在 target>close 調整。R15 注入 close=13:00、target=13:00，仍回傳13:00；到點時 session 已收盤。 | 改為包含等於的邊界，並驗證會調整至12:30。這是允許配置的邊界案例，不宣稱目前預設排程一定中招。 |
| R16 | `tests/conftest.py:20` 同時拼入 method 與整個 class 的原始碼，以 webui 等字串決定 skip；一個舊方法使整班核心測試被跳過。 | 以明確已移除模組／測試 ID／marker 表示淘汰範圍，避免全 class 文字搜尋。另檢查文件對139 skipped的說法；補跑的56個有效核心檢查均通過。 |

## 系統設計與 30 日定義仍需釐清的邊界

以下與上面的可重現缺陷分開，不把尚未定義的產品需求冒充程式 bug。

| 範圍 | 審查判斷 |
|---|---|
| 30 日自動協調 | `run_analysis_ab.py` 每次只處理一個 symbol/date；現有 `cli long-run` 是獨立的篩選／交易流程，沒有自動連接為30日雙臂 campaign。尚不能把單 pair runner 當作完整30日排程器。應明確固定起迄、交易日清單、股票清單、missed／partial 規則；重用既有日曆與 runner，無須再做一套 scheduler。 |
| 要測什麼 | 自己的帳戶、記憶與回饋持續演化，是自適應策略實驗的合理設計；但結果不是「僅更換一次分析 prompt」的純比較。請將同一 evidence 下的決策比較，與兩套策略30日組合績效分開解讀。各臂正常演化造成的不同持倉，不應被誤判為資料污染。 |
| 五次分析呼叫的公平性 | Traders 五個 analyst 與 Berkshire 四個角色加一個 lead，呼叫數相近不代表 token／上下文／實際成本相同。Berkshire 角色可讀整包，Traders frozen analyst 讀對應 section。要固定的是明確的模型與預算政策，不能宣稱每次成本相等。 |
| Berkshire 確定性計算 | coordinator 尋找 `fundamentals['calculations']`，但目前共同 collector 沒有產出此欄位。計算 helper 的存在／單元測試通過，不足以宣稱正常 campaign 已執行數值交叉驗證。若要作為實驗前提，需有來源可追溯的數字輸入與至少一次接通驗證，不能讓 LLM 自行捏造欄位。 |
| 學習與 telemetry | reflection 在主要 run logger 啟動前可能執行；共同 evidence 收集也在兩臂 run 外。僅加總兩份最終 run log 不是完整實驗成本。memory_enabled 也不代表 embedding／回饋成功；缺資料時的降級狀態應可觀察。 |
| Frozen Traders 的含義 | frozen analyst 使用自己的通用 guidance 與 frozen section，並非原始 live-tools analyst 完全不變的執行方式。應把實驗名稱與結論限定為實際跑的 frozen adapter 對 Berkshire team。 |
| 歷史 replay | 新抓歷史日期時，live-only social 來源會 unavailable；有當日保存的 packet 才可能重播同一證據。Risk 沒有歷史 portfolio context 時仍會安全退回不交易。不可將這類 HOLD 當作完整歷史決策驗證。 |
| 結束時持倉 | long-run 最後報告採讀取／觀察，不等於自動清倉；exit deadline 依維護執行時機檢查，也不是全天候獨立服務。30日觀察結束不必強制賣出，但結果須包含未實現部位與後續管理安排，避免把有持倉的結束狀態當成完全結案。 |
| 績效與精確度 | 現有 A/B summarizer 主要是訊號與 telemetry，沒有完成以起始資金、資金流、持倉市值、費用及共同估值時間計算的30日帳戶績效。訊號一致率不是正確率，也不是獲利能力。順序執行兩個帳戶的成交時點差異需保留為實驗限制。 |
| 文件版本 | `DUAL_30D_FINAL_REVIEW_2026-09-21.md` 的舊 prompt-profile／shadow-only 結論不足以描述本次改後程式；應當作歷史紀錄，不能當作最新驗收。本報告針對當前工作目錄。 |

## 已有且應保留的合理基礎

帳戶 A/B 的專用 credentials 不會回退到預設帳戶；paper factory 固定 paper-only，並對執行 HTTP 設定 timeout、禁止 SDK 自行重試 POST。execution DB 有帳戶綁定、outbox 交易邊界及穩定訂單識別；帳戶鎖、UNKNOWN recovery、保護單 ownership／取消競態與保護缺口檢查已有實作。這些是重要基礎，無須為本次問題重寫。

兩臂共享 frozen evidence hash、不同的 execution／memory／results 路徑、下游共同 Trader／Risk／Execution 節點，以及程式／prompt fingerprint 的方向合理。問題主要在「配置聲明」與「實際執行邊界」尚未完全接合；單純增加更多欄位或類別不會自動修好。

閱讀與交叉檢查涵蓋 campaign 配置／續跑／summary、資料抓取與凍結、兩種分析 backend、graph 與角色路由、Trader／Risk、持久記憶與回饋、Safety、paper factory、執行／outbox／對帳／恢復／保護單、long-run 日曆與狀態／報告，以及相關測試與目前未提交 diff。這是主要運作路徑的深度審查，不宣稱已窮舉所有第三方 SDK、所有市場事件或所有可能故障。

## 最小修正順序與再次驗收

1. **先修隔離與固定條件：** R01、R02、R12、R14。每臂使用自身狀態，恢復時的執行額度與實際模型路由不變。
2. **再修執行恢復與停止：** R03、R09、R10、R11、R13。已成交不能重新當新單；paused／unknown 不計完成，也不能默默跨日繼續。
3. **修資料與統計有效性：** R04、R05、R07、R08。無效資料、無效決策、重試與失敗樣本都要保留；同類故障兩臂遵守同一分類。
4. **修入口與驗證範圍：** R06、R15、R16，並明確定義30日起迄與協調入口。優先既有 helper 和條件修正，不新增服務／資料庫／抽象框架。
5. **再次驗收：** 原套件、17個契約案例、56個恢復的核心檢查全部通過；再做隔離 paper 整合驗證，至少涵蓋有效開倉、已成交後中斷恢復、UNKNOWN／PAUSED、跨日缺樣本與終止持倉報告。本次未進行真實券商驗證，只有 HOLD 的 smoke 也不足以取代這些情境。

審查已完成；以上缺陷尚未修正。先關閉 P1，再用同一批可重跑證據驗收，才有依據開始正式收集30日實驗樣本。
