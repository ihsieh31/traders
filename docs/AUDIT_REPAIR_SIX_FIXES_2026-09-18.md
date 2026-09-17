# 六項殘留修復完成紀錄

日期：2026-09-18
版本：`fd3819a` 加目前工作目錄修改。
範圍：逐項驗證確認的 U01、L02、D01、D02、D11、D14。

**六項已補修，原先 9 個失敗 assertions 已全通過；完整離線 suite 為 1,534 passed、0 failed、2 warnings。** 修補保留在工作目錄，原有未提交修改保留。

| 項目 | 修復後行為 | 驗證 |
|---|---|---|
| U01 | WebUI dispatch 在 graph config 固定 symbol＋generation。平行 analyst／risk coordinator 透過同一 helper，在 ownership lock 內驗證後才更新來源 symbol 的 report／status；非 WebUI 或未帶 dispatch 身分的 graph 不寫共用 UI。最終結果、圖表與 cleanup 也在鎖內重新驗證；慢 fetch 放在鎖外。交易入口接收原 generation，將 ownership predicate 傳至既有 execute_auto_trade 的 can_submit，過期交易結果不覆寫新 run。 | 同 symbol／跨 symbol Stop→Start、Stop、當前 owner 正常發布、平行 risk late status、最終慢 chart 中 Stop→Start、過期 trade dispatch。 |
| L02 | 五個 analyst 使用同一標準／raw tool-call normalizer；每輪保存一個包含全部 calls 的 assistant turn，再附對應 ToolMessages。保留原 AIMessage content、additional kwargs 及其他 provider metadata；不再把標準 calls 當 raw calls 重建成空 assistant。call ID／arguments 無法形成合法契約時明確失敗；budget exhaustion 也檢查標準 calls。 | 五個 analyst × 標準／OpenAI raw 格式 × 一輪兩 calls；真正 Anthropic／Google adapter request conversion，以及 native budget exhaustion。 |
| D01 | OHLCV 全部驗 finite、價格正值、OHLC 關係與非負 volume，允許 volume=0；驗證後數字寫回 cleaned frame，數字字串不以原字串進指標。 | volume 的 +inf／-inf／NaN 拒絕，正常數字字串與零量保留。 |
| D02 | fallback 先 normalize 合法 alias／SDK TimeFrame，再按精確 amount＋unit 白名單選 interval；只接受 1Hour／1h 與 1Day／1d。其他 interval／未知字串回 unavailable，不預設 daily。 | 4h／4Hour／2Hour／1h、daily aliases、minute／未知／零 amount／multi-day，以及 SDK 1／2／4Hour。 |
| D11 | 帶精確 instant cutoff 的報告不附 present quote；只有 date-only 的 live window 保留即時 quote 路徑。既有 date-only inclusive 與精確 broker request end 修補保留。 | 真正 analysis_date_mode 注入固定 now，驗當日過去 cutoff 不呼叫 get_latest_quote；完整歷史資料回歸。 |
| D14 | CPI／PPI 等需要 YoY 的 series 抓取至少 15 個月資料，為去年同月與發布延遲留餘裕；其他 series 維持原 requested lookback，較長的自訂窗口仍保留。既有同曆月配對維持不变，缺月不捏造同比。 | 遵守 fetch start/end 的 fake FRED → 正式 report 呼叫鏈；去年同月確實抓回，其他 series 不擴窗，缺月不硬算。 |

## 驗證結果

1. **原始獨立 probes：10 passed、2 warnings，2.18 秒。** 原驗證文件保存的測試原文未修改；修前是 9 failed、1 passed，修後全部通過。
2. **正式回歸：新增 47 個案例**於 `tests/test_audit_remaining_six_repairs.py`，包含原 probes 與 native multi-call、ownership、數值／interval 邊界。相關安全測試合跑為 153 passed。
3. **完整 suite 初輪**其餘 1,534 cases 通過，但舊 risk failure test 有 3 個失敗 subtests。原 fixture 使用未帶 WebUI 身分的 graph，卻期待直接寫 UI；已改為真正 AppState＋明確 dispatch 身分，保留例外傳遞、失敗不 completed 的 assertions，另驗真實 pending state。沒有刪除失敗角色案例或跳過測試。針對性重跑：79 passed、27 subtests passed。
4. **最終完整離線 suite：1,534 passed、0 failed、2 warnings，41.05 秒；exit code 0。** 包含既有 wheel 建置／離開 repo 資源與 CLI smoke。兩個警告來自 websockets legacy 與 LangGraph serializer。
5. **git diff --check 通過。** 使用既有 offline runner，停用 dotenv、隔離 mutable state 並啟用 Python 網路 guard。沒有真實交易、live provider 呼叫或修改原 characterization baseline hashes。

可重跑命令：

```sh
python3.12 scripts/refactoring/run_tests.py -- \
  docs/audit_verification_2026-09-18/test_remaining_gaps.py -q
python3.12 scripts/refactoring/run_tests.py -- \
  tests/test_audit_remaining_six_repairs.py tests/test_phase_b_retry_stop.py -q
python3.12 scripts/refactoring/run_tests.py --
```

## 保存的證據

`docs/audit_verification_2026-09-18/` 保留修前驗證，另增加：

- `six_fixes_original_probes_*`：原 probes 修後通過的 log、命令與 exit code。
- `six_fixes_contract_tests_*`：回歸與舊契約 fixture 更正後的結果。
- `six_fixes_full_suite_*`：最終完整 suite 的 log、命令與 exit code。
- `six_fixes_source_hashes.json`：此次 13 個產品來源檔與 2 個測試檔的內容 hashes。

先前 `AUDIT_REPAIR_VERIFICATION_2026-09-18.md` 是**本次補修之前**的逐項審查紀錄，6 項部分修復狀態已由本文件與修後測試更新。這次完成的是程式修補及離線驗收；未執行 live broker／provider／browser smoke、exact-lock 重建或 30 天觀察。
