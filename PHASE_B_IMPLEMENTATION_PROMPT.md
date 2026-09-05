# P2／Phase B 實作提示詞：資料品質、雙角色 Provider 與持股風險

## 任務與前置條件

實作原 Phase B 全部範圍，再加入 Analysis／Decision 分離、LLM retry 與持股 context；不開始 P3 自動選股。前提是 Phase A A6 的獨立 Accepted 證據。Paper observation 不阻擋 P2／P3 實作與離線驗收；缺少 observation 記錄不得因此列 prerequisite pending。啟用長期無人值守 Paper 自動交易前仍須完成 observation，另行取得執行授權；本提示不啟動真實 Paper run。

## 工作方式與不可變邊界

工作目錄：`/Users/zongen/Downloads/codex/tradingAlpaca`。P1＝Phase A，P2＝Phase B，P3＝Phase C。本文件是可整份交給實作者的規格；不得把 roadmap 的「計劃完成」當成程式已完成。

先讀適用的 AGENTS.md 與 `ponytail` skill（full）。先找現有 helper/client/guard，能沿用就沿用；不新增 generic router、retry engine、ORM、queue、第二個 scheduler、DB/service、security master、portfolio optimizer、ML ranking 或完整 evidence/PIT platform。不重寫 LangGraph、debate、execution ledger；不增加與本階段無關的依賴。不用過度簡化理由移除 authority、validation、freshness、lock 或 fail-closed。

先記錄 `git status -sb`、`git rev-parse HEAD`、完整 diff 與前置 acceptance evidence。保留使用者 dirty/untracked 檔案，不 reset、不 broad-stage。先盤點再修改，遇到同一檔案已有變更只合併必要區塊。這份提示不授權 commit/push、真實 LLM、Alpaca、Telegram/webhook call 或讀出 `.env` secrets；測試使用 stub transport、mock broker、temporary SQLite/檔案。需要新資料來源時實作 adapter 與 fixture，不以呼叫實際帳戶代替測試。

Phase A 的 Paper-only、strict TradeIntent、commit-before-submit、decision/client ID 冪等、UNKNOWN lookup、freshness、CLEAN/PAUSED、account lock、verified reducing exit 全部保持。broker POST 絕不套用 LLM retry。不得新增 execution bypass，也不得用 LLM 的持股敘述作為 broker authority。

## 必讀來源與實際接點

- `PROJECT_GOALS_AND_STATUS.md`、`ARCHITECTURE.md`、`README.md`、本階段 acceptance prompt。
- `tradingagents/default_config.py`、`tradingagents/llm_clients/{factory,openai_client,google_client,anthropic_client,azure_client}.py`。
- `tradingagents/graph/{trading_graph,setup,propagation,reflection,signal_processing}.py`。
- `tradingagents/agents/utils/structured.py`、`tradingagents/agents/{trader/trader,managers/risk_manager}.py` 及對應 `prompts/templates/`。
- `tradingagents/dataflows/{alpaca_utils,interface,interface_utils}.py`、`agents/utils/agent_utils.py`。
- `tradingagents/execution/{authority,service,store}.py`、`safety/guardrails.py`、`portfolio/__init__.py`、`risk/position_sizing.py`、`regime.py`。
- CLI、`webui/callbacks/trading_callbacks.py` 與 config/state/storage 的真實 callers；沿實際呼叫追到 graph 和 execution，不能只建立未使用的 helper。
- `.github/workflows/tests.yml` 及相關既有 tests。以上是起始接點，先用搜尋確認目前名稱，不照舊行號盲改。

## 按順序完成五個工作項

### B1：兩個固定 Provider／Model 角色

1. 沿用 `create_llm_client()`；設定增加 `analysis_provider/model/backend_url`、`decision_provider/model/backend_url`（即 `analysis_provider`、`analysis_model`、`analysis_backend_url` 等六個 key）。Provider、model、endpoint、credential 必須各自解析，不能將 Analysis 的 key/base URL 複製給另一角色。沿用現有各 provider key resolver；若同一家需不同 key，允許最小的 role-specific 環境變數 override，缺省才取該 provider 既有 key，secret 不進 UI 持久化、log 或 sample 值。
2. 新模式：五個 Analysts、Bull/Bear、Research Manager、Trader、Risky/Safe/Neutral 全部用同一 Analysis model；Risk Manager 唯一使用 Decision model。Reflection／legacy signal helper 沿用 Analysis；它們不能替代最終 strict Decision。GraphSetup 加一個 optional decision LLM 注入接點即可，保留舊呼叫相容；不改 graph topology。
3. 完全沒有新 role 設定時，保留舊 quick/deep 分工與 provider/endpoint 行為。只要任一 analysis/decision role key 有值就進新模式：Analysis provider/model 缺值分別取 `llm_provider`／`deep_think_llm`；Decision 未指定時取解析後 Analysis。明確指定另一 provider 卻缺 model 時 startup config error，不能帶入不相容 model；同 provider 的省略 model 可繼承。跨 provider 不繼承 backend URL/key；空字串視為未設，明確錯誤值不 fallback。明確 role provider 不被全域 local OpenAI 開關偷偷改寫。
4. 保留舊 key，不大刪 UI。CLI/WebUI/config persistence 必須能設定及顯示兩角色實際 provider/model/endpoint（endpoint 去除認證/query secrets），自訂 model ID 沿用既有能力。provider-specific kwargs 逐角色建立；OpenAI reasoning 不能漏到 Google/Anthropic。不得在此階段建立 Screening client。

### B2：LLM retry 與整輪停止

- `llm_max_retries=3`：第一次＋最多 3 次 retry＝最多 **4 次實際 HTTP 請求／同一 logical LLM invocation**。只允許整數 0–3，非法設定 startup error；0 表示不重試。對 timeout、connection、429、5xx 暫時錯誤重試；401/403、無效 key/model/request 立即停止。每次請求有有限 timeout、backoff 有上限。
- 優先使用目前 SDK 的 `max_retries`；先核對安裝版本與 transport 測試，不能假定所有 SDK 的參數語義一致。只允許一層 retry owner，停用重疊 LangChain/SDK/外層重試。若原生 SDK 不符範圍，僅在該 adapter 做最小修正，不建立通用 retry framework。不支援精確上限的路徑不能宣稱支援。
- 對每個實際 adapter family（OpenAI-compatible、Google、Anthropic、Azure）證明請求計數；structured/tool binding 的回傳 runnable 也要遵守，不能只驗 raw invoke。分析同一節點的正常多次推理不是 retry，但失败後不得用「free-text fallback」另開請求繞過上限。
- Provider 存取錯誤必須向上傳播成可識別的失敗（可用一個最小 exception，含 role/provider/model/attempts/error category）。检查 `structured.py`、analyst 個別 catch、parallel analysts 內外兩層 catch、parallel risk、Trader、Risk Manager、graph、CLI/WebUI/scheduler。不可吞掉例外回傳空 report、舊 state、正常 NO_TRADE 或 completed。
- Provider 失敗：停止本輪 run，取消尚未開始的工作，禁止後續節點、後续 symbols 與 execution；已在途的 thread request 無法強制取消時只允許 bounded timeout 收尾，丟棄結果並禁止新請求，不宣稱瞬間殺死 thread。scheduler 此輪 error 後停下自動 dispatch，需操作者明確重啟；不得下輪立即偷偷重跑。先前已完成的 broker action 不倒帶；停止後的 mutation 必須為零，必要 reconciliation 保留既有安全路徑。
- 請求成功但 schema/bind/validation 不合法：Risk Manager 維持 strict INVALID/NO_TRADE、零 intent/POST，不猜 free text、不為 schema error 再試模型。非 Risk 的合法內容 fallback 可保留，但不能捕捉 provider error。不得自動換 provider、用 checkpoint 舊決議或 stale report 繼續。
- UI/CLI/run log 顯示 run_id、symbol、role/provider/model、attempt count、去秘密化錯誤及 stopped。attempts 必須是真實可追蹤值，不將 401 也固定寫 4；不得記 token/key/auth headers 或完整敏感 URL。

### B3：Trader／Decision 持股 context 與 deterministic 上限

- 現有兩節點已呼叫 `get_positions_data/get_account_info`，但這些 UI helper 的失敗空值不能當「未持倉」。沿用 `capture_broker_snapshot` 的 strict account-bound 資料，抽一個共用 context formatter，替换兩處重複拼字串，不建立第二套 broker snapshot。
- Trader 開始前取得有效 snapshot，Risk Manager 開始前重新取得有效 snapshot；snapshot account 一致且經現有 freshness 檢查。LLM 不在 execution lock 內等待。execution 仍在 lock 內重新取其權威 snapshot；prompt snapshot 不能繞過 pre-submit revalidation。
- 只輸入當前 symbol 的 qty、market value、average entry、unrealized P/L（明示金額/比例）、position weight，以及 account equity/cash/buying power/gross exposure%、observed_at。`weight_pct=abs(position.market_value)/equity*100`；`gross_pct=sum(abs(market_value))/equity*100`。沿用 broker side/sign 的表達，不把 short 變成 long。P/L 或 average entry 若現有 snapshot 未攜帶，可最小擴充既有 snapshot，來源與時點必須一致；非核心展示欄缺失寫 unavailable，不能編 0。
- 完整且成功的 positions 回傳沒有該股才可寫 flat/qty=0；timeout、missing account、NaN/Inf、equity<=0、wrong account、stale/future timestamp 一律 stop/no execution，不能 fallback memory 或把 unknown 當 flat。價格/qty/unit 明確。
- 不主動把 broker holdings 注入 Analysts、Bull/Bear、Research Manager 或 P3 Screening。Trader plan 可自然傳到 risk debate，但不將整個帳戶 dump 至共享 research report/memory。測試檢查直接 prompt injection 邊界。
- 既有 `safety.max_symbol_concentration_pct` 已存在（百分比 0–100）；沿用為唯一 canonical cap，**不另建 `max_single_position_pct` 重複開關**。新曝險的 available headroom=`max(0, equity*cap/100 - abs(current market value) - outstanding increasing notional)`。送單 notional 取 proposed 與 headroom、既有 cash/order/gross/sector 限額之最小值，quantity 按既有精度向下取整後再驗，不足最小單位則 no order。
- 例：equity=100000，持股18000，cap=20%，想買5000 → 最多2000；已持28000 → 禁止增加。上限不代表投資建議；測試可設20%，保留目前預設25%，不默默放寬現有更嚴限制。自動交易需有效正上限，legacy 0 uncapped 在啟用本階段自動模式時 configuration error。
- 以現有 snapshot/open-order policy 避免重複使用 headroom；無法可靠估 outstanding notional 時保守拒絕增加，不猜0。直接 public execution 與 recovery/resubmit 都不能繞過 cap；若現有路徑已阻擋 open orders，不另造 reservation ledger。Position flip 必須分辨已驗證平倉與新開曝險；只 clip 增加部分，verified reducing exit 不因超 cap 被誤擋，但仍遵守 Phase A。

### B4：保留原 Phase B 的資料與 sector 工作

**SEC／IR primary source**：沿 `dataflows/interface`→Toolkit→fundamentals/news 的真實資料路徑新增最小 helper。美股以 SEC 官方 filing 索引/文件及已知公司官方 IR URL 為來源；symbol→CIK 使用官方映射或明確 fixture，不由 LLM 猜 URL/CIK。保留 source/url/published_at/retrieved_at，使用 UTC；以 filing/報告發佈時間判斷，不能因 retrieved_at 新就當 filing 新。SEC/IR 是補充原始來源，不移除既有市場/news fallback；fallback 必須標示來源、缺漏，不能冒充 SEC/IR。

對文件採有限 timeout/response size、官方 host/redirect 檢查、SEC User-Agent 與節流，沿既有 cache；實作時核對官方存取規則，不新增爬蟲框架。先支援最新 10-K/10-Q/8-K 與已設定 IR 發佈頁，不掃全市場 IR；無 IR URL 就顯示 missing，不猜。每類集中一個合理 freshness 設定並說明預設（filing 按其年度/季度/事件性質，不用分鐘級 quote TTL）。missing/future/unparseable timestamp 不可當新鮮；缺失 primary source 明確降級，若分析宣稱依賴它而無其他可用資料，該 symbol no decision；不得把普通資料缺漏偽裝成 provider retry。

**Corporate action quarantine**：沿既有 symbol/assets/bars metadata，以明確 split、ticker change、delisting、non-tradable 事件建立最小可持久化 quarantine 記錄（沿用檔案持久化模式即可，不新增 DB）。記 symbol、reason、effective/observed time、source 與狀態。已知未來事件到 effective 時生效；時間/調整狀態不明則立即 quarantine。禁止用價格大跌猜 split、猜新 ticker 或自動改 broker position/local fills；restart 不清除隔離。禁止新增曝險，保留 Phase A verified reducing exit；reconcile 並在現有 operator/alert 路徑顯示。解除只在權威資料更新、reconciliation CLEAN 且明確 operator 確認後；不能 TTL 到就自動解除。缺少可靠事件 feed 時提供既有設定/人工事件輸入與明確 coverage 限制，不能聲稱全面自動偵測。拆股資料混用 adjusted/unadjusted 不可繼續排名或 sizing。

**Sector constraints**：先找既有可靠 sector metadata，沒有則用小型人工 symbol→sector 設定供 watchlist/持倉，不新增付費來源。使用一個 `max_sector_exposure_pct`（預設30%，百分比，啟用時須0<cap<=100）；以 snapshot 持股加未成交增加曝險計 sector headroom，與 symbol/gross cap 取最小。任何持倉或候選 sector unknown 導致不可證明上限時拒絕新增風險並提示缺哪個 mapping；不隨便分散成各自 unknown sector。純平倉沿 verified exit。集中一個 evaluator，真實 execution caller 必須使用。

### B5：驗證現有策略模組與交付

保留 correlation、volatility sizing、regime、memory/reflection：先跑既有 tests，沿 caller 確認真的生效；補必要的 deterministic fixture（高相關抑制新增風險、regime scaling、memory maintenance/reflection 與 broker facts 不混淆）。已有等價測試就引用，不為「加功能」換演算法。Kelly 保持關閉；memory 不是即時持倉。記錄 before/after fixture 結果與有無需調整，不以 mock 測試宣稱報酬提升。

## 驗證、完成定義與輸出

每個 B 項先完成最小整合與 focused regression，再到下一項。最後跑 `python -m pytest tests/ -q`、`python -m compileall -q tradingagents cli webui`、`git diff --check`。使用符合 CI 的隔離 Python 環境；不把缺 dependency、skip 或中止算通過，不自行安裝大套件替代調查。至少保持既有 Phase A、structured、mock graph、LLM、safety、portfolio、regime、memory suites 綠，新增測試覆蓋本文件 acceptance matrix 的失敗路徑。

更新 README、ARCHITECTURE 與 PROJECT_GOALS_AND_STATUS 的實際 config、操作方式、coverage 和 pending acceptance；不把計劃當 runtime。交付依序列：改動檔案/用途、B1–B5 完成與未完、exact commands/counts/外部call=0、風險與已知限制、獨立驗收的 base/HEAD/dirty diff。只能報 `Implemented — pending independent acceptance`；缺前置或必要能力則明確未完成，不能自行 Accepted，不開始 P3。
