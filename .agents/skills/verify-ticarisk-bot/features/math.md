# Math Game Solver (`matematik.php`)

## Sub-features
- Automatic OCR solving for arithmetic operations (`+`, `-`, `*`, `/`).
- Slider puzzle verification solver using normalized cross-correlation (NCC).
- Vault cash withdrawal at hourly round limits.

## How to get to it (user POV)
- Navigate to `https://www.ticarisk.com/matematik.php`.
- Solve math problem questions sequentially until pool limit.
- Claim accrued reward into cash balance.

## Driving it with bot
```bash
python3 bot.py --sections math
```

## Gotchas
- Slider puzzle can trigger periodically and requires accurate guess coordinates.
- Vault must be explicitly withdrawn when questions are exhausted or round ends.
