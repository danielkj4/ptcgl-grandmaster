"""Regression tests for the PTCGL log parser, asserted against a REAL battle log.

Run with:  python3 tests/test_parser.py
(or: pytest tests/test_parser.py)

Ground truth for tests/fixtures/log_tigrao95_vs_danielkj4.txt, verified by hand:
  * danielkj4 took 4 prize cards; Tigrão95 took 6 (4 singles + one "took 2").
  * Tigrão95 won (last line: "Tigrão95 wins.").
  * Tigrão95's deck is built around Team Rocket's Honchkrow.
  * danielkj4 mulliganed, so their post-mulligan opening hand is NOT logged.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ptcg_parser import (  # noqa: E402
    clean_card_name, parse_decklist, parse_events, detect_players,
    detect_opponent_deck, detect_result, count_prizes, parse_game,
)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "log_tigrao95_vs_danielkj4.txt")
LOG = open(FIXTURE, encoding="utf-8").read()
ME, OPP = "danielkj4", "Tigrão95"

_failures = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {actual!r}" + ("" if ok else f"  (expected {expected!r})"))
    if not ok:
        _failures.append(label)


def check_true(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f": {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


print("\n--- card name cleaning ---")
check("set-code prefix stripped", clean_card_name("(sv7_58) Slowking"), "Slowking")
check("apostrophes kept", clean_card_name("(me1_114) Boss's Orders"), "Boss's Orders")
check("decklist-style suffix stripped", clean_card_name("Slowking SV7 58"), "Slowking")

print("\n--- decklist parsing ---")
d = parse_decklist("4 Slowking SV7 58\n2 Boss's Orders\n1 Rare Candy")
check("decklist counts", dict(d), {"Slowking": 4, "Boss's Orders": 2, "Rare Candy": 1})

print("\n--- event model (bullet lines fold into the event above) ---")
evs = parse_events(LOG)
draw2 = next(e for e in evs if e['text'] == "danielkj4 drew 2 cards." and e['cards'])
check("multi-card draw picks up its bullet list", draw2['cards'],
      ["Slowking", "Telepathic Psychic Energy"])
dmg = [e for e in evs if e['text'] == "Damage breakdown:"]
check_true("damage-breakdown bullets are not treated as cards",
           all(e['cards'] == [] for e in dmg), f"{len(dmg)} breakdown events")

print("\n--- players / result / prizes ---")
check("players detected", detect_players(LOG), sorted([ME, OPP]))
check("result for danielkj4", detect_result(LOG, ME), "Loss")
check("result for Tigrão95", detect_result(LOG, OPP), "Win")
check("danielkj4 prizes (mulligan must NOT count)", count_prizes(LOG, ME), 4)
check("Tigrão95 prizes ('took 2 Prize cards' counts as 2)", count_prizes(LOG, OPP), 6)

print("\n--- opponent deck detection ---")
opp_deck = detect_opponent_deck(LOG, ME)
check_true("opponent identified as Honchkrow", "Honchkrow" in opp_deck, opp_deck)
check_true("not 'Unknown'", opp_deck != "Unknown", opp_deck)

print("\n--- full game parse ---")
turns = parse_game(LOG, ME, deck_dict={}, target_card="None", manual_outs=1)
check_true("turns parsed", len(turns) > 0, f"{len(turns)} turns")
my_turns = [t for t in turns if t['is_me']]
check_true("player turns found", len(my_turns) > 0, f"{len(my_turns)} of mine")

# Deck size: 60 - 7 (opening hand) - 6 (prizes, never logged) = 47 at the start.
first = my_turns[0]
check_true("deck size accounts for the 6 unlogged prizes",
           first['deck_snapshot'] <= 47,
           f"first turn deck={first['deck_snapshot']} (must be <= 47, was 53 before the fix)")
check_true("deck size never goes negative",
           all(t['deck_snapshot'] >= 0 for t in turns),
           f"min={min(t['deck_snapshot'] for t in turns)}")

# Hand: multi-card draws must actually land in the hand.
all_hands = [c for t in my_turns for c in t['hand_snapshot']]
check_true("hand snapshots are populated", len(all_hands) > 0, f"{len(all_hands)} cards seen")
check_true("no comma-joined mega-card in hand (opening-hand bug)",
           not any(',' in c for c in all_hands),
           next((c for c in all_hands if ',' in c), "clean"))
check_true("hand holds real card names",
           any(c in ("Slowking", "Rare Candy", "Dawn", "Hilda", "Alakazam") for c in all_hands),
           str(sorted(set(all_hands))[:6]))

# Discard: KO'd Pokemon plus explicitly discarded cards.
all_discards = [c for t in my_turns for c in t['discard_snapshot']]
check_true("discard tracks KO'd Pokemon", "Slowking" in all_discards or "Slowpoke" in all_discards,
           str(sorted(set(all_discards))[:8]))
check_true("discard tracks multi-card discards (Haxorus was discarded to attack)",
           "Haxorus" in all_discards, str(sorted(set(all_discards))[:8]))

# The mulligan means the opening hand is unknown - we must not claim to know it.
check_true("mulligan clears the (now unknown) opening hand",
           "Wondrous Patch" not in turns[0]['hand_snapshot'],
           str(turns[0]['hand_snapshot']))

print("\n--- opponent-forced effects (needs a decklist to attribute) ---")
# Team Rocket's Archer makes danielkj4 shuffle their hand back and redraw, but the
# log credits Tigrão95. With a decklist loaded those cards must return to our deck.
MY_DECK = {c: 4 for c in [
    "Dawn", "Lillie's Determination", "Rare Candy", "Dudunsparce", "Dunsparce",
    "Alakazam", "Kadabra", "Abra", "Wondrous Patch", "Slowking", "Slowpoke",
    "Fezandipiti ex", "Haxorus", "Academy at Night", "Telepathic Psychic Energy",
    "Basic Psychic Energy", "Hilda", "Sacred Ash", "Poké Pad", "Night Stretcher",
    "Buddy-Buddy Poffin", "Enriching Energy", "Spectrier", "Kyurem", "Metagross",
    "Colress's Tenacity", "Enhanced Hammer", "Crushing Hammer", "Boss's Orders",
    "Lucky Helmet", "Battle Cage", "Team Rocket's Petrel",
]}
turns_dl = parse_game(LOG, ME, deck_dict=MY_DECK, target_card="None", manual_outs=1)
decks_dl = [t['deck_snapshot'] for t in turns_dl if t['is_me']]
decks_nodl = [t['deck_snapshot'] for t in my_turns]
check_true("decklist attribution returns forced-shuffle cards to the deck",
           decks_dl[-1] > decks_nodl[-1],
           f"with decklist final deck={decks_dl[-1]}, without={decks_nodl[-1]}")
check_true("deck stays non-negative with a decklist too",
           all(d >= 0 for d in decks_dl), f"min={min(decks_dl)}")

# ==========================================================================
# Second real log: a DIFFERENT matchup (Water/Mega Abomasnow), danielkj4 WINS,
# no mulligan on our side, multi-prize takes, and opponent-card mis-attribution.
# ==========================================================================
FIXTURE2 = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "log_gladys2meetu_vs_danielkj4.txt")
LOG2 = open(FIXTURE2, encoding="utf-8").read()
OPP2 = "gladys2meetu"
DECK2 = {c: 4 for c in [
    "Basic Fire Energy", "Telepathic Psychic Energy", "Night Stretcher", "Abra",
    "Rare Candy", "Enriching Energy", "Drakloak", "Dreepy", "Dragapult ex", "Kadabra",
    "Alakazam", "Fezandipiti ex", "Dawn", "Wondrous Patch", "Boss's Orders", "Hilda",
    "Enhanced Hammer", "Poké Pad", "Elgyem", "Special Red Card", "Crushing Hammer",
    "Sacred Ash", "Basic Psychic Energy", "Battle Cage", "Buddy-Buddy Poffin",
    "Moltres", "Eri", "Nighttime Mine", "Budew",
]}

print("\n=== LOG 2: gladys2meetu (Mega Abomasnow) vs danielkj4 ===")
print("\n--- result / prizes (this one danielkj4 WINS) ---")
check("result for danielkj4", detect_result(LOG2, ME), "Win")
check("result for gladys2meetu", detect_result(LOG2, OPP2), "Loss")
check("danielkj4 prizes (two x 'took 3 Prize cards')", count_prizes(LOG2, ME), 6)
check("gladys2meetu prizes", count_prizes(LOG2, OPP2), 3)

print("\n--- matchup ---")
opp2 = detect_opponent_deck(LOG2, ME)
check_true("opponent identified as Mega Abomasnow ex", "Abomasnow" in opp2, opp2)

print("\n--- opening hand (no mulligan on our side this game) ---")
turns2 = parse_game(LOG2, ME, deck_dict=DECK2)
t2_mine = [t for t in turns2 if t['is_me']]
opening = turns2[0]['hand_snapshot']
check_true("opening hand parsed as individual cards", len(opening) == 7, f"{len(opening)}: {opening}")
check_true("mulligan bonus draw landed in hand (Drakloak)", "Drakloak" in opening, str(opening))
check_true("both Abras played to the board left the hand",
           opening.count("Abra") == 0, str(opening))

print("\n--- opponent cards must NOT leak into our discard ---")
# The log says "danielkj4 discarded (x) Surfing Beach" and "... discarded from
# danielkj4's Mega Abomasnow ex", but both are gladys2meetu's cards.
d2 = [c for t in t2_mine for c in t['discard_snapshot']]
check_true("opponent Stadium (Surfing Beach) filtered out", "Surfing Beach" not in d2,
           str(sorted(set(d2))))
check_true("opponent Energy (Basic Water Energy) filtered out", "Basic Water Energy" not in d2,
           str(sorted(set(d2))))
check_true("our own KO'd cards still tracked",
           any(c in d2 for c in ("Alakazam", "Kadabra", "Abra")), str(sorted(set(d2))))
check_true("deck stays non-negative", all(t['deck_snapshot'] >= 0 for t in turns2),
           f"min={min(t['deck_snapshot'] for t in turns2)}")

print()
if _failures:
    print(f"❌ {len(_failures)} FAILING: {_failures}")
    sys.exit(1)
print("✅ All parser tests passed (2 real logs).")
