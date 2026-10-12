# Government Tenders (`ihale.php`)

## Sub-features
- Automatically monitors daily auctions opening at 19:00 TRT and closing at 00:00 TRT.
- Enters top 4 tenders matched against inventory stock (Apples, Pears, Tomatoes, Milk, Honey).
- Counter-bids by $0.01 undercut when outbid to preserve leading position.

## How to get to it (user POV)
- Navigate to `https://www.ticarisk.com/ihale.php`.
- View active tenders, ceiling prices, and inventory requirements.
- Submit bids and track winning status.

## Driving it with bot
```bash
python3 bot.py --sections ihale
```

## Gotchas
- Only active between 19:00 and 00:00 TRT.
- Client browser timer uses local time (`new Date()`) and shows misleading countdowns for non-TRT timezones. Always trust server `bitis_tarihi`.
