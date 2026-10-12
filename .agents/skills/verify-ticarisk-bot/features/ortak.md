# Joint Jobs (`ortak.php`)

## Sub-features
- Joins active high-paying joint job rooms (Design Work @ $49k-$94k, Photography @ $35k-$50k).
- Creates new rooms with puzzle verification when no rooms are open.
- Cancels rooms stuck waiting without player joins for >30 minutes.

## How to get to it (user POV)
- Navigate to `https://www.ticarisk.com/ortak.php`.
- Check open job rooms or create a new job.
- Payout auto-credits to wallet upon room reaching capacity (3/3).

## Driving it with bot
```bash
python3 bot.py --sections ortak
```

## Gotchas
- Requires sufficient energy (35 energy for Design Work).
- Creation requires solving a slider puzzle captcha.
