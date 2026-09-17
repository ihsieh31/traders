# tradingAlpaca 審查報告複核與最小處理方案

日期：2026-09-17
複核版本：`7953ca0`，與原報告相同。
範圍：原報告 E01–E07、L01–L11、D01–D14、U01–U20，共 52 項；另檢視驗收建議。
交付：第二意見與處理方案，**沒有修改產品程式或啟動交易**。

## 1. 判斷與下一步

**原報告大部分具體問題有程式碼依據，核心交易缺陷不是過度工程化。需要調整的是嚴重度、使用情境、部分描述，以及修復範圍。不能把 52 項全部當成 30 天 CLI Paper 觀察的阻擋項。**

先處理 E01、E02、E05；啟用產業上限時一起處理 E03。接著修實際使用的 LLM 路徑：L01、L03、L04，以及使用 Anthropic／Google 時的 L02。若使用 WebUI 自動交易或排程，再處理 U01、U07 與排程／失敗狀態問題。

本次沒有找到足以撤回核心 E01／E02／E05 的反證，也沒有證明真實 broker 發生錯單。多數 UI、數值與文字契約問題成立於模組層，不等於已穿透執行器的帳戶綁定、typed intent、即時快照及交易上限。

### 複核證據

- 對照目前來源、正式呼叫者、預設設定、README、Docker 綁定位址及已安裝 provider adapter 原始碼。
- 對 **23 個原編號**做自己的離線重現或函式邊界檢查：E01、E02、E03、E05、E06；L01、L03、L04、L07、L09、L11；D03、D07、D08、D09、D10；U01、U02、U07、U08、U09、U10、U11。
- E01／E02 是實際函式搭配最小 stub 的邊界檢查，**本次沒有重跑原報告的完整 service＋fake broker 重現**。L01 使用真正 StateGraph；L04 使用真正 adapter 並攔截生成函式；U07 執行抽出的原 patch，沒有建立 Dash app。
- 重跑四份既有相關測試：**61 passed，2 warnings，2.72 秒**。使用現有離線 runner：清潔環境、停用 dotenv、暫存狀態、Python 網路 guard。沒有 broker、LLM、webhook 或 browser 操作，未读取 `.env` 內容。
- 沒有重跑完整 suite、exact-lock 驗收或 wheel build。原報告的 1363 tests＋275 subtests 與 wheel 結果是原 agent 提供的證據，不冒充本次結果。第三方版本結論只適用於本機已安裝 adapter。

表中「成立」表示本次來源支持所述局部行為；不代表所有後續影響都已端到端重現。「有条件」指出 opt-in、非預設設定、特定 provider 或手動入口。「需補證」表示正式情境或競態尚未由本次執行證明。處理優先序是依產品使用路徑調整，沒有 CVSS／CVE 判定。

## 2. 誤判、描述修正與需要降級的地方

| 原項目 | 本次更正 | 對處理方式的影響 |
|---|---|---|
| L09 | 預設 `report_context_max_points_per_report=8`，不是 6。每節可取 2 點時，預設會保留前 4 節，可能排掉第 5 節；前 3 節耗盡 6 點只在上限設為 6 時成立。每節只有 1 個候選時，後段也可能保留。 | 章節順序偏向成立；「第 4／5 節必定丟失」不成立。應做章節均衡選取，不能把全域分數排序當成必然正確。 |
| L07 | 報告留下 production caller 不明的限制；本次已找到 Research Manager、Trader、Risk Manager 的正式呼叫者。但 Trader 在缺 action 時才補 completion，Risk Manager 最終 action 由 typed intent 決定。 | 文字 helper 有問題、正式使用也存在；「附加更正必然造成正式錯單」仍未證實。 |
| L11 | `max_chunks=1` 能取出 3 chunks，契約違反成立；預設上限 16、每 report 至少 1 chunk，5 類 report 的 coverage pass 不會單靠預設就超過 16。token budget 也仍有效。 | 非預設配置問題，降為低優先契約修正；不用建新 retrieval 系統。 |
| D07 | `risk_sizing_enabled=False` 是預設值；函式接受 NaN 的問題成立，但目前預設自動交易不會啟用這層 PositionSizer。 | 啟用前修數值驗證；不能宣稱預設路徑已繞過風控。 |
| D08 | `min_size_factor=2` 的確放大金額，但 2 是非預設錯誤配置，正常預設 0.25；執行層仍有獨立裁切。 | 簡單驗證 factor 並 clamp；不必重寫 sizing pipeline。 |
| D09 | 零波動得到 percentile=100／hostile／0.5 已重現；結果是縮小曝險。把零波動視為 calm 或資料停滯需產品定義。 | 建議降為 P3／研究語意問題，不列為交易安全阻擋項。 |
| E04 | README 已明列 F-02／ACCEPTED／PARTIAL recovery 的已知限制；這不是全新的未知缺陷。PAUSED 是正確保守行為。 | 納入既有 backlog，新增唯讀查明能力；不能直接加入允許 resubmit 的白名單。 |
| U01 | 舊 chunk 可以污染新 run state 已重現；final result 與 pre-trade 仍有 generation guard。 | 修串流 ownership；原 P1 應依是否使用 WebUI 自動執行排序，不當成已證明錯單。 |
| U06 | server env → browser 的資料流成立；多 tab 共用 process credentials 也成立。Docker 容器 listen `0.0.0.0`，但 compose host port 綁 `127.0.0.1`，不能只看容器 host 判定已對外公開。 | 本機也應修 secret 回傳及 run 中切換；「跨使用者外洩」需額外部署／存取證據。不要直接擴建多租戶平台。 |
| U12、E07、U14 | 來源有早退 cleanup／鎖外寫入問題，但本次未執行 scheduler 或多程序完整交錯。 | 保留局部修補與針對性驗證，區別靜態 race 與已發生競態。 |
| U17–U20 | 未實作按鈕、手動 helper、字元索引與 CSS 是實質小問題，但影響與下單保護不同。 | 放維修清單，不全部升格為 Paper 觀察阻擋項。 |

原報告已主動撤回 tracked runtime DB、daily budget、double-start 等推論，這些撤回是合理的。本次沒有將它們重新列為問題。

## 3. 52 項逐項結論與最小修法

### 3.1 交易、風控與恢復

| 編號 | 結論／優先序 | 本次依據與最小處理 |
|---|---|---|
| E01 | 成立；優先修 | [tradingagents/execution/protection.py:405](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/protection.py:405) 的無 child fallback 只認 FILLED。本次同一 4 股部位、canceled parent 得 `[]`，FILLED parent 得 GAP。用 durable fills／**尚未沖銷的程式開倉 lots**判義務，涵蓋 canceled／expired partial。不要只改成 `filled_qty>0`，否則很久以前已平掉的單也可能把新人工部位誤判為欠保護。 |
| E02 | 成立；優先修 | [tradingagents/execution/lifecycle.py:38](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/lifecycle.py:38) 固定 deadline ID；[tradingagents/execution/exits.py:159](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/exits.py:159) 對舊 REJECTED row 回 `success=True,deduped=True` 已重現。先讓結果真實反映拒絕／未完成並告警；只在 broker 查明終止、無 live／UNKNOWN close、重新核算殘餘部位後，允許新的 durable exit attempt。不能每次掃描換 ID 盲送。 |
| E03 | 成立；產業上限啟用時優先修 | [tradingagents/risk/exposure.py:251](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/risk/exposure.py:251) 檢查 candidate／持倉分類，沒有完整檢查增加暴露的在途單。本次未知 MSFT $29k 時 AAPL 核准 $10k，補 Tech mapping 後只核准 $1k。拒絕分類不明的**新增暴露**；已證明純減持的單不應被無差別擋住。 |
| E04 | 成立；已知可用性限制 | [tradingagents/execution/recovery.py:487](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/recovery.py:487) 與 [tradingagents/execution/store.py:434](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/execution/store.py:434) 不處理 ACCEPTED 的缺失 lookup。加入有界唯讀 client ID 查詢並 adopt broker 終止事實；查不到繼續 PAUSED。與既有 F-02 合併處理，勿放寬送單權限。 |
| E05 | 成立；優先修 | [tradingagents/risk/corporate_actions.py:197](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/risk/corporate_actions.py:197) 只按 reason＋active 去重，忽略生效時間。本次 future split 後追加立即 split 回 `effective_at=None`，實際 active=0。既有檔案鎖內合併較早生效時間（None 代表立即），或保留不同事件；回傳實際保存紀錄。 |
| E06 | 成立；一般維修 | [tradingagents/risk/corporate_actions.py:272](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/risk/corporate_actions.py:272) 先取旧 keys 才在各 symbol reload。雙 instance 重現 `all_active=0`、`active_for=1`。先 reload 再列 keys，必要時讀同一份 snapshot。這是狀態漏顯示，不是 Gate 直接失效。 |
| E07 | 靜態成立；programmatic resume 情境需競態驗證 | [tradingagents/long_run.py:1558](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/long_run.py:1558) 先讀 state、探鎖後釋放、寫 state、再拿鎖。把 load→increment→save→resume 包在同一 runner lock，統一 lock busy 轉譯。CLI 已有较完整的鎖，不需要新增分散式鎖或第二套 runner。 |

### 3.2 LLM、圖狀態與研究契約

| 編號 | 結論／優先序 | 本次依據與最小處理 |
|---|---|---|
| L01 | 成立；修正式圖契約 | [tradingagents/agents/trader/trader.py:251](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/trader/trader.py:251) 未 return identity，[tradingagents/agents/utils/agent_states.py:67](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/utils/agent_states.py:67) 沒 channel。本次真 StateGraph 下游 identity=None。新增 state channel，Trader 明確 return，Risk Manager 用它檢查相同帳戶；保留 executor DB binding。不是已證明跨帳戶送單。 |
| L02 | 成立；Anthropic／Google 路徑優先修 | [tradingagents/agents/analysts/market_analyst.py:210](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/analysts/market_analyst.py:210) 等五 analyst 只迴圈讀 raw `additional_kwargs.tool_calls`；本機兩 provider 的 adapter 產出標準 `.tool_calls`，content normalizer 沒補回 raw calls。優先讀標準 calls、兼容既有 raw 格式，正確保存 call ID／ToolMessage。各訊息形狀做離線 fake-message 測試即可；不用強制所有 provider live 驗收。 |
| L03 | 成立；小修 | [tradingagents/agents/utils/gpt5_llm.py:768](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/utils/gpt5_llm.py:768) pop timeout 後 Chat 分支未傳回。本次 `timeout=42` 得 `request_timeout=None`。把 timeout 傳給 Chat constructor；保留單一重試 owner。這是自訂值失效，不是完全無 timeout。 |
| L04 | 成立；Responses 路徑優先修 | [tradingagents/agents/utils/gpt5_llm.py:743](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/utils/gpt5_llm.py:743) 把 ChatPromptValue str 化。本次 system＋human 變成一個 HumanMessage；ToolMessage 轉換為 `[]`。使用 LangChain 標準訊息轉換／`to_messages()`，正確映射 function call／output；不支援的形狀明確失敗。補離線 adapter request-shape 測試，勿改成把所有角色攤平。 |
| L05 | 成立；證據一致性修正 | [tradingagents/agents/analysts/market_analyst.py:302](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/analysts/market_analyst.py:302) 的 field 加 regime、message 未加；[tradingagents/graph/setup.py:367](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/graph/setup.py:367) 優先 message。merger 優先明確 report field；或讓 message 與 field 同步。直接修資料來源優先序，不用新增 merger framework。 |
| L06 | 成立；含 secret URL 時修 | [tradingagents/graph/trading_graph.py:108](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/graph/trading_graph.py:108) 原樣印 backend URL。輸出前使用既有 sanitize，必要時只印 scheme／host。沒有檢查或輸出真實密鑰，也未證明目前設定含 token。 |
| L07 | 函式缺陷成立；正式錯單推論未成立 | [tradingagents/agents/utils/agent_trading_modes.py:134](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/utils/agent_trading_modes.py:134) 固定 action 搜尋順序；[tradingagents/agents/utils/agent_trading_modes.py:297](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/utils/agent_trading_modes.py:297) 保留舊 proposal。本次先 BUY、附 SELL 仍 extract BUY。正式 caller 已找到；應以已選出的 typed action 渲染唯一標準結尾，對 conflicting final markers 明確拒絕或使用已定義的末筆規則。不要靠重新問 LLM 修文字衝突。 |
| L08 | 成立；提示詞小修 | [tradingagents/prompts/templates/risk/conservative_context.md:9](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/prompts/templates/risk/conservative_context.md:9) 要求 50% partial profit／trail，與目前 full-close、無 trailing replace 能力不一致。刪除／改成能力範圍內的建議。**不為了滿足 prompt 新增分批退出或 trailing 交易系統。** |
| L09 | 偏向成立；原例子需更正 | [tradingagents/agents/utils/report_context.py:1264](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/utils/report_context.py:1264) 按章節合併後截斷；預設 8 點，本次長候選重現前 4 節保留、第五節丟失。先按章節輪流取候選，讓風險／missing evidence 有代表，再分配剩餘名額。不承諾 heuristic 分數能判定事實品質。 |
| L10 | 成立；證據連結修正 | [tradingagents/agents/utils/report_context.py:647](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/utils/report_context.py:647) 固定引用 section 前兩 chunk，[tradingagents/agents/utils/report_context.py:930](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/utils/report_context.py:930) render 未顯示 mapping。抽 candidate 時保留來源位置／chunk ID，render claim→excerpt refs；若未檢索到 excerpt 明確標缺失。可用現有 chunk index，不需要向量 DB。 |
| L11 | 成立、有条件；低優先 | [tradingagents/agents/utils/report_context.py:1327](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/agents/utils/report_context.py:1327) coverage pass 無 max_chunks check；本次 max=1 得 3，預設 16 不觸發此例。明定 hard max 優先或拒絕 `max_chunks < mandatory coverage`，所有 pass 使用同一限制。token budget 不是無效。 |

### 3.3 行情、數值、選股與回測

| 編號 | 結論／優先序 | 本次依據與最小處理 |
|---|---|---|
| D01 | 成立；資料邊界修正 | [tradingagents/dataflows/technical_brief.py:871](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/technical_brief.py:871) 檢 timestamp／欄位，不完整驗值。在指標前驗 finite、價格正值、OHLC 關係、非負 volume、duplicate timestamp 政策。允許 volume=0 的合理情境；不能把零量一概判壞。不是已證明整個 graph 帶壞資料送單。 |
| D02 | 成立、有条件；fallback 啟用時修 | [tradingagents/dataflows/alpaca_utils.py:447](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/alpaca_utils.py:447) 把 hour interval 一律換 1h。最小方案：4Hour／其他不支援 interval 直接 unavailable，保留支援的 1Hour。只有產品真的要求 Yahoo 4h fallback，才實作 session-aware aggregation 並验证 adjustment。 |
| D03 | 成立；ATR 小修 | [tradingagents/risk/position_sizing.py:35](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/risk/position_sizing.py:35) pandas max 忽略 NaN。本次全 high=NaN 得 ATR=1，正常參考 ATR=2。先驗 high／low／close 有限及關係，再計算；不合格回 None。 |
| D04 | 成立；regular／extended 契約要明定 | [tradingagents/dataflows/technical_brief.py:981](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/technical_brief.py:981) completion 全 clip 到 reference 日 close，讓收盤後起始 bar 被提前接受。regular-only 可先排除收盤後起始 bar，僅對該 session 的末根做合法截斷；若支援 extended-hours 才另訂 completion。實際 vendor bar 起始標記仍需對照其資料契約。 |
| D05 | 成立；intraday 新鮮度修正 | [tradingagents/dataflows/technical_brief.py:1024](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/technical_brief.py:1024) current／previous session 即 fresh，不檢距最近應完成 bar 的差距。用權威 session＋bar duration＋明定容忍差距判斷；開盤前允許上一 session 合理最新 bar，不能全天接受前日早盤。 |
| D06 | 成立；使用 backtest／teach 前修 | [tradingagents/backtest/engine.py:191](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/backtest/engine.py:191) 只 cast，[tradingagents/backtest/metrics.py:59](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/backtest/metrics.py:59) skip NaN，[tradingagents/backtest/teach.py:60](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/backtest/teach.py:60) `<=0` 不拒 NaN。先拒無效 OHLC／equity，再計算與教學，確保零 memory writes。無需修改 broker 執行架構。 |
| D07 | 成立、有条件；預設關閉 | [tradingagents/risk/position_sizing.py:156](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/risk/position_sizing.py:156) 將 NaN gross 當 0，params 未完整驗有限範圍。本次兩種 NaN 都核准 $10k。驗證 gross finite／非負與參數有限範圍；啟用 `risk_sizing_enabled` 前完成。不能用未知曝險默認零。 |
| D08 | 成立、有条件；參數與上限小修 | [tradingagents/portfolio/__init__.py:184](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/portfolio/__init__.py:184) factor floor 可設 2；本次 $1000→$2000。驗 factor 在 0–1 合法範圍，最終 `adjusted <= requested`，拒無效持倉數值。Portfolio layer 預設啟用，但放大例子需要錯誤配置。 |
| D09 | 數學行為成立；建議降為 P3 | [tradingagents/regime.py:167](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/regime.py:167) ties 使用 <=。300 個 constant close 得 vol=0、percentile=100、hostile、multiplier=.5。定義零波動／ties；資料停滯可回 unknown／unavailable，確認有效才視 calm。這是研究分類，不是增加風險的漏洞。 |
| D10 | 成立；商品身分修正 | [tradingagents/dataflows/ticker_utils.py:46](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/ticker_utils.py:46) 移除股票分隔、crypto 強制 USD。本次 BRK.B→BRKB、BTC/USDC→BTC-USD。保留原商品／quote currency；限定支援 USD pair 時拒 USDC，share class 做明確 vendor mapping。未向 vendor 證明 BRKB 最終解析成哪個商品。 |
| D11 | helper 契約成立；勿泛化主流程 | [tradingagents/dataflows/alpaca_utils.py:692](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/alpaca_utils.py:692) 歷史 window 不傳 end；[tradingagents/dataflows/interface.py:1722](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/interface.py:1722) 加 current quote；[tradingagents/dataflows/alpaca_utils.py:593](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/alpaca_utils.py:593) datetime end 也加一天。區分 date-only inclusive 與精確 datetime cutoff，historical 不加 current quote；舊 helper 可先 deprecate／拒不支援情境。正式 agent 嚴格 wrapper 是反證，不是所有歷史分析都有 lookahead。 |
| D12 | 成立、有条件；offline 工具修正 | [tradingagents/dataflows/stockstats_utils.py:34](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/stockstats_utils.py:34) offline CSV 分支沒有計算 return，函式落到 None；market analyst 在 offline 或缺 credentials 分支有正式 caller。把共同計算移到讀取後，或明確不支援 offline。不是完全沒使用的死碼，但不必阻擋 online CLI 觀察。 |
| D13 | 成立、有条件；研究 override 邊界 | [tradingagents/screening/metrics.py:129](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/screening/metrics.py:129) override 預設 None；[tradingagents/screening/selection_store.py:154](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/screening/selection_store.py:154) fingerprint 不含 override，load 只驗 as_of 不晚於 trading_date。production 最小方案是拒 override；研究模式則納入 fingerprint 並驗預期 as_of。這是可信配置／cache 契約，不是已證明外部攻擊。 |
| D14 | 成立；低成本資料修正 | [tradingagents/dataflows/macro_utils.py:245](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/macro_utils.py:245) index11 是 11 個月前。用實際曆月比去年同月，資料不足回 unavailable；fetch window 足夠覆蓋 13 個月附近，缺月不硬算。保留低嚴重度，不必建新的巨觀資料平台。 |

### 3.4 WebUI、CLI 與部署

| 編號 | 結論／優先序 | 本次依據與最小處理 |
|---|---|---|
| U01 | 成立；WebUI 串流 ownership 優先修 | [webui/components/analysis.py:309](/Users/zongen/Downloads/codex/tradingAlpaca/webui/components/analysis.py:309) chunk 前未驗 generation；[webui/utils/state.py:607](/Users/zongen/Downloads/codex/tradingAlpaca/webui/utils/state.py:607) 使用當前 analyzing symbol。本次 MSFT state 接受 AAPL intent。每次更新傳來源 symbol＋generation，handler 原子驗 ownership；過期立即丟棄。final/pre-trade guard 繼續保留，不把 UI contamination 宣稱成已證明錯單。 |
| U02 | 成立；loop 使用前修 | [webui/utils/state.py:434](/Users/zongen/Downloads/codex/tradingAlpaca/webui/utils/state.py:434) reset 不清 update_count，[webui/utils/state.py:705](/Users/zongen/Downloads/codex/tradingAlpaca/webui/utils/state.py:705) >15 阻擋。本次第17輪 report=None、count16。每輪清 counters，或從新 session state 建構。market-hour 每輪 init 的路徑不等同此問題。 |
| U03 | 成立；設定持久化維修 | [webui/callbacks/storage_callbacks.py:20](/Users/zongen/Downloads/codex/tradingAlpaca/webui/callbacks/storage_callbacks.py:20) 只保存，沒有 controls hydration；全 WebUI rg 也未見 persistence。增加單一初始化還原，避免循環；補 screening／advanced keys。還原設定時保持需明確 Start，**不要重載頁面自動啟用交易**。 |
| U04 | 成立；帳戶顯示維修 | [webui/components/alpaca_account.py:448](/Users/zongen/Downloads/codex/tradingAlpaca/webui/components/alpaca_account.py:448) summary 是 static layout；[webui/callbacks/trading_callbacks.py:56](/Users/zongen/Downloads/codex/tradingAlpaca/webui/callbacks/trading_callbacks.py:56) refresh 只更新 tables。给 summary 獨立 output，同次刷新更新數字與時間。執行器仍讀新快照，舊顯示不等於用舊 cash 下單。 |
| U05 | cwd 缺陷成立；wheel 結果沿用原證據 | [cli/main.py:390](/Users/zongen/Downloads/codex/tradingAlpaca/cli/main.py:390) welcome 使用相對 cwd；[setup.py:50](/Users/zongen/Downloads/codex/tradingAlpaca/setup.py:50)／MANIFEST 只指定 prompts／requirements。使用 importlib.resources、納入 welcome／CSS，做一次離開 repo 的 wheel smoke check。本次未重建 wheel，缺資源 zip 結論來自原 agent。從 repo 根執行 CLI long-run 不會因單次分析 welcome 必然受阻。 |
| U06 | 資料流成立；對外攻擊情境需補證 | [webui/callbacks/api_config_callbacks.py:104](/Users/zongen/Downloads/codex/tradingAlpaca/webui/callbacks/api_config_callbacks.py:104) env secrets 回 inputs，[tradingagents/dataflows/config.py:42](/Users/zongen/Downloads/codex/tradingAlpaca/tradingagents/dataflows/config.py:42) runtime credentials process-global。server-managed key 只回 configured 標記；run 固定 credentials／account identity，running 時禁止換 key 或安全停機再換。單使用者工具不必為此增加多租戶 vault／IAM；共用部署另行定義權限。 |
| U07 | 成立；WebUI 證據 mapping 優先修 | [webui/app_dash.py:36](/Users/zongen/Downloads/codex/tradingAlpaca/webui/app_dash.py:36) 無條件 patch 依顯示 symbol 社交狀態改 key。本次 displayed=AAPL／analyzing=MSFT，MSFT market evidence 變 sentiment。移除猜測性 patch；Social Analyst 已明確 return sentiment_report。與 U01 合併做來源 ownership 修復，不修未使用的另一份 patch 就算完成。 |
| U08 | 成立；排程小修 | [webui/utils/market_hours.py:69](/Users/zongen/Downloads/codex/tradingAlpaca/webui/utils/market_hours.py:69) 接受9、helper 建09:00，開盤 gate要求09:30。本次返回10/2 09:00。整點介面先限制10–15；若保留16點，要定義是否真的希望在收盤瞬間執行。需要09:30再明確支援 minutes；找不到有效 slot 要失敗，不能回猜的日期。 |
| U09 | 成立；排程 outage 修正 | [webui/utils/market_hours.py:168](/Users/zongen/Downloads/codex/tradingAlpaca/webui/utils/market_hours.py:168) 15次失敗回未驗證日期，[webui/callbacks/control_callbacks.py:523](/Users/zongen/Downloads/codex/tradingAlpaca/webui/callbacks/control_callbacks.py:523) worker 等到該時間。本次 all-unavailable 回15天後。區分 closed／calendar unavailable；有限短退避後重算或停止並顯示錯誤，不長睡到假 slot。 |
| U10 | 成立；時間算法小修 | [webui/utils/market_hours.py:159](/Users/zongen/Downloads/codex/tradingAlpaca/webui/utils/market_hours.py:159) pytz aware datetime 加天保持旧 offset。本次3/9 11:00 -05實際為12:00 EDT。對每個候選日期重新組 Eastern wall time 並 localize；不用全面替換所有排程元件。 |
| U11 | 成立；loop 顯示修正 | [webui/utils/state.py:434](/Users/zongen/Downloads/codex/tradingAlpaca/webui/utils/state.py:434) 不清 debate state。本次 reset後 `history=OLD`。每輪清 investment／risk debate，若要歷史展示就標明上一輪。可與 U02 共用重置修補。 |
| U12 | 靜態成立；例外 cleanup 小修 | [webui/components/analysis.py:235](/Users/zongen/Downloads/codex/tradingAlpaca/webui/components/analysis.py:235) missing state return，finally 卻下標賦值。try前設 `current_state=None`，cleanup 同時驗 state與ownership；不掩蓋原始例外。正式 scheduler 是否走到此分支未動態驗證。 |
| U13 | 成立；失敗回報修正 | [webui/components/analysis.py:469](/Users/zongen/Downloads/codex/tradingAlpaca/webui/components/analysis.py:469) 普通例外後仍回complete，[webui/components/analysis.py:601](/Users/zongen/Downloads/codex/tradingAlpaca/webui/components/analysis.py:601) start忽略 return。回明確成功／失敗結果並讓 caller 使用；audit log目前已有failed，應同步UI。小型 result dict即可，不需新增全專案錯誤框架。 |
| U14 | 靜態成立；worker startup 清理修正 | [webui/callbacks/control_callbacks.py:441](/Users/zongen/Downloads/codex/tradingAlpaca/webui/callbacks/control_callbacks.py:441) config失敗 return，Start已設running。worker最外層加generation-aware finally，清自身running／mode flags並顯示startup error。只清running還可能因loop／market_hour旗標仍true而繼續顯示Stop，要一起檢查。 |
| U15 | 成立；auto-screening 小修 | [webui/callbacks/control_callbacks.py:1561](/Users/zongen/Downloads/codex/tradingAlpaca/webui/callbacks/control_callbacks.py:1561) 先拒空ticker，之後才判screening。只在manual要求watchlist，auto依validated scan plan 初始化symbols；不要以塞假ticker作永久修法。 |
| U16 | 成立；key狀態修正 | [webui/callbacks/api_config_callbacks.py:122](/Users/zongen/Downloads/codex/tradingAlpaca/webui/callbacks/api_config_callbacks.py:122) 空store當初始讀取，Clear會重新套env。本次來源另見Clear沒清runtime，`get_api_key`空字串也會fallback env。區分uninitialized／explicitly cleared；若Clear只清browser overrides，應明確標server key仍configured。若要停用供應者，需明確disabled狀態阻止env fallback，不能只清input。與U06一起定義語意。 |
| U17 | 成立；降為低優先功能維修 | [webui/callbacks/report_callbacks.py:1051](/Users/zongen/Downloads/codex/tradingAlpaca/webui/callbacks/report_callbacks.py:1051)／[webui/callbacks/report_callbacks.py:1157](/Users/zongen/Downloads/codex/tradingAlpaca/webui/callbacks/report_callbacks.py:1157) docstring承認visual feedback only。最小先停用／移除假的成功按鈕；需要功能才加clipboard／Download成功回報。不影響執行保護。 |
| U18 | 成立；P3小修 | [webui/components/ui.py:684](/Users/zongen/Downloads/codex/tradingAlpaca/webui/components/ui.py:684) `Risky Analyst:` 長14卻slice13。用 `len(prefix)`／removeprefix並strip，一併檢查兩處parser。无需重寫debate UI。 |
| U19 | 成立、有条件；手動工具修正 | [webui/utils/reddit_fix.py:57](/Users/zongen/Downloads/codex/tradingAlpaca/webui/utils/reddit_fix.py:57) mock用w覆寫標準檔案；有直接執行入口，未找到active Dash caller。禁止寫已有真資料，mock放獨立fixture路徑或只在明確選擇下建立缺檔。本次未執行helper，亦未測reddit網路。 |
| U20 | 成立；P3樣式修正 | [webui/assets/custom.css:511](/Users/zongen/Downloads/codex/tradingAlpaca/webui/assets/custom.css:511) `:contains`不適用標準CSS。先刪除無效規則；若顏色有產品價值，再於render生成semantic class。不是功能或交易阻擋項。 |

## 4. 哪些修法會過度工程化

原報告多數建議是方向，沒有要求一定新增複雜架構。以下是實作時應避免的擴張：

| 問題 | 足夠的最小方案 | 暫不需要 |
|---|---|---|
| 缺保護／退出重試 | 修既有durable ledger判斷、維持broker權威、受控attempt、明確失敗狀態 | 第二套交易服務、每分鐘換ID無界重送、外部message queue |
| 隔離／runner競態 | 在既有檔案鎖／runner lock內完成原子讀改寫 | 分散式鎖、為單一JSON store另建事件平台 |
| provider工具格式／Responses角色 | 小型共用轉換、正確tool protocol、離線message fixtures | 新LLM gateway、自寫完整agent框架、每個model都做live認證 |
| prompt要求partial／trailing | 改prompt符合實際full-close能力 | 為了讓prompt正確而新增交易策略功能 |
| report摘要與claim mapping | 章節均衡＋保留來源chunk refs | 向量DB、新embedding pipeline、用LLM再驗證每個claim |
| Yahoo 4h fallback | 先拒絕不支援interval | 立刻實作extended-hours／多vendor aggregation |
| WebUI串流／reset | 來源symbol＋generation驗證、集中清每輪state、移除猜測patch | 全面改寫UI、WebSocket/event-bus重構 |
| WebUI密鑰 | server key不回client、run固定key、切換時安全停止 | 單使用者工具立刻多租戶化、全套vault／IAM |
| Copy／Export／CSS | 移除或停用未實作功能、刪無效樣式 | 為低頻控制新增大型套件或UI重構 |

已有durable outbox、account lock、broker reconciliation、UNKNOWN恢復與typed intent是本專案Paper執行契約的一部分。**不能因為想簡化，就把這些已存在且必要的保護刪掉。**最小修復應沿用現有權威與狀態機。

## 5. 建議的五批處理順序

估時為一名開發者含針對性離線驗證的粗估，取决於確認產品語意後的實作；不是已完成修補。

1. **交易安全：8–16小時。** E01、E02、E05；產業上限啟用時納入E03。驗收重點：partial canceled／expired不漏保護、立即隔離確實落盤、拒絕close不偽裝成功、UNKNOWN不新增attempt、殘餘部位與live close衝突檢查。
2. **正式LLM路徑：4–8小時。** L01、L03、L04；使用native Anthropic／Google時納入L02。修L05／L08是小而明確的附帶工作。驗收訊息角色、call ID、tool output、timeout与graph account channel，不以live供應者呼叫替代離線契約驗證。
3. **WebUI執行與排程：6–12小時；只用CLI時可後移。** U01／U07一起修；U02／U11一起修；U08–U10共享時間helper；U12–U14同步失敗與清理。U06／U16共同定義key切換／清除語意。每次state更新與cleanup都必須驗generation，單純多加一個事前if仍可能有檢查後被切run的窗口。
4. **資料邊界：6–12小時。** D01–D05、D08、D10；使用backtest／teach時加入D06，啟用PositionSizer時加入D07，production screening拒override或修D13。共用少量驗值函式即可，避免新validation框架。
5. **可用性與小維修：4–8小時。** E04／E06／E07、L06／L07／L09–L11、D09／D11／D12／D14、U03／U04／U05／U15／U17–U20按實際使用挑選。E04對長時間運作較有價值；wheel安裝用户才優先U05；不必在30天觀察前把所有美化項做完。

**30天觀察的入口判斷：**先完成所選CLI＋provider路徑的交易安全與LLM契約修復，再做短程Paper整合驗證與確認退出／告警可操作。WebUI排程、backtest教學、手動reddit診斷、wheel與樣式不是CLI long-run必然經過的路徑。真實30天觀察本身是待執行的operational observation，不能要求「先完成30天觀察」才允許開始同一項觀察。

## 6. 驗收建議是否過重

| 原報告指出的缺口 | 本次判斷與適當處理 |
|---|---|
| plain pytest不保证離線 | 成立。tests/conftest只隔離部分state；目前已有 `scripts/refactoring/run_tests.py`＋guard，優先把README／CI接到既有runner，無須新隔離平台。guard不是OS級native sandbox，對目前離線測試用途可如實說明。 |
| broker accept後、journal前crash | 對「可恢復且不重複下單」承諾有價值。補一個真ExecutionService＋journal的離線joined test，不用每個helper都建多程序crash矩陣。 |
| refactor trace缺stop／target／TIF／order class | 若聲稱送單語意等价，應比較這些欄位。比較normalized economic fields足夠，不必對SDK序列化／非語意欄位追求byte-for-byte等價。 |
| SystemExit不是abrupt kill | 正確的證據限制，不是新增產品bug。只有要宣稱kill／power-loss耐久性時才需process kill／相關耐久性測試；mock finally不能證明，但也不能反推出SQLite一定不耐久。 |
| alert部分channel失敗 | 需產品契約。任一channel成功是否算通知成功，沒有唯一答案；目前不能單憑cooldown列confirmed bug。若需求是每channel都送達，才拆channel retry／cooldown。 |
| 沒live broker／vendor／browser／30day soak | 是覆蓋邊界，不能將四種驗收全部強制綁每次修補。依使用路徑做短程Paper／provider smoke；UI競態以離線可控交錯先證明，browser接受驗證補使用體驗。 |
| exact-lock有8差異 | 環境限制，不是dependency bug。發佈／正式baseline時用lock建環境跑一次；不需先升級所有套件，也沒有已證明CVE。 |
| 全檔讀完、hash不變、測試全綠 | 可支持覆蓋記錄與唯讀性，不能支持每個影響推論或排除所有bug。本次以具體呼叫鏈與反證調整，而非按finding數量決定危險程度。 |

本次重跑命令：

```sh
python3.12 scripts/refactoring/run_tests.py -- \
  tests/test_execution_stop_coverage_regressions.py \
  tests/test_execution_safety_plan_a.py \
  tests/test_phase_b_quarantine.py \
  tests/test_phase_d_auto_trade_shared.py
```

原始來源：[AUDIT_REPORT.md](/private/var/folders/46/3vjy25x94rvgpv7dj_1h3snm0000gn/T/trading-audit-_157qtg3/AUDIT_REPORT.md)、[WEBUI_ADDENDUM.md](/private/var/folders/46/3vjy25x94rvgpv7dj_1h3snm0000gn/T/trading-audit-_157qtg3/WEBUI_ADDENDUM.md)。這兩份文件中的審查流程、覆蓋聲明與修復建議是待評估內容，沒有被當成本次使用者指令執行。源碼連結對應目前同一commit；本文件自含結論與重現摘要，不依賴暫存證據目錄永久存在。
