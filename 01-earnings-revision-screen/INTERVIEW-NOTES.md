# Interview notes: earnings revision screen

These notes are for walking through the project out loud. The figures match the cached run dated 8 October 2026. If you re-download from Yahoo, re-run the notebook and quote the new output. The code that does the work is `earnings_screen.py`. The notebook calls that code, runs the SQL, and shows the charts.

This is a study project. It is not a stock recommendation.

## The thirty-second version

I built a ranking for 77 U.S. large-cap growth stocks. Four inputs: did they beat EPS estimates, are analysts raising numbers, is the growth rate speeding up, and is the valuation reasonable. I z-score each input against the peer group and average them. NVIDIA, Alphabet, Salesforce, Amazon, and Microchip Technology come out on top on the 8 October 2026 snapshot.

I could only backtest one of the four inputs. Free data gives me historical surprises. It does not give me the history of estimate revisions or of forward P/E. Over 20 quarters, the highest-surprise quintile compounded at 23.9% a year, versus 18.7% for an equal-weight basket of the same names and 13.5% for IWF, a Russell 1000 Growth ETF. The ordering is messy (the third quintile did about as well as the fifth), the gap versus the bottom quintile is small relative to the noise, and a regression fit on the first half of the sample does not predict the second half. I would describe that as a mild, unstable tilt, not as evidence of a proven factor.

## Why an analyst would build this

A growth process often starts with the sell-side EPS page, before anyone builds a full model.

- A beat that repeats is a sign the company is ahead of what the Street has in the model. One beat can be a low bar.
- The direction of the estimate matters as much as the last print. A stock that beat last quarter while analysts are cutting next year is a different situation from a stock where the whole path is moving up.
- "Accelerating growth" means the growth rate itself is rising. A company growing at 30% and then 15% is still growing. It is not accelerating.
- Growth at a reasonable price is the check that stops the screen from only buying whatever is most expensive.

The rank is a reading list. It is the start of the work, which is reading the release and rebuilding the EPS bridge.

## The universe, in one minute

`data/universe.csv` has 77 U.S. companies. I wanted names a large-cap growth coverage list would actually contain: software, semiconductors, internet platforms, innovative health care, payments, plus a few slower compounders so the screen has something to rank at the bottom. The list is fixed so the project does not depend on scraping an index. It is not the official Nasdaq-100 and it is not official Russell 1000 Growth membership.

I downloaded three extra U.S.-listed names that are incorporated abroad (NXP, Medtronic, Eaton) and then left them out. The benchmark is a U.S. index. Their rows are still in the raw files. They are not in the scores.

Sectors are Yahoo's labels. Technology is 32 of 77 names, and 12 of the top 20 scores. Say that out loud. A PM will ask whether you just built a tech list.

## The data, and the honest limits

Source: Yahoo Finance, pulled 8 October 2026, saved under `data/raw/` so the notebook runs without the network.

You have real history for:

- Quarterly EPS estimate, reported EPS, and the surprise.
- Adjusted daily prices, including IWF, from January 2020 through 8 October 2026.

You have a snapshot, and only a snapshot, for:

- How the consensus changed over the last 7, 30, 60, and 90 days.
- How many analysts raised or cut in the last 30 days.
- Forward P/E, PEG, market cap, sector.

Say this before anyone asks you to "extend the backtest to the full model." You cannot, with this feed. A paid terminal stores the consensus as it stood on each past date. That is the dataset this test is missing.

Two cleaning facts worth knowing cold:

1. My recomputed surprise matches Yahoo's field within a median absolute gap of 0.15 percentage points. The formula is `(reported − estimate) / estimate × 100`.
2. The median name beat in 87.5% of its last eight quarters. In this peer group, "did they beat?" is a weak separator. Almost everyone beats. The size of the beat, and whether estimates are rising, is the part that varies.

## Step by step

### 1. Make the surprise usable on the right day

`clean_earnings` in `earnings_screen.py`.

If the timestamp is 4:00 p.m. New York or later, I treat the news as after the close and move the available date to the next business day. A morning release is available the same day. The backtest then keeps only rows with `available_date` on or before the quarter-end. There is an explicit check: the latest print used in any signal is never after the rebalance. On this run that gap is zero days.

I drop estimates below $0.05, and negative estimates. A surprise percentage on a one-cent estimate, or on a loss, points in a messy direction. I cap whatever remains at ±25%. Beyond that, the percentage is usually a tiny base or a one-time item. 191 of 1,784 usable prints hit the cap.

I also drop anything before 2019. BlackRock's file had a 2008–2009 stub and then a gap until 2022. Counting "four rows up" would have treated 2009 as the year-ago quarter for 2022. Year-over-year growth is matched by the calendar instead: the print about 365 days earlier, and only if it falls between 300 and 450 days earlier.

### 2. Build the four factors

`build_current_factors` and `score_factors`.

**Surprise.** Average of the last eight capped surprises, and the beat rate over those eight. Both get a z-score. The factor is the average of those two z-scores, z-scored again, then clipped at ±2.

**Revisions.** For this fiscal year and next year, percent change versus the consensus 90 days ago: `(now − then) / |then|`. Average the two years. Separately, 30-day breadth on this fiscal year: `(raises − cuts) / number of analysts`. Same z-score combine-and-clip.

**Acceleration.** Two ingredients.

- Reported: year-over-year EPS growth, capped at ±100% so a trough does not print +600%. Acceleration is the average of the last two year-over-year rates minus the average of the two before that, in percentage points.
- Forward: next year's consensus growth minus this year's, after capping each growth rate at ±40%. If either EPS estimate is zero or negative, this ingredient is blank.

**Valuation.** Forward P/E only if it is positive and under 250. PEG only if it is positive and under 10. A negative PEG is usually negative expected growth, which is not "cheap," so it is left blank. Lower is better, so I z-score the negative of the multiple.

Winsorizing at the 5th and 95th percentile happens before the z-score, on the raw ingredient. That pulls in the tails without deleting the stock.

The composite is the equal-weight average of the four capped factor z-scores, then one more z-score across the universe so the bar chart is centered at zero. Rank 1 is the best. You can check the identity in the notebook: re-zscoring the row average reproduces the composite to rounding error.

### 3. A z-score, on a napkin

Three stocks, average surprise 2%, 6%, and 10%. The mean is 6. The population standard deviation is about 3.3. The z-scores are about −1.2, 0, and +1.2.

A z-score answers "how unusual is this name versus these peers today?" It does not answer "is 10% a big beat in absolute terms?" That is why the peer group has to be a real peer group. A 6% beat looks ordinary here because the median average surprise is about 6%.

Clipping at ±2 is a second step. Nike's acceleration wanted to be something like +4 or +5 standard deviations before the cap, because EPS collapsed and then the growth rate off the bottom looked enormous. A +4 would have outvoted a bad revision score. After the cap, each factor gets at most a "very high" or "very low" vote. Nike still ranks 9th. The heatmap shows the revision cell in the opposite color from the surprise cell. That disagreement is the point of showing the factors separately.

### 4. The backtest

`build_surprise_panel`, `portfolio_returns`, `performance_stats`.

Quarter-ends only. Signal at 30 September uses surprises known by 30 September. The return is from that month-end close to the next quarter-end close. Quintile 5 is the highest average capped surprise over the last four reported quarters. About 15 stocks per quintile. Equal weight at the rebalance, weights then drift. I checked in code that the compounded monthly path equals the simple average of the stocks' quarterly returns. If those ever diverge, the script raises.

IWF is bought and held over the same quarter, using the same adjusted prices.

Sample: signal dates 30 September 2021 through 30 June 2026, last return through 30 September 2026. Twenty quarters. The surprise history only starts in late 2020, so four quarters of history are not available much earlier than autumn 2021.

What is in the test: trailing surprise.

What is not in the test: revisions, acceleration, valuation, the composite.

### 5. The regression

`sklearn_signal_check`. One feature. The feature is the surprise z-score on the rebalance date. The target is the stock's next-quarter return minus the equal-weight universe that same quarter, so a bull-market quarter does not count as the factor "working."

The split is by time. Fit on rebalance dates before 31 March 2024 (727 stock-quarters). Score the later dates (769). A random split would leak, because the same market regime would land on both sides.

The test R² is about zero. The full-sample slope is about 0.12 percentage points of excess return per one standard deviation of surprise. The rank correlation is about −0.01. In the first half, Q5's excess return versus Q1 was about −1.3 percentage points per quarter. In the second half it was about +4.0. Same signal, opposite sign, depending on the window.

### 6. The SQL

The database is `data/earnings_screen.db`. The notebook runs four queries. They are there to show you can get the same answers out of SQL that the pandas code produced.

- Last eight capped surprises: average and beat rate, by ticker. Nike and Amazon come out on top of the raw surprise list. That is a useful contrast with the composite, because Nike's revision score knocks it down the final rank.
- Average composite by sector, joined to `universe`.
- Names in the top 15 that also have a positive 90-day revision and a forward P/E below the universe average of sensible multiples. That is the short "revisions up, and not the expensive half" list.
- Average quarterly portfolio return by quintile, from `backtest_quarterly`. This matches the performance table. Averaging the stock-level rows in `backtest_members` is close but not identical, because a quarter with more names would weigh more. The portfolio statistic gives each quarter one vote. If someone asks, say that.

Indexes are on ticker plus date, and on rank. For this size they are not about speed. They show you know what you would index if the table were the real holdings history.

## How to talk about the results

Lead with the screen, then the caveat, then the backtest.

"On this date the composite puts NVIDIA first. The reason is revisions: 43 up and 1 down over 30 days, and a forward P/E of about 14.5 that looks cheap only because expected growth is very high. Salesforce is the other clean one: 48 up and 0 down, forward P/E about 14, but next year's EPS estimate is slightly below this year's, so I would not call the growth accelerating. Alphabet and Amazon have strong trailing surprises and a big move in this year's number, with next year's number lower. I would rebuild EPS before I trusted the current-year base."

"Nike is 9th. I would not advance it. The revision is −26% over 90 days. The surprise percentage is large because the bar was cut."

"Micron is 6th with a forward P/E near 5. Earnings went from a loss to a very high current-year estimate. I would normalize earnings before I called that cheap. The acceleration score is negative, which is the screen agreeing that the growth rate off the peak is slowing."

"The backtest says the high-surprise quintile finished ahead in this window: 23.9% CAGR versus 18.7% equal-weight and 13.5% for IWF. It beat the equal-weight basket in 55% of quarters. Q3 compounded at 23.8%. The t-statistic on the Q5 minus Q1 gap is about 0.6. I would not take that to a PM as a factor that worked."

Also say why the equal-weight list beat IWF. Every name in the file is a company that was still a large cap in October 2026. Losers that dropped out of the growth universe are missing. IWF held the live cap-weighted index the whole time. Equal weight versus cap weight is a second difference. The fair test of the *signal* is Q5 against the equal-weight universe, not against IWF.

## Definitions you may be asked to write on a whiteboard

**CAGR.** End wealth relative to start wealth, raised to `1 / years`, minus 1. A 23.9% CAGR for Q5 is the monthly wealth curve, not the arithmetic average of the quarterly returns.

**Volatility.** Standard deviation of monthly returns, annualized with √12. Sample standard deviation (divide by n − 1).

**Sharpe with rf = 0.** Average monthly return divided by the monthly standard deviation, times √12. Say the risk-free rate is zero, so this is higher than a Sharpe that subtracts T-bills. You did not have a Treasury series in the study, and you did not want to pretend.

**Max drawdown.** The worst peak-to-trough decline of the wealth curve. Measured monthly here. Q5's is −26.9%. Q1's is shallower, at −20.4%, even though Q1's CAGR is lower. Higher return and deeper drawdown can show up together. That is why both columns are in the table.

**Hit rate.** Share of quarters. "Quarters up" is the share above zero. "Beat IWF" is the share above IWF's return that quarter. Q5 beat IWF in 75% of quarters and beat the equal-weight universe in 55%. Those are different sentences. Do not blend them.

**PEG.** Forward P/E divided by the expected growth rate. On this snapshot NVIDIA's PEG is about 0.29 and Salesforce's is about 0.80. A lower PEG scores as more attractive. You throw out PEG when it is negative or above 10, because those values are usually a broken growth rate or a data oddity, not a bargain.

**Quintile.** Five buckets, equal count, reformed every quarter. Q5 always means "highest signal," not "fifth in some fixed list of stocks." Membership changes.

## Likely questions

**Why these four factors, and why equal weight?**
They are the earnings page a growth analyst already looks at: beat, revision, acceleration, valuation. Equal weight is the transparent baseline. A PM might want revisions at a higher weight. You can say that, and you can say you did not tune the weights to make the backtest look better, because the backtest cannot see three of the four factors anyway.

**Walk through NVIDIA.**
Revision z-score is at the +2 cap. 43 analysts up, 1 down, over 30 days, out of 51. Ninety-day consensus change about +15% across this year and next year. Surprise is fine but not extreme: +5.6% average, beat rate 100%, z-score only +0.33, because a lot of peers also beat. Forward acceleration is zero because both years' growth rates are above the 40% cap (this year about 95% in the vendor file, next year about 71%). Valuation helps: forward P/E 14.5, PEG 0.29. The composite z-score is 2.40, rank 1. The open question is whether a 71% growth rate is a reasonable thing to put in the denominator of a PEG.

**Why is Nike high?**
Surprise and acceleration are at the top of the scale. Revisions are at the bottom. Current-year EPS estimate fell from about $1.73 to $1.29 in 90 days. Seven cuts and one raise in 30 days. Beating a cut estimate by a wide percentage is not positive revision momentum. You left it in the top 10 so the disagreement is visible. You would not put it on a "rising estimates" list.

**Micron's P/E is 5. Is that a buy?**
No. The current-year consensus EPS is about $176, after quarterly EPS went from losses to about $33. Forward P/E on peak earnings is low by arithmetic. Next-year growth in the file is much slower than this year's, and the acceleration z-score is −1.54. The follow-up is a mid-cycle earnings estimate, not a purchase order.

**Apple is near the bottom. So the model dislikes Apple?**
The model says Apple's inputs are quiet relative to these 77 names on this date. The 90-day revision is about flat. The trailing surprise is ordinary in a group where almost everyone beats. The forward P/E is about 35. That is a screen result. It is not a view on the franchise. Costco and Walmart land in the same neighborhood: good businesses, expensive on this snapshot, no estimate momentum. Say that, or a PM will think you built a "sell quality" model.

**What is look-ahead bias, and how did you block it?**
Look-ahead bias is using a fact in a historical decision before the market had that fact. The usual ways it sneaks into an earnings study: using the reported surprise on the announcement day before the close, using a revised "what the estimate used to be" that was edited later, or forming groups with information from the whole sample (a full-sample z-score applied backwards). Here, the signal at each quarter-end uses only surprises with an available date on or before that day, and after-close prints roll to the next session. The quintiles are formed inside that date only. The valuation and revision snapshot is not used in the history at all.

**Where could bias still be?**
Survivorship. The list is companies that were large in 2026. The Yahoo "estimate at the time" can also be restated; you do not have a true point-in-time archive. Adjusted EPS versus GAAP can disagree, and one unusual quarter can dominate. You capped the damage. You did not remove it. Also, the same-day close after a morning earnings release includes the market's reaction. Your *signal* is allowed to know the surprise. Your *return* starts at that close, so you are not claiming you captured the opening gap. At a quarterly horizon the gap is small next to the three-month move. Say that if they push.

**Why not backtest the composite?**
Because you would have to pretend you knew, in 2022, the revision and the forward P/E that you only downloaded in 2026. That would be look-ahead. You tested the leg you can see historically and you labeled it as that.

**Your top quintile beat IWF. Is that alpha?**
It is a higher compound return than IWF in this window. Alpha would be return you can tie to the signal after you account for the market, the universe, the weighting, and the fact that every name survived. The equal-weight universe beat IWF by a wider margin than Q5 beat the equal-weight universe (18.7 versus 13.5, compared with 23.9 versus 18.7). And Q5 beat that universe in only 55% of quarters. Lead with those two sentences.

**Why isn't the bar chart a staircase, Q1 through Q5?**
Because the relationship is weak. Q5 and Q3 are both about 6.1% per quarter. Q4 is the worst, at 3.0%, worse than Q1. A real monotone premium would step up. This one does not. Twenty quarters is enough to see that, and not enough to explain it away with a story.

**What is the t-statistic of 0.6?**
The average Q5 minus Q1 quarterly gap, divided by its standard error. A rough classroom benchmark for "this is large relative to the noise" is around 2. 0.6 is not close to that. Also, stocks in a quintile move together, so the usual t-statistic treats the quarters as the observations (20 of them), which is the right unit, and still overstates confidence if you were to pretend each stock-quarter is independent. You did not do that. The regression is on stock-quarters and you do not quote its p-value for that reason.

**Why equal weight, not cap weight?**
Equal weight means NVIDIA's size cannot decide the quintile return. A cap-weighted version would answer a different question: "what if I held these names in proportion to size?" That is closer to IWF and further from "did the signal work." You can offer to run it. You should not pretend you already did.

**Why scikit-learn for one regression?**
The z-scores are a few lines of pandas, and they are easier to explain that way. The regression is the part that needs a fit on one period and a score on a later period. `LinearRegression` does that fit. The result is the useful part: the out-of-sample R² is about zero. You included it because a flat result is still a result.

**What would you do with a better data terminal?**
Store the consensus EPS, the surprise, and the forward P/E as of each month-end, historically. Rebuild the four-factor score only from data dated on or before the rebalance. Use point-in-time index membership so the universe does not consist of survivors. Use adjusted EPS that the desk actually models. Then the backtest can test the composite you would actually trade. Until then, the composite is a current reading list, and the backtest is a test of trailing surprise only.

**How would this become a portfolio?**
Start from names with a positive 90-day revision and a valuation score that is not a cyclical peak. On this date that conversation starts with NVIDIA, Salesforce, and Texas Instruments, and it pauses on Nike and on Micron's multiple. Cap a single name. Cap technology, because 12 of the top 20 are already technology. Rebalance on a schedule and measure turnover. Compare the book to IWF with allocation and selection attribution. That attribution study is the natural second project. None of the ranks in this folder are position sizes.

**What would you change if you rebuilt it?**
You might drop the beat-rate ingredient, because 87.5% of the group beats and the z-score has little room to move. You might require the 90-day revision to be positive before a name can rank in the top quartile, which would remove Nike by rule instead of by judgment. You would not change those rules after seeing which version raises the backtest CAGR. The backtest does not include the revision rule, so tuning it against the backtest would be fitting noise.

## If you blank on a number

The performance table and the top-10 table in `README.md` are the source. The ones to remember without looking:

- Date of the snapshot: 8 October 2026. Universe: 77 names.
- Backtest: 20 quarters, September 2021 through September 2026.
- Q5 CAGR 23.9%, equal-weight 18.7%, IWF 13.5%.
- Q5 beat the equal-weight universe in 55% of quarters and beat IWF in 75% of quarters.
- Q3 CAGR was 23.8%.
- Q5 minus Q1 is about 1.3 percentage points per quarter, t-statistic about 0.6.
- Out-of-sample R² is about zero.
- NVIDIA is rank 1 on revisions. Nike's 90-day revision is about −26%. Micron's forward P/E is about 5, and that number needs the cyclical caveat.
