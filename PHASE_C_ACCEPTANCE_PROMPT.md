# P3／Phase C 獨立驗收提示詞

唯一任務：驗收全市場eligibility/ranking、第三Provider、Top20、持股聯集與cache/scheduler整合；不修復、不啟動Paper run。

## 獨立性與工作規則

將本文件完整交給新的獨立 task；工作目錄 `/Users/zongen/Downloads/codex/tradingAlpaca`。先讀適用 AGENTS.md、`ponytail` skill（full）、`PROJECT_GOALS_AND_STATUS.md`、本階段完整 implementation prompt、README、ARCHITECTURE、實作 diff、相關 source/tests。implementation prompt 的公式、設定、邊界都是 mandatory contract，本矩陣不是刪減版替代規格。

只讀驗收：不修改 repo code/tests/docs、不 apply_patch、不 commit/push/reset、不切 branch、不刪使用者檔案。可以跑既有tests，以OS temporary目錄放一次性PoC、SQLite、cache；不得藉驗收修程式。實作時參與者不能兼任獨立驗收者。若發現缺陷，報告直接證據並停止依賴該前提的檢查，其他獨立檢查可繼續；修復需另一task，修後重新fresh acceptance。

禁止真實 LLM/broker/webhook/Telegram call；使用fake transport與mock broker。這是離線功能驗收，不要求新真實Paper訂單，也不宣稱證明真實vendor可用性、live資料完整性、盈利或長期無人值守穩定。前置P1 Paper evidence獨立列出，不把它冒稱本輪取得。

記錄 `git status -sb`、`git rev-parse HEAD`、base SHA與diff、前置accepted revision與證據。dirty實作允許在來源可識別且完整diff固定時驗收，記diff hash/未追蹤檔案hash；未知或驗收中變動的範圍＝prerequisite failure。歷史綠test totals、checkbox、實作者口述不能代替本輪證據。

每列必須 Pass/Fail/Blocked＋source位置＋test/PoC實際結果。不能只寫「有這個函式」；追 public caller、failure propagation、execution gate。優先重用可直接证明的既有tests；缺少關鍵反例時寫temporary PoC，不能補永久測試掩蓋實作缺口。任何mandatory項目缺證據都不能Accepted。

## 前置條件

取得P2 fresh Accepted report及exact revision，確認P3建立於該實作且後续修改仍納入本輪回歸。Paper observation／人工 watchlist 長期穩定運作證據不屬於 P3 離線驗收前置，缺少不得單獨造成 Not Accepted；啟用長期無人值守 Paper 自動交易前仍須完成 observation。P2未驗收或只是實作者自述則prerequisite pending；不得先替P2發Accepted再驗P3。

## 強制驗收矩陣

| ID | 必須直接證明 | 最低fixture／反例 |
|---|---|---|
| C01 | 三角色獨立配置、factory重用 | 三個不同fake provider/model/key/URL；只Screening看compact candidates |
| C02 | 手動模式不建Screening、自動缺設定拒啟用 | legacy手動可跑；auto缺model/provider config error，不偷用Analysis |
| C03 | 完整ACTIVE US_EQUITY universe | 多批/多頁fixture、inactive/nontradable/crypto排除；search limit/fallback不得混入 |
| C04 | eligibility門檻精確 | 4.99/5、ADV19999999/20000000、60/61bars、volume0、NaN/Inf |
| C05 | 資料時間與完整性正確 | 未收盤bar、週末/假日/DST、missing/duplicate session、stale最後bar、adjustment混用 |
| C06 | deterministic公式與tie-break正確 | 用獨立手算小fixture驗r5/r20/r60、samplevol、ratio、midrank、score；打亂input結果相同 |
| C07 | Top40／不足候選行為 | >=40取40、20–39全送、19以下零Screening；n=1的percentile0.5 |
| C08 | Screening只研究排序 | prompt factor/units完整，無持倉/全新聞/BUY權重；一次logical rerank |
| C09 | Top20嚴格schema | valid20；19/21、duplicate、out-of-input、extra/missingfield、rankgap、NaN、超長空reason都拒 |
| C10 | sector多樣性與降級明確 | complete sector每類<=5；capacity<20停止；missing metadata明示不套diversity，P2 cap照守 |
| C11 | retry/access error與invalid output分流 | transient第四次成功、四次失敗無第五次；401一次；invalid output不repair且零下游 |
| C12 | Top20聯集fresh持股 | 20+5且重複2→23唯一；holdings失敗不能當空；input不帶持股 |
| C13 | 額外持股只允許降低風險 | holdings-only想BUY/flip新entry拒；HOLD/verifiedSELL按Phase A；Top20重疊仍守cap |
| C14 | 被排除持股仍被管理 | nontradable/quarantine/缺bars明示held blocked review；無自動平倉或假行情 |
| C15 | 真實entry gate不可被LLM/resume繞過 | 偽造候選標記、checkpoint舊名單、directdispatch；不在本日有效selection不得自動entry |
| C16 | 每symbolcontext/pre-submit新鮮 | Trader→Decision→execution快照變動，下一股重算cash/cap；串行不超配 |
| C17 | 當日cache成功重用 | 首次1次logicalScreening，第二輪/重啟0新增scan；持股變更仍重取union |
| C18 | cache失效／人工refresh安全 | 隔日、config/model改、損毀、futuretimestamp、out-of-input；refresh失敗不得退回舊selection |
| C19 | 日期與雙執行者安全 | 非交易日不scan/entry；同時首次scan最多一份有效結果；無brokerlock包住LLM |
| C20 | 任一role耗盡停止後續工作 | screening/analysis/decision各注入outage；UI/CLI/scheduler stopped，無後續symbol/auto retry |
| C21 | 全鏈路真正接通 | assets→bars→Top40→Top20∪positions→graph→mockexecution，不是dead helpers |
| C22 | P2原有功能與P1全部回歸 | SEC/IR/quarantine/sector、retry/context/cap、brokerstate/冪等/UNKNOWN/lock保留 |
| C23 | Ponytail與文件可操作 | 無ML/optimizer/newDB/service/第二scheduler，全市場不抓新聞；公式、cache、設定/refresh文件一致 |

## 必做對抗PoC與測試

從scheduler或其真實dispatch入口跑至少：有效23symbol聯集；holdings-only LLM提議新entry；Screening輸出第21或越界symbol；人工refresh耗盡且舊cache存在；持股GET失敗；第一股後第二股Provider失敗；同日雙執行者；決策後broker持倉增加導致headroom縮小。記LLM transport count、分析symbol清單、cache狀態、intent rows與broker calls。若前一股已成交，報告stop後零新增mutation，不能把前一股訂單隱藏。

獨立重算rank fixture，不能以production同一函式產生expected。測試邊界應能捕捉60bars算60D、以calendar-day TTL誤殺週末、平均rank處理錯誤、已持但不在Top20加倉、refresh失敗重用cache等具體bug。

跑P3 focused與P2 regression、Phase A1/A2、graph/LLM/structured/safety/portfolio/market-hours相關tests，最後 `python -m pytest tests/ -q`、`python -m compileall -q tradingagents cli webui`、`git diff --check`。提供actual counts/duration/skips/warnings；沒有真實provider/network credentials也必須能離線跑，不能因此跳过核心矩陣。

## Verdict 與輸出（五項）

只有P2前置成立、C01–C23全部Pass、PoC/全suite綠、無public bypass、驗收範圍未變，給 `Accepted`。缺陷給 `Not Accepted — code/test defect`，缺前置/獨立性/直接證據給 `Not Accepted — prerequisite/evidence pending`。不以「大致完成」接受缺欄位或未接scheduler。

1. Verdict、HEAD/base/diff identity、P2 Accepted revision與前置證據。
2. Findings按嚴重度列file:line、觸發、PoC與影響。
3. C01–C23逐項Pass/Fail/Blocked與直接證據。
4. Commands/results、外部call=0、Ponytail與真實資料/供應商未驗證限制。
5. 前後git status與下一步。Accepted只代表離線P3功能；真實資料與小額Paper observation另經授權執行，不自行啟用交易。
