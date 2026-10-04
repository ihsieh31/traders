# 新做空公式搜尋：四個候選已固定並測試，沒有通過者

2026-09-29。找到並實作四個有明確定義的候選，先固定規格，再重取財報及測試：高應計盈餘、負營業現金流、負現金流公司反彈後做空、市場調整後短期反轉。**主20日與事前固定的次5日，全期成本後平均均為負，沒有一個滿足預定的探索條件。正式程式未接入新公式。**

本輪有兩個實質進展：三欄位proxy可算率達63.04%，改善了M-score幾乎算不出來的問題；另查到「取到正確數字」仍可能有不同合併範圍，不能宣稱財報品質資料已修好。

## 1. 公式與原始研究依據

取值前規格在 [METHOD.md](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-short-formula-search/METHOD.md)，完整可實作定義在 [FORMULAS.md](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-short-formula-search/FORMULAS.md)。

```text
AC = (NetIncomeLoss - 營業現金流) / 期末總資產
CB = -營業現金流 / 期末總資產
weak = 收盤價 < MA20 且 20日報酬 < 0
e5 = 5日報酬 - beta60 × SPY的5日報酬
```

| 方法 | 固定條件 | 排名 |
|---|---|---|
| price_weak（基準） | weak | 原價格負向lane分數 |
| accrual_weak（主候選） | AC>0 且 weak | AC降序 |
| cashburn_weak | CFO<0 且 weak | CB降序 |
| cashburn_rebound | CFO<0 且 weak 且 r5>0 | r5降序 |
| residual_rally | r5>0 且 e5>0 | e5降序 |

同分依ADV20降序、symbol升序，最多20檔，不足不補。所有方法比較同一可算三欄位的公司池；殘差式的beta只用訊號日以前60次日報酬。

[Sloan (1996)](https://www.cuhk.edu.hk/acy2/workshop/June2009Wasley/1996TAR%29.pdf) 的研究支持區分盈餘的應計與現金部分，但本輪的原始NetIncomeLoss標籤／期末Assets／現金流法proxy不等同原文的持續經營盈餘、平均資產及資產負債表法，也沒有原文對本輪20日裸空的證明。[Hribar & Collins (2002)](https://onlinelibrary.wiley.com/doi/10.1111/1475-679X.00041) 說明資產負債表變動估應計可能被併購等事件污染，支持查現金流口徑的動機。

[Nagel (2012) 作者校方頁](https://gsbpreserve.stanford.edu/view/41388/evaporating-liquidity) 提供短期反轉與流動性供給的研究依據；本輪簡單e5是待測proxy，沒有複製原文對沖或VIX條件。cashburn_rebound 的精確組合是自行固定的假設，沒有借用文獻收益作本地證據。更多口徑區別在 [SOURCES.md](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-short-formula-search/SOURCES.md)。

## 2. 資料覆蓋改善，但「完整」只表示三個tag可取

重取已快取的 [SEC 官方主表資料集](https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets) 2015Q1–2020Q4。只用同accession、USD、標準us-gaap tag、無segments/coreg、精確申報期末：NI/CFO為年度qtrs=4，Assets為qtrs=0。輸入有限、資產為正；缺值和矛盾值不補零、不推測custom tag。數值保留原ZIP SHA及1-based行號。

| 指標 | 本輪 |
|---|---:|
| 沿用的年度申報筆數 | 7,889 |
| 三個tag完整可算proxy | 4,973（63.04%） |
| 可算的不同CIK | 1,330 |
| 59個訊號日的公司池合計觀測 | 26,946 |

這與先前M-score完整率3.14%相比，證明減少資料需求能大幅增加可算覆蓋；不是把M-score資料修成完整。所有季度一致用主表，不混入只下載一季的附註。報告中的「完整」不表示經濟口徑已統一或所有財報已清理。

價格只計算2016–2020；財報數值只取2015–2020。**2021–2025檔案本輪未讀。** 此價格區間已反覆研究，不能當成新的獨立驗證集。原factor20 ledger在此checkout不存在，已盤點目前價格/M-score研究及history.md，但不能聲稱完整避開所有歷史變體；短期反轉本身已知，這是公司池複驗。

## 3. 全部結果：主20日、次5日

59個相隔20交易日的訊號，次日open入場，第H日close退出，做空價格收益 `1-exit/entry`，不截斷虧損。固定成本情境：往返10bp、年化借券5%按實際曆日扣除；未有真實歷史券源。每股原本金1/20，未選滿留現金。

下表是**有選股各期按實際投入名目正規化後的平均**，不靠較多空倉改善數字。5日是先寫定的敏感度，不能用它替代主20日。

| 方法 | 有選股期數 | 不同公司 | 20日均值 | 20日95% block CI | 5日均值 | 選滿20期數 |
|---|---:|---:|---:|---:|---:|---:|
| 同池價格基準 | 59 | 355 | −2.47% | [−4.35%,−0.98%] | −0.94% | 58 |
| 高應計＋弱勢 | 59 | 195 | **−2.80%** | [−5.12%,−0.91%] | −0.56% | 14 |
| 負現金流＋弱勢 | 58 | 83 | −4.44% | [−7.65%,−1.76%] | −1.04% | 2 |
| 負現金流＋反彈 | 37 | 54 | −2.90% | [−7.35%,+0.74%] | −0.55% | 0 |
| 市場調整短期反轉 | 59 | 483 | −2.54% | [−4.63%,−0.69%] | −0.50% | 59 |

四候選的20日毛收益平均亦為負（−2.31%、−3.95%、−2.41%、−2.05%）；借券免費只扣往返10bp仍為負。不是只由設定5%借券費造成失敗。完整六種費用情境、各年、剔除最佳貢獻公司見 [summary.json](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-short-formula-search/summary.json)。

共同有選股日期、按投入名目公平配對的20日收益差：

| 候選相對價格基準 | 均差（百分點） | 95% block CI | 配對期數 |
|---|---:|---:|---:|
| 高應計＋弱勢 | −0.33 | [−1.41,+0.71] | 59 |
| 負現金流＋弱勢 | −1.97 | [−4.53,−0.16] | 58 |
| 負現金流＋反彈 | +0.42 | [−2.11,+3.07] | 37 |
| 市場調整短期反轉 | −0.07 | [−1.26,+1.05] | 59 |

保留正面結果：反彈候選有相對少虧，5日反轉相對基準也少虧0.44個百分點，但區間均跨零；絕對全期收益仍為負。反彈候選20日在2016–2018平均+1.19%，2019–2020為−6.36%，排除2020後+0.20%。這證明挑區间會得到好看的數字，不能據此採用。預定條件要求兩分段正值、全期成本後正值、配對改善區間下界正、足夠期數及公司；四候選均未達到，5日也未達到。

block=3訊號期、2000次、seed20260929的區間只是探索描述，沒有校正歷史及本輪多重搜尋。cashburn的全20配對只有2期，該區間因重抽樣退化成單一值，不能當作精確推論。未把原本不完整／未知的公司補進樣本。

## 4. 財報風險標記可能增加追空反彈風險

| 方法 | 20日內最高high較入場漲至少10% | 漲至少20% |
|---|---:|---:|
| 同池價格基準 | 21.51% | 5.95% |
| 高應計＋弱勢 | 30.77% | 9.09% |
| 負現金流＋弱勢 | 49.44% | 21.63% |
| 負現金流＋反彈 | 44.71% | 16.47% |
| 市場調整短期反轉 | 34.41% | 12.12% |

分母是選股觀測次數，重複公司不是獨立樣本。所有候選本輪的反彈比例比同池價格基準高，不能宣稱排除了容易軋空的股票。

具體反例：2020-03-20訊號、03-23 open進場、04-20 close退出：

- W：已知2019年度CFO為−196.818百萬美元，cashburn_weak選入，外部quote進出場27.11→100.93，裸空價格損失約 **272.30%**。
- CVNA：cashburn及accrual均選入，外部quote進出場5.736→16.06（同一分割調整尺度），裸空價格損失約 **179.99%**。

這是沒有停損的價格診斷，不能稱為真實成交損失；但確實顯示負現金流公司也可能大幅上漲。現金流為負可能來自擴張、季節性或商業模式，不足以定義公司爛、破產或股票必跌。W財報源為 [2019 10-K](https://www.sec.gov/Archives/edgar/data/1616707/000161670720000025/a2019-12x31form10xk.htm)，價格原檔與差異在external_prices.json。

## 5. 口徑問題沒有被「來源核對通過」掩蓋

另查到CVNA的原始申報例子：同一2019申報的 `NetIncomeLoss=-114.659百萬`，`ProfitLoss=-364.639百萬`，CFO為−757.134百萬。前者是歸屬Class A股東的淨損；CFO是合併現金流。Assets在同一合併報表為2,057.748百萬。出處為 [CVNA 2019 10-K](https://www.sec.gov/Archives/edgar/data/1690820/000169082020000052/cvna-20191231.htm)。

凍結AC為0.31222；若取合併ProfitLoss，示例AC為0.19074。METHOD.md的「總NetIncomeLoss」中「總」是不精確措辭，程式一直使用原始NetIncomeLoss標籤。原數字、tag及行號都能對上SEC，但不代表NI和CFO經濟範圍一致。已把限制保存至 [semantic_audit.json](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-short-formula-search/semantic_audit.json)，未在看到收益後偷偷換tag／重排。這是辨識出的個案，不是已完成全部公司的口徑審查。

因此本輪AC結果只能是對固定raw-tag proxy的條件式測試，不能當成一致合併口徑的Sloan應計策略驗證。CB本身不使用NI，避免這一項比率混用；但共同U仍要求三tag，不能把它的結果外推到全部只需CFO/Assets的公司。這項發現也提醒既有M-score的NI適配需要語意核對；既有研究輸出保留，正式程式未採用M-score。

## 6. 獨立核對與資料限制

[verify.py](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-short-formula-search/verify.py) 不呼叫selector或矩陣面板：直接用CSV reader逐行核對原SEC ZIP及raw price，獨立計算比率、beta、價格因子、排名和收益。結果：

- 公司池用到2,765個申報，跨23季，共 **8,295個原數值引用**，來源SHA、單位、無分部、日期、qtrs及申報接受日均核對。
- 26,946筆公司池觀測核對當時最新申報，不退回舊完整分數。
- **590個方法期、7,024次選股觀測、3,158組不同signal因子、63,160根不同前瞻bars**核對；全部成本情境及daily-high事件一致。
- Decimal比率最大差0；因子尺度化差最大7.92e−14；單股損益差最大2.22e−16。ADV20美元量級的最大絕對浮點差為0.000097美元。

見 [verification.json](/Users/zongen/Downloads/codex/tradingBuffett/research/out/20260929-short-formula-search/verification.json)。這證明固定proxy及算式的取值／計算一致，沒有消除上一節的經濟口徑問題。

Yahoo只請求2016–2020歷史quote：W/CVNA兩個觀測損失案例，加上依固定SHA排序挑的主候選ADM/SPGI兩檔；58次模型觀測、37個不同進出場組，收益方向 **58/58一致**。最大收益差 **0.3192個百分點**，出現在ADM，直接公開，沒有強行調整本地價格。這個抽樣不是全市場價格驗證；不使用當前meta。原URL與SHA、缺值及逐筆差異在external_sources.json、external_prices.json、external_summary.json。

既有smoke／harness及新增自檢均通過，包括缺值／同比例縮放、申報時點／最新缺失、未來資料不改當下、beta已知關係、價格前綴不變、平手與20檔、正確空頭虧損可超過100%。獨立核對過程中修正了新verifier將量比誤寫為當日volume/MA20的問題；最終按既有生產定義MA5(volume)/MA20(volume)重跑通過，評估公式與結果沒有因此改動。

尚缺歷史普通股與ticker/CIK主檔、完整下市、歷史市值、股息補償、真實券源／費率／召回／融資；當前映射和trusted cache仍可能有身分／存活偏差。無前瞻缺價不等於已涵蓋下市。財報非金融10-K集合不是已驗證完整普通股宇宙。沒有模型或券商請求，沒有真實下單。

## 7. 交付判定

本次找到、實作並交付了四個新組合候選與完整否定證據，**尚未找到可採用的新做空公式**。可算率不再是唯一瓶頸：20日／5日效果、反彈風險及財報經濟口徑都需要解決。這些條件式失敗不能證明所有應計／現金流策略永遠無效，但足以拒絕將本輪候選接入正式流程。

之後若繼續，應先固定合併／歸屬範圍、持續經營及期間口徑；CFO/Assets兩欄位宇宙要另行預定測試，不能改這次樣本追求過關。文獻更長期的相對收益及多空對沖也不能當作本輪20日裸空證據。既有每帳戶每輪20檔LLM分析上限維持，未另外增加20檔做空分析。

## 8. 重現

在專案根目錄執行，只新增此目錄，未覆寫既有研究：

```bash
.venv-p2/bin/python -m research.src.smoke_test
.venv-p2/bin/python -m research.src.test_harness
.venv-p2/bin/python research/out/20260929-short-formula-search/compare.py --checks-only
.venv-p2/bin/python research/out/20260929-short-formula-search/prepare.py
.venv-p2/bin/python research/out/20260929-short-formula-search/compare.py
.venv-p2/bin/python research/out/20260929-short-formula-search/verify.py
.venv-p2/bin/python research/out/20260929-short-formula-search/external_check.py
```

METHOD SHA：`ac76dac2d3d89db48ebd34872010c4b8c3cbe1a940314a050b746215a3f45180`。
Financials SHA：`3a13d4e340f90ec503e62d04fe6d7c58592c59b71d708c26f480a34d53a0f986`。
程式與重用依賴SHA在code_manifest.json；資料面板SHA在summary.json。逐期／選股／候選母體在periods.csv、selections.csv、universe.csv，資料覆蓋在financial_coverage.json及coverage.csv；執行紀錄保留。
