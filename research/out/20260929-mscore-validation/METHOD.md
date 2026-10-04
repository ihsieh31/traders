# M-score：績效計算前固定規格

只計算2016–2020股票數值，新增SEC 2015Q1–2020Q4 as-filed財報；2015申報僅作早期已知財報。這段價格已反覆研究，本輪為條件式探索。僅新增本目錄及SEC快取，不改正式交易程式或其他研究，不呼叫LLM、不下單。

## 公司與時間

SEC官方當前ticker/CIK與trusted symbol交集，每CIK只取ticker字母序第一個。要求10-K/10-K/A、SIC已知且非6000–6999、USD、us-gaap標準tag、無segments/coreg。當前ticker對照不是歷史證券主檔，存在身分與存活偏差，不能宣稱完整普通股PIT宇宙。

訊號日前一天或更早已接受的最新年度申報；期末距訊號<=500日。當天申報一律延後。最新申報不完整時直接排除，不退回舊分數。兩年數字全來自同accession，絕不用後來申報回填過去訊號。年度流量qtrs=4，期末存量qtrs=0，相鄰期末相距350–380日。同tag/date/qtrs矛盾值拒絕、缺欄位不補零；不拿D&A替代純折舊。

## 八比率與資料適配

原八係數、M>-1.78、TATA期末資產分母。營收優先SalesRevenueNet、Revenues、RevenueFromContractWithCustomerExcludingAssessedTax、SalesRevenueGoodsNet，兩年同tag。毛利優先GrossProfit，否則營收減CostOfGoodsAndServicesSold/CostOfRevenue/CostOfGoodsSold，兩年同tag。應收限AccountsReceivableNetCurrent/AccountsReceivableNet，兩年同tag。長債用LongTermDebtNoncurrent，或LongTermDebt減LongTermDebtCurrent，兩項都須有。

其他為Assets、AssetsCurrent、PropertyPlantAndEquipmentNet、Depreciation、SellingGeneralAndAdministrativeExpense、LiabilitiesCurrent、NetCashProvidedByUsedInOperatingActivities、NetIncomeLoss。盈餘：若明列ExtraordinaryItemNetOfTax則NI減該項；否則只在由相鄰期末推定財年開始>2015-12-15、ASU2015-01已生效時接受NI。此為明列的XBRL適配，非Compustat逐欄複製。較早年度缺明確口徑則拒絕。毛利率、AQI殘餘占比、折舊率與分母須正值，財報欄位不看收益決定封頂。

## 固定四方法，不追加搜索

1. price_matched：完整可算M的同一公司集合內，price<MA20且r20<0，按舊負向lane價格分數排名取最多20。
2. m_only_raw：M>-1.78，不要求價格弱勢，按M降序、ADV降序、symbol升序取最多20。
3. m_weak_raw：M>-1.78且price<MA20、r20<0；同上排名，為前輪提出組合的主測試。
4. m_weak_winsor：當天完整可算M的資格集合內，八比率分別按1%/99%縮尾，再算M與套同門檻/價格確認。只用已知資料，這是敏感度，不是原論文年度縮尾流程的完整複製。

## 價格、收益、風險

沿用前輪59個不重疊20日訊號，61日價格/成交量完整、price>=5、ADV20>=20M，新增61日OHLC有限、正值且low<=open/close<=high。次日開盤進場、第20日收盤退出，做空報酬1-exit/entry，不截斷損失。每檔總本金1/20，未選足留現金。固定往返0.1%/0.2%和年借券0%/5%/20%，按實際曆日。未知退出價整期未知；daily high上漲10%/20%為逆向事件。未有實際券源、股息補償、召回、融資、保證金或完整下市，不能稱可交易淨绩效。

全期和每年公開所有方法、可算M公司數、門檻/價格確認後數量、20名額覆蓋、使用本金比例。空集合期收益為0，另列有持倉期均值。配對比較只用共同有持倉且收益已知日期，另列同樣選滿20日期；不足20時再按投入名目正規化收益比較，不能把低投入當選股改善。循環block bootstrap=3期/2000次/seed20260929、NW lag1；未做多重比較校正。

## 預定檢查

Decimal独立oracle：八比率=1、TATA=0時M=-2.48，TATA=.2時M=-1.5442。門檻嚴格大於；單位縮放、缺值拒絕、同日申報隔離、最新缺失不回退、未來申報不改過去、CIK去重與20名額。已選公司原始財報與來源保留，獨立scalar重算。

預先固定列排除2020的敏感度，不由年份挑勝者。若已有本地外部價格覆蓋，核對選入公司2016–2020進出場價/收益方向；缺覆蓋直接明列。成本後平均正值、同宇宙改善、跨年穩健與資料限制均需檢查；此輪不自動上線。
