# 本輪公式的原始研究依據

1. [Sloan (1996), Do Stock Prices Fully Reflect Information in Accruals and Cash Flows About Future Earnings?](https://www.cuhk.edu.hk/acy2/workshop/June2009Wasley/1996TAR%29.pdf)，The Accounting Review 71(3), 289–315。研究發現盈餘的應計部分較不持續，高應計公司後續異常報酬較低。原文用持續經營盈餘、平均資產及資產負債表變動口徑；本輪的 `(NetIncomeLoss-CFO)/期末Assets` 是明列的現金流法proxy，不是原文複製，也不借用原文收益當作本地20日結果。

2. [Hribar & Collins (2002), Errors in Estimating Accruals: Implications for Empirical Research](https://onlinelibrary.wiley.com/doi/10.1111/1475-679X.00041)，Journal of Accounting Research 40(1), 105–134。比較資產負債表法與直接現金流法，說明併購／停止營業等可能造成前者的量測誤差。這支持檢查現金流口徑的動機，沒有驗證本輪的AC>0門檻、價格確認或裸空收益。

3. [Nagel (2012), Evaporating Liquidity（作者校方頁）](https://gsbpreserve.stanford.edu/view/41388/evaporating-liquidity)，Review of Financial Studies 25(7), 2005–2039。短期反轉可用來研究流動性提供，效果隨市場環境變動。本輪的60日beta調整5日漲幅是一個簡單待測proxy；沒有複製原文投組、VIX條件或對沖，不把多空反轉文獻等同於單獨做空20日盈利。

4. [SEC Financial Statement Data Sets](https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets)。使用原申報季度ZIP的sub.txt及num.txt、同accession的USD／標準tag／無分部數值。季度URL、檔案bytes及SHA見本目錄source_manifest.json。SEC抽取資料不是完整財報的替代品，標準tag缺失及口徑差異仍需原申報查核。

cashburn_rebound 的「CFO<0＋弱勢＋最近5日反彈」是本輪自行固定的研究假設，不是上述文獻驗證過的精確組合。現金流為負可能來自擴張、季節性或營運模式，不能單憑這個數字認定詐欺、破產或公司沒有價值。
