#!/usr/bin/env python3
"""Ticarisk Fishing Automation Module (balikcilik.php).

Automates:
1. Rod Fishing: Casts rod and reels in catch (~3 min cycle).
2. Boat Trips: Sends crew out on rowboat and collects haul (~7-8 min cycle).
3. Daily Quests: Auto-claims completed quest rewards.
4. Cold Store Safety: Preserves all caught fish in cold store; only sells
   the bare minimum if an incomplete daily quest strictly demands NPC selling.
5. Captcha Protection: Auto-solves the slider puzzle before cast or boat departure.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3

import bot

log = logging.getLogger("fishing")


def parse_fishing_state(html: str) -> Dict[str, Any]:
    """Extract BC_BOOT state and daily quests from balikcilik.php HTML."""
    m_boot = re.findall(r"(?:window\.)?BC_BOOT\s*=\s*(\{.*?\});", html, re.DOTALL)
    boot: Dict[str, Any] = {}
    if m_boot:
        try:
            boot = json.loads(m_boot[0])
        except Exception as e:
            log.warning("failed to parse BC_BOOT JSON: %s", e)

    # Parse claimable quests
    claimable_keys: List[str] = re.findall(
        r'class="[^"]*btn-bc-ok\s+js-claim[^"]*"\s+data-key="([^"]+)"', html
    )
    if not claimable_keys:
        claimable_keys = re.findall(r'data-key="([^"]+)"', html)

    # Parse sell quests to check for deficits
    # Format: <div class="quest-row"> ... <div class="fish-name">Sell $5,000 worth of fish (NPC)</div>
    #         <div class="fish-meta">3200 / 5000 · $80.000 + 120 XP</div> ...
    sell_deficits: List[Dict[str, Any]] = []
    quest_blocks = re.findall(
        r'<div class="quest-row">(.*?)</div>\s*(?=<div class="quest-row">|<h3|</div>\s*</div>\s*</div>)',
        html,
        re.DOTALL,
    )
    for qb in quest_blocks:
        if "Claimed" in qb:
            continue
        if "js-claim" in qb:
            continue  # Already ready to claim

        m_name = re.search(r'<div class="fish-name">(.*?)</div>', qb)
        m_meta = re.search(r'<div class="fish-meta">(.*?)</div>', qb)
        if not m_name or not m_meta:
            continue

        qname = m_name.group(1).strip()
        qmeta = m_meta.group(1).strip()

        # Check for "Sell $X worth of fish"
        m_val = re.search(r"Sell\s+\$([\d,\.]+)\s+worth", qname, re.IGNORECASE)
        if m_val:
            m_prog = re.search(r"([\d\.]+)\s*/\s*([\d\.]+)", qmeta)
            if m_prog:
                cur = float(m_prog.group(1).replace(",", ""))
                tgt = float(m_prog.group(2).replace(",", ""))
                if cur < tgt:
                    sell_deficits.append({"type": "dollars", "needed": tgt - cur})

        # Check for "Sell X kg to the NPC"
        m_kg = re.search(r"Sell\s+(\d+)\s+kg", qname, re.IGNORECASE)
        if m_kg:
            m_prog = re.search(r"([\d\.]+)\s*/\s*([\d\.]+)", qmeta)
            if m_prog:
                cur = float(m_prog.group(1).replace(",", ""))
                tgt = float(m_prog.group(2).replace(",", ""))
                if cur < tgt:
                    sell_deficits.append({"type": "kg", "needed": tgt - cur})

    return {
        "boot": boot,
        "claims": claimable_keys,
        "sell_deficits": sell_deficits,
    }


def handle_quest_selling(
    sess: requests.Session, deficits: List[Dict[str, Any]], inv: List[Dict[str, Any]]
) -> int:
    """Sells only the bare minimum fish required to satisfy incomplete daily quests.

    All other catches remain preserved in the Cold Store.
    """
    if not deficits or not inv:
        return 0

    total_sold_kg = 0

    # Sort inventory by unit price ascending to sell cheapest fish first
    sorted_inv = sorted(inv, key=lambda x: float(x.get("npc", 999)))

    for defic in deficits:
        dtype = defic["type"]
        needed = defic["needed"]
        log.info("quest sell requirement: need %.1f %s", needed, dtype)

        for item in sorted_inv:
            stock = int(item.get("qty", 0))
            if stock <= 0:
                continue

            name = item.get("name")
            label = item.get("label", name)
            npc_price = float(item.get("npc", 0))

            if dtype == "kg":
                sell_kg = min(stock, int(needed))
                if sell_kg <= 0 and needed > 0:
                    sell_kg = 1
                if sell_kg > 0:
                    r = bot.post(
                        sess,
                        "/balikcilik.php",
                        {"ajax_request": "1", "action": "npc_sell", "urun_adi": name, "miktar": str(sell_kg)},
                    )
                    if isinstance(r, dict) and r.get("success"):
                        log.info("bare min sell: sold %d kg of %s ($%.0f/kg)", sell_kg, label, npc_price)
                        item["qty"] = stock - sell_kg
                        needed -= sell_kg
                        total_sold_kg += sell_kg
                        if needed <= 0:
                            break

            elif dtype == "dollars":
                if npc_price <= 0:
                    continue
                # Calculate minimum kg needed
                required_kg = int(-(-needed // npc_price))  # ceiling division
                sell_kg = min(stock, required_kg)
                if sell_kg > 0:
                    r = bot.post(
                        sess,
                        "/balikcilik.php",
                        {"ajax_request": "1", "action": "npc_sell", "urun_adi": name, "miktar": str(sell_kg)},
                    )
                    if isinstance(r, dict) and r.get("success"):
                        gained = sell_kg * npc_price
                        log.info("bare min sell: sold %d kg of %s for $%.0f", sell_kg, label, gained)
                        item["qty"] = stock - sell_kg
                        needed -= gained
                        total_sold_kg += sell_kg
                        if needed <= 0:
                            break

    return total_sold_kg


def fishing_step(sess: requests.Session, cyc: bot.Cycle) -> Dict[str, Any]:
    """Execute one full evaluation and action pass on balikcilik.php."""
    html = bot.get(sess, "/balikcilik.php")
    state = parse_fishing_state(html)
    boot = state["boot"]
    claims = state["claims"]
    sell_deficits = state["sell_deficits"]

    # 1. Claim any ready quests first
    for key in claims:
        r = bot.post(sess, "/balikcilik.php", {"ajax_request": "1", "action": "claim_quest", "gorev_key": key})
        msg = r.get("message", "claimed") if isinstance(r, dict) else str(r)
        cyc.add("fishing", f"claim_quest:{key}", bool(r.get("success") if isinstance(r, dict) else False), msg)
        log.info("claimed quest %s: %s", key, msg)

    # 2. Check if bare minimum selling is needed for an active quest
    if sell_deficits:
        sold = handle_quest_selling(sess, sell_deficits, boot.get("inv", []))
        if sold > 0:
            # Re-read page to pick up newly unlocked claim buttons
            html = bot.get(sess, "/balikcilik.php")
            state = parse_fishing_state(html)
            boot = state["boot"]
            for key in state["claims"]:
                r = bot.post(sess, "/balikcilik.php", {"ajax_request": "1", "action": "claim_quest", "gorev_key": key})
                cyc.add("fishing", f"claim_quest:{key}", True, r.get("message", ""))

    # 3. Rod Fishing: Repair if broken
    if boot.get("broken"):
        log.warning("rod is broken! repairing rod for $100,000...")
        r_rep = bot.post(sess, "/balikcilik.php", {"ajax_request": "1", "action": "repair_rod"})
        cyc.add("fishing", "repair_rod", bool(r_rep.get("success")), r_rep.get("message", ""))

    # 4. Rod Fishing: Collect catch if ready
    casting = bool(boot.get("casting"))
    ready = bool(boot.get("ready"))
    rod_left = int(boot.get("left") or 0)

    if casting and ready:
        log.info("fish on the line! reeling in...")
        r_col = bot.post(sess, "/balikcilik.php", {"ajax_request": "1", "action": "collect_catch"})
        msg = r_col.get("message", "") if isinstance(r_col, dict) else ""
        cyc.add("fishing", "collect_catch", bool(r_col.get("success")), msg)
        log.info("reeled in: %s", msg)
        casting = False
        ready = False
        rod_left = 0

    # 5. Rod Fishing: Cast if idle
    if not casting and not boot.get("broken"):
        log.info("rod waiting on shore; solving captcha to cast...")
        solved, _spent = bot.solve_puzzle(sess, "/balikcilik.php", cyc)
        if solved:
            r_cast = bot.post(sess, "/balikcilik.php", {"ajax_request": "1", "action": "start_cast"})
            msg = r_cast.get("message", "") if isinstance(r_cast, dict) else ""
            wait = int(r_cast.get("wait") or 150) if isinstance(r_cast, dict) else 150
            cyc.add("fishing", "start_cast", bool(r_cast.get("success")), f"{msg} (timer: {wait}s)")
            log.info("cast started: %s (timer: %ds)", msg, wait)
            rod_left = wait
    elif casting and not ready:
        cyc.add("fishing", "rod_status", True, f"line in water ({rod_left}s remaining)", soft=True)

    # 6. Boat Trip: Collect haul if ready
    missions = boot.get("missions", [])
    boat_left = 480
    boat_active = False

    for m in missions:
        boat_type = m.get("boat", "row")
        b_ready = bool(m.get("ready"))
        b_left = int(m.get("left") or 0)

        if b_ready:
            log.info("crew returned from boat trip (%s)! collecting haul...", boat_type)
            r_bcol = bot.post(
                sess,
                "/balikcilik.php",
                {"ajax_request": "1", "action": "collect_boat_mission", "boat": boat_type},
            )
            msg = r_bcol.get("message", "") if isinstance(r_bcol, dict) else ""
            cyc.add("fishing", f"collect_boat:{boat_type}", bool(r_bcol.get("success")), msg)
            log.info("crew haul collected: %s", msg)
        else:
            boat_active = True
            boat_left = min(boat_left, b_left)

    # 7. Boat Trip: Send boat if idle and crew is available
    crew_free = int(boot.get("crew_free_novice") or 0) + int(boot.get("crew_free_exp") or 0)
    # If no missions active, crew is at dock
    if not boat_active or crew_free > 0:
        log.info("crew available at dock; solving captcha to send boat...")
        solved, _spent = bot.solve_puzzle(sess, "/balikcilik.php", cyc)
        if solved:
            r_bsend = bot.post(
                sess,
                "/balikcilik.php",
                {"ajax_request": "1", "action": "start_boat_mission", "boats": "row"},
            )
            msg = r_bsend.get("message", "") if isinstance(r_bsend, dict) else ""
            b_wait = int(r_bsend.get("wait") or 480) if isinstance(r_bsend, dict) else 480
            cyc.add("fishing", "start_boat:row", bool(r_bsend.get("success")), f"{msg} (timer: {b_wait}s)")
            log.info("boat trip started: %s (timer: %ds)", msg, b_wait)
            boat_left = b_wait
    else:
        cyc.add("fishing", "boat_status", True, f"boat at sea ({boat_left}s remaining)", soft=True)

    next_sleep = max(10, min(rod_left if rod_left > 0 else 180, boat_left if boat_left > 0 else 420))
    return {
        "rod_left": rod_left,
        "boat_left": boat_left,
        "next_sleep": next_sleep,
    }


def loop_fishing(poll_limit: Optional[int] = None) -> None:
    """Run persistent fishing loop with intelligent dynamic sleep timers."""
    log.info("starting continuous Ticarisk Fishing loop (3-min rod / 7-min boat)...")
    sess = bot.login()
    cycles = 0

    while True:
        cycles += 1
        cyc = bot.Cycle()
        log.info("=== Fishing Pass #%d ===", cycles)
        try:
            res = fishing_step(sess, cyc)
            rod_left = res["rod_left"]
            boat_left = res["boat_left"]
            sleep_sec = res["next_sleep"] + 3  # small buffer so timer is definitively 0
            log.info(
                "Timers: Rod %ds (~%.1f min) | Boat %ds (~%.1f min) -> Sleeping %ds",
                rod_left,
                rod_left / 60,
                boat_left,
                boat_left / 60,
                sleep_sec,
            )
        except Exception as e:
            log.exception("error during fishing pass: %s", e)
            sleep_sec = 60

        if poll_limit and cycles >= poll_limit:
            log.info("reached poll limit of %d cycles. Exiting.", poll_limit)
            break

        time.sleep(sleep_sec)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )
    urllib3.disable_warnings()
    # If run directly with --loop, run infinite loop; otherwise run 1 single pass
    if "--loop" in sys.argv:
        loop_fishing()
    else:
        s = bot.login()
        c = bot.Cycle()
        res = fishing_step(s, c)
        print("\nPass Summary:")
        for step in c.steps:
            print(" ", step.line())
        print(f"Next optimal check in {res['next_sleep']}s")
