# Interview notes: portfolio attribution versus the Russell 1000 Growth

These notes are for walking through the project out loud. The figures match the cached run dated 9 October 2026, with prices through 8 October 2026. If you re-download from Yahoo, re-run the notebook and quote the new output. The code that does the work is `attribution.py`. The notebook calls that code, runs the SQL, and shows the charts.

This is a study project. It is not a stock recommendation. The one-page version is `MEMO.md`.

## The thirty-second version

I built a 14-stock book from a fixed list of 77 U.S. large-cap growth names. Every quarter it holds the two largest companies in each sector, equal-weighted. From September 2021 through September 2026 that book compounded at 20.8% a year. IWF, the Russell 1000 Growth ETF I use as the benchmark, compounded at 13.5%. A cap-weighted portfolio of the same 77 names compounded at 17.9%.

The gap versus the cap-weighted list is not a winning sector bet. Linked allocation is −16.7 percentage points of the cumulative gap. Selection is about −1 point. Interaction is +47 points. The book was 14% technology against 47% for the cap-weighted list, and it did not hold NVIDIA until the end of 2024. Caterpillar and Eli Lilly are the names that made the gap back. Versus IWF, a large piece of the lead is the 77-name list itself. A regression on four style ETFs describes about half the month-to-month variation of the gap and does not account for the average. The book beat the cap-weighted list in only 9 of 20 quarters, and the t-statistic on that gap is 0.5. I would not take this to anyone as a rule that has been shown to work.

## Why an analyst would build this

Beating a growth benchmark is not one number. Sector weights, the names inside those sectors, and a style tilt can all produce the same compound return. Attribution is how you stop yourself from telling the wrong story about a number you already have.

This book is deliberately simple. Two names per sector, equal weight, reformed each quarter. The point of the rule is that it refuses to let market-cap concentration decide the book. The Russell 1000 Growth, through the IWF snapshot on the download date, has about 57% of its assets in ten holdings and about 57% in technology. The question is what that refusal cost, or paid, over five years.

## The book, in one minute

`data/universe.csv` is the same 77 U.S. names as the earnings-revision project. Same reason: a fixed coverage list, so the project does not depend on scraping an index provider. It is not official index membership.

Seven Yahoo sectors. Two names each. Fourteen positions, each at 1/14, about 7.1%. Weights drift inside the quarter. Average one-way turnover is 7.9% per quarter, so this is not a high-turnover book. Average active share versus the cap-weighted 77 is 51%. That is a genuinely different book, not a closet copy of the list.

Names worth knowing cold:

- Technology is Apple and Microsoft until the 31 December 2024 rebalance. NVIDIA is in for 6 of 20 rebalances. It is out again on 31 March 2025, when Apple and Microsoft are larger, and it is back from 30 June 2025.
- Amazon and Tesla are the consumer-cyclical pair for the entire window. Visa and Mastercard are the financial pair. Eli Lilly and UnitedHealth are the health-care pair. Alphabet is always in. Meta replaces Disney at the 30 June 2022 rebalance.
- Industrials is GE and Honeywell once, then Caterpillar and Honeywell for two years, then Caterpillar and GE from 31 December 2023.
- The 30 June 2026 book, held through 30 September 2026, is Alphabet, Meta, Amazon, Tesla, Costco, Walmart, Mastercard, Visa, Eli Lilly, UnitedHealth, Caterpillar, GE, Apple, and NVIDIA.

Eighteen names appear at some point. The book is 14 names every quarter.

## The data, and the honest limits

Source: Yahoo Finance, pulled 9 October 2026, saved under `data/raw/` so the notebook runs without the network. Adjusted prices are the total-return series. The study uses month-ends from 30 September 2021 through 30 September 2026. October 2026 is not a finished month.

Say this before anyone asks you to attribute the book to the official Russell sectors in 2022. You cannot, with this feed. There is no point-in-time membership file and no history of IWF's holdings. The Brinson table is the book against a cap-weighted portfolio of the same 77 names. IWF is the external benchmark for the performance table and for the style regression. The gap between the cap-weighted 77 and IWF is its own line.

The market-cap rank is point in time. Shares outstanding on or before the rebalance, times the price that day. Yahoo's price is split-adjusted, including splits that happen later, and the share count is the count on the print date. Those two do not match around a split unless you rescale. NVIDIA is the check I would say out loud: at the end of September 2021 its market cap in this file is about $500 billion, not several trillion, because the 2024 split has not happened yet. By September 2026 it is about $5.5 trillion. If those two numbers were both in the trillions, the rank would be fiction. The script raises if they are wrong. At the end of the file, this market cap divided by Yahoo's own market-cap field has a median ratio of 0.99, and every name falls between 0.87 and 1.05. None of them use a fallback that smears today's share count backward.

A few spinoff factors sit in Yahoo's split file. GE HealthCare and GE Vernova are the ones that matter for the industrials pair. They rescale the old price. They do not change the share count. I reverse them so the market cap uses the price the stock traded at. That is cleaning. It is not a look at the later return.

The current IWF snapshot is context only. Top 10 holdings are 57.7% of the ETF. NVIDIA is 15.7%. Technology is 56.7%. Alphabet is in there twice, Class A and Class C. The list holds Class A only. Those weights are not used in the arithmetic.

## Step by step

### 1. Build the quarter

`build_quarter_panel` in `attribution.py`.

A name is eligible if it has a market cap on the rebalance date and an adjusted price at both ends of the quarter. The two largest in each sector, by that market cap, are the book. Ties break on the ticker, so the choice does not depend on row order. The cap-weighted benchmark is every eligible name, weights proportional to the same market cap. There is a check that a shares date used for a selected name is not after the rebalance.

### 2. Returns

`monthly_book_returns`. Weights are set at the quarter-end and then drift. The compound of the three monthly returns equals the beginning-weight quarterly return. If those ever diverge, the script raises. IWF is the ETF's own adjusted price over the same dates. CAGR and the drawdown use the monthly wealth curve. Volatility and both Sharpes use the monthly standard deviation with a sample divisor.

Sharpe with rf = 0 matches the earnings-revision project and is too high, because T-bills paid something from 2022 on. Sharpe using BIL subtracts a 1–3 month T-bill ETF. BIL compounded at about 3.7% a year here. Say which one you are quoting.

### 3. Brinson-Fachler

`brinson_fachler`.

Allocation is the sector-weight bet, measured against the cap-weighted list's own return. Overweight a sector that beat the list, and allocation is positive. Selection is the benchmark's weight in the sector times the gap between your names and the sector's cap-weighted return. Interaction is the cross term: your active weight times that same within-sector gap.

They sum to your return minus the cap-weighted return. Every quarter. That is an identity. I would say the word identity out loud, because the natural wrong sentence is "the model explained 100% of the gap." The model added the gap up. It did not test anything.

If the book had held nothing in a sector, I would have called that an allocation decision and set selection and interaction to zero. It does not come up. Every sector has two names.

The effects are then linked with Carino coefficients so they sum to the difference in cumulative returns. The average quarterly allocation is −0.60 percentage points. The linked allocation is −16.7 points of the cumulative gap. If someone asks why those are different, the answer is compounding and the fact that a percentage point in a later, larger book is not the same dollar as a percentage point at the start. Carino is one standard way to make the pieces add to the cumulative gap. It is a choice of linking, and I would say so.

The stock-level version is easier to talk about. Each name has one return, so the entire gap is a weight gap: active weight times the stock's return minus the benchmark return. Those linked contributions also sum to the +29.4 point cumulative gap. A name you never held still has a contribution. Broadcom was never in the book and it cost about 6.6 points, because the cap-weighted list held it and it beat the list.

### 4. The quarter you can recompute

30 June 2024 rebalance, held through September. Book +9.5%. Cap-weighted list +4.02%. IWF +3.1%. Technology was half the cap-weighted list and returned 1.93%. The book was Apple and Microsoft, equal-weighted, +3.60% in technology, at a 14.29% weight. NVIDIA was third in technology that day and was not held.

Allocation +0.75 points, selection +0.84, interaction −0.60. Underweighting technology helped in this quarter, because technology lagged the list. Over the whole window it did the opposite. Technology's linked allocation is −22.6 points. Remember the window. Use the quarter only as the arithmetic check.

### 5. The regression

`style_regression`. scikit-learn `LinearRegression`.

Single index: monthly book return on monthly IWF. Beta 0.79, R² 0.85. The intercept times 12 is about 8.9 percentage points a year. That is an arithmetic annualization, not a compound alpha. With BIL subtracted from both sides, beta is still 0.79 and the annualized intercept is about 8.1. A beta below 1 in a market that went up is a headwind. The book beat IWF anyway, which is why the intercept is positive. Do not say the lead was "just beta." Beta worked against the lead.

The cap-weighted 77 has a beta of 0.99 and an R² of 0.98 against IWF. Tracking error is 2.9% a year. Correlation is 0.99. The annualized intercept is about 3.9 percentage points. So the list tracks the ETF's path and still finishes higher. That is the survivorship and weighting residual. It is not a stock-picking result.

Four spreads, left-hand side equal to book minus IWF:

- IWD minus IWF, value minus growth. Beta +0.37. Descriptive t-statistic 6.6.
- IWM minus IWB, small minus large. Beta −0.10. t −1.3.
- MTUM minus SPY, momentum minus the S&P 500. Beta about zero. t 0.1.
- QUAL minus SPY, quality minus the S&P 500. Beta +0.35. t 1.7.

In-sample R² 0.52. The split is the midpoint of the 60 months, fit through March 2024, score from April 2024. Out-of-sample R² 0.42. The months are not independent, so I do not quote a p-value. The t-statistic is only a scale.

The value loading does not explain the average gap. Value lagged growth in this window, by about 2 percentage points a year on that spread. A positive loading on a negative spread subtracts from the average. The intercept is about 6.5 percentage points a year. The average monthly gap is about 0.48 percentage points, which is about 5.8 points a year if you multiply by 12. The chart shows this on purpose: the sum of the four spreads with the intercept removed finishes near zero, and the sum of the actual monthly gaps finishes near +29 points. That sum is not the compound-return gap. The compound gap versus IWF is 156.7% minus 88.2%, which is 68.5 points.

These are ETF proxies. They are not Ken French factors. They have fees, and value-minus-growth is correlated about 0.37 with small-minus-large in this sample. Say that if someone calls them SMB and HML.

## How to talk about the results

Lead with the return, then the two comparisons, then the name, then the limit.

"The book compounded at 20.8%, IWF at 13.5%, and a cap-weight of the same names at 17.9%. I beat IWF in 14 quarters out of 20. I beat the cap-weighted list in 9. The t-statistic versus the list is 0.5."

"Inside the list, allocation lost 16.7 points of the cumulative gap and interaction made 47. I was 14% technology against 47%. NVIDIA was not in the book until the December 2024 rebalance, and being light NVIDIA cost about 21 points. Caterpillar and Eli Lilly made about 17 and 15 back. So I would not say the sector bets worked. I would say I held a different set of weights, and two of the overweights paid for the NVIDIA underweight in this particular window."

"Versus IWF, the cap-weighted list itself compounded almost four points a year ahead, with a 0.99 beta and a 2.9% tracking error. That residual is mostly the list: today's survivors, my market-cap weights, no fee. I do not have historical index holdings, so I do not pretend to split that residual into allocation and selection."

"The style regression is the part that can actually fail. R² is about a half, and 0.42 out of sample, which means the path has a stable piece. The average gap is still in the intercept. The book looks a bit like value relative to IWF, and value lost to growth over these years, so that tilt does not explain the lead."

Also say 2022 in one sentence. The book was −16.2% and IWF was −29.3%. The drawdown trough for the book, the cap-weighted list, and IWF is 30 September 2022. The book's max drawdown is −19.9%, IWF's is −30.7%. Not being a cap-weighted technology book was the whole story in that year. It is not the whole story of the five years.

## Definitions you may be asked to write on a whiteboard

**CAGR.** End wealth relative to start wealth, raised to `1 / years`, minus 1. The 20.8% is the monthly wealth curve over 60 months, not the average of the quarterly returns.

**Active weight.** Portfolio weight minus benchmark weight. The book's active weight in technology averages about −32 percentage points.

**Allocation.** Active sector weight times the sector's cap-weighted return minus the whole benchmark's return. This is Brinson-Fachler, not Brinson-Hood-Beebower. The difference is the Fachler version subtracts the benchmark return inside the allocation term, so overweighting a sector that merely matched the benchmark contributes zero. I would mention the other version only if asked. I used Fachler.

**Selection.** Benchmark sector weight times your sector return minus the benchmark's sector return.

**Interaction.** Active sector weight times that same within-sector gap. It is large here because the active weights are large. Selection plus interaction is +46 points. Allocation is −17. If a portfolio manager wants one "selection" number, that sum is the one to hand them, and you should still show the cross term.

**Carino link.** A set of weights, one per quarter, so the linked pieces sum to the cumulative return gap. It is not a new estimate of the effects.

**Active share.** Half the sum of absolute active weights. About 51% versus the cap-weighted 77. Zero would mean you hold the benchmark. One hundred percent would mean you hold none of it.

**One-way turnover.** Half the sum of absolute weight changes from the drifted end-of-quarter book into the next book. Average 7.9%. At 10 basis points one-way, the CAGR drag is about 0.04 points. I report that as a sensitivity. It is not in the 20.8%.

**Beta and the intercept.** Beta is the slope on IWF. The intercept times 12 is not a compound alpha and it is not a promise. With a zero cash rate it is also not an excess-return alpha. The BIL version is the one that subtracts a cash proxy.

**Tracking error.** Standard deviation of the monthly gap between the cap-weighted 77 and IWF, times √12. 2.9%.

**t-statistic, descriptive.** Average quarterly gap divided by its standard error, using the 20 quarters as the observations. 0.5 versus the cap-weighted list. 1.6 versus IWF. A rough classroom benchmark for "large relative to the noise" is around 2. Neither one clears that. The 1.6 is closer, and it still mixes the list with the rule.

## Likely questions

**Why this rule, and why not the top 10 by market cap?**
Top 10 by market cap would have been a concentrated technology book, close to IWF, and the attribution would have had less to say. Two per sector is a concentration limit a person can explain in one sentence. I did not search over the number of names to raise the CAGR. Equal-weight of all 77 is on the chart as the other simple alternative. It compounded at 17.9%, the same one-decimal CAGR as the cap-weighted list, on a different path (total return 128.1% versus 127.3%). The sector-pair book finished ahead of both.

**Is the 20.8% versus 13.5% alpha?**
It is a higher compound return than IWF in this window. Alpha, in the single-index sense, is what is left after the beta. Beta is 0.79, and the annualized intercept is still about 8 to 9 points depending on whether you subtract BIL. That intercept is not a transferable alpha. The list is survivors, the window is five years, and the rule won against the cap-weighted list in only 9 quarters. Lead with those limits.

**Walk through NVIDIA.**
It is the largest negative name, about −21 points of the cumulative gap versus the cap-weighted list, on an average active weight of about −6 points. It is not in the book for the 2023 and most of the 2024 run, because Apple and Microsoft were still larger on the point-in-time market cap. It enters at 31 December 2024. It drops out for one rebalance, 31 March 2025. On 30 June 2024 it was third in technology, about a 10.8% weight in the cap-weighted list, and that particular quarter it returned −1.7%, so missing it helped. The window is the opposite of that quarter. The full-sample number is the one I would quote.

**Why is interaction so large?**
Because the active weights are large and the within-sector gaps are large, and those two land in the same sectors. In technology the book was underweight, and Apple plus Microsoft lagged cap-weighted technology, which contained NVIDIA. Underweight times a negative within-sector gap is a positive interaction. It means "I did not hold much of a sleeve whose names, the ones I did hold, also lagged." The net technology line is still about −34 points. Interaction did not turn technology into a win. It stopped the selection term, which uses the benchmark's 47% weight, from charging me as if I had held that whole sleeve.

**UnitedHealth is a negative contributor and health care is a positive sector. How can both be true?**
The sector total is about +15 points. Eli Lilly is about +15 by itself. UnitedHealth was held all 20 quarters, at an average active weight of about +5 points, and it lagged, which cost about 5 points. The sector can be a net positive while one of the two names is not. That is the reason to show names and sectors, not just one of them.

**Broadcom was never held and it shows up as a cost. Is that a bug?**
No. The benchmark held it. It beat the benchmark. A zero weight against a positive active-return name is a negative contribution. The same arithmetic gives Adobe a small positive contribution: the benchmark held it, it lagged, and not holding it helped. I would rather show that than hide every name with a zero portfolio weight.

**Your cap-weighted list beat IWF. Is your benchmark wrong?**
It is the wrong object if the question is "what did the Russell do." It is the right object if the question is "what did my weights do inside the list I actually cover." Correlation 0.99 and a 2.9% tracking error say the paths match. A 3.9 point annualized intercept says the levels do not. Survivorship is the leading explanation. Different weights and the ETF fee are real and smaller. I report both numbers and I do not push the residual into the Brinson columns.

**Why not use today's IWF sector weights for the whole history?**
Because that would be look-ahead, and because today's weights are not 2022's weights. The snapshot is in the README so a reader can see how concentrated the ETF is now. It is labeled as a snapshot.

**Where is the look-ahead, if there is any?**
The rank uses shares and a price known on the rebalance date. Splits are aligned using the ex-date and the share prints, not using future returns. The spinoff adjustment puts a retroactively rescaled Yahoo price back onto the traded price. The return itself starts at that close and ends at the next quarter's close. What I cannot fix with this feed is survivorship: the list is October 2026's names, held from 2021 as if I had known they would still be in a growth coverage universe. I say that first when someone asks about bias.

**Why equal weight inside the 14, not cap weight?**
Cap-weighting the 14 would have put the result back on Apple, Microsoft, Alphabet, Amazon, and, later, NVIDIA. The question I wanted was what happens when the sector weights are flat. Cap-weighting the 14 is a different question, closer to the benchmark, and I did not run it. I would say that rather than imply I did.

**Why scikit-learn for one regression?**
The attribution arithmetic is pandas, because it is an identity and it should be readable line by line. The regression is a fit on one period and a score on a later period. `LinearRegression` does that fit. The useful result is the out-of-sample R² of 0.42 and an intercept that is still most of the average gap. A flat result would have been a result too. This one is not flat, and it still does not explain the mean.

**What would you do with a better data terminal?**
Store Russell 1000 Growth membership, sector, and weight at each month-end, historically. Run the same Brinson-Fachler table against those weights, and keep IWF as a check on the total return rather than as a substitute for the holdings. Use the index provider's sectors, not Yahoo's. Then the residual I am currently labeling "the list versus the ETF" would either shrink or become a real allocation. Until then, the stock-level story is only as good as the 77-name universe.

**How does this sit next to the earnings-revision screen?**
That project ranks the same 77 names and can only backtest trailing surprises. This project does not use those ranks. It is a different question: given a simple book, what is the gap versus the growth benchmark made of. The earnings screen's interview note said the natural second project was allocation and selection against IWF. This is that project, with the limit that the holdings benchmark has to be the coverage list rather than the official index.

**What would you change if you rebuilt it?**
I would not tune the two-name rule against this CAGR. I might report selection and interaction both split and combined, because interaction is where a listener gets lost, and I already show the name-level contributions for that reason. I would add official index holdings the day I had them. I would not drop the t-statistic of 0.5 to make the write-up cleaner.

## If you blank on a number

The performance table and the sector table in `README.md` are the source. The ones to remember without looking:

- Window: 30 September 2021 through 30 September 2026. 20 quarters, 60 months. Cache date 9 October 2026.
- Book CAGR 20.8%, cap-weighted 77 at 17.9%, equal-weight 77 at 17.9%, IWF at 13.5%.
- Total returns: 156.7%, 127.3%, 128.1%, 88.2%.
- Book beat IWF in 14 of 20 quarters and beat the cap-weighted list in 9 of 20.
- t-statistics: 0.5 versus the list, 1.6 versus IWF.
- Linked versus the list: allocation −16.7, selection −1.0, interaction +47.1, gap +29.4.
- Technology average weight 14% versus 47%. NVIDIA linked contribution about −21. Caterpillar about +17. Eli Lilly about +15.
- Beta to IWF 0.79. Four-spread R² 0.52 in sample, 0.42 out of sample. Intercept about 6.5 points a year. The value loading is the one that is large relative to the noise, and it does not explain the average gap.
- Cap-weighted list versus IWF: correlation 0.99, beta 0.99, tracking error 2.9%.
- 2022: book −16.2%, IWF −29.3%. Drawdown trough 30 September 2022. Book −19.9%, IWF −30.7%.
