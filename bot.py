#!/usr/bin/env python3
"""Ticarisk production + farm automation.

Designed to run headless on a schedule (GitHub Actions / cron). One cycle:

  Production (businesses.php)
    1. collect output from every business      action=toplu_tum_isletme_topla
    2. restart any idle production slot        start_production

  Farm
    tarlalar.php  harvest ready fields, then replant empty fields
    bahceler.php  harvest ripe fruit, then water orchards waiting for water
    ahirlar.php   collect barn products (and feed, if enabled)
    kumesler.php  collect coop products (and feed, if enabled)
    aricilik.php  harvest honeycomb

Every mutating call is idempotent server-side: the game re-validates state and
returns a plain business error ("not ready", "not waiting for watering", ...)
rather than double-applying. That makes re-running a cycle safe.

Credentials come from TICARISK_USER / TICARISK_PASS.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

try:  # the slider solver needs numpy; everything else degrades gracefully
    import numpy as np
except Exception:  # pragma: no cover - numpy ships with the requirements
    np = None  # type: ignore[assignment]

import requests
import urllib3

urllib3.disable_warnings()

# --------------------------------------------------------------------------- config

BASE = os.environ.get("TICARISK_BASE", "https://www.ticarisk.com").rstrip("/")
USER = os.environ.get("TICARISK_USER", "")
PASSWORD = os.environ.get("TICARISK_PASS", "")

DEFAULT_SECTIONS = "production,fields,orchards,barns,coops,bees,math,jobs,tamir,bank,ortak"

# Bank 2% Daily Interest (bank.php)
BANK_AUTO_DEPOSIT = os.environ.get("TICARISK_BANK_DEPOSIT", "1") == "1"
BANK_RESERVE = float(os.environ.get("TICARISK_BANK_RESERVE", "500000"))  # keep $500k working capital
BANK_AUTO_WITHDRAW = os.environ.get("TICARISK_BANK_WITHDRAW", "1") == "1"

# Math game (matematik.php). Each question is a PNG, so this section shells out
# to the tesseract binary; without it, the section reports itself skipped.
MATH_ON = os.environ.get("TICARISK_MATH", "1") == "1"
MATH_MAX = int(os.environ.get("TICARISK_MATH_MAX", "650"))   # safety stop (covers full ~560k/hr pool)
MATH_WITHDRAW = os.environ.get("TICARISK_MATH_WITHDRAW", "1") == "1"
# Hard floor: the server rejects an answer arriving under ~1.4 s. OCR and both
# round trips are fitted INSIDE this window, so a question costs ~1.6 s total —
# the pause between requests (TICARISK_PAUSE) is deliberately not added on top.
MATH_MIN_MS = int(os.environ.get("TICARISK_MATH_MIN_MS", "1500"))
MATH_OP_TYPE = os.environ.get("TICARISK_MATH_OP_TYPE", "carpma")
MATH_OPS = {"toplama": "+", "cikarma": "-", "carpma": "*", "bolme": "/"}
# Slider captcha. 5 rejected verify_human calls lock the account for 600 s, so
# the budget is 4 — and it is spent ONE position per puzzle: burn a guess, drop
# that challenge and generate a fresh one, exactly as the userscript does. That
# is why the script never locks: it never fires a second guess at the same
# puzzle. The 4-guess ceiling is per gate and resets when a new gate appears.
CAPTCHA_GUESSES = int(os.environ.get("TICARISK_CAPTCHA_GUESSES", "4"))
CAPTCHA_ROUNDS = int(os.environ.get("TICARISK_CAPTCHA_ROUNDS", "4"))
CAPTCHA_MAX_GATES = int(os.environ.get("TICARISK_CAPTCHA_MAX_GATES", "60"))

# 3 = Tomato (4 h), 5 = Carrot (4 h), 1 = Wheat (4 h), 7 = Cotton (7 h), 6 = Strawberry (6 h).
# Fallback seed when rotation is off.
CROP_ID = int(os.environ.get("TICARISK_CROP_ID", "3"))

# Bulk-plant rotation: Tomato -> Carrot -> Wheat -> Cotton -> Strawberry (Potato removed).
CROP_ROTATION_MODE = os.environ.get("TICARISK_ROTATION_MODE", "daily")
CROP_DAILY_LIST = [
    int(x.strip())
    for x in os.environ.get("TICARISK_DAILY_CROPS", "3,5,1,7,6").split(",")
    if x.strip().isdigit()
]
CROP_ROTATION = os.environ.get("TICARISK_CROP_ROTATION", "1") == "1"
ROTATION_FILE = os.environ.get("TICARISK_ROTATION_FILE", "crop_rotation.json")

# Animal sections also spend feed. Set to 0 to harvest only.
FEED_ANIMALS = os.environ.get("TICARISK_FEED_ANIMALS", "1") == "1"

# Beekeeping: bal_hasat burns the honeycomb, so each cycle has to buy them
# back at $1,500 each. 0 = harvest only, never buy.
PETEK_REFILL = os.environ.get("TICARISK_PETEK_REFILL", "1") == "1"
PETEK_MAX_SPEND = float(os.environ.get("TICARISK_PETEK_MAX_SPEND", "60000"))
PETEK_PRICE = 1500

# Never bought unless > 0. One field needs 150 L at $120/L = $18,000.
BUY_WATER_LITERS = int(os.environ.get("TICARISK_BUY_WATER_LITERS", "0"))
MAX_WATER_SPEND = float(os.environ.get("TICARISK_MAX_WATER_SPEND", "0"))  # 0 = no cap

# Raw materials that stall a business when they run dry. Format, comma
# separated:  material_id:trigger:usd_cap   — buy up to `trigger` units when
# stock is under `trigger`, never spending more than `usd_cap` in one go.
# Cement/Cyanide/Acid/Silicon are NOT on the market; they come from factories.
RESTOCK_SPEC = os.environ.get(
    "TICARISK_RESTOCK",
    "besi_yemi:800:150000,kumes_yemi:4000:120000,besi_suyu:2000:150000",
)
MAX_RESTOCK_SPEND = float(os.environ.get("TICARISK_MAX_RESTOCK_SPEND", "400000"))
# water is billed separately: 7 fields x 6 cycles x $18,000 would blow past any
# per-purchase cap, so the whole day gets one number too
MAX_WATER_DAY = float(os.environ.get("TICARISK_MAX_WATER_DAY", "1000000"))
# Water is the root of the whole production chain — the chemical plant needs
# 200 L a cycle to make the Cyanide/Acid that unblocks the gold mine, and the
# concrete plant needs 62 L. Top the tank up on its own schedule, not only
# when a field happens to fail.
WATER_TRIGGER = int(os.environ.get("TICARISK_WATER_TRIGGER", "1000"))
RESTOCK_LEDGER = os.environ.get("TICARISK_RESTOCK_LEDGER", "restock_spend.json")

def _parse_restock(raw: str) -> List[Tuple[str, int, float]]:
    out: List[Tuple[str, int, float]] = []
    for part in raw.split(","):
        bits = [b.strip() for b in part.split(":")]
        if len(bits) == 3 and bits[0]:
            try:
                out.append((bits[0], int(bits[1]), float(bits[2])))
            except ValueError:
                log.warning("ignoring bad TICARISK_RESTOCK entry: %r", part)
    return out


RESTOCK = _parse_restock(RESTOCK_SPEC)

PAUSE = float(os.environ.get("TICARISK_PAUSE", "1.2"))  # between requests
REQUEST_TIMEOUT = float(os.environ.get("TICARISK_TIMEOUT", "30"))

DRY_RUN = False

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

XHR = {"X-Requested-With": "XMLHttpRequest"}
JSON_HDR = {"Accept": "application/json, text/plain, */*", **XHR}

log = logging.getLogger("ticarisk")


# --------------------------------------------------------------------------- result


@dataclass
class Step:
    section: str
    action: str
    ok: bool
    detail: str = ""
    soft: bool = False  # refused by game state (missing materials, ...) not a malfunction

    def line(self) -> str:
        mark = "ok  " if self.ok else ("warn" if self.soft else "FAIL")
        tail = f" — {self.detail}" if self.detail else ""
        return f"[{mark}] {self.section}: {self.action}{tail}"


@dataclass
class Cycle:
    steps: List[Step] = field(default_factory=list)

    def add(
        self, section: str, action: str, ok: bool, detail: str = "", soft: bool = False
    ) -> Step:
        s = Step(section, action, ok, detail, soft)
        self.steps.append(s)
        log.info(s.line())
        return s

    @property
    def failed(self) -> List[Step]:
        return [s for s in self.steps if not s.ok and not s.soft]

    @property
    def warned(self) -> List[Step]:
        return [s for s in self.steps if not s.ok and s.soft]


# --------------------------------------------------------------------------- http


def _dedupe_cookies(sess: requests.Session) -> None:
    """Drop duplicate cookie names. Two PHPSESSIDs mean the server validates a
    session that never saw the CSRF token — every action then returns
    'Security error' / 'CSRF token validation failed'."""
    by_name: Dict[str, list] = {}
    for c in list(sess.cookies):
        by_name.setdefault(c.name, []).append(c)
    for name, group in by_name.items():
        if len(group) < 2:
            continue
        # keep the most specific domain, drop the rest
        group.sort(key=lambda c: len(c.domain or ""), reverse=True)
        for c in group[1:]:
            try:
                sess.cookies.clear(c.domain, c.path, c.name)
            except Exception:  # pragma: no cover - defensive
                pass
        log.debug("collapsed %d cookies named %s", len(group), name)


def login() -> requests.Session:
    if not USER or not PASSWORD:
        raise SystemExit("TICARISK_USER / TICARISK_PASS are not set")

    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    sess.get(f"{BASE}/login.php", timeout=REQUEST_TIMEOUT, verify=False)
    sess.post(
        f"{BASE}/login.php",
        data={"login": USER, "password": PASSWORD, "remember": "on"},
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
        verify=False,
    )
    _dedupe_cookies(sess)

    home = sess.get(f"{BASE}/index.php", timeout=REQUEST_TIMEOUT, verify=False)
    if "login.php" in home.url or USER not in home.text:
        raise SystemExit("login failed — check TICARISK_USER / TICARISK_PASS")
    log.info("logged in as %s", USER)
    return sess


def get(sess: requests.Session, path: str, **kw) -> str:
    kw.setdefault("timeout", REQUEST_TIMEOUT)
    kw.setdefault("verify", False)
    r = sess.get(BASE + path, **kw)
    r.raise_for_status()
    return r.text


def post(
    sess: requests.Session,
    path: str,
    data,
    hdr=None,
    mutating: bool = True,
    pause: bool = True,
    **kw,
):
    kw.setdefault("timeout", REQUEST_TIMEOUT)
    kw.setdefault("verify", False)
    if mutating and DRY_RUN:
        log.info("dry-run: skip POST %s %s", path, data)
        return {"success": True, "message": "dry-run — skipped"}
    headers = dict(XHR)
    headers.update(hdr or {})
    if pause:
        time.sleep(PAUSE)
    r = sess.post(BASE + path, data=data, headers=headers, **kw)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return {"success": False, "message": r.text[:200]}


# Server messages that mean "nothing to do right now", not a malfunction.
BENIGN_RE = re.compile(
    r"(?:"
    r"you (?:have|do not own|don't have|don&#039;t have)"
    r"|no businesses"
    r"|nothing (?:ready|to)"
    r"|are not ready"
    r"|not waiting for watering"
    r"|no suitable field found"
    r"|no empty field"
    r"|no income or goods could be collected"
    r"|honey is not ready"
    r"|no operations available"
    r"|all animals are already fed"
    r"|no harvestable trees"
    r"|no ready products"
    r"|already fed"
    r"|no animals waiting for feed"
    r"|no animals awaiting feed"
    r"|hourly round limit"
    r"|limit reached"
    r"|try again next hour"
    r"|no income accumulated yet"
    r"|wait at least 1 hour"
    r")",
    re.I,
)

# Per-item "Errors:" suffixes that are still just an empty state. Any other
# item is a real problem and must stay visible.
OK_ITEM_RE = re.compile(
    r"no harvestable trees|not ready|no ready products|already fed|not waiting|nothing to",
    re.I,
)


def ok(res) -> bool:
    if not isinstance(res, dict):
        return False
    if res.get("success"):
        return True
    text = str(res.get("message") or "")
    if "Errors:" in text:
        items = [i.strip() for i in re.split(r",\s*", text.split("Errors:", 1)[1]) if i.strip()]
        # every reported item benign -> the call was fine, the game just had nothing to give
        return bool(items) and all(OK_ITEM_RE.search(i) for i in items)
    return bool(BENIGN_RE.search(text))


# Game-state refusals: the request was understood and correctly authenticated,
# the account simply cannot perform it right now. Reported, but not a failure.
SOFT_RE = re.compile(
    r"not enough materials|need: |already (?:producing|running)|nothing to collect",
    re.I,
)


def msg(res) -> str:
    return str((res or {}).get("message") or (res or {}).get("error") or res or "")


def balance(sess: requests.Session) -> Optional[float]:
    try:
        html = get(sess, "/index.php")
    except Exception:
        return None
    m = re.search(r"nav-stat-balance'[^>]*data-value='(-?[\d.]+)'", html)
    return float(m.group(1)) if m else None


# --------------------------------------------------------------------------- helpers


def field_states(html: str) -> Dict[str, str]:
    """tarla-id -> data-durum (bos | ekilmis | hasat_hazir | buyuyor)."""
    out: Dict[str, str] = {}
    for tag in re.findall(r"<div class=\"tarla-item\"[^>]*>", html):
        fid = re.search(r'data-tarla-id="(\d+)"', tag)
        dur = re.search(r'data-durum="([a-z_]+)"', tag)
        if fid:
            out[fid.group(1)] = dur.group(1) if dur else "unknown"
    if out:
        return out
    # attribute-order tolerant fallback
    for m in re.finditer(r'data-tarla-id="(\d+)"', html):
        window = html[m.start(): m.start() + 300]
        dur = re.search(r'data-durum="([a-z_]+)"', window)
        if dur and m.group(1) not in out:
            out[m.group(1)] = dur.group(1)
    return out


def owned_ids(html: str, attr: str) -> List[str]:
    return sorted(set(re.findall(r'data-%s-id="(\d+)"' % attr, html)), key=int)


def crop_options(html: str) -> Dict[int, str]:
    """Crop id -> crop name, in the order the page lists them.

    Prefers the bulk-plant select (#topluEkimUrun): one line per option and
    data-seviye / data-sure attributes. Falls back to the single-field
    #urun_id select, which is the same list laid out over several lines.
    """
    for sel_id in ("topluEkimUrun", "urun_id"):
        box = re.search(r'id="%s".*?</select>' % sel_id, html, re.S)
        if not box:
            continue
        out: Dict[int, str] = {}
        for i, raw in re.findall(r'<option value="(\d+)"[^>]*>\s*([^<]+)', box.group(0)):
            name = re.split(r"\s*\(", raw, 1)[0].strip()
            if name:
                out[int(i)] = name
        if out:
            return out
    return {}


def planted_crops(html: str) -> List[str]:
    """Crop names currently in the ground, from each field's title line."""
    out: List[str] = []
    for title in re.findall(r'<h5 class="tarla-baslik">\s*([^<]+?)\s*</h5>', html):
        name = re.sub(r"\s+Planted\s*$", "", title.strip(), flags=re.I).strip()
        if name and name.lower() not in ("empty", "empty field", "bo\u015f"):
            out.append(name)
    return out


def _read_rotation(path: str) -> Optional[int]:
    try:
        with open(path, encoding="utf-8") as fh:
            return int(json.load(fh).get("next_index"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _write_rotation(path: str, idx: int) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"next_index": idx}, fh)
    except OSError as exc:  # read-only workspace (CI) — fall back to state seeding
        log.debug("rotation cursor not saved: %s", exc)


def pick_crop(opts: Dict[int, str], html: str) -> Tuple[int, str, str]:
    """(crop id, name, why) for the next bulk planting."""
    ids = list(opts)
    if not ids:
        return CROP_ID, str(CROP_ID), "no crop list parsed"

    if CROP_ROTATION_MODE == "daily" and CROP_DAILY_LIST:
        now_trt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
        today = now_trt.date()
        idx = today.toordinal() % len(CROP_DAILY_LIST)
        target_id = CROP_DAILY_LIST[idx]
        if target_id in opts:
            name = opts[target_id]
            return target_id, name, f"daily calendar rotation (Day #{idx + 1}/{len(CROP_DAILY_LIST)}: {name} on {today} TRT)"

    if not CROP_ROTATION:
        i = ids.index(CROP_ID) if CROP_ID in ids else 0
        return ids[i], opts[ids[i]], "rotation off"

    stored = _read_rotation(ROTATION_FILE)
    if stored is not None:
        i = stored % len(ids)
        src = "stored cursor"
    else:
        names = list(opts.values())
        grown = planted_crops(html)
        if grown:
            common = Counter(grown).most_common(1)[0][0]
            i = (names.index(common) + 1) % len(names) if common in names else 0
            src = "seeded after %s" % common
        else:
            i = 0
            src = "seeded (nothing growing)"
    if not DRY_RUN:  # a dry run must not consume the cursor
        _write_rotation(ROTATION_FILE, (i + 1) % len(ids))
    return ids[i], opts[ids[i]], src


# A crop the field's level will not accept. Anything else is a different problem.
LEVEL_BLOCKED_RE = re.compile(
    r"level|seviye|unlock|locked|not (?:yet )?available|Sv\.\d", re.I
)


def animal_tasks(html: str, id_attr: str) -> List[Tuple[str, str, str]]:
    """(building_id, hayvan_turu, urun_tipi) triples found on action buttons."""
    seen: List[Tuple[str, str, str]] = []
    for m in re.finditer(r"<[^>]+data-hayvan-turu=\"([^\"]+)\"[^>]*>", html):
        tag = m.group(0)
        hid = re.search(r'data-%s-id="(\d+)"' % id_attr, tag)
        if not hid:
            continue
        ut = re.search(r'data-urun-tipi="([^"]*)"', tag)
        key = (hid.group(1), m.group(1), ut.group(1) if ut else "")
        if key not in seen:
            seen.append(key)
    return seen


def lazy(sess: requests.Session, panel: str) -> dict:
    r = sess.get(
        f"{BASE}/businesses.php",
        params={"ajax_lazy": panel},
        headers=JSON_HDR,
        timeout=REQUEST_TIMEOUT,
        verify=False,
    )
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return {"success": False, "message": r.text[:200]}


def dump(path: str, content) -> None:
    """Write a page dump (str) or a binary payload (bytes) into inspect/."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if isinstance(content, (bytes, bytearray)):
            with open(path, "wb") as fh:
                fh.write(bytes(content))
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
    except OSError as exc:  # pragma: no cover
        log.warning("could not write %s: %s", path, exc)


# --------------------------------------------------------------------------- production


def section_production(sess, cyc: Cycle, inspect: bool) -> None:
    # Cards are server-rendered plain forms: csrf_token + business_id + a submit
    # button named collect_all or start_production. No product/slot arguments.
    page = get(sess, "/businesses.php")
    if inspect:
        dump("inspect/businesses.html", page)

    # 1 — collect everything (one bulk call, no captcha involved)
    res = post(
        sess,
        "/businesses.php",
        {"action": "toplu_tum_isletme_topla", "csrf_token": csrf_of(page)},
        hdr={"Content-Type": "application/x-www-form-urlencoded"},
    )
    cyc.add("production", "collect all", ok(res), msg(res))
    if inspect:
        dump("inspect/production_collect.json", _pretty(res))

    # re-read: collection clears the Collect buttons and leaves idle cards
    page = get(sess, "/businesses.php")
    owned = lazy(sess, "owned_bundle")
    if inspect:
        dump(
            "inspect/production_owned.html",
            (owned.get("factories") or "") + (owned.get("food") or ""),
        )

    sources = [
        ("mines", page),
        ("factories", owned.get("factories") or ""),
        ("food", owned.get("food") or ""),
    ]
    forms = [(label, *f) for label, src in sources for f in action_forms(src)]

    # per-card collect is only worth the requests when the bulk call failed
    if not ok(res):
        for label, bid, btn, tok in [f for f in forms if f[2] == "collect_all"]:
            r2 = post(
                sess,
                "/businesses.php",
                {"csrf_token": tok, "business_id": bid, btn: "1"},
            )
            cyc.add(
                "production",
                f"collect {bid} ({label})",
                ok(r2),
                msg(r2),
                soft=not ok(r2) and SOFT_RE.search(str(msg(r2))) is not None,
            )

    starts = [f for f in forms if f[2] == "start_production"]
    if not starts:
        cyc.add("production", "restart", True, "no idle production slot found")
    else:
        for label, bid, _btn, tok in starts:
            r2 = post(
                sess,
                "/businesses.php",
                {"csrf_token": tok, "business_id": bid, "start_production": "1"},
            )
            good = ok(r2)
            cyc.add(
                "production",
                f"start {bid} ({label})",
                good,
                msg(r2),
                soft=(not good) and SOFT_RE.search(str(msg(r2))) is not None,
            )

    _top_up_water(sess, cyc)
    _restock(sess, cyc)


# --------------------------------------------------------------------------- farm


def section_fields(sess, cyc: Cycle, inspect: bool) -> None:
    # let the server flip any timers that just elapsed
    res = post(
        sess, "/tarlalar.php", {"ajax_request": 1, "check_tarla_status": 1}, mutating=False
    )
    log.debug("check_tarla_status: %s", msg(res))

    html = get(sess, "/tarlalar.php")
    if inspect:
        dump("inspect/tarlalar.html", html)
    states = field_states(html)
    if not states:
        cyc.add("fields", "scan", True, "no fields owned")
        return

    counts: Dict[str, int] = {}
    for st in states.values():
        counts[st] = counts.get(st, 0) + 1
    log.info("fields: %s", ", ".join(f"{v}x {k}" for k, v in sorted(counts.items())))

    ready = [i for i, st in states.items() if st in ("hasat_hazir", "hazir", "ready")]
    if ready:
        res = post(
            sess,
            "/tarlalar.php",
            {"ajax_request": 1, "toplu_hasat": 1, "tarla_ids[]": ready},
        )
        cyc.add("fields", f"harvest {len(ready)}", ok(res), msg(res))
    else:
        cyc.add("fields", "harvest", True, "nothing ready")

    # re-read: harvested fields come back empty and can be replanted now
    html = get(sess, "/tarlalar.php")
    states = field_states(html)
    empty = [i for i, st in states.items() if st in ("bos", "empty", "")]
    if not empty:
        cyc.add("fields", "replant", True, "no empty field")
        return

    opts = crop_options(html)
    crop, label, why = pick_crop(opts, html)

    def plant(cid: int):
        return post(
            sess,
            "/tarlalar.php",
            {"ajax_request": 1, "toplu_ekim": 1, "urun_id": cid, "tarla_ids[]": empty},
        )

    res = plant(crop)
    if not ok(res) and "water" in msg(res).lower():
        if _maybe_buy_water(sess, cyc):
            res = plant(crop)

    cyc.add("fields", f"plant {label} on {len(empty)} ({why})", ok(res), msg(res))

    # a crop above this field's Sv level is refused — walk the rotation forward
    # until the server takes one, rather than getting stuck on it every hour
    tried = [crop]
    while not ok(res) and opts and "water" not in msg(res).lower() and LEVEL_BLOCKED_RE.search(str(msg(res))):
        nxt = next((c for c in opts if c not in tried), None)
        if nxt is None:
            break
        tried.append(nxt)
        crop, label = nxt, opts[nxt]
        res = plant(nxt)
        cyc.add("fields", f"plant {label} (rotation fallback)", ok(res), msg(res))


def _market_html(sess: requests.Session) -> str:
    return get(sess, "/hammaddeler.php")


def _listing(html: str, mid: str) -> Optional[dict]:
    """Read the buy button for one raw material.

    The button carries everything the POST needs and every cap the server will
    enforce: price, csrf, current stock, warehouse max, daily limit and how
    much of that limit is already used.
    """
    for m in re.finditer(r"<button\b[^>]*>", html):
        tag = m.group(0)
        if f'data-material-id="{mid}"' not in tag:
            continue

        def g(k: str, d: str = "") -> str:
            r = re.search(rf'data-{k}="([^"]*)"', tag)
            return r.group(1) if r else d

        def n(k: str) -> int:
            try:
                return int(g(k, "0") or 0)
            except ValueError:
                return 0

        return {
            "id": mid,
            "name": g("material-name", mid),
            "unit": g("unit", ""),
            "price": n("price"),
            "csrf": g("csrf"),
            "stock": n("current-amount"),
            "max": n("max"),
            "dlimit": n("daily-limit"),
            "dtaken": n("daily-taken"),
        }
    return None


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _ledger() -> dict:
    try:
        with open(RESTOCK_LEDGER, "r") as fh:
            d = json.load(fh)
    except Exception:
        d = {}
    if d.get("day") != _today():
        d = {"day": _today(), "spend": 0.0}
    # "spend" is the legacy total; categories ride alongside it so an old
    # ledger file still loads
    for k in ("spend", "restock", "water"):
        try:
            d[k] = float(d.get(k, 0.0))
        except (TypeError, ValueError):
            d[k] = 0.0
    if d.get("restock", 0.0) == 0.0 and d.get("spend", 0.0):
        d["restock"] = d["spend"]
    return d


def _ledger_add(amount: float, key: str = "restock") -> float:
    d = _ledger()
    d[key] = d.get(key, 0.0) + amount
    try:
        with open(RESTOCK_LEDGER, "w") as fh:
            json.dump(d, fh)
    except Exception as exc:
        log.debug("restock ledger not written: %s", exc)
    return d["spend"]


def _buy_material(
    sess: requests.Session,
    cyc: Cycle,
    step: str,
    mid: str,
    want: int,
    max_spend: float,
    html: Optional[str] = None,
    ledger_key: str = "restock",
) -> bool:
    """Buy `want` units of a raw material.

    Quantity is clamped by, in order: the warehouse free space, the server's
    daily limit, our own USD cap, and the account balance. Any of them hitting
    zero ends the buy with a reason instead of posting a doomed request.
    """
    try:
        L = _listing(html if html is not None else _market_html(sess), mid)
    except Exception as exc:
        cyc.add(step, f"buy {mid}", False, str(exc))
        return False
    if not L or not L["csrf"]:
        cyc.add(step, f"buy {mid}", False, "listing not found on the market page")
        return False
    if L["price"] <= 0:
        cyc.add(step, f"buy {L['name']}", False, "no price on the listing")
        return False

    price, qty = L["price"], int(want)
    room_stock = max(L["max"] - L["stock"], 0) if L["max"] else qty
    room_daily = max(L["dlimit"] - L["dtaken"], 0) if L["dlimit"] else qty
    qty = min(qty, room_stock, room_daily)
    if max_spend > 0:
        qty = min(qty, int(max_spend // price))
    bal = balance(sess)
    if bal is not None:
        qty = min(qty, int(bal // price))

    label = f"{L['name']} ({mid})"
    if qty <= 0:
        why = (
            "warehouse full"
            if room_stock <= 0
            else "daily limit used up"
            if room_daily <= 0
            else "spend cap or balance"
        )
        cyc.add(step, f"buy {label}", True, f"skipped — {why}")
        return False

    res = post(
        sess,
        "/hammaddeler.php",
        {"csrf_token": L["csrf"], "material_id": mid, "quantity": qty, "buy_material": "1"},
    )
    good = ok(res)
    cyc.add(step, f"buy {qty} {L['unit']} {L['name']}", good, msg(res))
    if good:
        _ledger_add(qty * price, ledger_key)
    return good


def _maybe_buy_water(sess, cyc: Cycle) -> bool:
    if BUY_WATER_LITERS <= 0:
        cyc.add("fields", "buy water", True, "skipped (TICARISK_BUY_WATER_LITERS=0)")
        return False
    spent = _ledger().get("water", 0.0)
    if MAX_WATER_DAY > 0 and spent >= MAX_WATER_DAY:
        cyc.add(
            "fields",
            "buy water",
            True,
            f"daily water cap reached — ${spent:,.0f} of ${MAX_WATER_DAY:,.0f}",
        )
        return False
    return _buy_material(
        sess, cyc, "fields", "su", BUY_WATER_LITERS,
        min(MAX_WATER_SPEND, MAX_WATER_DAY - spent) if MAX_WATER_DAY > 0 else MAX_WATER_SPEND,
        ledger_key="water",
    )


def _top_up_water(sess, cyc: Cycle) -> None:
    """Keep the water tank above WATER_TRIGGER.

    Field planting used to be the only thing that ever bought water, which
    meant the factories could starve while the fields were still growing.
    """
    if WATER_TRIGGER <= 0:
        cyc.add("production", "water", True, "disabled (TICARISK_WATER_TRIGGER=0)")
        return
    spent = _ledger().get("water", 0.0)
    if MAX_WATER_DAY > 0 and spent >= MAX_WATER_DAY:
        cyc.add("production", "water", True,
                f"daily water cap reached — ${spent:,.0f} of ${MAX_WATER_DAY:,.0f}")
        return
    try:
        L = _listing(_market_html(sess), "su")
    except Exception as exc:
        cyc.add("production", "water", False, str(exc))
        return
    if not L:
        cyc.add("production", "water", False, "water listing not found")
        return
    if L["stock"] >= WATER_TRIGGER:
        cyc.add("production", "water", True, f"{L['stock']} L in stock (trigger {WATER_TRIGGER})")
        return
    budget = MAX_WATER_SPEND
    if MAX_WATER_DAY > 0:
        budget = min(budget, MAX_WATER_DAY - spent) if MAX_WATER_SPEND > 0 else MAX_WATER_DAY - spent
    _buy_material(sess, cyc, "production", "su", WATER_TRIGGER, budget, ledger_key="water")


def _restock(sess, cyc: Cycle) -> None:
    """Top up the materials that idle a business when they run out.

    Only buys when stock is actually under its trigger, so a healthy hour
    produces no market traffic at all. The whole day is additionally capped by
    TICARISK_MAX_RESTOCK_SPEND via the ledger file.
    """
    if not RESTOCK:
        cyc.add("production", "restock", True, "disabled (TICARISK_RESTOCK is empty)")
        return

    spent = _ledger().get("restock", 0.0)
    if MAX_RESTOCK_SPEND > 0 and spent >= MAX_RESTOCK_SPEND:
        cyc.add(
            "production",
            "restock",
            True,
            f"daily restock cap reached — ${spent:,.0f} of ${MAX_RESTOCK_SPEND:,.0f}",
        )
        return

    try:
        html = _market_html(sess)
    except Exception as exc:
        cyc.add("production", "restock", False, str(exc))
        return

    healthy, bought, short = [], 0, []
    for mid, trigger, cap in RESTOCK:
        budget = MAX_RESTOCK_SPEND - spent if MAX_RESTOCK_SPEND > 0 else cap
        if budget <= 0:
            short.append("daily cap reached")
            break
        L = _listing(html, mid)
        if not L:
            short.append(f"{mid}: listing not found")
            continue
        if L["stock"] >= trigger:
            healthy.append(f"{L['name']} {L['stock']}")
            continue
        if _buy_material(sess, cyc, "restock", mid, trigger, min(cap, budget),
                         html=html, ledger_key="restock"):
            bought += 1
            spent = _ledger().get("restock", 0.0)
        else:
            short.append(f"{L['name']} {L['stock']}<{trigger}")

    if bought == 0:
        detail = "healthy: " + (", ".join(healthy) or "—")
        if short:
            detail += " | not bought: " + ", ".join(short)
        cyc.add("production", "restock", True, detail)


def section_orchards(sess, cyc: Cycle, inspect: bool) -> None:
    try:
        post(
            sess, "/bahceler.php", {"ajax_request": 1, "check_plants_status": 1}, mutating=False
        )
    except Exception as exc:
        log.debug("check_plants_status: %s", exc)

    html = get(sess, "/bahceler.php")
    if inspect:
        dump("inspect/bahceler.html", html)
    ids = owned_ids(html, "bahce")
    if not ids:
        cyc.add("orchards", "scan", True, "no orchards owned")
        return

    res = post(
        sess,
        "/bahceler.php",
        {"ajax_request": 1, "toplu_meyve_topla_coklu": 1, "bahce_ids[]": ids},
    )
    cyc.add("orchards", f"harvest {len(ids)}", ok(res), msg(res))

    res = post(
        sess,
        "/bahceler.php",
        {"ajax_request": 1, "toplu_bahce_sula": 1, "bahce_ids[]": ids},
    )
    cyc.add("orchards", f"water {len(ids)}", ok(res), msg(res))


def _animals(sess, cyc: Cycle, inspect: bool, section: str, path: str, attr: str) -> None:
    url = f"/{path}.php"
    html = get(sess, url)
    if inspect:
        dump(f"inspect/{path}.html", html)
    ids = owned_ids(html, attr)
    if not ids:
        cyc.add(section, "scan", True, f"no {section} owned")
        return

    tasks = animal_tasks(html, attr)
    if tasks:
        for hid, tur, urun in tasks:
            res = post(
                sess,
                url,
                {
                    "ajax_request": 1,
                    "toplu_urun_topla": 1,
                    f"{attr}_id": hid,
                    "hayvan_turu": tur,
                    # the barn endpoint always sends urun_tipi (possibly empty), like the site
                    **({"urun_tipi": urun} if (urun or attr == "ahir") else {}),
                },
            )
            cyc.add(section, f"collect {hid}/{tur}", ok(res), msg(res))
            if FEED_ANIMALS:
                res = post(
                    sess,
                    url,
                    {"ajax_request": 1, "toplu_besle": 1, f"{attr}_id": hid, "hayvan_turu": tur},
                )
                cyc.add(section, f"feed {hid}/{tur}", ok(res), msg(res))
        return

    if not FEED_ANIMALS:
        cyc.add(
            section,
            "collect",
            True,
            "no per-animal buttons found and feeding disabled — nothing safe to call",
        )
        return

    # single "do everything" call (feed + collect, never sells)
    for hid in ids:
        res = post(sess, url, {"ajax_request": 1, "tum_islemler": 1, f"{attr}_id": hid})
        cyc.add(section, f"feed+collect {hid}", ok(res), msg(res))


def bee_areas(html: str) -> Dict[int, Dict[str, int]]:
    """aricilik_id -> {hives, petek, max_petek}.

    The ids exist only inside the buy-button onclick handlers
    (kovanAlModal(3179, 4, 0) / petekAlModal(3179, 4, 40)). There is no
    data-aricilik-id attribute anywhere on the page, which is why a scan for
    it reported "no beekeeping area owned" on an account that has one.
    """
    out: Dict[int, Dict[str, int]] = {}
    for i, tl, _diamond in re.findall(
        r"kovanAlModal\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", html
    ):
        d = out.setdefault(int(i), {"hives": 0, "petek": 0, "max_petek": 0})
        d["hives"] += int(tl)  # cash hives; diamond hives are a separate 100-limit pool
    for i, hives, mevcut in re.findall(
        r"petekAlModal\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", html
    ):
        d = out.setdefault(int(i), {"hives": 0, "petek": 0, "max_petek": 0})
        d["hives"] = max(d["hives"], int(hives))
        d["petek"] = int(mevcut)
    for d in out.values():
        d["max_petek"] = d["hives"] * 10  # site rule: maxPetek = kovanSayisi * 10
    return out


def section_bees(sess, cyc: Cycle, inspect: bool) -> None:
    html = get(sess, "/aricilik.php")
    if inspect:
        dump("inspect/aricilik.html", html)
    areas = bee_areas(html)
    if not areas:
        cyc.add("bees", "scan", True, "no beekeeping area owned")
        return

    for aid in sorted(areas):
        res = post(sess, "/aricilik.php", {"ajax_request": 1, "bal_hasat": 1, "aricilik_id": aid})
        cyc.add("bees", f"harvest {aid}", ok(res), msg(res))

    if not PETEK_REFILL:
        cyc.add("bees", "refill comb", True, "skipped (TICARISK_PETEK_REFILL=0)")
        return

    # Harvesting burns the honeycomb, so the count on screen only drops once a
    # harvest actually went through. Re-read the page and top back up to the
    # hive limit — if the harvest did not consume anything, this is a no-op.
    html = get(sess, "/aricilik.php")
    if inspect:
        dump("inspect/aricilik_after.html", html)
    for aid, st in sorted(bee_areas(html).items()):
        want = max(0, st["max_petek"] - st["petek"])
        if want <= 0:
            cyc.add("bees", f"comb {aid}", True, f"full ({st['petek']}/{st['max_petek']})")
            continue
        cost = want * PETEK_PRICE
        if cost > PETEK_MAX_SPEND:
            cyc.add(
                "bees",
                f"buy {want} comb",
                False,
                f"${cost:,} exceeds TICARISK_PETEK_MAX_SPEND=${PETEK_MAX_SPEND:,.0f}",
                soft=True,
            )
            continue
        bal = balance(sess)
        if bal is not None and cost > bal:
            cyc.add(
                "bees",
                f"buy {want} comb",
                False,
                f"cost ${cost:,} is above the ${bal:,.2f} balance",
                soft=True,
            )
            continue
        res = post(
            sess,
            "/aricilik.php",
            {"ajax_request": 1, "petek_ekle": 1, "aricilik_id": aid, "petek_adet": want},
        )
        cyc.add("bees", f"buy {want} comb (${cost:,})", ok(res), msg(res))




# --------------------------------------------------------------------------- misc


def _pretty(obj) -> str:
    import json

    try:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except TypeError:
        return str(obj)


def csrf_of(html: str) -> str:
    m = re.search(r'data-csrf="([0-9a-f]{64})"', html)
    if not m:
        raise SystemExit("could not find the businesses CSRF token")
    return m.group(1)


def action_forms(src: str) -> List[Tuple[str, str, str]]:
    """(business_id, button_name, csrf) for every server-rendered action form.

    Each card embeds its own csrf_token, and tokens rotate per render, so the
    token carried out of the very form we are about to submit is the safe one.
    """
    out: List[Tuple[str, str, str]] = []
    for f in re.findall(r"<form[^>]*>.*?</form>", src, re.S):
        bid = re.search(r'name="business_id"\s+value="(\d+)"', f)
        btn = re.search(r'<button[^>]*name="([a-z_]+)"', f)
        tok = re.search(r'name="csrf_token"\s+value="([0-9a-f]{64})"', f)
        if bid and btn and tok:
            out.append((bid.group(1), btn.group(1), tok.group(1)))
    return out


# --------------------------------------------------------------------------- captcha
#
# The game gates matematik.php behind a slider puzzle. It is the same
# generate_puzzle / verify_human pair the browser bundle uses, and the
# `user_position` it wants is a PERCENT of the background width, not pixels.


def _png_load(data: bytes):
    """Pure-Python PNG decode -> (float HxWx3 0..255, w, h).

    Second line of defence only. The captcha currently ships JPEG for both the
    background and the piece, so ImageMagick is required in practice — this
    exists for the day the puzzle is served as PNG, and for any other PNG a
    caller wants decoded without shelling out.
    """
    if np is None:
        raise RuntimeError("numpy is required for captcha solving")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("not a PNG")
    pos, idat, trns = 8, [], None
    w = h = depth = ctype = interlace = 0
    plte = b""
    while pos + 8 <= len(data):
        ln = int.from_bytes(data[pos : pos + 4], "big")
        tag = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + ln]
        pos += 12 + ln
        if tag == b"IHDR":
            w, h, depth, ctype, _c, _f, interlace = (
                int.from_bytes(body[0:4], "big"),
                int.from_bytes(body[4:8], "big"),
                body[8], body[9], body[10], body[11], body[12],
            )
        elif tag == b"PLTE":
            plte = bytes(body)
        elif tag == b"tRNS":
            trns = bytes(body)
        elif tag == b"IDAT":
            idat.append(bytes(body))
        elif tag == b"IEND":
            break
    if depth != 8 or interlace != 0 or not idat:
        raise RuntimeError(f"unsupported PNG (depth={depth}, interlace={interlace})")
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if ch is None:
        raise RuntimeError(f"unsupported PNG colour type {ctype}")

    import zlib

    raw = zlib.decompress(b"".join(idat))
    stride, bpp = w * ch, ch
    if len(raw) < h * (stride + 1):
        raise RuntimeError("short PNG data")

    out = bytearray()
    prev = bytearray(stride)
    i = 0
    for _ in range(h):
        f = raw[i]
        i += 1
        line = bytearray(raw[i : i + stride])
        i += stride
        if f == 1:
            for j in range(stride):
                line[j] = (line[j] + (line[j - bpp] if j >= bpp else 0)) & 0xFF
        elif f == 2:
            for j in range(stride):
                line[j] = (line[j] + prev[j]) & 0xFF
        elif f == 3:
            for j in range(stride):
                left = line[j - bpp] if j >= bpp else 0
                line[j] = (line[j] + ((left + prev[j]) >> 1)) & 0xFF
        elif f == 4:
            for j in range(stride):
                a = line[j - bpp] if j >= bpp else 0
                b = prev[j]
                c = prev[j - bpp] if j >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[j] = (
                    line[j] + (a if (pa <= pb and pa <= pc) else b if pb <= pc else c)
                ) & 0xFF
        elif f != 0:
            raise RuntimeError(f"bad PNG filter type {f}")
        out += line
        prev = line

    px = np.frombuffer(bytes(out), dtype=np.uint8).reshape(h, w, ch)
    if ctype == 3:
        if not plte:
            raise RuntimeError("palette PNG without PLTE")
        idx = px[:, :, 0]
        rgb = np.frombuffer(plte, dtype=np.uint8).reshape(-1, 3)[idx]
    elif ctype == 0:
        rgb = np.repeat(px, 3, axis=2)
    elif ctype == 4:
        g = px[:, :, :1]
        rgb = np.repeat(g, 3, axis=2)
    else:
        rgb = px[:, :, :3]
    return rgb.astype(float), w, h


def _im_load(data: bytes):
    """Decode an image to a float HxWx3 array.

    ImageMagick when it is installed, pure-Python PNG otherwise — the puzzle
    only ever serves PNGs, so a runner missing IM still solves captchas.
    """
    if np is None:
        raise RuntimeError("numpy is required for captcha solving")
    if not (shutil.which("identify") and shutil.which("convert")):
        try:
            return _png_load(data)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "ImageMagick is not installed, and this image is not a decodable "
                f"PNG ({exc}). The captcha ships JPEG for both the background and "
                "the piece — imagemagick is required."
            ) from exc
    fd, path = tempfile.mkstemp(suffix=".img")
    rgb = path + ".rgb"
    try:
        os.write(fd, data)
        os.close(fd)
        fd = -1
        dim = subprocess.run(
            ["identify", "-format", "%w %h", path], capture_output=True, text=True, timeout=60
        )
        parts = dim.stdout.split()
        if len(parts) < 2:
            raise RuntimeError(f"identify failed: {dim.stderr[:200]}")
        w, h = int(parts[0]), int(parts[1])
        subprocess.run(
            ["convert", path, "-depth", "8", f"rgb:{rgb}"],
            check=True, capture_output=True, timeout=60,
        )
        arr = np.fromfile(rgb, dtype=np.uint8)[: w * h * 3]
        if arr.size < w * h * 3:
            raise RuntimeError("short pixel read from convert")
        return arr.reshape(h, w, 3).astype(float), w, h
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        for junk in (path, rgb):
            if os.path.exists(junk):
                try:
                    os.remove(junk)
                except OSError:
                    pass


def _cv_profile(img):
    return np.abs(img - 0.5 * (np.roll(img, 3, 1) + np.roll(img, -3, 1))).mean(2)


def _ch_profile(img):
    return np.abs(img - 0.5 * (np.roll(img, 3, 0) + np.roll(img, -3, 0))).mean(2)


def _winmean(profile, lo, width):
    cs = np.concatenate([[0.0], np.cumsum(profile)])
    return (cs[np.minimum(lo + width, len(profile))] - cs[lo]) / width


def score_positions(img, pw, ph, py, margin=4, topk=8):
    """Rank candidate x positions by how well a box outline fits the hole."""
    h, w, _ = img.shape
    CV, CH = _cv_profile(img), _ch_profile(img)

    y0, y1 = max(0, py), min(h, py + ph)
    col = CV[y0:y1].mean(0)
    topP = CH[max(0, py - 1):min(h, py + 2)].mean(0)
    botP = CH[max(0, py + ph - 1):min(h, py + ph + 2)].mean(0)

    lo = np.arange(margin, max(margin + 1, w - margin - pw))
    win = max(4, pw - 2 * margin)
    score = col[lo] + col[lo + pw] + _winmean(topP, lo, win) + _winmean(botP, lo, win)

    yT, yB = np.clip(py, 0, h - 3), np.clip(py + ph - 3, 0, h - 3)
    order = np.argsort(-score)[: max(topk * 12, 72)]
    bonus = np.zeros_like(score)
    for k in order:
        L = int(lo[k])
        if L - 1 < 0 or L + pw + 2 > w:
            continue
        cl = img[y0:y1, L - 1:L + 2].reshape(-1, 3).mean(0)
        cr = img[y0:y1, L + pw - 1:L + pw + 2].reshape(-1, 3).mean(0)
        ct = img[yT:yT + 3, L + margin:L + pw - margin].reshape(-1, 3).mean(0)
        cb = img[yB:yB + 3, L + margin:L + pw - margin].reshape(-1, 3).mean(0)
        spread = float(np.array([cl, cr, ct, cb]).std(0).mean())
        bonus[k] = max(0.0, 70.0 - spread)

    total = score + 1.6 * bonus
    idx = np.argsort(-total)[:topk]
    return [(int(lo[i]), float(score[i]), float(total[i])) for i in idx]


def pct_of(L: float, w: int, pw: int) -> float:
    return max(0.0, min(100.0, ((L + pw / 2) - pw / 2) / w * 100.0))


def _runs(mask, minlen):
    out, start = [], None
    for i, v in enumerate(list(mask) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= minlen:
                out.append((start, i - 1, i - start))
            start = None
    return out


def edge_candidates(img, pw, ph, py, topk=6):
    """Run-pairing rectangle finder — the userscript's detectCandidates."""
    h, w = img.shape[:2]
    Eh = np.abs(img[3:] - img[:-3]).mean(2)
    Ev = np.abs(img[:, 3:] - img[:, :-3]).mean(2)
    band_lo = max(0, py - 25)
    band_hi = min(min(h - 3, Ev.shape[0]), py + ph + 25)
    min_v = max(30, int(ph * 0.55))
    min_h = int(pw * 0.55)
    span_ok = lambda s: pw - 8 <= s <= pw + 12

    out, seen = [], set()
    for T in (45, 35, 26, 20):
        vedges, hedges = [], []
        for x in range(Ev.shape[1]):
            for y0, y1, L in _runs(Ev[band_lo:band_hi, x] > T, min_v):
                vedges.append((x, y0 + band_lo, y1 + band_lo, L))
        for y in range(band_lo, min(band_hi, Eh.shape[0])):
            for x0, x1, L in _runs(Eh[y] > T, min_h):
                hedges.append((y, x0, x1, L))

        for i in range(len(hedges)):
            for j in range(i + 1, len(hedges)):
                a, b = hedges[i], hedges[j]
                lo, hi = min(a[1], b[1]), max(a[2], b[2])
                if span_ok(hi - lo) and abs((a[1] + a[2]) - (b[1] + b[2])) <= max(8, pw * 0.3):
                    if (lo, hi) not in seen:
                        seen.add((lo, hi))
                        out.append(((lo, hi), T))
        for i in range(len(vedges)):
            for j in range(i + 1, len(vedges)):
                a, b = vedges[i], vedges[j]
                if b[0] > a[0] and span_ok(b[0] - a[0]):
                    if (a[0], b[0]) not in seen:
                        seen.add((a[0], b[0]))
                        out.append(((a[0], b[0]), T))

    out.sort(key=lambda c: (abs((c[0][1] - c[0][0]) - pw), c[1]))
    pcts = []
    for (lo, hi), _T in out[:topk]:
        box_c = (lo + hi) / 2.0
        piece_left = box_c - pw / 2.0
        max_pct = ((w - pw) / w) * 100.0
        pcts.append(max(0.0, min(piece_left / w * 100.0, max_pct)))
    return pcts


def ncc_candidates(bg, piece, pw, ph, py, topk=5, gap=6):
    """Match the cut piece against every slot position (normalised cross
    correlation). This is the userscript's nccDetect and the strongest signal."""
    if piece is None:
        return []
    h, w = bg.shape[:2]
    Y, X = piece.shape[:2]
    W, H = min(pw, X), min(ph, Y)
    y0 = int(round(py))
    if W < 8 or H < 8 or y0 < 0 or y0 + H > h:
        return []

    t = piece[:H, :W]
    tm = float(t.mean())
    tvar = float(((t - tm) ** 2).mean())
    if tvar <= 1e-6:
        return []

    scores = np.empty(w - W + 1, dtype=float)
    for x in range(w - W + 1):
        win = bg[y0:y0 + H, x:x + W]
        sm = float(win.mean())
        svar = float(((win - sm) ** 2).mean())
        den = (tvar * svar) ** 0.5
        scores[x] = float(((t - tm) * (win - sm)).mean()) / den if den > 1e-6 else -1.0

    max_pct = ((w - pw) / w) * 100.0
    order = np.argsort(-scores)
    out, picked = [], []
    for idx in order:
        x = int(idx)
        if any(abs(x - q) < gap for q in picked):
            continue
        picked.append(x)
        pct = max(0.0, min(x / w * 100.0, max_pct))
        conf = float(scores[x])
        out.append((pct, conf))
        if len(out) >= topk:
            break
    return out


def build_candidates(score_pcts, ncc_hits, edge_pcts):
    """Merge detectors the way the userscript does: NCC leads only when its peak
    is clearly dominant, otherwise the 4-side scorer goes first."""
    out, seen = [], set()

    def add(pct):
        if pct is None or pct != pct:
            return
        k = round(float(pct), 2)
        if k in seen:
            return
        seen.add(k)
        out.append(k)

    n0 = ncc_hits[0] if ncc_hits else None
    n1 = ncc_hits[1] if len(ncc_hits) > 1 else None
    # n0[1] is the NCC correlation confidence (-1.0 to 1.0)
    dominant = n0 is not None and n0[1] >= 0.55 and (n1 is None or (n0[1] - n1[1]) >= 0.12)
    if dominant:
        add(n0[0])
        add(score_pcts[0] if score_pcts else None)
    else:
        add(score_pcts[0] if score_pcts else None)
        add(n0[0] if n0 else None)
    for p in score_pcts[1:]:
        add(p)
    for p, _conf in ncc_hits[1:]:
        add(p)
    for p in edge_pcts:
        add(p)
    return out


def detect_slot(bg, piece, pw, ph, py):
    """Ordered percent guesses for the slider, best first."""
    score_pcts = [pct_of(L, bg.shape[1], pw) for L, _s, _t in score_positions(bg, pw, ph, py)]
    ncc_hits = ncc_candidates(bg, piece, pw, ph, py)
    edge_pcts = edge_candidates(bg, pw, ph, py)
    return build_candidates(score_pcts, ncc_hits, edge_pcts)


def _unlock_seconds(v: dict) -> int:
    for key in ("remaining_time", "remaining", "retry_after", "wait"):
        try:
            return int(float(v.get(key))) + 5
        except (TypeError, ValueError):
            pass
    return 605


def solve_puzzle(sess, path: str, cyc: Cycle) -> Tuple[bool, int]:
    """Clear one gate. Returns (solved, guesses_spent).

    Budget: CAPTCHA_GUESSES total rejected verify_human calls. Each guess uses
    its own freshly generated puzzle, so a miss never stacks on the same
    challenge — that stacking is what produced the 600 s lock. Give up before
    the budget is gone rather than after.
    """
    if np is None:
        cyc.add("captcha", "solve", False, "numpy not installed", soft=True)
        return False, 0

    spent = 0
    for rnd in range(1, CAPTCHA_ROUNDS + 1):
        if spent >= CAPTCHA_GUESSES:
            cyc.add("captcha", "verify_human", False,
                    f"budget used ({spent}/{CAPTCHA_GUESSES} guesses)", soft=True)
            return False, spent

        g = post(sess, path, {"ajax_request": 1, "action": "generate_puzzle"}, pause=False)
        if not (isinstance(g, dict) and g.get("success")):
            if isinstance(g, dict) and g.get("locked"):
                wait = _unlock_seconds(g)
                cyc.add("captcha", "generate", False, f"locked for {wait - 5}s: {msg(g)}", soft=True)
                time.sleep(wait)
                continue
            cyc.add("captcha", "generate", False, msg(g), soft=True)
            return False, spent

        pd = g.get("puzzle_data") or {}
        token = str(pd.get("challenge_token") or "")
        raw_bg = str(pd.get("background_image") or "")
        if not token or "," not in raw_bg:
            cyc.add("captcha", "generate", False, "puzzle payload incomplete", soft=True)
            return False, spent

        pw = int(pd.get("piece_width") or 50)
        ph = int(pd.get("piece_height") or 50)
        py = int(pd.get("piece_y") or 0)
        try:
            bg, bw, _bh = _im_load(base64.b64decode(raw_bg.split(",", 1)[1]))
            piece = None
            raw_piece = str(pd.get("piece_image") or "")
            if "," in raw_piece:
                piece, _pw2, _ph2 = _im_load(base64.b64decode(raw_piece.split(",", 1)[1]))
            guesses = detect_slot(bg, piece, pw, ph, py)
        except Exception as exc:  # noqa: BLE001
            cyc.add("captcha", "detect", False, str(exc), soft=True)
            return False, spent
        if not guesses:
            cyc.add("captcha", "detect", False, "no slot found", soft=True)
            continue

        pct = guesses[0]
        spent += 1
        v = post(
            sess, path,
            {"ajax_request": 1, "action": "verify_human",
             "user_position": f"{pct:.4f}", "challenge_token": token},
            pause=False,
        )
        log.info("captcha round %d/%d guess %d/%d: pct=%.2f -> %s",
                 rnd, CAPTCHA_ROUNDS, spent, CAPTCHA_GUESSES, pct,
                 json.dumps(v, ensure_ascii=False)[:160])

        if isinstance(v, dict) and v.get("success"):
            cyc.add("captcha", "verify_human", True,
                    f"position {pct:.2f}% ({spent} guess{'' if spent == 1 else 'es'} used)")
            return True, spent
        if isinstance(v, dict) and v.get("locked"):
            wait = _unlock_seconds(v)
            cyc.add("captcha", "verify_human", False, f"locked for {wait - 5}s: {msg(v)}", soft=True)
            time.sleep(wait)
            continue
        # miss — throw this puzzle away, a fresh one is generated next round
        time.sleep(0.3)

    cyc.add("captcha", "verify_human", False,
            f"{CAPTCHA_ROUNDS} puzzles, {spent}/{CAPTCHA_GUESSES} guesses — giving up", soft=True)
    return False, spent


# --------------------------------------------------------------------------- math

def _ocr_png(path: str) -> str:
    """Read a question PNG. tesseract 5 gets the raw file right; a negated and
    thresholded pass is the fallback when the digits do not come through."""
    wl = "0123456789+-*=xX. "

    def run(img: str) -> str:
        try:
            r = subprocess.run(
                ["tesseract", img, "stdout", "-l", "eng", "--psm", "7",
                 "-c", f"tessedit_char_whitelist={wl}"],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"tesseract failed: {exc}") from exc
        return " ".join((r.stdout or "").split())

    text = run(path)
    if len(re.findall(r"\d+", text)) >= 2:
        return text
    fixed = path + ".fix.png"
    try:
        magick = shutil.which("magick") or shutil.which("convert")
        if not magick:
            return text
        subprocess.run(
            [magick, path, "-negate", "-threshold", "60%", "-resize", "300%", fixed],
            capture_output=True, timeout=60, check=True,
        )
        better = run(fixed)
        return better if len(re.findall(r"\d+", better)) >= 2 else text
    except (OSError, subprocess.SubprocessError):
        return text
    finally:
        if os.path.exists(fixed):
            try:
                os.remove(fixed)
            except OSError:
                pass


_Q_FULL = re.compile(r"^\s*(-?\d+)\s*([+\-*/xX])\s*(-?\d+)\s*$")
_Q_COMPACT = re.compile(r"^\s*(-?\d+)([+\-*/xX])(-?\d+)\s*$")


def _parse_reading(text: str) -> Optional[Tuple[int, int]]:
    """Pull (a, b) out of one OCR pass.

    Handles clean forms, compact forms, signed/negative operands (e.g. -1 + 99),
    and noisy tokens (e.g. '+' recognized as '4+' or '1+').
    """
    if not text:
        return None
    flat = " ".join(text.split()).strip(". ,:;")

    def fix_2digit(n: int) -> int:
        sign = -1 if n < 0 else 1
        val = abs(n)
        if val > 99:
            s = str(val)
            if len(s) == 3 and s[0] == '4':
                return sign * int(s[1:])
            if len(s) == 3 and s[-1] == '4':
                return sign * int(s[:-1])
        return n

    m = _Q_FULL.match(flat)
    if m:
        return fix_2digit(int(m.group(1))), fix_2digit(int(m.group(3)))

    m = _Q_COMPACT.match(flat.replace(" ", "").strip(". ,:;"))
    if m:
        return fix_2digit(int(m.group(1))), fix_2digit(int(m.group(3)))

    parts = flat.split()
    if len(parts) >= 2 and parts[0].lstrip("-").isdigit() and parts[-1].lstrip("-").isdigit():
        return fix_2digit(int(parts[0])), fix_2digit(int(parts[-1]))

    m = re.search(r"(-?\d+)\s*([+\-*/xX])\s*(-?\d+)", flat)
    if m:
        return fix_2digit(int(m.group(1))), fix_2digit(int(m.group(3)))

    nums = [int(n) for n in re.findall(r"-?\d+", flat)]
    if len(nums) == 2:
        return fix_2digit(nums[0]), fix_2digit(nums[1])
    if len(nums) == 3 and nums[1] in (1, 4):
        return fix_2digit(nums[0]), fix_2digit(nums[2])
    return None


def _clean_image(path: str) -> Optional[str]:
    """Clean image at native resolution, removing isolated noise stars via connected components."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        arr, _w, _h = _im_load(data)
    except Exception:  # noqa: BLE001
        return None

    vals = arr.mean(2)
    fg = vals > 35
    visited = np.zeros(fg.shape, dtype=bool)
    clean_fg = np.zeros(fg.shape, dtype=bool)
    h, w = fg.shape

    for y in range(h):
        for x in range(w):
            if fg[y, x] and not visited[y, x]:
                q = [(y, x)]
                visited[y, x] = True
                comp = [(y, x)]
                for cy, cx in q:
                    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < h and 0 <= nx < w and fg[ny, nx] and not visited[ny, nx]:
                            visited[ny, nx] = True
                            q.append((ny, nx))
                            comp.append((ny, nx))
                if len(comp) >= 18:
                    for cy, cx in comp:
                        clean_fg[cy, cx] = True

    out = np.where(clean_fg, 0, 255).astype(np.uint8)
    out = np.pad(out, 16, mode="constant", constant_values=255)
    dest = path + ".clean.pgm"
    try:
        with open(dest, "wb") as fh:
            fh.write(f"P5\n{out.shape[1]} {out.shape[0]}\n255\n".encode())
            fh.write(out.tobytes())
    except OSError:
        return None
    return dest


def _read_question(path: str) -> Optional[Tuple[int, int, str]]:
    """OCR a question image using dual passes on both cleaned and raw image.

    Returns (a, b, text).
    """
    wl = "0123456789+-*x "
    votes: Dict[Tuple[int, int], int] = {}
    sample_texts: Dict[Tuple[int, int], str] = {}

    def scan(img_path: str, weight: int = 1) -> None:
        base = ["tesseract", img_path, "stdout", "-l", "eng",
                "-c", f"tessedit_char_whitelist={wl}"]
        for psm in ("7", "6"):
            try:
                r = subprocess.run(base + ["--psm", psm], capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                continue
            t = " ".join((r.stdout or "").split())
            pair = _parse_reading(t)
            if pair is not None:
                votes[pair] = votes.get(pair, 0) + weight
                if pair not in sample_texts:
                    sample_texts[pair] = t

    clean = _clean_image(path)
    if clean:
        scan(clean, weight=2)
        try:
            os.remove(clean)
        except OSError:
            pass

    scan(path, weight=1)

    if not votes:
        fallback_txt = _ocr_png(path)
        pair = _parse_reading(fallback_txt)
        if pair is not None:
            return pair[0], pair[1], fallback_txt
        return None

    (a, b), _n = max(votes.items(), key=lambda kv: kv[1])
    return a, b, sample_texts.get((a, b), f"{a} * {b}")


def _pick_math_op(text: str, tipi: str) -> str:
    """The server's islem_tipi wins; OCR symbols are only a fallback."""
    if tipi in MATH_OPS:
        return MATH_OPS[tipi]
    if "+" in text:
        return "+"
    if "-" in text:
        return "-"
    if "*" in text or "x" in text or "X" in text:
        return "*"
    if "/" in text:
        return "/"
    return "+"


def _apply_op(a: int, b: int, op: str) -> Optional[int]:
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        return a // b if b else None
    return None


def section_math(sess, cyc: Cycle, inspect: bool) -> None:
    """Solve every question the server will hand out, then empty the vault.

    get_question -> PNG -> tesseract -> check_answer, at >= MATH_MIN_MS apart.
    Stops on an unreadable question rather than guessing, on
    verification_required (the slider captcha), or when kalan_islem hits 0.
    """
    if DRY_RUN:
        cyc.add("math", "solve", True, "skipped (dry-run)")
        return
    if not MATH_ON:
        cyc.add("math", "solve", True, "skipped (TICARISK_MATH=0)")
        return
    if not shutil.which("tesseract"):
        cyc.add("math", "solve", True, "skipped (tesseract is not installed)")
        return

    right = wrong = 0
    kalan: Optional[int] = None
    kasa: Optional[str] = None
    reason = ""
    gates = 0
    consecutive_unreadable = 0
    workdir = tempfile.mkdtemp(prefix="ticarisk_math_")
    qpath = os.path.join(workdir, "q.png")
    try:
        for n in range(1, MATH_MAX + 1):
            q = post(
                sess,
                "/matematik.php",
                {"ajax_request": 1, "action": "get_question", "islem_tipi": MATH_OP_TYPE},
                pause=False,
            )
            # The anti-bot timer starts when the server issues the question, so
            # measure from here — not from before get_question — or the answer
            # lands early and trips verification_required.
            issued = time.time()
            if not (isinstance(q, dict) and q.get("success")):
                reason = msg(q) or "no question available"
                break
            rel = str(q.get("question_image_url") or "")
            if not rel:
                reason = "question response had no image"
                break

            img = sess.get(
                urljoin(BASE + "/matematik.php", rel), timeout=REQUEST_TIMEOUT, verify=False
            )
            img.raise_for_status()
            with open(qpath, "wb") as fh:
                fh.write(img.content)
            if inspect and n <= 3:
                dump(f"inspect/math_q{n}.png", img.content)

            read = _read_question(qpath)
            if read is None:
                consecutive_unreadable += 1
                log.warning(
                    "math: unreadable question image (attempt %d/3): %r",
                    consecutive_unreadable, _ocr_png(qpath)
                )
                if consecutive_unreadable >= 3:
                    reason = f"could not read question: {_ocr_png(qpath)!r}"
                    break
                time.sleep(1.0)
                continue
            consecutive_unreadable = 0
            a0, b0, text = read
            nums = [a0, b0]
            tipi = str(q.get("islem_tipi") or "")
            op = _pick_math_op(text, tipi)
            if n == 1:
                log.info("math: server islem_tipi=%r -> op %r (MATH_OPS=%s)",
                         tipi, op, json.dumps(MATH_OPS, ensure_ascii=False))
            ans = _apply_op(a0, b0, op)
            if ans is None:
                reason = f"unhandled operator in {text!r}"
                break

            # OCR and the image download already ate part of the window — top it up
            wait = MATH_MIN_MS / 1000.0 + 0.1 - (time.time() - issued)
            if wait > 0:
                time.sleep(wait)

            a = post(
                sess,
                "/matematik.php",
                {
                    "ajax_request": 1,
                    "action": "check_answer",
                    "cevap": str(ans),
                    "token": str(q.get("token") or ""),
                },
                pause=False,
            )
            if not isinstance(a, dict):
                reason = "check_answer returned non-JSON"
                break
            if a.get("verification_required"):
                gates += 1
                if gates > CAPTCHA_MAX_GATES:
                    reason = f"gate appeared {gates} times — stopping"
                    break
                log.info("math: verification required — solving the slider puzzle")
                solved, _spent = solve_puzzle(sess, "/matematik.php", cyc)
                if not solved:
                    reason = "verification required and the slider was not solved"
                    break
                continue
            if "dogru" not in a:
                # refusal with no verdict — "Hourly round limit reached!", a
                # stale token, anything else. Stop instead of spinning.
                reason = msg(a) or "check_answer returned no verdict"
                break
            if a.get("dogru"):
                right += 1
            else:
                wrong += 1
                # keep everything needed to diagnose the miss: the image the
                # server sent, what OCR made of it, and the verdict itself.
                # The verdict distinguishes a bad read from a bad submission
                # (too fast, stale token, wrong operator).
                dump(f"inspect/math_miss_{wrong}.png", img.content)
                log.warning(
                    "math MISS #%d | islem_tipi=%r ocr=%r nums=%s op=%r "
                    "ans=%s -> %s",
                    wrong,
                    str(q.get("islem_tipi") or ""),
                    text, nums, op, ans,
                    json.dumps(a, ensure_ascii=False)[:400],
                )
            if a.get("kalan_islem") is not None:
                try:
                    kalan = int(a["kalan_islem"])
                except (TypeError, ValueError):
                    pass
            if a.get("matematik_kasa") is not None:
                kasa = str(a["matematik_kasa"])
            if kalan is not None and kalan <= 0:
                break
            if n % 25 == 0:
                log.info("math: %d solved, %s left, vault $%s", right, kalan, kasa)
    except Exception as exc:  # noqa: BLE001 - reported as a step, never fatal
        reason = f"aborted: {exc}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    done = right + wrong
    detail = f"{right}/{done} correct"
    if kalan is not None:
        detail += f", {kalan} questions left"
    if kasa is not None:
        detail += f", vault ${kasa}"
    if reason:
        detail += f" — {reason}"

    if done == 0:
        benign = bool(BENIGN_RE.search(reason or ""))
        cyc.add("math", "solve", benign, detail, soft=not benign)
    else:
        cyc.add("math", f"solve {right}/{done}", True, detail)

    if MATH_WITHDRAW and done:
        w = post(sess, "/matematik.php", {"ajax_request": 1, "action": "hesaba_cek"}, pause=False)
        cyc.add("math", "withdraw vault", ok(w), msg(w), soft=not ok(w))


# --------------------------------------------------------------------------- jobs


def section_jobs(sess, cyc: Cycle, inspect: bool) -> None:
    """Routine Jobs (jobs.php): collects completed job salaries and restarts idle jobs."""
    page = get(sess, "/jobs.php")
    if inspect:
        dump("inspect/jobs.html", page)

    csrf = ""
    m_csrf = (
        re.search(r'data-csrf="([0-9a-f]{64})"', page)
        or re.search(r'name="csrf_token"\s+value="([^"]+)"', page)
        or re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', page)
    )
    if m_csrf:
        csrf = m_csrf.group(1)

    # Extract all action keywords found inside the page scripts
    script_actions = set(re.findall(r'action\s*:\s*[\'"]([^\'"]+)[\'"]', page))

    # 1. Bulk Collect
    collect_actions = [a for a in script_actions if "topla" in a or "collect" in a]
    collect_candidates = collect_actions + [
        "toplu_topla",
        "collect_all",
        "toplu_is_topla",
        "toplu_gelir_topla",
        "toplu_islem_topla",
        "toplu_tum_is_topla",
    ]
    collected = False
    for act in collect_candidates:
        r = post(sess, "/jobs.php", {"action": act, "csrf_token": csrf, "ajax": "true", "ajax_request": 1}, pause=False)
        if isinstance(r, dict) and r.get("success"):
            cyc.add("jobs", "collect all", True, msg(r))
            collected = True
            break
        elif isinstance(r, dict) and any(w in str(msg(r)).lower() for w in ("zaten", "ready", "bekle", "yok", "tamamlan")):
            cyc.add("jobs", "collect all", True, msg(r), soft=True)
            collected = True
            break

    # Discover job IDs
    job_ids = sorted(
        set(
            re.findall(r'data-job(?:-id)?="(\d+)"', page)
            + re.findall(r'name="job_id"\s+value="(\d+)"', page)
            + re.findall(r'data-id="(\d+)"', page)
            + re.findall(r'id="job[_-]?(\d+)"', page)
            + re.findall(r'onclick="[^"]*?(?:topla|collect|baslat|start)[^\d]*(\d+)', page, re.I)
        ),
        key=int,
    )

    if not collected and job_ids:
        for jid in job_ids:
            r_col = post(sess, "/jobs.php", {"action": "topla", "job_id": str(jid), "csrf_token": csrf, "ajax": "true", "ajax_request": 1}, pause=False)
            if isinstance(r_col, dict) and r_col.get("success"):
                cyc.add("jobs", f"collect job {jid}", True, msg(r_col))

    # 2. Bulk Start / Individual Start
    start_actions = [a for a in script_actions if "baslat" in a or "start" in a]
    start_candidates = start_actions + [
        "toplu_baslat",
        "start_all",
        "toplu_is_baslat",
        "toplu_islem_baslat",
        "toplu_tum_is_baslat",
    ]
    started = False
    for act in start_candidates:
        r = post(sess, "/jobs.php", {"action": act, "csrf_token": csrf, "ajax": "true", "ajax_request": 1}, pause=False)
        if isinstance(r, dict) and r.get("success"):
            cyc.add("jobs", "start all", True, msg(r))
            started = True
            break
        elif isinstance(r, dict) and any(w in str(msg(r)).lower() for w in ("enerji", "energy", "yetersiz", "devam")):
            cyc.add("jobs", "start all", True, msg(r), soft=True)
            started = True
            break

    if not started and job_ids:
        for jid in job_ids:
            r_st = post(sess, "/jobs.php", {"action": "baslat", "job_id": str(jid), "csrf_token": csrf, "ajax": "true", "ajax_request": 1}, pause=False)
            if isinstance(r_st, dict) and r_st.get("success"):
                cyc.add("jobs", f"start job {jid}", True, msg(r_st))
            elif isinstance(r_st, dict) and any(w in str(msg(r_st)).lower() for w in ("enerji", "energy")):
                cyc.add("jobs", f"start job {jid}", True, msg(r_st), soft=True)
                break


# --------------------------------------------------------------------------- auto repair (tamir.php)


def section_tamir(sess, cyc: Cycle, inspect: bool) -> None:
    """Auto Repair Workshop (tamir.php): calls customers, assigns repairs, and collects completed revenue."""
    page = get(sess, "/tamir.php")
    if inspect:
        dump("inspect/tamir.html", page)

    csrf = ""
    m_csrf = (
        re.search(r'data-csrf="([0-9a-f]{64})"', page)
        or re.search(r'name="csrf_token"\s+value="([^"]+)"', page)
        or re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', page)
    )
    if m_csrf:
        csrf = m_csrf.group(1)

    script_actions = set(re.findall(r'action\s*:\s*[\'"]([^\'"]+)[\'"]', page))

    # 1. Collect / Deliver finished repairs
    collect_candidates = [a for a in script_actions if any(k in a for k in ("teslim", "collect", "tamamla", "finish", "deliver"))]
    collect_candidates += ["tamir_tamamla", "teslim_et", "collect_repair", "tamir_bitir", "complete_repair"]
    for act in collect_candidates:
        r = post(sess, "/tamir.php", {"action": act, "csrf_token": csrf, "ajax": "true", "ajax_request": 1}, pause=False)
        if isinstance(r, dict) and r.get("success"):
            cyc.add("tamir", "deliver finished repairs", True, msg(r))
            break

    # Discover waiting customers and their quoted payouts
    candidates: List[Tuple[int, float]] = []
    seen_ids = set()

    for chunk in re.split(r'(?=<div[^>]*class=["\'][^"\']*(?:card|customer|musteri|item|repair|slot))', page):
        id_m = (
            re.search(r'data-(?:id|repair-id|customer-id|musteri-id)=["\'](\d+)["\']', chunk)
            or re.search(r'name=["\'](?:repair|customer|musteri)_id["\']\s+value=["\'](\d+)["\']', chunk)
            or re.search(r'onclick=["\'][^"\']*?(?:tamire_al|tamir|accept)[^\d]*(\d+)', chunk, re.I)
        )
        if not id_m:
            continue
        cid = int(id_m.group(1))
        if cid in seen_ids:
            continue
        seen_ids.add(cid)

        # Extract price / payout quote
        price_m = (
            re.search(r'data-(?:price|fiyat|ucret|odul|kazanc)=["\']([0-9.,]+)["\']', chunk)
            or re.search(r'\$([0-9,.]+)', chunk)
            or re.search(r'([0-9,.]+)\s*(?:\$|TL)', chunk)
        )
        price = 0.0
        if price_m:
            raw_p = price_m.group(1).replace("$", "").replace("TL", "").strip()
            if raw_p.count(",") == 1 and raw_p.count(".") == 0:
                raw_p = raw_p.replace(",", "")
            elif raw_p.count(".") == 1 and raw_p.count(",") == 0 and len(raw_p.split(".")[-1]) == 3:
                raw_p = raw_p.replace(".", "")
            else:
                raw_p = raw_p.replace(",", "")
            try:
                price = float(raw_p)
            except ValueError:
                price = 0.0
        candidates.append((cid, price))

    # Add any raw IDs not mapped to chunks
    all_raw_ids = set(
        re.findall(r'data-repair(?:-id)?="(\d+)"', page)
        + re.findall(r'data-customer(?:-id)?="(\d+)"', page)
        + re.findall(r'data-musteri(?:-id)?="(\d+)"', page)
        + re.findall(r'name="repair_id"\s+value="(\d+)"', page)
    )
    for rid_s in all_raw_ids:
        rid = int(rid_s)
        if rid not in seen_ids:
            candidates.append((rid, 0.0))
            seen_ids.add(rid)

    # PRIORITIZE: Sort waiting customers by HIGHEST paying repair first
    candidates.sort(key=lambda x: x[1], reverse=True)

    # 2. Start / Accept highest-paying repairs first into open bays
    if candidates:
        for rid, price in candidates:
            r = post(sess, "/tamir.php", {"action": "tamire_al", "repair_id": str(rid), "customer_id": str(rid), "csrf_token": csrf, "ajax": "true", "ajax_request": 1}, pause=False)
            if isinstance(r, dict) and r.get("success"):
                label = f"${price:,.0f}" if price > 0 else "top quote"
                cyc.add("tamir", f"start repair {rid} ({label})", True, msg(r))
            elif isinstance(r, dict) and any(w in str(msg(r)).lower() for w in ("dolu", "slot", "kapasite", "full", "max")):
                cyc.add("tamir", f"repair bays full (holding lower-paying jobs)", True, msg(r), soft=True)
                break

    # 3. Call New Customers ("+ Call new customer")
    call_candidates = [a for a in script_actions if any(k in a for k in ("musteri", "call", "customer"))]
    call_candidates += ["musteri_cagir", "call_customer", "new_customer", "call_new_customer"]
    called = False
    for act in call_candidates:
        r = post(sess, "/tamir.php", {"action": act, "csrf_token": csrf, "ajax": "true", "ajax_request": 1}, pause=False)
        if isinstance(r, dict) and r.get("success"):
            cyc.add("tamir", "call new customer", True, msg(r))
            called = True
            break
        elif isinstance(r, dict) and any(w in str(msg(r)).lower() for w in ("dolu", "limit", "bekle", "max", "dakika", "saniye", "cooldown")):
            cyc.add("tamir", "customer cooldown", True, msg(r), soft=True)
            called = True
            break
    if not called:
        cyc.add("tamir", "call customer", True, "workshop slots checked", soft=True)


# --------------------------------------------------------------------------- bank (bank.php)


def section_bank(sess, cyc: Cycle, inspect: bool) -> None:
    """Bank (bank.php): earns 2% daily interest on deposits up to $30M, withdrawing at maturity and compounding."""
    page = get(sess, "/bank.php")
    if inspect:
        dump("inspect/bank.html", page)

    csrf = ""
    m_csrf = (
        re.search(r'data-csrf="([0-9a-f]{64})"', page)
        or re.search(r'name="csrf_token"\s+value="([^"]+)"', page)
        or re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', page)
    )
    if m_csrf:
        csrf = m_csrf.group(1)

    # 1. Check if a mature deposit can be withdrawn with 2% interest
    if BANK_AUTO_WITHDRAW:
        for act in ("withdraw", "para_cek", "cek", "withdraw_deposit"):
            r = post(sess, "/bank.php", {"action": act, "csrf_token": csrf, "ajax": "true", "ajax_request": 1}, pause=False)
            if isinstance(r, dict) and r.get("success"):
                cyc.add("bank", "withdraw matured deposit", True, f"collected with 2% interest! ({msg(r)})")
                break
            elif isinstance(r, dict) and any(w in str(msg(r)).lower() for w in ("sure", "vade", "bekle", "yok", "maturity")):
                break

    # 2. Deposit idle cash to earn 2% daily interest (up to $30M cap, keeping reserve liquidity)
    if BANK_AUTO_DEPOSIT:
        bal = balance(sess) or 0.0
        m_dep = re.search(r'Total\s+Deposited\s*<[^>]+>\s*\$([0-9,.]+)', page, re.I)
        currently_deposited = 0.0
        if m_dep:
            try:
                currently_deposited = float(m_dep.group(1).replace(",", "").replace(".", ""))
            except ValueError:
                pass

        if bal > BANK_RESERVE and currently_deposited < 30000000:
            deposit_amt = int(min(bal - BANK_RESERVE, 30000000 - currently_deposited))
            deposit_amt = (deposit_amt // 1000) * 1000  # round down to nearest thousand
            if deposit_amt >= 1000:
                deposited = False
                for act in ("deposit", "para_yatir", "yatir", "add_deposit"):
                    r = post(
                        sess,
                        "/bank.php",
                        {"action": act, "amount": str(deposit_amt), "miktar": str(deposit_amt), "csrf_token": csrf, "ajax": "true", "ajax_request": 1},
                        pause=False,
                    )
                    if isinstance(r, dict) and r.get("success"):
                        cyc.add("bank", "deposit", True, f"locked ${deposit_amt:,} earning 2% daily interest ({msg(r)})")
                        deposited = True
                        break
                    elif isinstance(r, dict) and "limit" in str(msg(r)).lower():
                        cyc.add("bank", "deposit", True, msg(r), soft=True)
                        deposited = True
                        break
                if not deposited:
                    cyc.add("bank", "deposit", True, f"eligible ${deposit_amt:,} (reserve kept ${BANK_RESERVE:,.0f})", soft=True)


# --------------------------------------------------------------------------- joint jobs (ortak.php)


def section_ortak(sess, cyc: Cycle, inspect: bool) -> None:
    """Joint Jobs (ortak.php): collects completed joint jobs, joins highest-paying open lobbies, or creates top-tier jobs."""
    page = get(sess, "/ortak.php")
    if inspect:
        dump("inspect/ortak.html", page)

    csrf = ""
    m_csrf = (
        re.search(r'data-csrf="([0-9a-f]{64})"', page)
        or re.search(r'name="csrf_token"\s+value="([^"]+)"', page)
        or re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', page)
    )
    if m_csrf:
        csrf = m_csrf.group(1)

    script_actions = set(re.findall(r'action\s*:\s*[\'"]([^\'"]+)[\'"]', page))

    # 1. Collect completed joint jobs
    collect_candidates = [a for a in script_actions if any(k in a for k in ("topla", "collect", "odul", "claim", "bitir", "finish"))]
    collect_candidates += ["topla", "odul_topla", "collect_reward", "is_topla", "job_collect", "tamamla"]
    for act in collect_candidates:
        r = post(sess, "/ortak.php", {"action": act, "csrf_token": csrf, "ajax": "true", "ajax_request": 1}, pause=False)
        if isinstance(r, dict) and r.get("success"):
            cyc.add("ortak", "collect earnings", True, msg(r))
            break

    # 2. Check if user already has an active job in progress or waiting
    has_active = bool(
        re.search(r'You have an active job', page, re.I)
        or re.search(r'aktif\s+(?:bir\s+)?i(?:s|ş)iniz\s+var', page, re.I)
        or re.search(r'badge[^>]*>\s*(?:Waiting|Bekliyor|Devam)\s*<', page, re.I)
        or re.search(r'1 more people needed', page, re.I)
    )

    if has_active:
        cyc.add("ortak", "status", True, "active joint job in progress/waiting for partner", soft=True)
        return

    # 3. Discover available open jobs to JOIN ("Jobs You Can Join")
    join_candidates: List[Tuple[int, float, str]] = []
    seen_ids = set()

    for chunk in re.split(r'(?=<div[^>]*class=["\'][^"\']*(?:card|job|oda|item|slot))', page):
        # Skip if requires level above current user (e.g. "Level 24 required")
        if re.search(r'Level\s+(\d+)\s+required', chunk, re.I) or re.search(r'seviye\s+(\d+)\s+gerekli', chunk, re.I):
            continue

        id_m = (
            re.search(r'data-(?:id|job-id|room-id|oda-id)=["\'](\d+)["\']', chunk)
            or re.search(r'name=["\'](?:job|room|oda)_id["\']\s+value=["\'](\d+)["\']', chunk)
            or re.search(r'onclick=["\'][^"\']*?(?:katil|join)[^\d]*(\d+)', chunk, re.I)
        )
        if not id_m:
            continue
        jid = int(id_m.group(1))
        if jid in seen_ids:
            continue
        seen_ids.add(jid)

        title_m = re.search(r'<h[3-6][^>]*>([^<]+)</h[3-6]>', chunk) or re.search(r'class=["\'][^"\']*title[^"\']*["\'][^>]*>([^<]+)<', chunk)
        title = title_m.group(1).strip() if title_m else f"Job #{jid}"

        # Extract payout quote
        payout_m = (
            re.search(r'If you join\s*<[^>]+>\s*\$([0-9,.]+)', chunk, re.I)
            or re.search(r'Pool\s*<[^>]+>\s*\$([0-9,.]+)', chunk, re.I)
            or re.search(r'\$([0-9,.]+)', chunk)
        )
        payout = 0.0
        if payout_m:
            try:
                payout = float(payout_m.group(1).replace(",", "").replace(".", ""))
            except ValueError:
                payout = 0.0

        join_candidates.append((jid, payout, title))

    # Prioritize: highest payout open job first
    join_candidates.sort(key=lambda x: x[1], reverse=True)

    joined = False
    join_actions = [a for a in script_actions if any(k in a for k in ("katil", "join", "gir"))]
    join_actions += ["katil", "join", "oda_katil", "join_job"]

    if join_candidates:
        for jid, payout, title in join_candidates:
            for act in join_actions:
                r = post(
                    sess,
                    "/ortak.php",
                    {"action": act, "id": str(jid), "job_id": str(jid), "oda_id": str(jid), "csrf_token": csrf, "ajax": "true", "ajax_request": 1},
                    pause=False,
                )
                if isinstance(r, dict) and r.get("success"):
                    label = f"${payout:,.0f}" if payout > 0 else "top quote"
                    cyc.add("ortak", f"join {title} ({label})", True, msg(r))
                    joined = True
                    break
                elif isinstance(r, dict) and any(w in str(msg(r)).lower() for w in ("enerji", "energy")):
                    cyc.add("ortak", f"join {title}", True, msg(r), soft=True)
                    joined = True
                    break
            if joined:
                break

    # 4. If no open job joined, create a top-tier job (e.g. Design Work)
    if not joined:
        create_actions = [a for a in script_actions if any(k in a for k in ("olustur", "create", "kur", "ac", "baslat"))]
        create_actions += ["is_olustur", "create_job", "oda_olustur", "yeni_is"]

        # Discover available job types in select/modal if present
        type_options = re.findall(r'<option[^>]*value=["\']([^"\']+)["\'][^>]*>([^<]+)</option>', page)
        target_type = None
        for val, label in type_options:
            if any(w in label.lower() for w in ("design", "tasarim", "photo", "fotograf")):
                target_type = val
                break
        if not target_type and type_options:
            target_type = type_options[0][0]

        for act in create_actions:
            payload = {"action": act, "csrf_token": csrf, "ajax": "true", "ajax_request": 1}
            if target_type:
                payload["job_type"] = str(target_type)
                payload["is_tipi"] = str(target_type)
                payload["type"] = str(target_type)
            r = post(sess, "/ortak.php", payload, pause=False)
            if isinstance(r, dict) and r.get("success"):
                cyc.add("ortak", "create job", True, f"created room ({msg(r)})")
                break
            elif isinstance(r, dict) and any(w in str(msg(r)).lower() for w in ("enerji", "energy", "aktif", "active", "limit")):
                cyc.add("ortak", "create job", True, msg(r), soft=True)
                break
        else:
            cyc.add("ortak", "status", True, "checked open joint jobs", soft=True)


# --------------------------------------------------------------------------- main


def run_cycle(sections: Sequence[str], inspect: bool, dry_run: bool) -> Cycle:
    global DRY_RUN
    DRY_RUN = dry_run
    cyc = Cycle()
    sess = login()

    if dry_run:
        log.warning("DRY RUN — no mutating request will be sent")

    if "production" in sections:
        section_production(sess, cyc, inspect)
    if "fields" in sections:
        section_fields(sess, cyc, inspect)
    if "orchards" in sections:
        section_orchards(sess, cyc, inspect)
    if "barns" in sections:
        _animals(sess, cyc, inspect, "barns", "ahirlar", "ahir")
    if "coops" in sections:
        _animals(sess, cyc, inspect, "coops", "kumesler", "kumes")
    if "bees" in sections:
        section_bees(sess, cyc, inspect)
    if "tamir" in sections:
        section_tamir(sess, cyc, inspect)
    if "jobs" in sections:
        section_jobs(sess, cyc, inspect)
    if "ortak" in sections:
        section_ortak(sess, cyc, inspect)
    if "math" in sections:
        section_math(sess, cyc, inspect)
    if "jobs" in sections:
        section_jobs(sess, cyc, inspect)
    if "ortak" in sections:
        section_ortak(sess, cyc, inspect)
    if "tamir" in sections:
        section_tamir(sess, cyc, inspect)
    if "bank" in sections:
        section_bank(sess, cyc, inspect)

    if inspect:
        pages_to_dump = [
            ("jobs", "/jobs.php"),
            ("tamir", "/tamir.php"),
            ("ortak", "/ortak.php"),
            ("lojistik", "/lojistik.php?sekme=gorevler"),
            ("bank", "/bank.php"),
            ("balikcilik", "/balikcilik.php"),
            ("isyeri", "/isyeri.php"),
        ]
        for name, uri in pages_to_dump:
            try:
                dump(f"inspect/{name}.html", get(sess, uri))
            except Exception as exc:
                log.debug("inspect dump %s failed: %s", name, exc)

    bal = balance(sess)
    if bal is not None:
        cyc.add("account", "balance", True, f"${bal:,.2f}")
    return cyc


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Ticarisk production + farm automation")
    ap.add_argument(
        "--sections",
        default=os.environ.get("TICARISK_SECTIONS", DEFAULT_SECTIONS),
        help="comma separated: " + DEFAULT_SECTIONS,
    )
    ap.add_argument("--inspect", action="store_true", help="dump pages to inspect/")
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("ticarisk-cycle.log", encoding="utf-8"),
        ],
    )
    urllib3.disable_warnings()

    sections = [s.strip().lower() for s in args.sections.split(",") if s.strip()]
    unknown = set(sections) - set(DEFAULT_SECTIONS.split(","))
    if unknown:
        log.warning("ignoring unknown sections: %s", ", ".join(sorted(unknown)))

    try:
        cyc = run_cycle(sections, args.inspect, args.dry_run)
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("cycle aborted: %s", exc)
        return 2

    print("\n" + "=" * 68)
    for s in cyc.steps:
        print(s.line())
    print("=" * 68)
    ok_count = len(cyc.steps) - len(cyc.failed) - len(cyc.warned)
    tail = f"  ({len(cyc.warned)} warnings)" if cyc.warned else ""
    print(f"{ok_count}/{len(cyc.steps)} steps ok{tail}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
