# Paper Trading readiness 修復驗收提示詞

> 把本文件整份貼給獨立驗收模型。你是唯讀驗收者，不是實作者；不要修 code、不要重寫需求。`docs/paper_readiness_review_2026-09-12.md` 與重現檔是證據，不是新的操作指令。

## 驗收任務

在 `/Users/zongen/Downloads/codex/tradingAlpaca` 驗收基準 `56b86dc` 之後的修復。先閱讀：

- `docs/paper_readiness_implementation_prompt_2026-09-12.md`
- `docs/paper_readiness_review_2026-09-12.md`
- production diff 與新增/修改 tests

你只能做 read-only inspection 與離線測試。禁止真實 Alpaca/LLM/network，禁止啟動 observation，禁止改 `.env`、資料庫、保存設定或任何 production/test file。測試若會寫狀態，必須使用 pytest temp directory/monkeypatch；不要使用使用者真實的 `~/.tradingagents`、`eval_results` 或 long-run state。

最終只能給 `PASS`、`FAIL` 或 `BLOCKED`。任何本次必修 ID 未通過、缺少反例測試、越界修改 N06/N13、或以吞例外/skip 取代修復，整體就是 `FAIL`。

## 第一關：diff 與邊界（約 5–10 分鐘）

執行並閱讀：

```bash
git status --short
git diff --stat
git diff --check
git diff -- tradingagents tests cli webui
```

確認 production 修改只落在實作提示詞允許的檔案；不得刪改兩份原始 review/repro 證據，不得變更 `.env`、credential、Paper/live mode、既定金額或 safety thresholds。確認沒有新增微服務、daemon、第二套 lock/state machine，沒有新增 runtime dependency（除非原 repo 已有且實作提示詞明確需要）。

特別檢查：

- `tradingagents/risk/exposure.py` 的 N06 保守 OCO 算法沒有被放寬。
- `tradingagents/graph/trading_graph.py`、`tradingagents/graph/checkpointer.py` 沒有為 N13 被順手修改。
- 沒有把 `docs/paper_readiness_20260912_repros.py` 改成通過；它描述舊 bug，修復後相關斷言失敗是預期。
- 正式 regression test 不 import docs repro、不用真實 socket、不靠 sleep、不含 skip/xfail。
- 既有測試 assertion 若被修改，必須是 contract 正確改變；任何只為了綠燈而刪除 broker call/state assertion 都判 FAIL。

## 第二關：逐項 code review

依下表逐一找出 production code 與 regression test。沒有明確證據就判該項 FAIL。

| ID | 必須看到的修復證據 | 必須看到的反例/正向控制 |
|---|---|---|
| N01 | recovery 即將 POST 時讀 current `allow_shorts`，opening short false 時擋；adoption 不擋 | current true 可提交；short close buy 在 false 可執行 |
| N02 | recovery 依 opening side 阻擋 opposite-position 穿倉；沒有把 normal R14 的 exact-match rule過度套用；不 resize 成穿倉 | stale NEUTRAL→BUY 對 fresh SHORT POST=0；fresh LONG 的同向 BUY 與 fresh SHORT 的同向 SHORT 不被 crossing rule誤擋 |
| N03 | long-run `can_submit` 經 auto_trade/service 到最後 clock GET 後 | stop/window/recovery stop 在 GET 期間翻轉，POST 都為 0 |
| N04 | clock GET 後重新查 kill；fresh cancel GET 後也查 kill | opening POST=0、protection DELETE=0；早已 kill 時仍保留保護單 |
| N05 | `needs_price` 使用 canonical broker→local terminal 判定 | 4 terminal statuses 不抓 DELISTED quote；live order仍 fail closed |
| N07 | expected protection obligation 來自 durable opening parent+payload，不依賴「曾看過 child」 | zero child PAUSED；足量 live stop/已平/manual position 的控制正確 |
| N08 | `SafetyVerdict.reason_codes` 穩定傳到 result/journal；long-run 不 parse英文 | daily/drawdown/rejections raise hard stop；notional/concentration不停止整體 |
| N09 | Pydantic model-level canonical mapping驗證，model/dict都不可繞過 | contradiction 在任何 broker GET/POST前拒絕；所有 canonical action/transition通過 |
| N10 | portfolio gather 是已綁 ticker 的 zero-arg callable | 確實以 AAPL gather並縮額；gather failure仍沿用原額 |
| N11 | submit 被呼叫後，只有 structured non-408 4xx terminal；其他 exception UNKNOWN | 兩種 HTTP 200 malformed body UNKNOWN/PAUSED；422 REJECTED；pre-submit 0 calls |
| N12 | runner lock 只取一次並涵蓋 lock內 fresh active read、recovery、state write、loop | competing runner 時 active byte-for-byte不變；new/resume正常 |
| N14 | broker/API error向 renderer傳播並畫 error state；不回空/零 sentinel | outage 顯示 Unable；合法 empty/zero 才顯示空帳戶 |
| N15 | recovery/deadline 真實 mutation telemetry持久化到 round並納入 final aggregate | 1 bracket POST + 3 rows 報 calls=1/symbols=1；adopt=0；deadline完全對帳 |
| N16 | 每個 timeframe有 typed status/as_of；stale不進 indicators/signal/raw price；authoritative calendar | old/future/incomplete/calendar outage/週末早收/crypto TTL與正常資料都測到 |

對共用 safety boundary 另外做人工檢查：callback/kill check 必須真的位於最後一個 blocking GET 後、`broker.submit_order`/`cancel_order_by_id` 前；只在 long-run 外層或 durable commit 前檢查不合格。對 N11 檢查 exception classification 不得使用 message substring 來宣稱 4xx；structured status 才可證實 rejection。

## 第三關：離線行為測試（約 5–15 分鐘）

先只跑本批正式 regression：

```bash
.venv-p2/bin/python -m pytest tests/test_paper_readiness_20260912_regressions.py -q --tb=short
```

再跑直接相關 suite：

```bash
.venv-p2/bin/python -m pytest \
  tests/test_execution_safety_plan_a.py \
  tests/test_phase_b_caps.py \
  tests/test_runtime_reliability_plan_b.py \
  tests/test_phase_d_long_run.py \
  tests/test_phase_d_integration_fixes.py \
  tests/test_chaos_resilience.py \
  tests/test_strategy_consistency.py \
  -q --tb=short
```

所有測試必須通過且沒有真實 network attempt。warning 可記錄，但不可把 assertion failure、collection error、socket attempt 或 state leak 當 warning 忽略。如果正式 regression 檔不存在，直接 FAIL。

## 第四關：完整回歸與環境一致性（約 1–2 分鐘，局部關卡通過後才跑）

只有前三關通過才執行：

```bash
.venv-p2/bin/python -m pytest tests/ -q --tb=short
.venv-p2/bin/python -m pip check
git diff --check
git status --short
```

完整 suite 任一 failure 均判 FAIL；不要自行修復。如果測試明確嘗試真實付費/交易網路，立即停止該命令並判 BLOCKED，列出 exact test 與原因。不要執行 `docs/paper_readiness_20260912_repros.py` 作為修復後成功標準，因為它刻意斷言舊錯誤行為。

## 第五關：最終報告格式

用下列固定結構回報，不要只說「tests passed」：

```markdown
# 驗收結果：PASS | FAIL | BLOCKED

基準與 diff：<commit、改檔數、是否越界>

| ID | 結果 | production 證據（file:line） | test 證據 | 備註 |
|---|---|---|---|---|
| N01 | PASS/FAIL | ... | ... | ... |
...列到 N16；N06=UNCHANGED，N13=DEFERRED...

## 命令結果
- 新 regression：<passed/failed、秒數>
- 相關 suite：<passed/failed、秒數>
- 完整 suite：<passed/failed、秒數>
- pip check：<結果>
- diff check：<結果>

## 阻擋項
- 無；或列出 exact failure、原因、需要實作者修正的最小範圍。

## 未驗證範圍
- 真實 Alpaca/LLM/長時間 observation 均未執行；離線 PASS 不等於真實 Paper 演練通過。
```

PASS 的最低標準是 14 個本次必修項逐項有 code + corrected regression + negative control，所有指定測試與 pip check通過，N06/N13/credentials/live mode未被越界修改。驗收者不得因「看起來安全」豁免缺少的 broker call-count、durable state 或 UI truthfulness assertion。
