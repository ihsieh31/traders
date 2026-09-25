# Traders 專案文件總覽

> 這是本專案 `docs/` 的唯一人工可讀文件。它整合原始審查、驗收、投資方法評估、A/B 設計、Paper recovery 驗收程序與 execution/long-run 重構記錄。
>
> **文件基準**：2026-09-25 前的文件整理基準；程式目前以 Git `main` 的實作為準。
> **狀態聲明**：本專案是 Paper-only 的交易研究與工程驗證系統，不是投資建議，也不宣稱策略已證明具有持續超額報酬。
> **原始證據**：已刪除的原始 Markdown、JSON、Python 腳本、日誌、回傳碼與快取的 SHA-256 索引見本文末尾；完整原始內容不再作為現行操作指令。

## 1. 當前狀態

- Paper-only hard lock、strict `TradeIntent`、durable SQLite outbox、券商快照與 reconciliation、UNKNOWN lookup/adopt、帳戶鎖、曝險上限與安全 guardrail 已完成。
- Phase C 全市場 Top20 screening、entry gate、資料完整性與 cache seal 已完成。
- Phase D 30 日 observation 的設定、preflight、journal、crash/resume、排程、停止語意與最終報告已完成離線驗證。
- Traders × AI-Berkshire frozen-evidence A/B runner、雙帳戶設定、30 NYSE-session campaign coordinator 與 shadow smoke 已完成。
- 2026-09-23 C01–C04 離線修復驗收通過；但**真實 Alpaca Paper crash/restart recovery gate 尚未完成**。
- 不可把 mock、shadow、離線測試或 Paper 權益變化描述成真實券商驗證、策略盈利證明或全部啟動前驗收。

## 2. 系統與安全邊界

資料流是：市場/新聞/社群/基本面/總經濟資料 → screening → 分析師與辯論 → `TradeIntent` → 風險與安全檢查 → `ExecutionService` → Alpaca Paper。`ExecutionService` 是持久化 execution 的單一入口；所有 broker mutation 必須經過帳戶綁定、market clock、fresh snapshot、reconciliation、final authority 與 deterministic `client_order_id`。

關鍵不變量：

- durable intent/order/fill ledger 先寫入本地，再進行外部 POST。
- broker 回應不確定時只允許 lookup/adopt，不允許盲目重送。
- recovery、cancel、protective children、close 與 exposure 變更都要重新核對帳戶與 authority。
- kill switch、daily loss、drawdown、單筆/產業/總曝險限制優先於模型信心。
- screening、quote、calendar、snapshot 或 safety evidence 不可用時 fail closed；不以零值或過期資料代替。
- A/B 兩臂共享 frozen evidence，但 memory、execution DB、Safety state、broker lock 與 Paper account 隔離。
- 所有長時間 observation 都要有 runner lock、session journal、stop code、最終 broker snapshot 與可重現報告。

## 3. 投資方法評估（2026-09-23）

### 3.1 判定

目前流程可作為有風險限制的 2–10 個交易日波段研究框架，但**沒有足夠證據證明它是有效投資策略**。研究流程的合理性不等於交易結果具有正期望值。

應保留的設計包括：允許 HOLD/WAIT、研究與執行分離、數字停損與授權價格區間、默認關閉未校準 Kelly、模型共識不當作獨立投票、持股跌出 Top20 不必然立即換股，以及 A/B 使用 frozen evidence。

優先補足的策略問題：

1. **策略定位**：明確區分「企業品質研究」與「短期價格波段」；Berkshire 可作企業品質、護城河、管理、財務與反證來源，但目前不是完整長期巴菲特式估值系統。
2. **Setup 分類**：突破、回檔、趨勢延續與均值回歸應分開定義條件、失效條件、持有期與統計，不可只用合併勝率。
3. **重複資訊去重**：多指標、多週期與多角色可能只是同一價格或同一新聞的重述；應保存來源、事件時間、缺口與最強反證。
4. **報酬風險比**：提示詞的 2:1 尚未成為完整強制進場條件；若宣稱要求 2:1，必須按最差可接受成交價、成本與目標重新驗算。
5. **組合風險**：單筆風險上限不能回答組合同時虧損多少。需另記錄全組合剩餘停損風險、產業/題材集中、共同事件跳空與現金預算。
6. **空頭與相關性**：目前部分縮倉規則偏向多頭，不能宣稱空頭已有完整對稱風控；相關性需保留部位方向。
7. **交易卡**：每筆交易保存 strategy/setup、原始假說、反證、資料截止、事件、進場範圍、停損、退出期限、原始 R、保護單與成交結果。
8. **事件風險**：明確標示財報/二元事件、日期來源、確認時間與跳空壓力；不能只依賴模型敘述或正常 ATR。
9. **估值狀態**：區分「文件存在」「數字已核實」「估值可重算」；SEC/IR 報告存在不代表完整 DCF 或 intrinsic value 已成立。
10. **學習與歸因**：不要用五日後資產漲跌直接判定決策正確。應分開規則遵守、來源品質、最大有利/不利變動、退出原因、實現 R、交易成本與基準。
11. **A/B 解讀**：A/B 可比較整套研究政策，不能直接歸因成純分析能力；兩臂依序執行，成交時間、延遲、持倉與自適應記憶都會造成差異。
12. **經濟性**：納入 LLM、資料、運行、滑價與交易成本；30 日適合流程觀察，不足以證明長期 alpha。

### 3.2 建議的最小研究流程

1. 先檢查既有持倉、保護單、交易卡、到期與事件風險。
2. 以一種明確 setup 篩選新機會，不以故事補足缺失條件。
3. 建立可反駁的交易方案：假說、失效條件、價格區間、停損/退出、最長持有期與成本後情境。
4. 統一比較候選方案和既有部位重疊，按最小風險限制決定金額；沒有合適方案可以全部不交易。
5. 執行後以實際成交與保護單為準，記錄偏離計畫，再以版本化方式評估改進。

## 4. Traders × AI-Berkshire A/B

A/B 的控制變數是 `analysis_backend`：Traders 使用原生五 analyst；Berkshire 使用 frozen-evidence 的 business analyst、financial analyst、industry researcher、risk assessor 與非執行式 Team Lead。兩臂共享同一份 frozen `EvidencePacket`，下游 Report Context、Bull/Bear、Trader、Risk 與安全執行邊界保持一致。

隔離項目：analysis profile、memory、agent memory、cache、audit、execution DB、Safety state、broker lock，以及 Paper 模式下各自的 Alpaca account。固定 symbol/date hash 與兩臂先後順序，但順序執行會帶來時間與成交差異。

解讀結果時分開回答：

- 相同 evidence、相同假設部位與共同條件下的研究/決策品質；
- 真實延遲、各自持倉與自適應記憶下的完整 Paper policy 表現。

兩臂都 HOLD 可以是合理結果；訊號一致率不等於 alpha。最終權益以 broker account snapshot 為權威，不以 local DB、fills 或訊號推算。

## 5. 30 日 Paper readiness 與 recovery gate

### 5.1 已完成的離線驗收

2026-09-23 修復後驗收結果：

- C01：pair 只有 `pair_state.json` 且為 `COMPLETED`、summary 尚未寫出時，resume 可驗證 fingerprint、pair 身分、frozen evidence 與兩臂結果，不重跑或下單。
- C02：含 `risk_invalid_reason` 的完成 run log 恢復為 `FAILED_TERMINAL`，`decision_valid=False`。
- C03：任一 execution DB 缺失或帳戶綁定不符時拒絕結算。
- C04：空首窗口延伸時，正確納入下一個 NYSE 交易日。
- 聚焦與完整離線 suite 在 Python 3.12.13/pytest 9.1.1 下通過；README 記錄的啟動前快照為 `1632 passed, 334 subtests passed`。較早日期的 `1614`、`1363`、`1318` 等數字是不同基準的歷史證據，不可混成當前唯一結果。

### 5.2 真實 Paper gate：PENDING

此 gate 必須在正常美國交易時段、專用且無無關部位/訂單的 Alpaca Paper 帳戶上，使用正常 `long-run` production entry point。不得強制 symbol/signal、關閉 guard、修改 clock 或直接呼叫 broker SDK。

程序：

1. 確認 Paper endpoint、帳戶綁定、Top20、正常 notional 與市場時段。
2. 以正常命令啟動 observation：`python -m pdb -m cli.main long-run --backend traders --duration-days 1`。
3. 在正常 READY 分支的 `broker.submit_order(...)` 返回後設 breakpoint，記錄 `decision_id`、`client_order_id`、broker order ID/status 與本地 `SUBMITTING` 狀態。
4. 從第二終端 `kill -9 <pid>`，保留 run directory 與 execution DB；查詢 Paper broker 帳戶確認原訂單。
5. 以正常 CLI resume 重啟，讓 `startup_recover()` 與 reconciliation 完成；submit breakpoint 不得再次到達。
6. 確認同一 `decision_id`/`client_order_id` 對應恰好一個 broker primary order 與一個 local primary row，沒有第二次 POST，且帳戶為 `CLEAN`。

證據 note 必須記錄市場開市、run ID、兩個 ID、崩潰前/後狀態、submit breakpoint 次數（1/0）、lookup/adoption、reconciliation、最終 broker status 與數量。不得附上 API key、secret、credential 或未遮蔽環境輸出。不確定就記為 FAILED/PENDING，不可刪除或重建訂單來讓證據通過。

## 6. Execution / long-run 重構摘要

原始重構將大型入口拆成 bounded owners，但保留公開入口、舊 import、signature、lock、ID、outbox、recovery、protective order、journal 與 report 語義。Execution owners 包括 requests、order planning、dispatch、protection、recovery、exits、intent execution；long-run support owners 包括 state、config、sessions、preflight、round support、symbols、reporting。

原始結構審計曾驗證 184 個 baseline records、156 個 definitions、唯一 implementation owner、舊簽名與 call-time seams；後續獨立審查修正了 malformed resumable journal 的 `STATE_CORRUPT` fail-closed 行為。完整 inventory 與原始 JSON 已移除，摘要與 SHA-256 索引保留在本文件；測試以源碼 AST/入口契約與本文件的 owner marker 驗證所有權。

<!-- ownership-summary:start -->
| Implementation owner | Responsibility |
|---|---|
| `execution` | requests, order planning, dispatch, protection, recovery, exits, intent execution |
| `long_run_support` | state, config, sessions, preflight, round support, symbols, reporting |
| `tradingagents.graph` | analysis graph and call-time compatibility wrappers |
<!-- ownership-summary:end -->

長期 observation 的硬規則：

- `active.json` 或 journal 不可讀時 `STATE_CORRUPT`，不得在損壞狀態上建立新 observation。
- 授權前零 broker mutation；授權後建立 observation 前只做一次 recovery，且必須 CLEAN。
- 已完成 session 不重跑；`MISSED`/`STOPPED` 不安全重跑。
- `MISSED_PROCESS_DOWN` 不回填過期分析或晚下單。
- 普通 round 例外 finalize 為 `STOPPED/UNEXPECTED_ROUND_ERROR`；`KeyboardInterrupt`/`SystemExit` 傳播。
- 最終報告只使用持久化證據與新鮮 final broker snapshot，不以 stale equity 補值。
- 單一 global runner lock、account lock、session lock 的順序固定；競爭者 fail closed。

## 7. 驗證等級與限制

| 等級 | 能證明 | 不能證明 |
|---|---|---|
| 單元/契約測試 | 函式、錯誤分類、fail-closed、ID、狀態與呼叫 seam | 真實 broker/provider、延遲、滑價、成交深度 |
| mock/fault injection | 重啟、recovery、重複 POST 防護、持久化與故障順序 | 物理斷電、硬體 flush 真實保證 |
| shadow A/B | 固定 evidence、兩臂流程、零 broker mutation | 交易收益、純分析因果、完整 campaign readiness |
| Paper account | 券商 API、Paper 時鐘、訂單與 reconciliation | 真實資金、完整市場衝擊與所有 provider 路徑 |
| 30 日 observation | 有限前瞻流程覆蓋與 journal/recovery | 長期 alpha、統計顯著性、所有市場狀態 |

目前沒有聲稱 Python 3.11、長時間真實 provider、完整市場滑點、真實資金或所有可能輸入都已驗證。依賴警告（websockets legacy、LangGraph `allowed_objects`）是既有警告，不應被當成測試失敗或隱藏。

## 8. 歷史時間線

- **2026-09-08**：全量審查 19 項；Phase 1/2 修復與獨立驗收。
- **2026-09-11–12**：Paper readiness 審查；修復 execution authority、runtime、state、screening、No-POST、recovery 與 WebUI 邊界。
- **2026-09-14–15**：provider failover、5xx 分類、schema validation passthrough、bounded screening GET；區分主模型/備援模型與真實 503。
- **2026-09-17–18**：execution/long-run 結構拆分、原始來源審查、journal corruption 修正與 52 項修復驗證。
- **2026-09-21–22**：雙 30 日 A/B、provider/model generations、Execution/long-run 最終深審。
- **2026-09-23**：投資方法評估、state-only recovery、C01–C04 修復與離線驗收；真實 Paper recovery gate 維持 PENDING。
- **2026-09-25**：文件合併為本文件；原始 docs 產物改以摘要與 SHA-256 索引保存。

## 9. Provenance 與授權

本專案正式 A/B backend 的研究概念改寫自：

- Source repository: `xbtlin/ai-berkshire`
- Source commit: `1cc1e362378cd3fea99a4f4c3b50676bce9aa4c6`
- Adapted concepts: `investment-team`、`investment-research`、`financial_rigor`

改寫範圍限於 frozen-evidence 的 business analyst、financial analyst、industry researcher、risk assessor 與非執行式 Team Lead synthesis。保留 business essence、moat、inversion、management quality、industry/civilization trend、valuation/margin of safety、uncertainty、missing-data discipline 與 cross-validation。刻意排除 portfolio review、industry funnel、quality screen、thesis tracker、news pulse、earnings team、portfolio sizing、trade execution 與 final buy/sell recommendation，避免改變 A/B 控制變數或繞過共享下游決策。

若後續複製 substantial source code，必須保留來源、commit、license 與 attribution。專案整體另繼承 AlpacaTradingAgent 與 TradingAgents 的來源脈絡與 Apache-2.0 授權；詳見根目錄 `README.md`、`LICENSE` 與 Git 歷史。

## 10. 原始文件處置與證據索引

下表涵蓋合併前 `docs/` 的 64 個檔案。`current` 表示內容已被本文件吸收；`historical`/`superseded` 表示保留背景但不可直接作現行操作指令；`generated` 是機器產物；`unsafe-to-run` 是歷史復現或 prompt，不應執行；`.pyc` 是被忽略的 Python 3.12/pytest 9.1.1 快取。SHA-256 是合併前檔案內容指紋。

| 原始檔案 | SHA-256 | 狀態 |
|---|---|---|
| `docs/AI_BERKSHIRE_ADAPTATION.md` | `138d23445c819ce0781e3729eb725a8b78c9aa785da25429baa026ed1b0e41a3` | current |
| `docs/AUDIT_REPAIR_COMPLETION_2026-09-17.md` | `741b0c230a2a7a0226a254d8861e07428c83bc21915e13cc3e9b67de157eca62` | historical |
| `docs/AUDIT_REPAIR_SIX_FIXES_2026-09-18.md` | `7fd034b0e08a0387e0c0ac203d7f88c7b1721f9153b56994a8b0930cc11556b3` | historical |
| `docs/AUDIT_REPAIR_VERIFICATION_2026-09-18.md` | `d6a02f0da35ebfdc61241f1ca2f533a9753cfb4a5b17ed3493db9c56a942d3de` | historical |
| `docs/AUDIT_SECOND_OPINION_2026-09-17.md` | `10219e3261304c9639995d1f78d292ab64605bd1c1b75868db04ca07cc53815e` | historical |
| `docs/CONTINUOUS_30D_ACCEPTANCE_2026-09-23.md` | `91bb492fc7619c87c2e7558ab8cd391af327308987522a9baf304ac5e9892a11` | current |
| `docs/CONTINUOUS_30D_FINAL_AUDIT_2026-09-23.md` | `7d388736c15ba13d01334baedc00a8df8444384a78b26b1c815ca3ab18b0202a` | historical |
| `docs/DUAL_30D_FINAL_REVIEW_2026-09-21.md` | `1656543887739ba765741ad2436382b061fa4d3c0ff602f51360f3735110e37f` | historical |
| `docs/FINAL_30D_DEEP_REVIEW_2026-09-22.md` | `943d53ae732609def21f03914edfdb19ca76b59ad687d44aab6c00bbd1f11fac` | historical |
| `docs/INVESTMENT_LOGIC_REVIEW_2026-09-23.md` | `2e89b8f07d568c63b0dbe66bb313e1d56d7c91c7f0225cd537038d01ba2deaa9` | current |
| `docs/PHASE_2_IMPLEMENTATION_REPORT.md` | `151560059345f026f65b2142a695b6b8fa69b9571ba432ba72ed67f81fefe1cb` | historical |
| `docs/PRE_30D_SMOKE_RECOVERY_ACCEPTANCE.md` | `fa83042e778383ac31e63696c1f95cd4c3c93f9eddaec804ddf1af4b2ed4450c` | historical |
| `docs/REAL_ALPACA_PAPER_RECOVERY_GATE.md` | `4c3db2e3e74aaad80f6aa0e5a5dfaccf47777464820e55e5afc19521c2ff4b19` | current |
| `docs/REMAINING_FIXES_IMPLEMENTATION.md` | `3f6d8ca97034b7e28e7f2585a5b9af532e761965586976a604d0ebf41e628bec` | historical |
| `docs/STRATEGY_REPAIR_PLAN.md` | `4f176ca53037decde2f24babc1bdffe354e89d1aa9c7b57a7af771913087dbf4` | historical |
| `docs/TRADERS_FINAL_TARGETED_REMEDIATION_PLAN.md` | `48703b361a3e388b49c248e50ce4d7105a8f6387085771d105bcb71a2345db76` | historical |
| `docs/TRADERS_P1_P3_ACCEPTANCE_PROMPT.md` | `4a7b7d1b8a412ac319c58ea5fa73c3d86a5634a92a83a614b27eea08e422b035` | unsafe-to-run |
| `docs/TRADERS_P1_P3_IMPLEMENTATION_PROMPT.md` | `1e72ed14928918d5cc6cefb4fb416fe165211b97782795a73804e9f1137482a2` | unsafe-to-run |
| `docs/TRUE_BERKSHIRE_AB_IMPLEMENTATION.md` | `4e2262b72070d9df8f00d10e4c2a73cb280a39a9fffeae30bcd79b1db68c35fe` | current |
| `docs/audit_verification_2026-09-18/__pycache__/test_remaining_gaps.cpython-312-pytest-9.1.1.pyc` | `66b27199b230475e070dcc050043ef0b72413bfe0df81aafcb82b1c6ee4d0dc3` | generated-cache |
| `docs/audit_verification_2026-09-18/full_suite_returncode.txt` | `9a271f2a916b0b6ee6cecb2426f0b3206ef074578be55d9bc94f6f3fe3ab86aa` | generated |
| `docs/audit_verification_2026-09-18/full_suite_run.json` | `036d335bcdbfc51c887a4ccdaa90c2f37f84d536279ad40c26c3bc4c390f1749` | generated |
| `docs/audit_verification_2026-09-18/full_suite_stderr.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | generated |
| `docs/audit_verification_2026-09-18/full_suite_stdout.log` | `8bccf14346a8d3bdadf5345e448de93a6f48d0b0622268787f61288cbf921b98` | generated |
| `docs/audit_verification_2026-09-18/gap_probes_returncode.txt` | `4355a46b19d348dc2f57c046f8ef63d4538ebb936000f3c9ee954a27460dd865` | generated |
| `docs/audit_verification_2026-09-18/gap_probes_run.json` | `f0c77843840a01bc0073c9abdb96bb650b93b289d2464c5685790aef89e013a8` | generated |
| `docs/audit_verification_2026-09-18/gap_probes_stderr.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | generated |
| `docs/audit_verification_2026-09-18/gap_probes_stdout.log` | `ceb2f08c7995903afc8bfd8b1dff231c383ed80d23fa667ac10ea256f8992f5f` | generated |
| `docs/audit_verification_2026-09-18/six_fixes_contract_tests_returncode.txt` | `9a271f2a916b0b6ee6cecb2426f0b3206ef074578be55d9bc94f6f3fe3ab86aa` | generated |
| `docs/audit_verification_2026-09-18/six_fixes_contract_tests_run.json` | `239c317f472da3ffaadac8403cc19d95de01cd891ef98413b040dbb7b4637419` | generated |
| `docs/audit_verification_2026-09-18/six_fixes_contract_tests_stderr.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | generated |
| `docs/audit_verification_2026-09-18/six_fixes_contract_tests_stdout.log` | `bd0cf1358d9984ab402980ce3b76bee5d1377847a3e243edff4c1c3ac28c6e64` | generated |
| `docs/audit_verification_2026-09-18/six_fixes_full_suite_returncode.txt` | `9a271f2a916b0b6ee6cecb2426f0b3206ef074578be55d9bc94f6f3fe3ab86aa` | generated |
| `docs/audit_verification_2026-09-18/six_fixes_full_suite_run.json` | `fd6cfb35aa967d9790699127a1c48adef9cf189e8511ab4f83a65afdb9454128` | generated |
| `docs/audit_verification_2026-09-18/six_fixes_full_suite_stderr.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | generated |
| `docs/audit_verification_2026-09-18/six_fixes_full_suite_stdout.log` | `bafa3fa85db16a0519c56d027c9195170c435d1e7edec7a7e19aa4d5cf8f8b16` | generated |
| `docs/audit_verification_2026-09-18/six_fixes_original_probes_returncode.txt` | `9a271f2a916b0b6ee6cecb2426f0b3206ef074578be55d9bc94f6f3fe3ab86aa` | generated |
| `docs/audit_verification_2026-09-18/six_fixes_original_probes_run.json` | `9ca18ada8af8cc4fb37d465d74f0c5dbdef134bce2e5e5253aad3e5154bfb68c` | generated |
| `docs/audit_verification_2026-09-18/six_fixes_original_probes_stderr.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | generated |
| `docs/audit_verification_2026-09-18/six_fixes_original_probes_stdout.log` | `c51b8695ce5d0f6fe48385437f7248ce130992e0643101fe186784723a5d6869` | generated |
| `docs/audit_verification_2026-09-18/six_fixes_source_hashes.json` | `99e7eae9102ed06830ad4123fc24f96de7d26f13d23857a88462683827dfdcce` | generated |
| `docs/audit_verification_2026-09-18/source_hashes.json` | `d7bfd37df9f1ad7070c52ab261e81cac0aa2275a85a3e9a86c4690085b1e33b4` | generated |
| `docs/audit_verification_2026-09-18/test_remaining_gaps.py` | `761dd18ca4e9552e66e173d213305bedc83c4bd0c338882e8e6a6b530d43bd22` | unsafe-to-run |
| `docs/paper_readiness_20260911_repros.py` | `02421bd0b4adeda52efc7719839456a0f64d5a3d43a2c7fcd1d7d77701df1850` | unsafe-to-run |
| `docs/paper_readiness_20260912_repros.py` | `303699bc0667cfdd22d71e7cbd96f99d3291a4ca166b7e684ab39a3428a07e11` | unsafe-to-run |
| `docs/paper_readiness_acceptance_prompt_2026-09-12.md` | `f68d03e53e868bc40c886bbefe85f3f12b031253a5ea74c00562de411f27e2c7` | unsafe-to-run |
| `docs/paper_readiness_implementation_prompt_2026-09-12.md` | `3a9739ea526c1ad9c417c034d3106c0c2ec9002c9538896e40ea46411f12575f` | unsafe-to-run |
| `docs/paper_readiness_review_2026-09-11.md` | `a7e01870e8b73067506603d43b3b86dc37f51e474886521ab4bf5fc42fe4be53` | historical |
| `docs/paper_readiness_review_2026-09-12.md` | `414b299f1508f731765a389b26c2235bc5575c30b6a7e591b6f25af7f21fa17d` | historical |
| `docs/refactoring/execution-long-run-audit.json` | `0b93476bfaf252bc69332853e27ed1306557a8175a09864af42b4936f6c14557` | generated |
| `docs/refactoring/execution-long-run-boundaries.md` | `a677eb51827329b1d301b9f1ed153a496a31111a673f2d0ba430dcef701905c6` | historical |
| `docs/refactoring/execution-long-run-independent-review.md` | `2c8268a78b7ddd716f67b0e52544854bc911d342c8b1e209f23a4f887eee45b6` | historical |
| `docs/refactoring/execution-long-run-inventory.json` | `245547ccc37dac0a4f724ed22d966418f3a284074bfce85a62c8878ffe2c6b58` | generated |
| `docs/refactoring/execution-long-run-remaining-plan.md` | `e7c4de5affd77802e2646fb49834fc3ac1d6057efb889737b8e952e7a4bb920f` | historical |
| `docs/refactoring/execution-long-run-validation.md` | `3edc81bb6801a8d37c5f9cb697078fd8ce327537a119a801618f504d94d44a62` | historical |
| `docs/review_2026_09_22/__pycache__/test_final_review_contracts.cpython-312-pytest-9.1.1.pyc` | `e01319d85e71eb03128fcfee8104421c7fe20f346b699fa4632462dfe277016d` | generated-cache |
| `docs/review_2026_09_22/baseline_pytest.log` | `6587d57206c8cc673350de5acae17c15f0a819939752584fcbb52d8f46768de9` | generated |
| `docs/review_2026_09_22/contract_failures.log` | `21dd3b62dd055ef20648853291b85e1ea0a5386930575eb875034bb68af64834` | generated |
| `docs/review_2026_09_22/rescued_core_checks.log` | `59402c5309c273690082f43fa4c00acd14049f7df681fe0fe75eb2b0407cb7ae` | generated |
| `docs/review_2026_09_22/rescued_core_nodeids.json` | `a583a1ab91432633709085defe255a3d24c04e234c9dbcff2af79fc7d0cb9579` | generated |
| `docs/review_2026_09_22/run_skipped_core_checks.py` | `42ed80e8de735bc5bcb6d2a10f0e68c3c1439481f6976d6a0a5144c634247f79` | unsafe-to-run |
| `docs/review_2026_09_22/source_snapshot.json` | `40a63d0d3f2ed194c22fc8ccb08f5efa6acdeffe1fe5f8097c51783e0d6228ab` | generated |
| `docs/review_2026_09_22/test_final_review_contracts.py` | `2ba9802fd8fb9d00e2c89c2c06bbcdae08d13cdf13ba444bb190354c986c8086` | unsafe-to-run |
| `docs/tradingAlpaca-full-review-2026-09-08.md` | `bd8830a6d61251d1818544e48705449f1df373b09444a3bbdaf13f5f9d28c456` | historical |

## 11. 維護規則

- 新增結論先寫入本文件，不恢復多份相互矛盾的 docs。
- 若需要原始完整證據，應另建 CI/release artifact 或受控 evidence bundle，不要把機器輸出、API key、credential 或絕對本機路徑放回本文件。
- 每次更新需同步 README 的目前狀態、測試基準、Paper gate 狀態與本文件的來源索引。
- `docs/` 最終只允許本文件；根目錄的 `README.md`、`ARCHITECTURE.md`、`PROJECT_GOALS_AND_STATUS.md` 與測試程式碼是專案文件/契約的外部入口，不得建立新的 docs 子目錄。
