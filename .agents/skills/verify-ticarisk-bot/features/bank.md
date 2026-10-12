# Bank Compounding (`bank.php`)

## Sub-features
- Auto-deposits liquid cash exceeding safety reserve ($500,000) up to $30M cap.
- Collects 2% daily interest upon 24-hour maturity.
- Prevents premature early withdrawals that would forfeit interest.

## How to get to it (user POV)
- Navigate to `https://www.ticarisk.com/bank.php`.
- View total deposited amount and countdown timer.
- Deposit money or withdraw when remaining time reaches 0.

## Driving it with bot
```bash
python3 bot.py --sections bank
```

## Gotchas
- Early withdrawals forfeit 2% interest. Must only withdraw when `data-seconds` <= 0.
- Maximum deposit limit is strictly $30,000,000.
