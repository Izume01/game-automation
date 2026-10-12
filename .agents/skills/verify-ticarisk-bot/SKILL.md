---
name: verify-ticarisk-bot
description: "Verify ticarisk-bot behavior across all game modules: math solver, auto repair, bank interest, joint jobs, and tenders."
---

# Verify Ticarisk Bot

Drives and verifies the Ticarisk headless automation bot against the live web endpoints and local simulation harnesses.

## 1. Launch & Doctor
- **Health Check / Doctor**:
  ```bash
  python3 -c "
  import bot
  sess = bot.login()
  bal = bot.balance(sess)
  assert bal is not None, 'Failed to fetch account balance'
  print('Doctor OK - Logged in as:', bot.USER, 'Balance: $' + f'{bal:,.2f}')
  "
  ```
- **Cycle Dry-Run Check**:
  ```bash
  python3 bot.py --dry-run
  ```

## 2. Drive Features
- **Math Solver**:
  ```bash
  python3 bot.py --sections math
  ```
- **Auto Repair Workshop**:
  ```bash
  python3 bot.py --sections tamir
  ```
- **Bank Deposit & Compounding**:
  ```bash
  python3 bot.py --sections bank
  ```
- **Joint Jobs**:
  ```bash
  python3 bot.py --sections ortak
  ```
- **Full Automation Cycle**:
  ```bash
  python3 bot.py
  ```

## 3. Evidence
- Output logs in terminal and `ticarisk-cycle.log`.
- Inspect snapshots in `inspect/*.html`.
- Bank transaction audits via `bank.php?bank_tx_ajax=1`.
