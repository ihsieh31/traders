# tradingAlpaca 審查修復接手與完成紀錄

日期：2026-09-17
接手版本：`fd3819a`；原始審查版本：`7953ca0`。
範圍：兩份審查報告與第二意見中的 E01–E07、L01–L11、D01–D14、U01–U20，共 52 項。

## 結論

**原 agent 大致有照「交易安全 → LLM → WebUI 執行與排程 → 資料」的順序修復，沒有發現必須撤回整批修補的順序問題。但提交過修復不代表問題已完整解決：部分只修了一半，D04 還引入 daily bar 回歸。已保留原有修改並補完本次確認的遺漏。**

- 接手時已有 28 個修復提交；另有尚未提交的 D05 程式與測試，已接續完成。
- 當時完全未完成的項目：**L06、L07、L09–L11、D06、D09、D11–D13、U03–U05**。
- 需要補強／更正的項目：**E01、E02、E04、L04、D03–D05、D07、D08、U01、U06、U14、U16**。
- 本次完成後的完整離線 suite：**1,487 passed，0 failed，2 個第三方警告**。新增一份含 33 個案例的回歸測試，包含真正 ExecutionService＋fake broker 的到期拒單重試與缺失成交恢復。
- wheel 已在暫存目錄建立，驗證 welcome、CSS、prompt 資源存在；離開專案目錄後匯入已打包模組與 CLI help 也通過。

修補保留在工作目錄，可直接檢閱 diff；沒有代替使用者執行真實下單、付費 LLM 呼叫或啟動長期交易觀察。

## 接手時的重要缺口與本次處理

| 項目 | 原修補還缺什麼 | 完成的處理 |
|---|---|---|
| E01 | 雖讀取 remaining lots，仍可能以某個歷史 protected entry 判整個 symbol 欠保護；ledger 錯誤也可能被忽略。 | 逐筆尚未沖銷 lot 判斷義務，只計算需要保護的數量；歷史已平倉的 stop 不污染新的無保護 lot。讀不懂 ledger／intent 回 PROTECTION_GAP。 |
| E02 | 舊拒單已回報失敗，但固定 deadline decision ID 仍無法完成後續退出。 | 帳戶鎖內選 durable attempt；只有原退出已證明終止且成交數量已對帳，才使用新 ID。保留 PENDING／SUBMITTING／UNKNOWN／ACCEPTED／PARTIAL 的原 ID，每個 deadline 最多 3 次。重新確認即時殘餘部位與衝突單後才送出。 |
| E04 | ACCEPTED 可查 client ID，但 adoption 沒記查詢結果中的新增成交；position mismatch 也可能讓唯讀查明根本無法開始。 | 在任何恢復 POST 前查明缺失 ACCEPTED／SUBMITTING／PARTIAL 的 broker 事實，驗證 client ID、broker ID、symbol、side 與累計數量；按累計成本差記增量成交，重新擷取帳戶事實。缺證據繼續 PAUSED，不重送 ACCEPTED。 |
| L04 | prompt value 角色保留了，但 AI 工具請求及 ToolMessage 仍可能消失。 | LangChain 標準訊息轉換，輸出配對 function_call／function_call_output，保留 name、arguments、call_id；不支援的角色／輸入明確失敗。 |
| D04／D05 | 只裁切 reference session 的部分 bars，daily midnight timestamp 因而被錯誤排除；盤中只比 session date。 | 每根 bar 使用自己的實際 session close，daily 依 session 收盤完成，盤後開始的 intraday bar 排除；盤中按預期完成時間與最多一個 timeframe 延遲判斷，不跨缺失交易日。歷史日曆使用單一範圍抓取，避免每根 bar 都打遠端 API。 |
| D03／D07 | 已拒部分 NaN，但 OHLC 結構、數值轉換、比例範圍等仍有洞。 | ATR 拒非正價格與不合理 high／low／close；sizer 驗證 gross exposure、ATR、stop、risk parameters 與最終 notional 的有限性／合理範圍。 |
| D08 | 只 clamp min_size_factor；其他 factor 或 book 數字不合法時仍可能放大 requested amount。 | 參數與部位數字非有限時拒新增曝險；最後調整金額不高於原 requested amount。 |
| U01／U14 | chunk ownership 已修，但 final status 未指定來源 symbol，worker cleanup 的 generation check 與清 flags 還在鎖外。 | 最終 agent status 指定 ticker；worker 例外／finally 與相關排程失敗清理在 ownership lock 內重新驗 generation。provider failure 與 analysis cleanup 也在同一鎖內驗證。 |
| U06／U16 | 首次載入 env 已隱藏，但「Load from .env」仍回傳 server secret；執行中仍可切 key；Clear 空字串仍 fallback env。 | 所有 env 載入入口只回空 input／configured 狀態；分析或排程啟用時停用按鈕，server 也在 ownership lock 內拒切 key。顯式清除的空值阻止一般 get_api_key 的 env fallback；普通空白 override 使用 None 保留 env。Clear 標記、重新載入與 UI 說明一致。 |

E02 的 3 次是**原 attempt 加 2 次後續 attempt**，不是每次掃描都再給 3 次。若 broker 查詢不確定、live order 尚在、成交數量尚未記帳或重試額度用完，流程回 PAUSED／明確錯誤，需操作人員查明；這是保留的保守行為，不應靠盲目新 ID 繞過。

U06／U16 維持原本單使用者、process-global credentials 的工具架構。Clear 作用於畫面列出的 runtime keys；獨立的 role-specific／embedding server credentials 維持各自既有設定，並非新增全帳戶 secret vault 或多租戶隔離。

## 52 項完成狀態

「沿用」表示保留原 agent 的已完成修補，並納入本次完整離線回歸；不表示對每項都另做 live 重現。「補修」表示接手後完成遺漏、修正回歸或補足原修法。

### 交易與恢復

| ID | 處理 | 目前方法 |
|---|---|---|
| E01 | 補修 | 尚未沖銷 lot 的保護義務，ledger 無法證明則暫停。 |
| E02 | 補修 | 拒單結果真實；broker 證明終止後有界 durable retry。 |
| E03 | 沿用 | 產業上限拒絕分類不明的 exposure-adding 在途單。 |
| E04 | 補修 | 缺失訂單有界唯讀 lookup，驗 identity 並恢復成交。 |
| E05 | 沿用 | 隔離紀錄合併最早生效時間，立即事件不被 future event 遮蔽。 |
| E06 | 沿用 | all_active 先 reload 再列目前紀錄。 |
| E07 | 沿用 | programmatic resume 的 load／increment／save／run 共用 runner lock。 |

### LLM 與報告契約

| ID | 處理 | 目前方法 |
|---|---|---|
| L01 | 沿用 | graph 明確傳 broker account identity。 |
| L02 | 沿用 | analyst tool loop 優先標準 tool_calls，兼容 raw calls。 |
| L03 | 沿用 | Chat Completions constructor 收到配置 timeout。 |
| L04 | 補修 | prompt roles、工具 call／output 與 ID 正確映射。 |
| L05 | 沿用 | parallel merger 優先明確 report field。 |
| L06 | 補修 | backend URL 日誌使用既有 sanitize_url。 |
| L07 | 補修 | extract 使用最後明確 final marker；渲染移除舊 final markers，保留分析及唯一 canonical proposal。 |
| L08 | 沿用 | 保守風控 prompt 符合 full-close 能力，不要求未支援的 partial／trailing。 |
| L09 | 補修 | 章節輪流取 coverage points，避免前段候選耗盡名額。 |
| L10 | 補修 | candidate 對應實際來源 chunks，跨 chunk 候選保留相關 refs；claim 渲染顯示 refs，不捏造前兩 chunk 引用。 |
| L11 | 補修 | coverage pass 與 relevance pass 都遵守 max_chunks。 |

### 資料、回測與數值

| ID | 處理 | 目前方法 |
|---|---|---|
| D01 | 沿用 | OHLCV 有限性、價格／volume／range、timestamp 與去重驗證。 |
| D02 | 沿用 | Yahoo fallback 拒不支援的 4h／minute interval，不冒充等价 bars。 |
| D03 | 補修 | ATR 對非有限及不合理 OHLC 回 None。 |
| D04 | 補修 | own-session completion，修復 daily／early-close 回歸。 |
| D05 | 補修 | 盤中完成時間與延遲期限驗證，接續原未提交修改。 |
| D06 | 補修 | backtest normalize 拒非法 OHLCV；performance metrics 拒非有限 equity／PnL，避免污染教學結果。 |
| D07 | 補修 | PositionSizer 輸入、參數及輸出數值檢查。 |
| D08 | 補修 | floor 合理化、非有限 book 拒新增、不得把 requested amount 放大。 |
| D09 | 補修 | 有效零波動 percentile=0；其他 ties 使用 midpoint percentile。 |
| D10 | 沿用 | 保留股票 share class 與 crypto quote identity，Yahoo share class mapping 明確。 |
| D11 | 補修 | 歷史 window 傳 end；date-only inclusive、精確 instant 不加一天；歷史報告不抓 current quote。 |
| D12 | 補修 | offline CSV 與 online 共用 indicator 計算，先截止到 requested date，明確回值／unavailable。 |
| D13 | 補修 | override 納入 cache fingerprint，必須符合 authoritative completed session；cache as_of 重新驗預期日期。任意回溯 override 不可用于正式 selection。 |
| D14 | 沿用 | YoY 比去年相同曆月，缺月不硬算。 |

D13 選擇驗證 override 與日曆一致，保留合法的既有離線 fixtures；沒有為研究 override 放寬 production entry gate。這比一律拒絕 override 更兼容，也能修掉原本的 stale-cache 漏洞。

### WebUI、CLI 與包裝

| ID | 處理 | 目前方法 |
|---|---|---|
| U01 | 補強沿用 | chunk symbol＋generation 在鎖內驗 ownership；final agent status 指定 symbol。 |
| U02 | 沿用 | 每轮 reset update counters。 |
| U03 | 補修 | 一次初始化還原 controls；保存 scheduling、screening 與 advanced model settings，未還原前不覆寫 local settings；仍需明確按 Start。 |
| U04 | 補修 | summary 獨立 callback，隨 interval／refresh／key store 更新數字與 UTC 時間。 |
| U05 | 補修 | welcome 使用 importlib.resources；wheel／sdist 包含 welcome 與 CSS。 |
| U06 | 補修 | server env 不回 client，執行中 server-side 禁切 credentials。 |
| U07 | 沿用 | 移除依 display symbol 猜 report key 的 patch。 |
| U08 | 沿用 | 整點排程只接受實際 session 可用時段。 |
| U09 | 沿用並補強 | 無可證明 slot 時停止顯示錯誤；失敗清理不污染新 generation。 |
| U10 | 沿用 | 每個 candidate date 重新建立 Eastern wall time，處理 DST。 |
| U11 | 沿用 | 新轮 reset 清 investment／risk debate state。 |
| U12 | 沿用並補強 | missing state 回明確失敗，finally 不下標 None；cleanup 加 ownership lock。 |
| U13 | 沿用 | ordinary exception 回失敗 result，caller 顯示真實狀態。 |
| U14 | 補強沿用 | startup failure／thread start failure 清 flags，鎖內驗自身 generation。 |
| U15 | 沿用 | auto-screening 不要求人工 ticker。 |
| U16 | 補修 | uninitialized／cleared 分開；Clear 的 disabled runtime 與畫面說明一致。 |
| U17 | 沿用 | 移除假的 Copy／Export 成功按鈕與回呼，沒有新增未實作的成功宣稱。 |
| U18 | 沿用 | debate parser 按完整 prefix 長度切除。 |
| U19 | 沿用 | 手動 mock helper 不覆寫已有真資料。 |
| U20 | 沿用 | 移除無效 :contains CSS。 |

## 驗證與修改範圍

1. **接手基線：**完整 suite 原有 4 failed、1,450 passed；3 個 daily／歷史完成時間回歸，另 1 個舊 freshness 期望與 D05 新契約衝突。已修來源並更正過期測試期望，沒有隱藏或刪除失敗案例。
2. **最終完整離線 suite：**使用既有 runner，環境清潔、dotenv 停用、網路 guard、狀態／資料庫／結果隔離到暫存目錄；1,487 passed、0 failed，40.89 秒。最終驗證產物：`/private/var/folders/46/3vjy25x94rvgpv7dj_1h3snm0000gn/T/tradingalpaca-refactor-pujoz0p0`。兩個警告來自 websockets legacy 與 LangGraph serializer，未壓掉警告或為消警告升級整套依賴。
3. **新增驗證：**[test_audit_repair_completion.py](/Users/zongen/Downloads/codex/tradingAlpaca/tests/test_audit_repair_completion.py) 包含 33 案例，涵蓋拒單後真正 close、重試上限、live／UNKNOWN 不换身份、不一致成交拒重試、缺失 ACCEPTED 成交恢復、歷史 lot 不污染新 lot、Responses request shape、唯一 proposal、backtest 數值、offline indicator 截止日、key 保護、設定還原、實際 Dash callback 註冊、摘要 refs、chunk cap、日期 override、歷史 request／quote 與 wheel smoke。
4. **既有證據保持：**execution／long-run characterization 仍對照原始 hashes；沒有更新 baseline hashes 來掩蓋差異。`git diff --check` 通過。原 agent 的已提交修補、未提交 D05、原第二意見文件與其他未追蹤工作均保留。

重跑命令：

```sh
python3.12 scripts/refactoring/run_tests.py --
```

核心修改集中在既有 execution、LLM adapter、dataflows、screening 與 WebUI callbacks；沒有新增交易服務、向量資料庫、多租戶 key 系統或分批／trailing 策略。

## 仍待實際運行確認的範圍

本次已補完可由來源與離線 fixtures 驗證的已知缺口。**完整測試通過不等於已完成真實 Paper 整合或 30 天觀察。**

- 真實 broker 接受／拒絕、protective order 時序、listing／client-ID API 行為，仍需短程 Paper smoke；API 暫時不可用與未知成交仍應暫停，不用重試新 ID 強行解鎖。
- 各 live LLM provider 的服務端訊息接受度、vendor feed 延遲與真實交易日曆，未做付費／網路 smoke。
- WebUI 驗證完成到 Python 回呼／真正 Dash 註冊；沒有宣稱已做瀏覽器逐頁操作、重載 localStorage 與多 tab 交錯驗收。
- 未重建 exact-lock 環境，也未執行長期 soak。這些是 operational 驗證範圍，不能冒充本次程式修復的結果。

原報告中的操作流程與修復建議作為審查資料；本次執行範圍依使用者「確認尚未修的項目並修完」的要求決定。
