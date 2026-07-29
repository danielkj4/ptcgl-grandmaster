"""Pure PTCGL battle-log parsing.

No Streamlit imports here on purpose: this module is importable and unit-testable
on its own (see tests/test_parser.py), so parsing regressions get caught rather
than silently feeding the AI coach bad data.

Log format notes (derived from real PTCGL logs, see tests/fixtures/):
  * Card names are prefixed with a set code in parentheses: "(sv7_58) Slowking".
    The presence of that prefix means the card came from a known zone (hand/deck);
    a "played X." line WITHOUT a prefix means an in-play Stadium was used.
  * Multi-card events put the card names on a FOLLOWING bullet line:
        - danielkj4 drew 2 cards.
           • (sv7_58) Slowking, (me3_88) Telepathic Psychic Energy
    So bullet lines must be attached to the event above them.
  * Sub-events are prefixed with "- " and are consequences of the line above.
  * Prizes are never logged as being set aside; both players always set aside 6.
"""
import re
import math
from collections import Counter


# --------------------------------------------------------------------------
# Card-name utilities
# --------------------------------------------------------------------------
def clean_card_name(name):
    """Strip set-code prefixes/suffixes and decorations from a card name."""
    name = re.sub(r'\{.*?\}', '', name)
    name = re.sub(r'\(.*?\)', '', name)          # "(sv7_58) Slowking" -> " Slowking"
    name = re.sub(r'\s+[A-Z0-9]{2,4}\s+\d+.*$', '', name)  # "Slowking SV7 58" -> "Slowking"
    return re.sub(r'\s+', ' ', name).strip()


def parse_decklist(deck_text):
    """Parse '<count> <card name>' lines into a Counter of {clean_name: count}."""
    deck = Counter()
    for line in deck_text.split('\n'):
        match = re.match(r'^(\d+)\s+(.+)', line.strip())
        if match:
            count, name = match.groups()
            cleaned = clean_card_name(name)
            if cleaned:
                deck[cleaned] = int(count)
    return deck


def _remove_one(bucket, card):
    """Remove a single instance of card from a list. Returns True if removed."""
    if card in bucket:
        bucket.remove(card)
        return True
    return False


# Matches "(set_code) Card Name" up to the next action word or punctuation.
_CARD_MENTION_RE = re.compile(
    r'\([a-z0-9][a-z0-9_\-]*\)\s+(.+?)(?=\s+(?:to|on|in|was|is|used|from|and|for|into)\b|[,.]|$)',
    re.IGNORECASE)


def extract_card_names(log_text):
    """Every distinct card name mentioned anywhere in the log, both players'.

    Used to fetch oracle text for opponent cards too — without it the coach has no
    idea what an opponent's card does (e.g. Budew's Itchy Pollen item-lock) and
    can't warn about effects that carry into the player's turn.
    """
    names = set()
    for m in _CARD_MENTION_RE.finditer(log_text):
        nm = clean_card_name(m.group(1))
        if nm and len(nm) > 1:
            names.add(nm)
    return names


# --------------------------------------------------------------------------
# Event model: a line plus any card names listed on the bullet line(s) under it
# --------------------------------------------------------------------------
def _split_card_list(bullet_text):
    """'• (a) Name1, (b) Name2' -> ['Name1', 'Name2'].
    Returns [] for non-card bullets (e.g. '• 1 Pokémon: 60 damage')."""
    t = bullet_text.lstrip('•').strip()
    if not re.search(r'\([^)]+\)', t):
        return []
    out = []
    for piece in t.split(','):
        cleaned = clean_card_name(piece)
        if cleaned:
            out.append(cleaned)
    return out


def _split_card_tokens(bullet_text):
    """Like _split_card_list but keeps the set-code: [(code, name), ...].
    Board reconstruction needs codes to match a Pokemon across turns."""
    out = []
    for piece in bullet_text.lstrip('•').split(','):
        m = re.search(r'\(([a-z0-9][a-z0-9_\-]*)\)\s*(.+)', piece.strip(), re.I)
        if m:
            out.append((m.group(1), clean_card_name(m.group(2)) or m.group(2).strip()))
    return out


def parse_events(log_text):
    """Turn raw log lines into events: {'raw', 'text', 'level', 'cards'}.

    'level' is 'top' for a player action or 'sub' for a '- ' consequence line.
    Bullet lines are folded into the preceding event's 'cards'.
    """
    events = []
    for raw in log_text.split('\n'):
        s = raw.strip()
        if not s:
            continue
        if s.startswith('•'):
            cards = _split_card_list(s)
            if cards and events:
                events[-1]['cards'].extend(cards)
                events[-1]['card_tokens'].extend(_split_card_tokens(s))
            continue
        if s.startswith('- '):
            events.append({'raw': s, 'text': s[2:].strip(), 'level': 'sub', 'cards': [], 'card_tokens': []})
        else:
            events.append({'raw': s, 'text': s, 'level': 'top', 'cards': [], 'card_tokens': []})
    return events


# --------------------------------------------------------------------------
# Match-level detection
# --------------------------------------------------------------------------
def detect_players(log_text):
    """Player names, taken from the turn headers."""
    return sorted(set(re.findall(r"^(.+?)'s Turn\s*$", log_text, re.M)))


def count_prizes(log_text, player):
    """Count prize cards actually taken. Handles 'took a Prize card' and
    'took 2 Prize cards', and ignores non-prize 'took' lines (e.g. mulligans)."""
    total = 0
    for m in re.finditer(fr"{re.escape(player)} took (a|\d+) Prize card", log_text):
        total += 1 if m.group(1) == 'a' else int(m.group(1))
    return total


def detect_result(log_text, target_user):
    """Best-effort win/loss detection. Returns 'Win', 'Loss', or 'Unknown'."""
    tl = target_user.lower()
    for line in log_text.split('\n'):
        if "coin" in line.lower():   # skip coin-flip lines containing 'win/won'
            continue
        m = re.search(r"(.+?)\s+(?:wins the game|won the game|has won|wins!|wins\.)", line)
        if m:
            return "Win" if tl in m.group(1).lower() else "Loss"
        m = re.search(r"(.+?)\s+(?:conceded|has conceded|surrendered)", line, re.IGNORECASE)
        if m:
            return "Loss" if tl in m.group(1).lower() else "Win"
    return "Unknown"


_SUFFIX_RE = re.compile(r'\b(ex|GX|V|VSTAR|VMAX|Radiant)\b')


def detect_opponent_deck(log_text, target_user):
    """Identify the opponent's deck from the Pokemon they actually used.

    Reads the OWNER out of each line's possessive rather than discarding every
    line that mentions the target (attack lines name both players).
    """
    players = detect_players(log_text)
    others = [p for p in players if p != target_user]
    if not others:
        return "Unknown"
    opp = others[0]
    esc = re.escape(opp)

    key_pokemon = []
    # "Opp's (code) Name used Attack ..."
    key_pokemon += [clean_card_name(m) for m in
                    re.findall(fr"{esc}'s (\([^)]*\) [^.]+?) used ", log_text)]
    # "Opp evolved (code) A to (code) B on the Bench / in the Active Spot."
    key_pokemon += [clean_card_name(m) for m in
                    re.findall(fr"{esc} evolved \([^)]*\) .+? to (\([^)]*\) .+?) (?:on the Bench|in the Active Spot)", log_text)]
    # "Opp played (code) Name to the Active Spot / Bench."
    key_pokemon += [clean_card_name(m) for m in
                    re.findall(fr"{esc} played (\([^)]*\) .+?) to the (?:Active Spot|Bench)", log_text)]

    key_pokemon = [k for k in key_pokemon if k]
    if not key_pokemon:
        return "Unknown"

    counts = Counter(key_pokemon)
    # A multi-prize / headline Pokemon is the deck's identity if one was used.
    for pkmn, _ in counts.most_common():
        if _SUFFIX_RE.search(pkmn):
            return f"{pkmn} Core"
    return f"{counts.most_common(1)[0][0]} Core"


# --------------------------------------------------------------------------
# Odds + prize-pool inference
# --------------------------------------------------------------------------
def calculate_odds(deck_size, outs, cards_drawn):
    if deck_size <= 0 or outs <= 0 or cards_drawn <= 0:
        return 0.0
    if cards_drawn > deck_size:
        cards_drawn = deck_size
    num_ways_to_miss = math.comb(deck_size - outs, cards_drawn)
    total_ways = math.comb(deck_size, cards_drawn)
    return 0.0 if total_ways == 0 else round((1 - (num_ways_to_miss / total_ways)) * 100, 2)


def track_prizes(log_text, deck_list, target_user):
    """Infer which of the player's cards were never seen (likely prized)."""
    prized = {}
    user_log = "\n".join([l for l in log_text.split('\n') if target_user in l])
    for card_name, total in deck_list.items():
        count_in_log = len(re.findall(re.escape(card_name), user_log, re.IGNORECASE))
        missing = total - count_in_log
        if missing > 0:
            priority = 1
            if _SUFFIX_RE.search(card_name):
                priority = 3
            if any(x in card_name for x in ["Unfair Stamp", "Maximum Belt", "Hero's Cape",
                                            "Scoop Up Cyclone", "Prime Catcher"]):
                priority = 4
            if "Boss" in card_name:
                priority = 2
            prized[card_name] = {"count": missing, "priority": priority}
    return dict(sorted(prized.items(), key=lambda i: i[1]['priority'], reverse=True)[:6])


# --------------------------------------------------------------------------
# The main state machine
# --------------------------------------------------------------------------
_LOCK_ACTIONS = ("played", "attached", "evolved", "retreated", "used")


def _add_discard(st, card):
    """Add a card to the player's discard pile, filtering out cards the log
    mis-attributes to us.

    PTCGL credits the ACTING player, so knocking out an opponent's Stadium or
    hammering their Energy is logged as "danielkj4 discarded (x) Surfing Beach" /
    "... was discarded from danielkj4's Mega Abomasnow ex" even though those are the
    opponent's cards. When a decklist is loaded, anything not in it isn't ours.
    """
    if not card:
        return
    if st['deck_dict'] and card not in st['deck_dict']:
        return
    st['discard'].append(card)


def _cards_belong_to_target(cards, deck_dict):
    """Heuristic: are these revealed cards the target player's?

    The log is written from the target's perspective, so their cards get named
    while the opponent's stay hidden. When a decklist is loaded we can attribute
    a revealed list properly. Requires a majority match to avoid false positives
    on cards both decks share (Rare Candy, Boss's Orders, ...).
    """
    if not deck_dict or not cards:
        return False
    known = sum(1 for c in cards if c in deck_dict)
    return known * 2 > len(cards)


def _apply_event(ev, tu, st):
    """Mutate hand/discard/deck for the target user based on one event."""
    text, cards, level = ev['text'], ev['cards'], ev['level']

    # ---- Effects the OPPONENT forces on us ----
    # PTCGL credits the acting player ("Tigrão95 shuffled 9 cards into their deck")
    # even when the cards moved are ours. If the revealed cards are in our decklist,
    # the movement happened to OUR deck/hand.
    if tu not in text and cards and _cards_belong_to_target(cards, st['deck_dict']):
        m = re.search(r"^.+? shuffled (\d+) cards into their deck\.$", text)
        if m:
            st['deck'] += int(m.group(1))
            for c in cards:
                _remove_one(st['hand'], c)
            return
        m = re.search(r"^.+? drew (\d+) cards\.$", text)
        if m:
            st['deck'] -= int(m.group(1))
            st['hand'].extend(cards)
            return

    if tu not in text:
        return
    esc = re.escape(tu)

    # ---- Setup: opening hand (and the 6 prizes that are never logged) ----
    if re.search(fr"^{esc} drew \d+ cards for the opening hand", text):
        st['deck'] -= 7
        if not st['prizes_placed']:
            st['deck'] -= 6
            st['prizes_placed'] = True
        st['expect_opening'] = True
        return

    # ---- Mulligan: hand is shuffled back and redrawn (new hand is not logged).
    #      Logged as "took a mulligan" or "took 2 mulligans".
    if re.search(fr"^{esc} took (?:a|\d+) mulligans?", text):
        st['hand'].clear()
        st['expect_opening'] = False
        return

    # ---- Draws that go straight to the Bench (never touch the hand) ----
    m = re.search(fr"^{esc} drew (\d+) cards? and played (?:them|it) to the Bench", text)
    if m:
        st['deck'] -= int(m.group(1))
        return
    if re.search(fr"^{esc} drew \(.+?\) .+? and played it to the Bench", text):
        st['deck'] -= 1
        return

    # ---- Draws into hand ----
    m = re.search(fr"^{esc} drew (\d+) cards?\.$", text)
    if m:
        st['deck'] -= int(m.group(1))
        st['hand'].extend(cards)          # names arrive on the bullet line
        return
    m = re.search(fr"^{esc} drew (\(.+?\) .+?)\.$", text)
    if m:
        st['deck'] -= 1
        st['hand'].append(clean_card_name(m.group(1)))
        return
    if re.search(fr"^{esc} drew a card\.$", text):
        st['deck'] -= 1
        return

    # ---- Cards added to hand (prize taken, Night Stretcher, Miracle Headset) ----
    m = re.search(fr"^(\(.+?\) .+?) was added to {esc}'s hand\.$", text)
    if m:
        st['hand'].append(clean_card_name(m.group(1)))
        return
    m = re.search(fr"^{esc} moved {esc}'s (\(.+?\) .+?) to their hand\.$", text)
    if m:
        card = clean_card_name(m.group(1))
        st['hand'].append(card)
        _remove_one(st['discard'], card)   # usually recovered from the discard pile
        return
    if re.search(fr"^{esc} moved {esc}'s \d+ cards to their hand\.$", text):
        for c in cards:
            st['hand'].append(c)
            _remove_one(st['discard'], c)
        return

    # ---- Shuffles back into the deck ----
    m = re.search(fr"^{esc} shuffled (\d+) cards into their deck\.$", text)
    if m:
        st['deck'] += int(m.group(1))
        for c in cards:
            if not _remove_one(st['hand'], c):
                _remove_one(st['discard'], c)   # e.g. Sacred Ash pulls from the discard
        return
    m = re.search(fr"^{esc} shuffled (\(.+?\) .+?) into their deck\.$", text)
    if m:
        st['deck'] += 1
        _remove_one(st['hand'], clean_card_name(m.group(1)))
        return
    if re.search(fr"^{esc} shuffled their hand into their deck", text):
        st['deck'] += len(st['hand'])
        st['hand'].clear()
        return

    # ---- Put back on the deck ----
    m = re.search(fr"^{esc} put (\(.+?\) .+?) on (?:top|the bottom) of their deck", text)
    if m:
        st['deck'] += 1
        _remove_one(st['hand'], clean_card_name(m.group(1)))
        return

    # ---- Discards ----
    if re.search(fr"^{esc} discarded their hand", text):
        st['discard'].extend(st['hand'])   # our own hand: always ours
        st['hand'].clear()
        return
    m = re.search(fr"^{esc} discarded (\d+) cards\.$", text)
    if m:
        for c in cards:
            _remove_one(st['hand'], c)
            _add_discard(st, c)
        return
    m = re.search(fr"^{esc} discarded (\(.+?\) .+?)\.$", text)
    if m:
        card = clean_card_name(m.group(1))
        _remove_one(st['hand'], card)
        _add_discard(st, card)
        return

    # ---- Knock Outs (the Pokemon and everything attached go to the discard) ----
    m = re.search(fr"^{esc}'s (\(.+?\) .+?) was Knocked Out", text)
    if m:
        _add_discard(st, clean_card_name(m.group(1)))
        return
    if re.search(fr"^\d+ cards were discarded from {esc}'s ", text):
        for c in cards:
            _add_discard(st, c)
        return
    m = re.search(fr"^(\(.+?\) .+?) was discarded from {esc}'s ", text)
    if m:
        _add_discard(st, clean_card_name(m.group(1)))
        return

    # ---- Plays from hand. A "(code)" prefix means it came from hand;
    #      "played Academy at Night." (no prefix) is using a Stadium already in play.
    m = re.search(fr"^{esc} played (\(.+?\) .+?)(?: to the (?:Bench|Active Spot|Stadium spot))?\.$", text)
    if m:
        _remove_one(st['hand'], clean_card_name(m.group(1)))
        return

    # ---- Attachments. Top-level = from hand; sub-events come from deck/discard. ----
    m = re.search(fr"^{esc} attached (\(.+?\) .+?) to ", text)
    if m and level == 'top':
        _remove_one(st['hand'], clean_card_name(m.group(1)))
        return

    # ---- Evolutions: the evolution card comes from hand ----
    m = re.search(fr"^{esc} evolved \(.+?\) .+? to (\(.+?\) .+?) (?:on the Bench|in the Active Spot)\.$", text)
    if m:
        _remove_one(st['hand'], clean_card_name(m.group(1)))
        return


def parse_game(log_text, target_user, deck_dict=None, target_card="None", manual_outs=1):
    """Parse a full battle log into per-turn records for the target player.

    Each turn: number, player, is_me, actions, score, and snapshots of the
    hand / discard / deck size / outs taken at that player's decision point.
    """
    deck_dict = deck_dict or {}
    events = parse_events(log_text)

    st = {'hand': [], 'discard': [], 'deck': 60, 'deck_dict': deck_dict,
          'prizes_placed': False, 'expect_opening': False}

    turns, cur = [], None
    my_turn_count = 0
    target_card_seen = 0

    for ev in events:
        text, cards = ev['text'], ev['cards']

        # The opening hand arrives as "- 7 drawn cards." + a bullet list.
        if st['expect_opening'] and re.match(r"^\d+ drawn cards\.$", text):
            st['hand'].extend(cards)
            st['expect_opening'] = False
            if cur is None:
                pass
            continue

        # Track how many copies of the tracked card we've seen (for draw odds).
        if target_card != "None" and target_user in text:
            target_card_seen += cards.count(target_card)
            if target_card in clean_card_name(text):
                target_card_seen += 1

        # ---- Turn header ----
        m = re.match(r"^(.+?)'s Turn$", text)
        if m:
            if cur:
                turns.append(cur)
            p_name = m.group(1).strip()
            is_me = (p_name == target_user)
            if is_me:
                my_turn_count += 1
            outs = (max(0, deck_dict.get(target_card, 0) - target_card_seen)
                    if target_card != "None" else manual_outs)
            cur = {
                "number": len(turns) + 1,
                "player_turn_num": my_turn_count if is_me else 0,
                "player": p_name, "is_me": is_me, "actions": [], "score": 0.0,
                "hand_snapshot": list(st['hand']),
                "discard_snapshot": list(st['discard']),
                "deck_snapshot": st['deck'],
                "outs_snapshot": outs,
                "snapshot_locked": False,
            }
            continue

        # ---- Apply state changes ----
        _apply_event(ev, target_user, st)
        # A negative deck is never a real game state; it means an effect moved cards
        # we couldn't attribute (see _cards_belong_to_target — needs a decklist).
        st['deck'] = max(0, st['deck'])

        if cur is None:
            continue

        cur["actions"].append(ev['raw'])

        # Momentum scoring
        if re.search(fr"{re.escape(cur['player'])} took (?:a|\d+) Prize card", text):
            n = 1
            mm = re.search(r"took (\d+) Prize", text)
            if mm:
                n = int(mm.group(1))
            cur["score"] += (4.0 * n) if cur["is_me"] else (-4.0 * n)
        if text.startswith(f"{cur['player']} attached") and cur["is_me"]:
            cur["score"] += 0.5

        # Keep the snapshot current through the draw step, then lock it at the
        # first real decision so the AI sees the hand the player decided from.
        if cur["is_me"] and not cur["snapshot_locked"]:
            if " drew " in text:
                cur["hand_snapshot"] = list(st['hand'])
                cur["discard_snapshot"] = list(st['discard'])
                cur["deck_snapshot"] = st['deck']
            if any(f"{target_user} {a}" in text for a in _LOCK_ACTIONS) or "used" in text:
                cur["snapshot_locked"] = True

    if cur:
        turns.append(cur)
    return turns


# --------------------------------------------------------------------------
# Board-state reconstruction (for a visual replay)
#
# Rebuilds the full board for BOTH players after each turn: active + benched
# Pokemon, energy counts, damage, prizes remaining. Positions / energy / prizes
# are reliable; damage is best-effort (prevention, weakness and healing make
# exact HP hard, so treat damage as approximate).
#
# The load-bearing signal is "<player>'s (code) Name is now in the Active Spot",
# which the log emits after every retreat / gust / KO-replacement / switch — so
# we don't have to model each of those separately.
# --------------------------------------------------------------------------
_CODE = r'[a-z0-9][a-z0-9_\-]*'


def _new_board():
    return {"active": None, "bench": [], "prizes": 6}


def _mk_poke(code, name):
    return {"code": code, "name": clean_card_name(name) or name.strip(),
            "energy": 0, "damage": 0}


def _board_find(board, code):
    """Find a Pokemon by set-code on a board. Returns (poke, 'active'|'bench'|None)."""
    if board["active"] and board["active"]["code"] == code:
        return board["active"], "active"
    for p in board["bench"]:
        if p["code"] == code:
            return p, "bench"
    return None, None


def _find_anywhere(boards, code):
    for owner, b in boards.items():
        p, loc = _board_find(b, code)
        if p:
            return owner, p, loc
    return None, None, None


def _copy_board(b):
    return {
        "active": dict(b["active"]) if b["active"] else None,
        "bench": [dict(p) for p in b["bench"]],
        "prizes": b["prizes"],
    }


def _set_active(board, code, name):
    """Make the coded Pokemon this board's active; bench the current active."""
    poke, loc = _board_find(board, code)
    if loc == "bench":
        board["bench"].remove(poke)
    if poke is None:
        poke = _mk_poke(code, name)
    if board["active"] and board["active"]["code"] != code:
        board["bench"].append(board["active"])
    board["active"] = poke


def _apply_board_event(ev, boards, ctx):
    """Mutate both boards for one event. ctx carries cross-event state:
      ctx['last_dmg'] = (owner, code, amount) to undo on a 'prevented' line;
      ctx['last_used'] = code of the last Pokemon that used an attack/ability
                         (so a self-shuffle like Run Away Draw / Teleporter can be
                         removed from the board, unlike Sacred Ash's discard-shuffle).
    """
    text, cards = ev['text'], ev['cards']

    # Remember which Pokemon just used something (source of any following effect).
    um = re.search(fr"'s \(({_CODE})\) .+? used ", text)
    if um:
        ctx['last_used'] = um.group(1)

    # --- Placement ---
    m = re.match(fr"^(.+?) played \(({_CODE})\) (.+?) to the Active Spot\.$", text)
    if m:
        boards.setdefault(m.group(1), _new_board())["active"] = _mk_poke(m.group(2), m.group(3))
        return
    m = re.match(fr"^(.+?) played \(({_CODE})\) (.+?) to the Bench\.$", text)
    if m:
        boards.setdefault(m.group(1), _new_board())["bench"].append(_mk_poke(m.group(2), m.group(3)))
        return
    # "X drew N cards and played them to the Bench" (+ bullet list) / single-card form
    m = re.match(fr"^(.+?) drew \d+ cards? and played (?:them|it) to the Bench", text)
    if m and (ev.get("card_tokens") or cards):
        b = boards.setdefault(m.group(1), _new_board())
        tokens = ev.get("card_tokens") or [("", nm) for nm in cards]
        for code, nm in tokens:
            b["bench"].append(_mk_poke(code, nm))
        return
    m = re.match(fr"^(.+?) drew \(({_CODE})\) (.+?) and played it to the Bench", text)
    if m:
        boards.setdefault(m.group(1), _new_board())["bench"].append(_mk_poke(m.group(2), m.group(3)))
        return

    # --- Evolution (keep the energy/damage already on the Pokemon) ---
    m = re.match(fr"^(.+?) evolved \(({_CODE})\) .+? to \(({_CODE})\) (.+?) (?:on the Bench|in the Active Spot)\.$", text)
    if m:
        owner, from_code, to_code, to_name = m.group(1), m.group(2), m.group(3), m.group(4)
        poke, _ = _board_find(boards.setdefault(owner, _new_board()), from_code)
        if poke:
            poke["code"], poke["name"] = to_code, clean_card_name(to_name) or to_name
        return

    # --- Energy attach (Tools are attached too; only count Energy cards) ---
    m = re.match(fr"^(.+?) attached \(({_CODE})\) (.+?) to \(({_CODE})\) (.+?)(?: in the Active Spot| on the Bench)?\.$", text)
    if m and "Energy" in m.group(3):
        poke, _ = _board_find(boards.setdefault(m.group(1), _new_board()), m.group(4))
        if poke:
            poke["energy"] += 1
        return

    # --- Energy / card discarded from a specific Pokemon ---
    m = re.match(fr"^\(({_CODE})\) (.+?) was discarded from (.+?)'s \(({_CODE})\) (.+?)$", text)
    if m and "Energy" in m.group(2):
        poke, _ = _board_find(boards.setdefault(m.group(3), _new_board()), m.group(4))
        if poke and poke["energy"] > 0:
            poke["energy"] -= 1
        return

    # --- Damage from an attack ("... on Y's (code) Target for N damage") ---
    m = re.search(fr"'s \({_CODE}\) .+? used .+? on (.+?)'s \(({_CODE})\) (.+?) for (\d+) damage", text)
    if m:
        owner, code, dmg = m.group(1), m.group(2), int(m.group(4))
        poke, _ = _board_find(boards.setdefault(owner, _new_board()), code)
        if poke:
            poke["damage"] += dmg
            ctx['last_dmg'] = (owner, code, dmg)
        return

    # --- Weakness bonus ("... took N more damage because of ... Weakness") ---
    m = re.search(fr"'s \(({_CODE})\) .+? took (\d+) more damage because of", text)
    if m and ctx.get('last_dmg'):
        owner = ctx['last_dmg'][0]
        poke, _ = _board_find(boards.get(owner, _new_board()), m.group(1))
        if poke:
            poke["damage"] += int(m.group(2))
        return

    # --- Damage prevented (e.g. Battle Cage) — undo the damage we just applied ---
    m = re.match(fr"^Damage to \(({_CODE})\) .+? was prevented", text)
    if m and ctx.get('last_dmg'):
        owner, code, amount = ctx['last_dmg']
        if code == m.group(1):
            poke, _ = _board_find(boards.get(owner, _new_board()), code)
            if poke:
                poke["damage"] = max(0, poke["damage"] - amount)
        ctx['last_dmg'] = None
        return

    # --- Damage counters placed by an effect (find the Pokemon by code anywhere) ---
    m = re.match(fr"^(.+?) put (\d+) damage counters on .+?'s \(({_CODE})\) (.+?)$", text)
    if m:
        _, poke, _ = _find_anywhere(boards, m.group(3))
        if poke:
            poke["damage"] += int(m.group(2)) * 10
        return

    # --- A Pokemon shuffled back into the deck (Run Away Draw / Teleporter). Only
    #     remove the Pokemon that JUST used an ability — that's the self-shuffle.
    #     Discard-recovery (Sacred Ash) also shuffles into the deck but pulls from the
    #     discard, so it must not touch the board; its cards weren't 'used'. ---
    m = re.match(r"^(.+?) shuffled \d+ cards? into their deck", text)
    if m and ctx.get('last_used'):
        b = boards.setdefault(m.group(1), _new_board())
        codes = {c for c, _ in ev.get("card_tokens", [])}
        if ctx['last_used'] in codes:
            poke, loc = _board_find(b, ctx['last_used'])
            if loc == "active":
                b["active"] = None
            elif loc == "bench":
                b["bench"].remove(poke)
            ctx['last_used'] = None
        return

    # --- Knock Out: remove the Pokemon (and everything on it) ---
    m = re.match(fr"^(.+?)'s \(({_CODE})\) (.+?) was Knocked Out", text)
    if m:
        b = boards.setdefault(m.group(1), _new_board())
        poke, loc = _board_find(b, m.group(2))
        if loc == "active":
            b["active"] = None
        elif loc == "bench":
            b["bench"].remove(poke)
        return

    # --- Authoritative active-setter (handles retreat / gust / switch / KO-replace) ---
    m = re.match(fr"^(.+?)'s \(({_CODE})\) (.+?) is now in the Active Spot\.$", text)
    if m:
        _set_active(boards.setdefault(m.group(1), _new_board()), m.group(2), m.group(3))
        return

    # --- Prizes remaining ---
    m = re.match(r"^(.+?) took (a|\d+) Prize card", text)
    if m:
        n = 1 if m.group(2) == "a" else int(m.group(2))
        b = boards.setdefault(m.group(1), _new_board())
        b["prizes"] = max(0, b["prizes"] - n)
        return


def parse_board_states(log_text):
    """Reconstruct the board after each turn. Returns a list of snapshots:
        {"number", "player", "boards": {player_name: board_copy, ...}}
    where board_copy = {"active": poke|None, "bench": [poke], "prizes": int}
    and poke = {"code", "name", "energy", "damage"}.
    """
    players = detect_players(log_text)
    if len(players) < 2:
        return []
    boards = {p: _new_board() for p in players}
    ctx = {'last_dmg': None, 'last_used': None}

    snapshots = []
    cur = None
    turn_no = 1
    for ev in parse_events(log_text):
        m = re.match(r"^(.+?)'s Turn$", ev['text'])
        if m:
            if cur:
                snapshots.append({"number": cur["number"], "player": cur["player"],
                                  "boards": {p: _copy_board(boards[p]) for p in players}})
            cur = {"number": turn_no, "player": m.group(1).strip()}
            turn_no += 1
            continue
        _apply_board_event(ev, boards, ctx)
        # Enforce the real 5-card bench cap. This is a legal invariant (you can never
        # have >5 benched) and it also bounds instance-tracking drift on decks that
        # cycle many same-named Basics (e.g. a 4-Dunsparce Run Away Draw engine),
        # where duplicate set-codes make exact composition best-effort.
        for b in boards.values():
            if len(b["bench"]) > 5:
                del b["bench"][:len(b["bench"]) - 5]

    if cur:
        snapshots.append({"number": cur["number"], "player": cur["player"],
                          "boards": {p: _copy_board(boards[p]) for p in players}})
    return snapshots
