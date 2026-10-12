# Auto Repair Workshop (`tamir.php`)

## Sub-features
- Delivers completed car repairs via `collect_income_tamir.php`.
- Discovers waiting customers in queue and filters for high-yield quotes (>$30,000).
- Assigns repairs into open bays (bays 1–6).
- Calls new customers on 60-second cooldown with slider puzzle solver.

## How to get to it (user POV)
- Navigate to `https://www.ticarisk.com/tamir.php`.
- View bay timers and collect ready vehicles.
- Call customers and accept repair offers.

## Driving it with bot
```bash
python3 bot.py --sections tamir
```

## Gotchas
- 60s cooldown is enforced between calling customers.
- Puzzle slider verification is mandatory before calling customer.
