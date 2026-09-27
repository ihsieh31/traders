# 全專案深度審查與修復

審查期間：2026-09-26 至 2026-09-27（Asia/Taipei）。

本次確認並修復 19 類問題，另外保留 1 類沒有上游修補版本的依賴風險。這是有範圍、有反例和回歸證據的工程審查，不代表所有可能輸入、外部服務行為或交易獲利已獲證明。

## 起始狀態與審查方式

- 起始分支 `main`；只有使用者既有未追蹤 `.zcodeignore`，保留不動。未提交、推送或部署。
- 原環境 Python 3.12.13；修改前完整測試 **1812 passed、335 subtests、2 dependency warnings**，`pip check` 通過。這個全綠基線仍漏掉下列問題。
- 先追蹤呼叫與資料流，再建立故障案例；以不合法券商回覆、矛盾身分、延遲 I/O、損壞 journal、被改寫的 evidence、非有限數值和真實 SDK transport 替身質疑原有假設。
- 全樹執行未定義名稱／語法掃描；對主要交易、恢復、資料與風險路徑做深入追蹤。沒有把每一個檔案都宣稱為逐行形式驗證。
- 比對官方 Alpaca 契約、套件實際程式碼和安全公告。測試使用假金鑰、替身傳輸及暫存資料庫；未呼叫付費模型、未送出真實券商 POST／DELETE。
- 先以原 Python 環境建立基線，再建立 Python 3.11.15 與 3.12.13 的乾淨環境，從重新產生的 lock 安裝驗證。兩版本完整回歸通過後，已同步工作區 `.venv-p2` 至新 lock；同步前套件清單保存在 `/tmp/tradingbuffett-audit-original-environment.txt`。

## 修復計畫與順序

1. **先封住券商寫入邊界**：完整 authority、reconciliation 身分、POST／DELETE 的最終授權、UNKNOWN 恢復。
2. **確保決策依據一致**：資產 API、frozen evidence、checkpoint、長跑 journal 與恢復決策綁定。
3. **修正帳本與研究結果**：安全數值、token 記帳、回測參數、財務指標、損壞報表資料。
4. **處理整體維運風險**：憑證遮罩、依賴漏洞、嚴格反序列化、平台聲稱及脆弱測試。
5. 每個邊界加入可重現案例；保留既有交易與金額斷言，最後在兩個 Python 版本、安裝包及 CLI 做驗證。

## 已修復問題

| 編號 | 優先度 | 原始問題及影響 | 修復與證據 |
| --- | --- | --- | --- |
| A01 | P1 | 缺 broker ID 的訂單被靜默跳過；重複持倉、非法 side／超額 fill、非有限／交叉報價可進入 authority。可能低估或誤認曝險。 | 在 capture／quote 共用邊界拒絕矛盾資料；OPEN iterator 先物化再檢查上限。`test_deep_audit_execution.py`。 |
| A02 | P1 | 對帳未完整核對本地與券商的 symbol、side、broker/client ID；重複或衝突的 fills 仍可能更新狀態與帳本。 | 寫入前集中驗證 identity、累積成交量和 fill 來源；衝突訂單在所有投影／fill／最終同步階段均排除，帳戶保持 PAUSED。涵蓋「先污染狀態、再發現 fill 錯誤」反例。 |
| A03 | P1 | recovery 把錯誤字串中出現 `404` 當成訂單不存在，代理錯誤的數字也可觸發恢復送單。 | 僅接受結構化 HTTP 404，支援 exception chain；純文字仍屬 uncertain。 |
| A04 | P1 | POST 回覆 rejected／canceled／expired 仍回傳成功；缺 status 被補成 accepted。 | 共用回覆解析要求 broker ID 和明確 status；不完整回覆保持 UNKNOWN，終止失敗回覆回傳失敗；確認 durable transition 確實成功。新單和平倉共用規則。 |
| A05 | P1 | 移除保護單的 DELETE 在 safety disabled 或 guard 無法初始化時仍可能繼續；之後平倉卻被 kill switch 阻擋。 | DELETE 使用共用 authority gate，阻塞 clock GET 前後都檢查；kill switch 不因 disabled 被忽略，guard unavailable 停止寫入。 |
| A06 | P1 | 恢復送單 freshness 檢查早於 DB commit 和 request 建構，慢 I/O 可讓已過期授權繼續 POST。 | 將最終開倉驗證移至 durable SUBMITTING／request build 之後，最後 GET 後再次核對 stop／kill。分別注入慢 commit、慢 request 證明零 POST。 |
| A07 | P1 | 記憶體 state、checkpoint 與長跑恢復路徑可略過 frozen packet 驗證；run log 只綁 observation，未綁 evidence。 | 統一驗證 symbol、date、完整性、內容 hash 和 pinned hash；graph resume 檢查 checkpoint；ANALYZING／ANALYZED／EXECUTING 恢復先核對檔案，run-log intent 須使用相同 packet。缺失或改寫時在券商恢復／執行前停止。 |
| A08 | P1 | 空白、JSON 空集合／null、`UNAVAILABLE_FOR_HISTORICAL_AS_OF` 被錯認為有效來源。 | 修正來源分類，共用完整性檢查拒絕缺資料 section；保留「成功搜尋但零結果」的既有語義。`test_deep_audit_evidence.py`。 |
| A09 | P1 | evidence／journal 可保存例外中的憑證；環境憑證只在第一次掃描，輪替或 runtime 新 key 可能漏出；多處直接輸出原始例外。 | 註冊 runtime key，持續辨識新環境值並保留舊 key 遮罩；evidence 在計算 hash 前遮罩，journal 在落盤前遮罩；顯式例外文字、診斷報告、console 與工具錯誤回傳使用共用 sanitizer。用假 key 驗證落盤、console、輪替與 hash 一致性。 |
| A10 | P1 | Alpaca 已移除 `easy_to_borrow`，SDK Asset 仍把它視為 required；原 raw adapter 只覆蓋 execution，其他資產讀取可整批失敗。分頁邏輯另可能丟失末筆資產或永遠循環。 | 所有 paper factory 的 asset reads 使用同一 raw API adapter；共用欄位存取支援 dict／SDK 物件，保留 read-only gate。分頁不能證明完整時失敗、拒絕重複 token、保留真正末筆資產。以實際 SDK＋移除舊欄位的 JSON 驗證。 |
| A11 | P2 | NaN／Infinity／負數安全上限可停用限制；負 token 可減少用量並損壞下一次載入；必填 notional 的 NaN 被當成 0。state rename 未同步目錄。 | 啟動時驗證安全設定，token 只接受非負整數，日期須標準 ISO；拒絕非法必填 notional；重用同時 fsync 檔案與目錄的原子寫入器。驗證失敗更新不改變原帳本。 |
| A12 | P2 | 回測接受負佣金、負滑價、非法倉位及無效訊號；全零 drawdown／CAGR overflow 產生 NaN 或例外；財務 Decimal 接受 NaN／Infinity。 | 驗證資金、倉位、費用、滑價上下限、年化週期、訊號日期與 action；未定義指標回傳 None；財務輸入須有限，市值輸入為正，tolerance 在合法範圍。`test_deep_audit_numbers.py`。 |
| A13 | P2/P3 | 靜態掃描找到 15 處未定義名稱，包括 Any、抽出模組的型別參照、測試 lambda 拼錯參數及 deprecated CLI 的不可達程式。 | 補正匯入／TYPE_CHECKING、修正 lambda，移除死碼；全樹 `F821,F822,F823,E9` 通過。沒有把未執行過的測試分支視為已驗證。 |
| A14 | P2 | 損壞快照被靜默略過，可能仍顯示完整報酬／drawdown；部份持倉缺 unrealized P/L 仍被加總成「總額」。 | 保留原始檔案，報表列出 snapshot errors；序列不完整時不宣稱報酬／drawdown。任何持倉損益缺失或非有限，總額回傳 unavailable。`test_deep_audit_recovery_reporting.py`。 |
| A15 | P2 | run_id 可含絕對路徑或 `..`，本地狀態可能導向指定 runs 目錄外。 | run_id 限制為單一路徑元件；拒絕 traversal、斜線、反斜線及空值。這是本地狀態邊界修復，未宣稱存在遠端攻擊入口。 |
| A16 | P2 | provider usage 為 Infinity 時可在 best-effort 記帳路徑拋出 OverflowError；adapter 已記帳的 callback 提前返回會遺留開始時間。 | 處理非有限 usage／布林值，所有完成路徑先移除開始時間，再判斷是否需要記帳；保留不重複計費規則。 |
| A17 | P2 | README／setup 宣稱 Python 3.10 和原生 Windows 可用，與 lock 的套件最低版本及 POSIX fcntl 實作不符。 | 最低 Python 調整為 3.11，Windows 指向 WSL2／Linux 容器；用乾淨 3.11／3.12 環境實際安裝與測試，未僅引用舊 CI 聲稱。 |
| A18 | P1 | 舊 LangChain／LangGraph 依賴命中已公告漏洞，持久化 checkpoint 在實際程式路徑上；即使新版 serializer 仍可能以相容性預設允許任意 msgpack 類別。 | 升級維護中的 core／provider／graph／checkpoint 套件，移除程式未使用的 langchain／experimental 聚合依賴，重新產生 universal lock；checkpoint 明確設 `allowed_msgpack_modules=None, pickle_fallback=False`。測試任意 constructor 不執行、正常訊息及交易 state 可 round-trip。 |
| A19 | P2 | 既有安全測試使用真實鐘點，時段外會在到達目標故障注入點前停止；部分替身用矛盾 ID、不存在 evidence 檔案或依賴已變更的 SDK 私有 API。 | 固定 scheduler clock 在 session window；對帳替身使用一致 broker ID；A/B 替身建立真實封存 packet；Anthropic／Google 改在 HTTP transport 計數，仍驗證 4 次請求上限；工具歷史驗證保留正文與兩個 tool calls，未降低斷言。 |

## 主要反例與驗收

新增測試集中在以下六個檔案，避免把檢查散落到不可重現的臨時操作中：

- `tests/test_deep_audit_execution.py`：錯誤券商 facts、污染帳本、UNKNOWN 查詢、POST 狀態、保護單 DELETE、過期恢復授權。
- `tests/test_deep_audit_evidence.py`：缺資料標記、state 改寫／錯 symbol／錯 hash、runtime 金鑰與 console／檔案洩漏。
- `tests/test_deep_audit_assets.py`：實際 SDK 資產契約、read-only、分頁完整性。
- `tests/test_deep_audit_numbers.py`：不合法設定／token／notional、durability barrier、回測及 Decimal 邊界。
- `tests/test_deep_audit_recovery_reporting.py`：凍結決策恢復、journal、路徑隔離、損壞快照、部分損益及 usage cleanup。
- `tests/test_deep_audit_checkpoints.py`：不受信任類別不可重建；正常分析 state 仍可序列化與恢復。

最初新增的執行安全、evidence、asset、數值及恢復案例均有在修復前觀察到失敗。後續補強案例涵蓋延遲 I/O、分頁循環、strict serializer 和新增的持久化邊界。沒有把所有測試都聲稱為在原始版本跑過；部分案例是修復設計完成後補入。

## 依賴與安全公告

`uvx pip-audit -r requirements.lock --no-deps --disable-pip` 的版本命中結果：

| 項目 | 修復前 | 修復後 |
| --- | ---: | ---: |
| 此環境可適用的鎖定依賴 | 185 | 173 |
| 命中套件 | 10 | 1 |
| 原始公告紀錄（含重複） | 27 | 5 |
| 不同的「套件＋公告 ID」 | 18 | 4 |

這是套件版本掃描，不把每筆命中等同於可利用的專案漏洞，也不把「沒有掃到」當成不存在漏洞。修復後掃描仍返回非零，原因如下，沒有加入忽略清單掩蓋。

**A20：Chroma 1.5.9，尚無上游修補版本。** 保留的 4 個不同公告是 CVE-2026-45829、CVE-2026-45830、CVE-2026-45831、CVE-2026-45833。它們涉及 Chroma server 的 collection API、remote-code embedding 設定及多租戶授權。程式追蹤顯示本專案只用本機 `PersistentClient`／`Client`，不啟動 Chroma HTTP server、不使用 `HttpClient`，亦未提供 `trust_remote_code` 設定。據此判斷，公告所述遠端入口目前不在應用流程中；依賴風險仍保留，不能宣稱已修復。

處理條件：若未來要新增 Chroma server／多租戶／外部 collection 管理入口，應先取得修補版或替換該元件，再開放入口。當前不為追求掃描零命中而移除既有持久化記憶功能或自行修改 vendor 套件。

參考來源：

- [Alpaca 移除 easy_to_borrow 的官方公告](https://docs.alpaca.markets/us/changelog/2026-06-05-borrow-status-6b96a5a)：移除日期 2026-09-22；short gate 應保留新的 `borrow_status` 契約。
- [LangGraph JSON checkpoint advisory](https://github.com/langchain-ai/langgraph/security/advisories/GHSA-fjqc-hq36-qh5p) 與 [msgpack advisory](https://github.com/langchain-ai/langgraph/security/advisories/GHSA-g48c-2wqr-h844)：驗證升級與允許清單的依據。
- [Chroma 未驗證身分的程式碼注入](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c)、[collection 更新程式碼注入](https://github.com/advisories/GHSA-36p7-vc44-83pf)、[RBAC 範圍問題](https://github.com/advisories/GHSA-xph7-9rjv-w5fr)、[跨租戶授權問題](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr)。

## 最終驗證

| 驗證 | 實際結果 |
| --- | --- |
| Python 3.12.13 完整回歸 | **1919 passed、335 subtests、1 warning**，77.94 秒 |
| Python 3.11.15 完整回歸 | **1919 passed、335 subtests、1 warning**，75.12 秒 |
| 本次新增案例 | **107 tests collected**（六個 `test_deep_audit_*.py`） |
| 依賴相容性 | 兩個乾淨環境與工作區 `.venv-p2` 的 `uv pip check` 通過 |
| 安裝產物 | sdist／wheel 建置成功；wheel 資源與 `.env` 排除檢查通過；工作目錄外的 CLI `--help` 成功 |
| 靜態檢查 | 全樹 Ruff `F821,F822,F823,E9`、`compileall`、diff whitespace check 通過 |
| 工作區同步後檢查 | `.venv-p2` 的新增案例、provider HTTP transport 與 checkpoint 測試：**138 passed、24 subtests、1 warning** |

可機讀結果與去重後的依賴公告 ID 見 [DEEP_AUDIT_20260926_RESULTS.json](DEEP_AUDIT_20260926_RESULTS.json)，其中包含本次驗證的 lock SHA-256。

最終的一個 warning 為第三方 `websockets.legacy` 棄用通知。全新 Python 3.11 第一次載入 backtrader 時另曾出現套件原始碼 escape-sequence 警告；未隱藏警告，亦未將它們當作本專案測試失敗。舊 LangGraph serializer 的 warning 已隨升級消失。

已完成：

- Universal lock 由 `uv pip compile requirements.txt requirements-dev.txt --universal --python-version 3.11 --output-file requirements.lock` 產生，未手改 lock。
- Python 3.11.15／3.12.13 乾淨環境的依賴相容性檢查通過。
- source distribution 和 wheel 建置成功；從工作目錄外呼叫已安裝 wheel 的 `tradingagents --help` 成功。
- 全樹未定義名稱／語法掃描、`compileall`、差異空白檢查通過。原有 CRLF 檔案按 `cr-at-eol` 規則檢查。
- Docker 實際嘗試建置，但在解析 `docker.io/docker/dockerfile:1.7` 時 `DeadlineExceeded`；尚未進入專案依賴安裝，**不能宣稱容器建置通過**。

## 檢查範圍與保留限制

| 範圍 | 本次證據 | 尚不能證明的部分 |
| --- | --- | --- |
| 券商執行、恢復、撤保護、風險上限 | 跨模組追蹤、暫存 SQLite、故障注入、既有完整回歸 | 真實 Paper 帳戶的斷網、券商延遲一致性、程序被終止後的端到端演練 |
| LLM、analyst、Berkshire、工具迴圈 | 完整回歸、實際 SDK serializer／HTTP 替身、usage／evidence 驗證 | 真實 provider 可用性、付費呼叫結果品質、任意 prompt injection 的完整防禦 |
| 歷史資料、篩選、回測與財務計算 | 契約核對、數值／缺資料反例、全套相關測試 | 所有外部來源的 point-in-time 正確性、真實成交滑價、分紅／融資／研究費用的完整策略歸因 |
| 長跑／A/B／報表 | 真實 packet bytes、journal 損壞、恢復與時段控制、測試替身修正 | 尚未進行數日真實市場 unattended 運行；帳戶報酬仍是未調整存提款的 equity change |
| 安裝、CLI、部署 | 兩版本乾淨安裝、套件建置／啟動、靜態檢查、依賴公告掃描 | Docker registry timeout 後的容器完成驗證、原生 Windows（明確不支援） |

既有單一標的 signal diagnostic 不是完整投組回放；walk-forward 分段不等於模型的 out-of-sample 訓練；tests passed 不等於策略獲利。這些界線保留，沒有透過更動報表文案消除風險。

## 驗收後操作

1. 本機 `.venv-p2` 已同步；其他部署環境仍須依新 `requirements.lock` 同步依賴，檢查 `pip check`，再跑回歸。不應讓舊的 LangChain 0.3 環境搭配這份修復後原始碼。
2. 部署前保留既有 SQLite／journal／safety state。strict checkpoint 不再重建任意自訂類別；若舊 checkpoint 包含這類物件，需要確認 schema／重跑分析，不能改回寬鬆反序列化來消除錯誤。
3. 網路允許時重新完成 Docker 建置。真實 Paper 的恢復／kill-switch 演練須使用專用帳戶、可控小額及明確測試窗口；本次未進行任何真實券商寫入。
