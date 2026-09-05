# P2／Phase B 獨立驗收提示詞

唯一任務：驗收P2全部原有資料品質工作、雙角色Provider、retry、持股context與風險整合；不修復、不開始P3。

## 獨立性與工作規則

將本文件完整交給新的獨立 task；工作目錄 `/Users/zongen/Downloads/codex/tradingAlpaca`。先讀適用 AGENTS.md、`ponytail` skill（full）、`PROJECT_GOALS_AND_STATUS.md`、本階段完整 implementation prompt、README、ARCHITECTURE、實作 diff、相關 source/tests。implementation prompt 的公式、設定、邊界都是 mandatory contract，本矩陣不是刪減版替代規格。

只讀驗收：不修改 repo code/tests/docs、不 apply_patch、不 commit/push/reset、不切 branch、不刪使用者檔案。可以跑既有tests，以OS temporary目錄放一次性PoC、SQLite、cache；不得藉驗收修程式。實作時參與者不能兼任獨立驗收者。若發現缺陷，報告直接證據並停止依賴該前提的檢查，其他獨立檢查可繼續；修復需另一task，修後重新fresh acceptance。

禁止真實 LLM/broker/webhook/Telegram call；使用fake transport與mock broker。這是離線功能驗收，不要求新真實Paper訂單，也不宣稱證明真實vendor可用性、live資料完整性、盈利或長期無人值守穩定。前置P1 Paper evidence獨立列出，不把它冒稱本輪取得。

記錄 `git status -sb`、`git rev-parse HEAD`、base SHA與diff、前置accepted revision與證據。dirty實作允許在來源可識別且完整diff固定時驗收，記diff hash/未追蹤檔案hash；未知或驗收中變動的範圍＝prerequisite failure。歷史綠test totals、checkbox、實作者口述不能代替本輪證據。

每列必須 Pass/Fail/Blocked＋source位置＋test/PoC實際結果。不能只寫「有這個函式」；追 public caller、failure propagation、execution gate。優先重用可直接证明的既有tests；缺少關鍵反例時寫temporary PoC，不能補永久測試掩蓋實作缺口。任何mandatory項目缺證據都不能Accepted。

## 前置條件

P1 A6 Accepted的revision/evidence可核對，P2實作範圍可固定。Paper observation 不屬於 P2／P3 離線驗收前置；不得僅因 observation 尚未開始或缺少人工 watchlist 長期運作證據而給 Not Accepted。啟用長期無人值守 Paper 自動交易前仍須完成 observation；本輪 Accepted 不代表該運行要求已滿足。

## 強制驗收矩陣

| ID | 必須直接證明 | 最低測試／反例 |
|---|---|---|
| B01 | 新模式Analysis覆蓋全部研究節點，Decision只在Risk Manager | 不同fake models捕捉所有node注入；Research Manager/Trader不可誤走Decision |
| B02 | 舊config保留quick/deep，新role解析一致 | legacy only、全部role、僅Analysis、跨provider缺model、空值、invalid provider |
| B03 | 角色endpoint/key/kwargs隔離，UI/CLI持久化生效 | 兩個不同provider+custom URL；local開關不能覆寫explicit role；secret不落log |
| B04 | retry只有一層，0–3 retries且有timeout上限 | 各adapter family transport：前三次transient第四次成功；四次皆敗无第五次；retry=0一次 |
| B05 | permanent與schema錯誤不做access retry | 401/403/invalid-model各一次；成功invalid schema零repair/free-text request |
| B06 | structured/tool runnable也守同樣retry | fake transport實際計數，不只assert constructor kwargs |
| B07 | parallel analyst/risk及所有catch不吞Provider錯誤 | 單一analyst耗盡，Research/Trader/Decision/execution均未啟動；並行late result無下游用途 |
| B08 | Trader/Decision失败整輪stop且operator可見 | UI/CLI/scheduler status=error/stopped，role/model/attempts正確；後續symbol與下輪auto不dispatch |
| B09 | strictRisk schema仍NO_TRADE；access failure可辨別 | 三種bind/invoke/validation失敗分流；無新intent/POST、不復用checkpoint舊final |
| B10 | 持股context計算與刷新真實接到兩個prompt | equity100000、NVDA28000→28%；多持倉gross正確；Decision使用較新的snapshot |
| B11 | 空倉和資料未知區分 | 完整positions=[]可flat；timeout/stale/future/wrong-account/NaN/equity0全部停止 |
| B12 | prompt只注入Trader/Decision | Analysts/Bull/Bear/Research prompt沒有直接帳戶dump；memory不覆寫snapshot |
| B13 | 沿用單一canonical symbol cap並clip | 18%+5%、cap20%→<=2%；28%禁止加倉；向下取整與最小單位 |
| B14 | cap在execution/recovery入口有效 | directcaller/兩筆順序委託/openorder/恢復resubmit不得重用headroom；不確定pending值拒絕 |
| B15 | reducing exit與flip安全 | 超cap合法verified減倉允許；flip新開仍受cap；不繞過Phase A |
| B16 | SEC/IR接入真實dataflow而非dead helper | fixture索引→官方document→metadata→Analyst；缺URL不猜、wrongCIK不認 |
| B17 | source/time/fallback誠實 | stale/future/missing日期、非官方redirect、timeout；retrieved新但published舊不可冒充新資料 |
| B18 | quarantine持久且守實際execution | split/ticker-change/delisting/nontradable逐項阻擋新風險；restart保留、不能TTL自解 |
| B19 | action解除與adjustment安全 | reconcile未CLEAN不能解除；人工確認與fresh事實齊備才解除；raw/adjusted混用拒絕 |
| B20 | sector上限與unknown fail closed | equity100000、sector已有28000、cap30%、提出5000→<=2000；unknown持股sector拒增加 |
| B21 | 三種cap合併取最小，不被UI/config繞過 | symbol/sector/gross/cash/order不同最嚴限制；非法/uncapped自動模式拒啟用 |
| B22 | correlation/regime/memory沿用且有直接證據 | 高相關/不同regime fixture；memory maintenance/reflection既有回歸；Kelly仍關 |
| B23 | Phase A安全回歸 | Paper-only、idempotency、durable commit、UNKNOWN、freshness、lock、CLEAN、verified exit |
| B24 | Ponytail與文件一致 | 無新router/retry framework/DB/scheduler；設定範例可解析，runtime能力未超報 |

## 必做public-entry PoC

至少從實際graph/run/dispatch或execution公開入口驗證：parallel analyst耗盡；Risk provider耗盡與invalid schema區別；同股持倉在Trader與Decision間增加；execution前再增加；pending買單占headroom；quarantine restart；sector unknown；provider錯誤後scheduler不繼續。每個記錄LLM HTTP count、下游節點count、intent rows、broker mutation count。失敗前已完成的工作另列；不將之後零POST誤寫整輪歷來零POST。

## 必跑檢查與判定

跑對應新增P2測試，以及 `tests/test_llm_clients.py`、`test_structured_decisions.py`、`test_trading_graph_mocked.py`、`test_phase_a1_execution_foundation.py`、`test_phase_a2_broker_authority.py`、safety/portfolio/regime/memory相關tests；最後 `python -m pytest tests/ -q`、`python -m compileall -q tradingagents cli webui`、`git diff --check`。記exact command、passed/failed/skipped/subtests/warnings/duration；任何missing dependency、未解釋skip、timeout均不是pass。

只有前置成立、B01–B24全部Pass、source/public-entry PoC無bypass、完整離線suite通過、驗收前後source未變才能 `Accepted`。任一code/contract缺陷=`Not Accepted — code/test defect`；缺前置/測試/獨立性證據=`Not Accepted — prerequisite/evidence pending`。Accepted僅表示P2離線功能，不替代真實provider或observation驗證。

## 最終輸出（五項）

1. Verdict、HEAD/base/diff identity、前置與獨立性。
2. Findings按嚴重度列file:line、觸發、直接PoC、影響；無則明示。
3. B01–B24逐項證據表，不省略Blocked。
4. Tests、外部call=0核對、Ponytail範圍與已知coverage限制。
5. 前後git status與下一步：Accepted才可交P3實作；其他結果交獨立remediation，自己停止不修。
