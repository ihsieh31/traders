# 做空公司篩選公式：找到可研究的財報公式，尚無已驗證的 20 日放空公式

**結論：下一輪值得測的是 Beneish M-score，而不是單純把多頭分數反過來。** M-score 提供財報品質的篩選依據；但不能把它直接等同「未來 20 日一定下跌」。本輪固定比較的四種價格公式，在現有本地宇宙的平均 20 日做空報酬全部為負，且宇宙混有 ETF。這次僅新增研究產物，未把新做空公式接進正式交易程式。

## 1. 公司層公式：Beneish M-score

採 Beneish、Lee、Nichols（2013）附錄 A 的八變數係數，Accruals 採現金流口徑，記為 TATA：

```text
M = -4.84
    + 0.920 DSRI + 0.528 GMI + 0.404 AQI + 0.892 SGI
    + 0.115 DEPI - 0.172 SGAI + 4.679 TATA - 0.327 LVGI
```

`M > -1.78` 是該論文使用的風險標記門檻；**分數越高，越值得進行財報查核。標記不等於已證實詐欺。** 使用八變數版本和這個門檻時，不混用五變數係數或其他資料口徑。來源：[Earnings Manipulation and Expected Returns（作者校方 PDF）](https://cpb-us-w2.wpmucdn.com/sites.udel.edu/dist/a/855/files/2020/07/Earnings-Manipulation-and-Expected-Returns.pdf)，附錄 A／Exhibit A1、pp.76–77。

以下 `t` 是最新已公開的完整會計年度，`t-1` 是前一年；所有財報數值、單位與期間必須一致。

| 變數 | 固定定義 |
|---|---|
| DSRI | `(應收帳款_t / 營收_t) / (應收帳款_{t-1} / 營收_{t-1})` |
| GMI | `毛利率_{t-1} / 毛利率_t`，毛利率為 `(營收-銷貨成本)/營收` |
| AQI | `[1-(流動資產_t+淨PPE_t)/總資產_t] / [1-(流動資產_{t-1}+淨PPE_{t-1})/總資產_{t-1}]` |
| SGI | `營收_t / 營收_{t-1}` |
| DEPI | `折舊率_{t-1}/折舊率_t`，折舊率為 `折舊/(折舊+淨PPE)` |
| SGAI | `(銷管費_t/營收_t)/(銷管費_{t-1}/營收_{t-1})` |
| TATA | `(非常項目前盈餘_t-營業現金流_t)/總資產_t` |
| LVGI | `[(流動負債_t+長期負債_t)/總資產_t] / [(流動負債_{t-1}+長期負債_{t-1})/總資產_{t-1}]` |

口徑注意：此處固定使用 Exhibit A1 的期末總資產作 TATA 分母；文中附錄 B 的案例說明另有平均總資產文字，複現時須先處理這項差異。不能拿一般 NetIncome 或含攤銷的 D&A 任意替代所需盈餘／折舊。分母為零、缺欄位、毛利率不適合這些比率、不可比年度或財報過期，都應標為「不可評估」，不能補零當成合格。

### 證據能支持到哪裡

該研究在 **1993–2010、43,544 筆公司年度**，高 M-score 組的後續一年**規模調整報酬 -7.5%**，未標記組 **+2.4%**，差 **9.9 個百分點**。收益觀察始於會計年度結束後第五個月，並處理下市；效果在後半樣本較弱。來源：[同一論文 pp.61–62、Table 1](https://cpb-us-w2.wpmucdn.com/sites.udel.edu/dist/a/855/files/2020/07/Earnings-Manipulation-and-Expected-Returns.pdf)。

這是「相對報酬較差」的歷史證據，**不是裸空淨收益，也不是 20 日做空績效**；不能推論你的固定 20 日策略能賺 7.5%。論文排除金融業，對預測變數做年度 1%／99% 縮尾；未處理這些條件的原始分數不會自動複製表中績效。未來本地驗證必須事前固定縮尾邊界（只由當時已知資料決定）、財報口徑與申報可用時間。

## 2. 可以交付程式實作的候選規格

先用確定性程式建立候選，不需要 LLM 看全市場：

```text
基本集合：普通公司股、非金融業；排除 ETF、ETN、權證、優先股等非目標證券。
資料與流動性：既有正式資格門檻，且兩年財報可比、當時已公开。
借券：最新 shortable == True 且 borrow_status == easy_to_borrow。
財報風險：M > -1.78。
價格確認候選：close < MA20 且 r20 < 0。
排名：M 由高到低；平手依 ADV20 由高到低，再 symbol 升序。
```

**價格確認是新增待測假設，不是 M-score 論文驗證過的組合。** 暫不加入未證明有用的額外權重，也不把本輪價格公式的排名混進 M-score。借券資格須在送單前重新驗證；目前正式 execution 已有此防線，但其存在不代表研究資料有歷史券源。

名額由共用預算分配：`新空頭名額 <= max(0, 20 - 既有持倉審查的唯一股票數 - 已分配給新多頭的名額)`，新多頭名額須已排除與持倉重複者。既有持倉審查本身超過20時，應明確停止該輪並處理預算不足。不足合格公司就少選；沒有合格公司就不做空。這能把分析量限制在 20，卻不能保證固定每天都有 20 個放空機會。

本地目前沒有可重建上述歷史申報時點的完整財報面板，因此這份規格尚未產生公司名單或回測收益。資料來源可採 [SEC Submissions 與 Company Facts API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)：按 accession 和申報接受時間重建當時資料，不用之後重編的報表回填。SEC 的 frames 是跨申報彙整的最後申報值，不可直接拿作歷史已知數值；缺少標準 XBRL tag 時需查原申報，不能拼接不同會計期間。

## 3. 本地價格公式比較：全部公布

規格先寫入 [METHOD.md](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-short-screen/METHOD.md)，才執行 [compare.py](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-short-screen/compare.py)。計算只使用 2016-01-04 至 2020-12-31 的數值：1,259 個交易日、9,753 個 trusted symbol 欄位（部分期間沒有資料）、5,342,671 根在窗內 bars，59 個不重疊訊號。61 日資料不足的初期不交易；訊號後次日開盤進場、20 日收盤退出。每檔占初始本金 1/20，不足名額留現金。

| 固定方法 | 平均20日毛報酬 | 往返0.1%＋年借券5%情境 | 單檔曾上漲≥10% | 曾上漲≥20% | 選足20檔期數 |
|---|---:|---:|---:|---:|---:|
| 舊做空負向 lane 代理 | -1.02% | -1.51% | 21.36% | 5.42% | 59/59 |
| 原多頭公式反向 mirror28 | -0.12% | -0.60% | 15.34% | 1.75% | 56/59 |
| 限制追跌 bounded45 | -1.22% | -1.70% | 28.29% | 5.83% | 57/59 |
| 持續下跌 efficient60 | -0.57% | -1.05% | 13.14% | 1.04% | 57/59 |

借券 5% 是假定的年費率，按實際持有曆日計算，並非本地觀測費率；尚未計股息補償、召回與融資。即使假設借券免費，只扣往返 0.1%，四方法平均仍為 **-1.12%、-0.22%、-1.32%、-0.67%**。

mirror28 的 56/59 期選足20，兩期只有16與5檔，一期空集合；其均值包含留現金。其他三個公式亦按原規格報告所有期。單檔反彈使用持有途中的最高 **daily high**，不是只看期末收盤；比例的分母是選股次數（同股票不同日期算不同次），不是獨立樣本數。

### 不能把「虧得較少」宣稱成做空優勢

在共同日期且雙方都選滿20檔的配對比較：

| 新方法相對舊代理 | 毛報酬差／期 | 95% block CI | 反彈≥10%事件率差 | 95% block CI |
|---|---:|---:|---:|---:|
| mirror28（56期） | +0.80百分點 | [-0.31,+2.30] | -4.82百分點 | [-9.73,-0.27] |
| bounded45（57期） | -0.26百分點 | [-1.27,+1.01] | +7.63百分點 | [+4.65,+10.70] |
| efficient60（57期） | +0.41百分點 | [-0.55,+1.68] | -7.63百分點 | [-10.61,-4.65] |

所有收益差區間都跨零。風險較低的兩種公式可作候選過濾研究，不能據此宣稱盈利；區間未做多重比較校正。因這段資料已反覆用於研究，結果只能是探索診斷。

各年平均20日毛報酬也沒有稳定優勢：

| 方法 | 2016 | 2017 | 2018 | 2019 | 2020 |
|---|---:|---:|---:|---:|---:|
| 舊代理 | -2.00% | -0.43% | +1.02% | -1.24% | -2.80% |
| mirror28 | -2.65% | -0.53% | +1.50% | -0.63% | +1.48% |
| bounded45 | -3.44% | -1.34% | +0.38% | -1.60% | -0.34% |
| efficient60 | -1.12% | -0.41% | +0.35% | -1.92% | +0.32% |

## 4. 影響判讀的實際問題

**ETF 混入是本輪直接查到的證據，不能忽略。** 2020-03-20，mirror28 選入 VCSH、MINT、VCIT 等，efficient60 也選入 MINT、VCSH、HYG 等；這些排名不能當作「劣質公司」證據。VCSH 的 [Vanguard 官方頁](https://advisors.vanguard.com/investments/products/vcsh/vanguard-short-term-corporate-bond-etf) 確認它是短天期公司債 ETF；[PIMCO 的 MINT 官方資料](https://www.pimco.com/us/en/documents/f67aec88085c324bcdae7fdb1694127e0b0c477bf4175a1cf8cd4da06a0ea1eff30a8dd357e3d010b5bfc8e92b80d6b1?app=dot) 確認 MINT 是短天期固定收益 ETF。這些是對具體例子的身分核對，沒有用現在的全市場分類回填歷史篩選。

同日舊代理混入 SPXL、UPRO、TNA 等，其整期價格做空組合虧 **39.83%**；SPXL 的 [Direxion 官方資料](https://www.direxion.com/product/daily-sp-500-bull-bear-3x-etfs) 確認其為每日3倍標普500 ETF。這是現有候選宇宙的失敗例，不可外推為純公司股的績效。

[Momentum Crashes（Daniel、Moskowitz，2016）](https://spinup-000d1a-wp-offload-media.s3.amazonaws.com/faculty/wp-content/uploads/sites/3/2019/09/Mom_crashes_JFE_final.pdf) 指出市場急跌、高波動後的反彈，可能讓先前輸家猛烈上漲。這支持「不能只追最弱股票」的風險判斷，但沒有證明本輪 .28、.45 或 -25% 門檻最佳。

[In Search of Distress Risk（Campbell、Hilscher、Szilagyi，2008）](https://campbell.scholars.harvard.edu/sites/g/files/omnuum5881/files/campbell/files/campbellhilscherszilagyi_jf2008.pdf) 也發現財務困境股較低報酬伴隨較高波動與市場 beta，且套利摩擦重要。因此不建議單靠「快破產」分數作裸空進場依據。

本輪選入觀測的缺失終價／high 都是0；**不表示下市或券源問題已解決**。未進入快取或 trusted 集合的失敗公司根本不會出現在這個計數裡。歷史市值、歷史純公司分類、完整下市結算及實際借券仍未知。上述價格結果不是公司專屬回測，也不是完整做空成交／維持保證金回測。

## 5. 下一步的具體驗收

1. 先建立按申報接受時間可重建的兩年公司財報資料及歷史普通股宇宙，固定 M-score 口徑、縮尾方法、財報新鮮度与價格確認規則。
2. 僅就你的固定20日持有期做下一個事前登記測試，和「價格弱勢而未加 M-score」在同時點、同券源條件、同預算的候選比较，分別量測收益、反彈與覆蓋。文獻12個月證據不替代這個測試。
3. 納入實際券源／費率、股息補償、下市結算和召回；事前選定獨立資料或前向 paper 時段，不再用這次已看過的2016–2020作驗收。正式納入前需要證明成本後有效，且多空與持倉的分析聯集不超過20。

## 6. 可重現證據

- [summary.json](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-short-screen/summary.json)：所有方法、年度、費用情境、配對區間及資料／規格雜湊。
- [periods.csv](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-short-screen/periods.csv)：59期×4方法逐期收益、選股數與反彈事件。
- [selections.csv](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-short-screen/selections.csv)：每個訊號的實際名單、分數和因子。
- [market.csv](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-short-screen/market.csv)：同日合格宇宙的價格做空診斷基準。
- [run.log](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-short-screen/run.log)：本地執行完成紀錄。
- [verification.json](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-short-screen/verification.json)：獨立逐筆核算；[verify.py](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-short-screen/verify.py) 不呼叫篩選器或矩陣面板，直接由原始 bars 重算236組逐期结果、4,619次選股觀測、59,000根不重複持有期bars，收益／費用／反彈事件全部一致。另在三個日期抽查240筆因子，與正式程式最大差4.76×10^-14；持有期OHLC次序不合法的bars為0。

```bash
.venv-p2/bin/python research/out/20260929-short-screen/compare.py --checks-only
.venv-p2/bin/python research/out/20260929-short-screen/compare.py
.venv-p2/bin/python research/out/20260929-short-screen/verify.py
```

自檢通過：正確空頭損益（含虧損超過100%）、daily high 反彈、缺失終價使整期未知、前綴不變性、平手／20檔上限與正式舊負向 lane 的分數等價。程式封鎖研究網路，未呼叫 LLM、券商或下單。輸入 gzip 的日期／symbol 用於判斷範圍，只有2016–2020窗內資料轉為數值並參與計算。
