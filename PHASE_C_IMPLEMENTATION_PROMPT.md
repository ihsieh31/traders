# P3／Phase C 實作提示詞：全美股篩選、獨立 Screening Provider 與 Top20

## 任務與前置條件

前置為 P2 在當前基礎 revision 已 fresh independent Accepted。Paper observation／人工 watchlist 長期穩定運作證據不阻擋 P3 實作與離線驗收；啟用長期無人值守 Paper 自動交易前仍須完成 observation。使用者已要求規劃全市場選股，不需為「是否需要此功能」重問；但執行本提示前仍須有 P2 acceptance evidence。本階段沿用 P2 provider/retry/context/風險控制，不重做。保留原 Phase C 的 Universe、liquidity/eligibility、deterministic ranking 與既有 multi-agent pipeline，加入第三角色 Screening 與每天一次的 Top20。

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

### C1：獨立 Screening 角色與 Universe

- 加 `screening_provider`、`screening_model`、`screening_backend_url`，沿用 P2 相同 credential/config/CLI/WebUI 解析模式。自動選股啟用時 provider+model 必填，缺失 startup error；可與 Analysis 同 vendor，但有獨立可配置值，不能暗自 fallback。停用 screener 時不建立/呼叫 Screening client。保留手動 watchlist 模式且預設不自動切換。
- Screening 僅篩選候選，不形成 TradeIntent、不拿 broker key、不接 execution tools。三角色 provider/model/endpoint/key 分別生效，用三個不同 fake provider/model 做整合證明。重用 P2 `llm_max_retries=3`，最多4次實際請求，失敗整輪 stopped，禁止跨 provider fallback。
- Universe 從 Alpaca 完整 ACTIVE US_EQUITY assets 取得（處理 API 有分頁時的所有頁），只保留 tradable=true。`_get_searchable_assets/search_assets` 的搜尋 limit 或 fallback 靜態名單不能當全市場 universe。沿用現有 client，不建立 security master；stock screener 不混入 crypto。
- 使用 batch daily bars 和 bounded chunk（預設每批100 symbols，現有服務限制更低則取低），不逐股抓新聞/fundamentals，不做新增 thread pool。某 symbol 資料壞可排除並記原因；整體 assets/bars 失敗或無法確認完整性則 run failed，不以舊名單或默認熱門股冒充完成。

### C2：確定性 eligibility 與 Top40 公式

以下是本次規劃採用的可測試預設，目的為研究優先順序，並非收益保證；寫入集中設定/常數，不把每個係數變成 UI 開關。

1. as_of 為最近已完成的美股 regular trading session，按既有交易日曆/market-hours 處理 NY timezone、DST、週末/假日；當日未收盤 bar 不使用。需要至少 **61 根完整有效 daily bars** 才有60日報酬（對話的「約60」在此精確化）。最後一根必須屬 as_of，不能用「72小時內」替代交易日判斷。bars 依交易日排序且唯一、價格>0、volume>=0、無 NaN/Inf，必需窗口不得缺交易日；修復不了則排除，不 forward-fill。採同一明示 adjustment policy；split 未解決或混合 adjusted/raw 就 quarantine/exclude。
2. 最新完整收盤價>=5美元；最近20日 `mean(close*volume)>=20,000,000` 美元。排除 P2 quarantine/non-tradable。臨界值相等合格。
3. 對每個合格標的計：`r5=C[t]/C[t-5]-1`、`r20=C[t]/C[t-20]-1`、`r60=C[t]/C[t-60]-1`；`adv20=mean(C*V,last20)`；`vol20=sample_std(last20 simple daily returns,ddof=1)*sqrt(252)`；`volume_ratio=mean(V,last5)/mean(V,last20)`（分母0則排除）；`trend=C[t]/mean(C,last20)-1`。所有單位寫入 compact table。
4. 在全部合格 universe 上，對 adv20、r20、r60、vol20、volume_ratio 計 ascending percentile：`p=(average_rank-1)/(n-1)`，同值用平均 rank，n=1 時p=0.5。score=`100*(0.20*p_adv20+0.25*p_r20+0.25*p_r60+0.15*(1-p_vol20)+0.15*p_volume_ratio)`。這是固定 baseline，不做 ML 或 backtest 調參。
5. score 降序，完全同分時 normalized symbol 字母升序，取最多40。同一輸入改順序仍得到同結果。合格20–39檔時送全部；少於20時整輪 `INSUFFICIENT_CANDIDATES`，不補不合格股票，不呼叫 Screening 或新增 execution。記排除總數、原因、as_of、資料時間/adjustment/config版本即可，不建立 evidence platform。

### C3：Screening Top20 契約與策略

一次將 Top40 compact table 交給 Screening：symbol、price、adv20、r5/r20/r60、vol20、volume_ratio、trend、deterministic score，及 P2 有提供才加的 sector。不给持倉、cash、buying power、完整新聞或全市場 bars。

提示要求比較「趨勢持續性（20/60日）、近期量能/5日變化、流動性、波動風險」，選最值得完整研究的20個機會；不能輸出 BUY/SELL、股數或權重，short_reason 必須依 table 的實際因子，不能杜撰新聞/財報。同 provider 下優先低隨機性既有設定，但不要求 LLM 結果 deterministic。

沿用既有 structured-output 技術，回傳恰20筆，每筆只有 `rank`（1..20且唯一連續）、`symbol`（input成員且唯一）、`screening_score`（有限0..100）、`short_reason`（非空且<=300字元）。禁止 unknown/missing/extra fields；回傳按 rank 排序，score只是研究評分，不當 calibrated probability 或資金比例。strict 驗證成功前不可存成可用 selection。

Sector metadata 全部候選可用時，每 sector 最多5檔，先確認候選 capacity `sum(min(count_by_sector,5))>=20`；不足則 `INSUFFICIENT_SECTOR_CAPACITY`，不偷偷放寬。部分/全部 unknown 時，依對話不新增 data source，整次 selection 明示 `sector_diversity_applied=false` 與缺漏；不將 unknown 假設成多個 sector。P2 execution sector cap 仍照常 fail closed，這個篩選降級不能放寬交易風險。

19/21筆、重複、越界symbol、NaN、rank錯誤、空reason、sector超5都 fail closed；不補齊、不截20、不 deterministic fallback、不換模型、不做第二輪 LLM repair。Provider暫時錯誤才套 P2 retry，成功但輸出不合法則 screening failed，零下游 analysis/intent/POST。

### C4：Top20 聯集持股、單日 cache 與 scheduler

- `entry_candidates=Top20`；`deep_analysis_set=Top20 UNION fresh broker current_positions`，normalized去重。順序 Top20 按rank，額外持股按symbol排序；20檔加5持股其中2重複＝23檔，各只分析一次。持股失敗/unknown 不能當空集合；整輪停止。
- 只有 Top20 可新增曝險；額外持股只可 HOLD/SELL/verified risk-reducing exit，不能加倉、flip後新開或short。Top20與持股重疊可提出加倉，但仍受 P2 頭寸/sector/gross 與 Phase A gate。entry eligibility 是程式衍生，不讓 LLM 自報；在實際 execution dispatch 再驗（直接 caller、checkpoint resume 都不能繞過）。手動 watchlist 仍走其明確模式，不被舊自動名單默默授權。
- 非tradable/quarantined或缺行情的持股也必須列入 held review：明示 blocked/review reason，不為了湊完整分析去捏造行情；只有可安全分析的才呼叫 graph，exit僅沿既有 verified路徑，不能因不在Top20就直接平倉。其他 asset class 的現有持倉保留現有管理路徑並清楚列出，不混入美股新entry ranking。
- Trader與Decision逐symbol使用 P2 context；Decision前refresh，送單前再按Phase A取權威snapshot，不能用選股開始時一份快照跑整天。每筆交易後既有reconcile，後一股不重用已消耗的headroom。沿現有串行symbol dispatch，不建立全市場分析並行/optimizer。
- 每個美股交易日一次全市場scan＋一次Screening logical invocation；之後每輪使用本交易日已驗證Top20。非交易日不開新scan/新entry；held風險管理按原規則。沿既有小檔案cache/atomic replace即可，包含 trading_date、as_of、generated_at、role/model、relevant config fingerprint、Top40特徵與validated Top20、sector模式；檔案不可含credentials。key不綁持倉，持倉每輪重取，Screening仍看不到。
- 讀cache重驗schema、input membership、date/config、數據時點；隔日、損壞、未來日期、config/provider/model変更不可當有效selection。重啟有效cache可重用；首次scan前用現有process/scheduler保護或最小stdlib file lock避免雙執行者重複scan/寫檔，不在broker execution lock內等LLM。
- 人工refresh是明確新scan，失敗不能退回本日舊selection或隔日cache，本輪停止；把該cache標失效/移除，resume需明確重試成功。成功selection不表示每輪可以重送同一decision，Phase A冪等仍生效。
- Screening/Analysis/Decision任一Provider耗盡：P2的run stop一路傳回UI/CLI/scheduler，禁止後續候選execution。已完成訂單如實保留，不能宣稱整日零成交；stop後零新增mutation，reconciliation/已授權安全流程不受破壞。

### C5：整合與交付

UI/CLI加入最小自動/人工模式、Screening角色設定、Top20/選取理由、selection日期、refresh和stopped原因；沿現有元件，不重做dashboard。完整mock跑 `assets→bars→Top40→Screening→20∪positions→Analysis→Decision→execution gate`，執行端為mock broker/temporary SQLite。必須證明helper真的接到scheduler，而非只測獨立screener函數。

## 驗證與完成定義

新增或補足對應 acceptance matrix 的deterministic fixtures、transport計數與public-entry整合測試，沿既有框架不建新的測試系統。測試至少含少於20/少於40、交易假日/未收盤bar、同分、各種invalid LLM輸出、cache刷新失败、持股不在Top20、三角色fail-fast、P2 cap與Phase A冪等回歸。

最後跑 `python -m pytest tests/ -q`、`python -m compileall -q tradingagents cli webui`、`git diff --check`；記錄actual counts/duration/skips與外部call=0。更新README、ARCHITECTURE、PROJECT_GOALS_AND_STATUS，說明公式、限制、cache/date/retry語義、啟用/refresh方法和data coverage。交付 C1–C5完成表、base/HEAD/dirty diff、測試證據與未覆蓋真實供應商資料限制；只給 `Implemented — pending independent acceptance`，不自行Accepted，不啟動無人值守Paper run。
