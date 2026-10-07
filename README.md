# ticarisk-bot

Headless automation for Ticarisk's **Production Center** and **Farm**, scheduled
through GitHub Actions. One cycle every hour:

| section | what it does | endpoint |
|---|---|---|
| `production` | collect every business's output, then restart every idle card | `businesses.php` |
| `fields` | harvest ripe fields, then replant empty ones, **rotating** through the crop list | `tarlalar.php` |
| `orchards` | harvest ripe fruit, then water orchards awaiting water | `bahceler.php` |
| `barns` | collect barn products (and feed, if enabled) | `ahirlar.php` |
| `coops` | collect coop products (and feed, if enabled) | `kumesler.php` |
| `bees` | harvest honey, then buy honeycomb back up to the hive limit | `aricilik.php` |
| `math` | OCR-solve every available arithmetic question, then empty the vault | `matematik.php` |

It never sells animals (`toplu_sat`) and never spends money unless you turn the
water auto-buy or the honeycomb refill on. Every spend has its own switch and its
own hard ceiling. `math` earns money instead of spending it: **$1,500–1,700 per
correct answer**, paid into a vault that the section empties into your balance
when it finishes.

**Requirements.** `requests` + `numpy` (`pip install -r requirements.txt`), plus
the `tesseract` binary for `math` — `sudo apt-get install tesseract-ocr` /
`brew install tesseract`. Without it the math section reports itself skipped and
everything else still runs.

## Setup

1. Push this folder to a GitHub repo.
2. Add a **secret** for each:
   - `TICARISK_USER`
   - `TICARISK_PASS`
3. *(optional)* add **variables** under *Settings → Secrets and variables → Actions*:

   | variable | default | meaning |
   |---|---|---|
   | `TICARISK_SECTIONS` | all six | which sections run |
   | `TICARISK_CROP_ID` | `4` (Potato, 6 h) | fallback crop; `1` = Wheat (4 h) |
   | `TICARISK_CROP_ROTATION` | `1` | `1` = walk the crop list forward on every planting instead of repeating one crop |
   | `TICARISK_ROTATION_FILE` | `crop_rotation.json` | where the rotation cursor is kept between runs (git-ignored; created fresh on CI) |
   | `TICARISK_BUY_WATER_LITERS` | `0` | buy this many litres when planting fails for lack of water |
   | `TICARISK_MAX_WATER_SPEND` | `0` (no cap) | hard ceiling on $ spent on water |
   | `TICARISK_FEED_ANIMALS` | `1` | `0` = collect only, never buy feed |
   | `TICARISK_PETEK_REFILL` | `1` | after a harvest, buy honeycomb back up to `hives × 10` |
   | `TICARISK_PETEK_MAX_SPEND` | `60000` | hard ceiling ($) on one refill — honeycomb is **$1,500** each |
   | `TICARISK_MATH` | `1` | `0` = never touch the math game |
   | `TICARISK_MATH_MAX` | `400` | safety stop on questions per run (the pool is ~300/h) |
   | `TICARISK_MATH_WITHDRAW` | `1` | `1` = move the vault into the balance when done |
   | `TICARISK_MATH_MIN_MS` | `1500` | delay floor per answer — the server rejects under ~1400 ms |
   | `TICARISK_CAPTCHA_GUESSES` | `4` | slider guesses per gate. **5 rejected = 600 s lock.** 4 is the ceiling on purpose |
   | `TICARISK_CAPTCHA_ROUNDS` | `4` | puzzles per gate — one fresh puzzle per guess, never two guesses on one |

4. Actions → **ticarisk-cycle** → *Run workflow*.

The game runs on **Europe/Istanbul (TRT, UTC+3, no DST)**; GitHub cron is UTC,
so the two scheduled triggers are written in both timezones:

| cron (UTC) | TRT | why |
|---|---|---|
| `11 * * * *` | `:11` every hour | **everything**, incl. math — farm (~15 s) then that hour's ~300 questions (~11 min). Offset off `:00` to dodge GitHub's stampede |
| `5 21 * * *` | `00:05` | right after the daily limits reset (forex $200 M/day, water 25,000 L/day) |

GitHub often runs scheduled jobs several minutes late — that is expected.

Every run uploads `ticarisk-cycle.log` and an `inspect/` folder containing raw
page dumps, so you can see exactly what the bot parsed.

## Running locally

```bash
pip install -r requirements.txt
TICARISK_USER=you TICARISK_PASS=secret python3 bot.py --dry-run --inspect
python3 bot.py                      # real cycle
python3 bot.py --sections fields    # just the farm
```

Exit code is `0` when every step succeeded. Steps that the game refused for a
pure state reason (`Not enough materials…`) are reported as `warn` and do **not**
fail the job; authentication and server errors still do.

## Minutes budget

The math pool is **strictly per-hour**: `check_answer` answers
`Hourly round limit reached! Try again next hour.` and the pool does **not**
accumulate — an hour the bot misses is lost forever. So the schedule is hourly.

| repo visibility | minutes | result |
|---|---|---|
| **public** | **unlimited** | the schedule below runs free — this is the intended setup |
| private, free plan | 2,000/month | **does not fit**: 720 hourly runs × ~12 min ≈ 8,600 min |
| private, Student Pack | 3,000/month | still does not fit |

On a private repo, drop `math` from the hourly cron (keep farm-only) and it
falls to ~460 min/month — or run math on your own machine, where tesseract is
already installed:

```bash
TICARISK_USER=you TICARISK_PASS=secret python3 bot.py --sections math
```

That costs zero minutes and drains the same ~300 questions.

## Cost model — managed spends

Every dollar the bot can spend is capped, and the caps are enforced *before*
the POST, not after. Water bought only when a plant actually fails; feed bought
only when stock is under its trigger.

| what | when | cap |
|---|---|---|
| **water** | tank drops under `TICARISK_WATER_TRIGGER` (1,000 L), or a field says `Not enough water!` | `TICARISK_MAX_WATER_SPEND` per buy (**$20,000**) **and** `TICARISK_MAX_WATER_DAY` per day (**$1,000,000**) |
| **feed top-up** | only when stock < trigger | per-material cap + `TICARISK_MAX_RESTOCK_SPEND` (**$400,000/day**) |
| **honeycomb** | only when the page reports combs below max | `TICARISK_PETEK_MAX_SPEND` (**$60,000**) |
| animal feed action | — | free (uses stock) |

Triggers are sized against measured burn, not guesswork — the account burns
**6,000 kg of poultry feed and ~1,000 kg of fattening feed a day**, so the
default trigger sits at roughly half a day's buffer rather than a token amount.

```bash
# default: restock each material up to `trigger` when stock falls under it,
# never more than the third field in one purchase
TICARISK_RESTOCK="besi_yemi:800:150000,kumes_yemi:4000:120000,besi_suyu:2000:150000"
TICARISK_MAX_RESTOCK_SPEND=400000     # per day, feed only
TICARISK_BUY_WATER_LITERS=150         # 0 disables water buying entirely
TICARISK_MAX_WATER_SPEND=20000        # per purchase
TICARISK_MAX_WATER_DAY=1000000        # per day, 7 fields x 6 cycles ≈ $756k
```

`restock_spend.json` is the daily ledger — `restock` and `water` are tracked in
separate buckets and reset on date change. Expected real spend: feed ~$300k/day,
water ~$756k/day **only after fields start replanting** (they have not yet).

**Not buyable.** Cement, Cyanide, Acid, Marble and Silicon are absent from
`hammaddeler.php` — they are factory output. A mine stuck on
`Need: Cyanide (7 short)` is waiting on your factories, not on money.

Harvest/replant is otherwise free.

**Math grind.** Costs nothing to run — it pays. Each correct answer lands
$1,500–1,700 in the vault; `hesaba_cek` transfers it at the end of the section.
The binding cost is time, not money: the server refuses an answer that arrives
in under ~1.4 s, so one question costs ~1.6–2.0 s end to end.

**Honeycomb refill.** A hive holds 10 honeycomb (`maxPetek = kovanSayisi * 10`)
and each costs **$1,500**. The refill only fires when the page reports the area
below its limit — so if a harvest does not actually consume the comb, the step
is a no-op and nothing is spent. It is additionally capped by
`TICARISK_PETEK_MAX_SPEND` and by the account balance.

## How safe is a re-run?

Every mutating call is re-validated by the server, which returns a plain
business error instead of double-applying the action:

- `toplu_hasat` on an unripe field → `Selected fields are not ready.`
- `toplu_bahce_sula` on an orchard that isn't waiting → `This orchard is not waiting for watering right now.`
- `toplu_tum_isletme_topla` with no businesses → `You have no businesses yet!`

Those are reported as `ok`, not failures. Game-state refusals — notably
`Not enough materials to start production! Need: Cyanide (30 short)` — are
reported as `warn`. Real problems (`Security error`,
`CSRF token validation failed`, `Not enough water!`) still fail the step and
therefore the job.

A partial `Errors:` list (e.g. seven orchards, all *"No harvestable trees"*) is
`ok` only when **every** item matches the benign allow-list; one unfamiliar item
and the step fails so nothing is silently swallowed.

## Validation status

Verified end-to-end against `shrey33` (5 mines, 3 factories, 7 orchards,
1 field, 1 barn, 1 coop):

- login, single-session cookie handling, per-page CSRF extraction
- `toplu_tum_isletme_topla` (collect all) — e.g. *"Collected from 5 businesses!
  Income: $5,632 · Tax: $226 · Wood: 11, Water: 30, …"*
- **`start_production` — proven.** The card is a plain form
  (`csrf_token` + `business_id` + a `start_production` button); no product or
  slot argument is sent. Two factories came back `Production started
  successfully!`, and a gold mine came back
  `Not enough materials to start production! Need: Cyanide (30 short), Acid (40 short)`
  (surfaced as `warn`).
- `check_tarla_status`, `toplu_hasat`, `toplu_ekim` (reaches the water check,
  which only happens after the server has accepted the ids and crop)
- **crop rotation** — unit-tested against the real page: options come back in
  page order (Wheat, Potato, Carrot, Corn, Tomato, Strawberry, Cotton), the
  cursor advances one step per planting, and with no cursor file it seeds after
  whatever crop is in the ground (7× Tomato → Strawberry → Cotton → Wheat → …)
- `bahceler` harvest/water, `ahirlar`/`kumesler` scans
- **`aricilik` — fixed.** The area id exists only inside the buy-button
  `onclick` handlers (`kovanAlModal(3179, 4, 0)` / `petekAlModal(3179, 4, 40)`);
  there is no `data-aricilik-id` attribute, so the old scan reported
  *"no beekeeping area owned"* on an account that has one. The bee section now
  finds `3179`, reports `comb 3179 — full (40/40)` (so no spend), and would buy
  `10×hives − current` when a harvest burns one.
- the `tarla_ids[]=…` wire format — a plain `tarla_ids=…` is silently ignored
- **full six-section cycle: 11/11 steps ok, exit code 0** (2026-10-08)
- **`math`: 220/220 answers correct, pool drained to 0, 25/25 steps ok, exit
  0, 548 s** — tesseract reads the question PNG raw with no preprocessing
  (`--psm 7` + digit whitelist). The section ends by transferring the vault:
  `$363,875 moved`, balance `841,396.89 → 1,224,771.89`.
- **slider captcha: 22 gates cleared in that run, never locked.** 18 solved on
  the first guess, 4 needed a second. The earlier lock was self-inflicted — the
  first version allowed **5 rejected `verify_human` calls on one puzzle**, which
  is exactly the lock threshold. It now spends at most `TICARISK_CAPTCHA_GUESSES`
  (4) guesses *across the whole gate*, and each guess gets its own freshly
  generated puzzle. Detectors merged the same way the userscript does: 4-side
  slot scorer, normalised cross-correlation against `piece_image`, and edge
  run-pairing.

The `productSelectModal<bid>_slot<sid>` / `selectBusinessProductSlot(...)`
scrape that used to live here matched **zero** occurrences in the real payload —
it was a guess at a flow the page doesn't use. The form parser replaced it.

## Notes

- The slider captcha in `businesses.php` is a **client-side gate only**: the
  collect request carries nothing but `action` and `csrf_token`. This bot never
  triggers it, so no captcha solving is needed here.
- The gate posts `generate_puzzle` / `verify_human` to
  `window.PUZZLE_AJAX_URL || window.location.href`, and `PUZZLE_AJAX_URL` is
  never assigned — so on `businesses.php` the puzzle talks to **that page**. The
  payload is byte-for-byte the same shape as the math page's (`canvas_width`
  400, `canvas_height` 250, `piece_width`/`piece_height` 50, `piece_y` 100).
  A live solve returned `{"success": true}`.
- Two `PHPSESSID` cookies in one jar means the server validates a session that
  never saw your CSRF token, and every action returns `Security error`. The
  bot logs in fresh each run and collapses duplicates defensively.
- GitHub's runner IPs are datacenter addresses. If Cloudflare starts challenging
  them you'll see HTML instead of JSON — the log will show it immediately.
- Scheduled workflows are disabled by GitHub after **60 days** without a commit
  to the repo.
- **The math grind lives in the userscript, not here.** `matematik.php` hands out
  a 400×120 PNG and takes the answer ≥ ~1.4 s later; reading it needs OCR, and
  this runner has no `tesseract`/`PIL` and no sudo to install them. Headless it
  would mean `sudo apt-get install tesseract-ocr` plus `pip install pytesseract
  pillow` on the runner — script-only until you ask for that.

## Companion userscript

`ticarisk-math-bot.user.js` **v3.4** (in the parent folder) is the browser half:
it solves the slider captcha on **every** page of the site, runs the same
production/farm cycle at `:23` past each hour Europe/Istanbul while the PC is
on, grinds the math game, and can check itself for a newer version. It is
independent of this bot — run either, or both. The math grind now exists in
**both**: the script (tesseract.js in the browser) and `bot.py` (tesseract CLI),
so pick whichever machine is on.

Parity with `bot.py`: same six sections, same soft-fail/benign classification,
same crop rotation (cursor in `localStorage`), same honeycomb refill, same
water auto-buy (both off by default).

**Math grind.** After each hourly cycle it solves every question the server will
give out — `get_question` → tesseract.js OCR → `check_answer` at ≥1400 ms, until
`kalan_islem` reaches `0` — then calls `hesaba_cek` to move the vault into the
main balance. If a question trips `verification_required` the existing slider
solver clears it and the grind resumes; it stops rather than guess when OCR
cannot read a question. Hit **Solve all math now** in the panel to run it by
hand. It is a no-op lockout-wise if the captcha is already locked.

Panel switches live in the script header (`MATH_GRIND`, `MATH_WITHDRAW`,
`PETEK_REFILL`, `CROP_ROTATION`, `BUY_WATER_LITERS`).

To turn on self-update, push that file to a repo and set `UPDATE_URL` in the
script header plus the `@downloadURL` / `@updateURL` metadata lines to its raw
URL. Leave `UPDATE_URL` empty to disable the check.
