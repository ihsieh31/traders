# 四個可實作的做空研究公式

本檔把取值前固定的METHOD.md展開；沒有新增候選、改門檻或選取最好期限。四式本輪均未通過預定的探索條件，正式流程未採用。

共同集合U：trusted symbol、非金融年度申報、最新已知申報期末距訊號<=500日、同accession的3個標準數值可取，且61交易日完整、收盤>=5美元、ADV20>=2000萬美元、OHLC有效。申報接受日在訊號日當天的一律不可用；最新缺值不回退舊分數。

```python
AC = (NetIncomeLoss - CFO) / Assets
CB = -CFO / Assets
trend = close / mean(close[-20:]) - 1
r20 = close / close_20_sessions_ago - 1
r5 = close / close_5_sessions_ago - 1
weak = trend < 0 and r20 < 0
```

| 候選 | 篩選條件 | 排名分數（高分優先） |
|---|---|---|
| 高應計＋弱勢 | `AC > 0 and weak` | `AC` |
| 現金流為負＋弱勢 | `CFO < 0 and weak` | `CB` |
| 現金流為負＋弱勢中的反彈 | `CFO < 0 and weak and r5 > 0` | `r5` |
| 市場調整後近期上漲 | `r5 > 0 and e5 > 0` | `e5` |

最後一式：

```python
beta60 = Cov(last_60_stock_daily_returns, last_60_SPY_daily_returns) / Var(last_60_SPY_daily_returns)
e5 = r5 - beta60 * r5_SPY
```

60日beta包含截至訊號日的60個簡單日報酬；不是用未來持有期估beta。市場方差為零或資料不全時不評估這一式。它是價格反轉候選，沒有公司品質條件。

分數相同依ADV20降序，再symbol升序。`ADV20=mean(close*volume,last20)`。比率、報酬分數是原始值，並非正式Screening輸出的0–100評分；本輪只有研究實作。

研究各方法最多取20、不足留空。正式使用時的新做空額度應依既有共用預算取 `max(0,20-持倉審查唯一股票數-已分配新多頭股票數)`，且排除已占名額的重複股票；不能另開20檔做空。

特別限制：AC使用原始NetIncomeLoss標籤和期末資產，是計算proxy。已發現CVNA該標籤為歸屬股東淨損，與合併CFO範圍不同；不能當成所有公司口徑統一的應計盈餘。CB本身不使用NI，但本輪為公平比較共同U仍要求三欄位；不能把其收益外推到僅需兩欄位的更大宇宙。財報標記不等於企業爛、詐欺或股票必跌。
