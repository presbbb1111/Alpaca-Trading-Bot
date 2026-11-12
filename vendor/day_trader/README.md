# day_trader

Three tools from the Day Trader skill pack by Jake Schincariol
(github.com/Jakeschincariol/day-trader-skill, commit 3d0f4ef, MIT license, see LICENSE).
Copied unchanged. `approvals.py` runs them as command-line tools:

- `scan.py`: opening-range breakout scan of every US stock (Yahoo 5-minute bars)
- `exit.py`: plans the stop (opening-range midpoint) and the target (2R)
- `timemachine.py`: replays the rest of the day 10,000 times from the stock's
  last 60 days, and re-runs the same rule on each of those days
