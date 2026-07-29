import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import anthropic
import re
import requests
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sys
import subprocess
import hashlib
import datetime
from collections import Counter

# Pure log-parsing lives in ptcg_parser.py so it can be unit-tested without
# Streamlit (see tests/test_parser.py).
from ptcg_parser import (
    clean_card_name, parse_decklist, calculate_odds, track_prizes,
    detect_opponent_deck, detect_result, count_prizes, parse_game,
    extract_card_names, parse_board_states,
)

# --- 1. LOCAL STORAGE SETUP (Deck Manager) ---
DECKS_FILE = "saved_decks.json"

def load_saved_decks():
    if os.path.exists(DECKS_FILE):
        with open(DECKS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_deck(deck_name, deck_text):
    decks = load_saved_decks()
    decks[deck_name] = deck_text
    with open(DECKS_FILE, "w") as f:
        json.dump(decks, f, indent=4)

def delete_deck(deck_name):
    decks = load_saved_decks()
    if deck_name in decks:
        del decks[deck_name]
        with open(DECKS_FILE, "w") as f:
            json.dump(decks, f, indent=4)

# --- 1b. GAME HISTORY (cross-game weakness tracking) ---
HISTORY_FILE = "game_history.json"

# Fixed taxonomy so weaknesses are aggregatable across games (not free text).
WEAKNESS_CATEGORIES = [
    "Sequencing", "Energy Management", "Prize Trading", "Bench Management",
    "Overextension", "Resource Management", "Setup / Mulligan", "Target Selection",
]

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=4)

def append_game(record):
    """Add a game record, replacing any existing one with the same id (dedup on re-analysis)."""
    hist = [g for g in load_history() if g.get("id") != record.get("id")]
    hist.append(record)
    save_history(hist)

def clear_history():
    save_history([])

# --- 2. PREMIUM UI / UX SETUP ---
def setup_ui():
    st.set_page_config(page_title="PTCGL Grandmaster", layout="wide", page_icon="🔮")
    custom_css = """
    <style>
        .stApp { background: linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%); }
        p, span, h1, h2, h3, h4, h5, h6, label, li { color: #e2f3f5 !important; }
        [data-testid="stSidebar"] { background: rgba(15, 12, 41, 0.4) !important; backdrop-filter: blur(15px) !important; border-right: 1px solid rgba(102, 252, 241, 0.15) !important; }
        .stTextArea textarea, .stTextInput input, .stSelectbox div[data-baseweb="select"], .stNumberInput input { background-color: rgba(0, 0, 0, 0.4) !important; color: #ffffff !important; border: 1px solid rgba(102, 252, 241, 0.3) !important; border-radius: 8px !important; }
        .stTextArea textarea:focus, .stTextInput input:focus, .stSelectbox div[data-baseweb="select"]:focus { border: 1px solid #66fcf1 !important; box-shadow: 0 0 8px rgba(102, 252, 241, 0.6) !important; }
        [data-testid="stExpander"] { background: rgba(255, 255, 255, 0.05) !important; border-radius: 10px !important; border: 1px solid rgba(255, 255, 255, 0.1) !important; margin-bottom: 10px !important; }
        [data-testid="stExpander"] details summary:focus, [data-testid="stExpander"] details summary:active { outline: none !important; box-shadow: none !important; background: transparent !important;}
        [data-testid="stExpander"] details summary { color: #66fcf1 !important; font-weight: 600; }
        .ai-box { background: rgba(31, 60, 136, 0.4); border-left: 4px solid #66fcf1; padding: 15px; border-radius: 8px; margin-bottom: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.5); backdrop-filter: blur(10px); color: #ffffff !important; font-size: 1.05rem; }
        .hand-box { background: rgba(45, 40, 62, 0.6); border: 1px solid #4a47a3; padding: 10px; border-radius: 8px; margin-bottom: 15px; font-family: monospace; color: #e2f3f5 !important; }
        .stButton > button[kind="primary"] { background: linear-gradient(90deg, #4a47a3 0%, #66fcf1 100%) !important; color: #0b0c10 !important; font-weight: bold !important; border: none !important; border-radius: 8px !important; transition: all 0.3s ease !important; padding: 10px 24px !important; }
        .stButton > button[kind="primary"]:hover { transform: scale(1.02); box-shadow: 0 0 15px rgba(102, 252, 241, 0.5) !important; }
        div[data-testid="stMetricValue"] { color: #66fcf1 !important; }
        /* Lock the app to its dark look: hide the toolbar/menu that exposes the Light/Dark theme switcher. */
        [data-testid="stToolbar"] { display: none !important; }
        #MainMenu { visibility: hidden !important; }
        header[data-testid="stHeader"] { background: transparent !important; }
        footer { visibility: hidden !important; }
    </style>
    """
    st.markdown(custom_css, unsafe_allow_html=True)

# --- 3. WEAKNESS TAGGING FALLBACK ---
# Keyword map used only as a FALLBACK when the AI doesn't emit weakness tags.
_WEAKNESS_KEYWORDS = [
    (["sequenc", "order", "before deck-search", "search first", "thin your deck", "draw supporter", "play order"], "Sequencing"),
    (["energy", "attach"], "Energy Management"),
    (["prize", "trade"], "Prize Trading"),
    (["bench"], "Bench Management"),
    (["overextend", "over-extend", "boss", "gust", "expose"], "Overextension"),
    (["discard", "over-draw", "overdraw", "resource", "wasted"], "Resource Management"),
    (["setup", "mulligan", "opening"], "Setup / Mulligan"),
    (["target", "wrong pokemon", "wrong pokémon", "focus fire"], "Target Selection"),
]

def derive_weakness_tags(advice_dict):
    """Fallback: infer weakness categories from the AI's per-turn advice text via keywords."""
    text = " ".join(v["text"].lower() for v in advice_dict.values() if "optimal" not in v["text"].lower())
    tags = []
    for keys, cat in _WEAKNESS_KEYWORDS:
        if any(k in text for k in keys) and cat not in tags:
            tags.append(cat)
    return tags

LESSON_FIELDS = ("LESSON_TURN", "SITUATION", "YOU_PLAYED", "COST",
                 "BETTER_LINE", "PRINCIPLE", "TELL")

def parse_lesson(full_text):
    """Extract the structured teaching moment. Tolerant of multi-line values and
    of the model omitting fields; returns {} if there's no usable lesson."""
    m = re.search(r"LESSON_START(.*?)LESSON_END", full_text, re.DOTALL)
    if not m:
        return {}
    out, cur = {}, None
    for line in m.group(1).split("\n"):
        s = line.strip()
        if not s:
            continue
        head = s.split(":", 1)
        if len(head) == 2 and head[0].strip().upper() in LESSON_FIELDS:
            cur = head[0].strip().upper()
            out[cur] = head[1].strip()
        elif cur:
            out[cur] += " " + s          # continuation of a wrapped value
    lesson = {k.lower(): v.strip() for k, v in out.items() if v.strip()}
    # Drop any leftover template placeholders if the model echoed the format.
    return {k: v for k, v in lesson.items() if not v.startswith("<")}

def build_weakness_evidence(tags, tag_turns, advice_dict):
    """Map each flagged weakness -> the specific turns that show it, so My Progress
    can drill from a trend down to the actual moments it happened.

    Uses the AI's per-category turn numbers; falls back to keyword-matching the
    advice text when the model didn't supply them.
    """
    evidence = {}
    for cat in tags:
        turn_nums = list(tag_turns.get(cat, []))
        if not turn_nums:
            keys = next((k for k, c in _WEAKNESS_KEYWORDS if c == cat), [])
            turn_nums = [n for n, v in advice_dict.items()
                         if "optimal" not in v["text"].lower()
                         and any(k in v["text"].lower() for k in keys)]
        items = []
        for n in sorted(set(turn_nums)):
            v = advice_dict.get(n)
            if v:
                items.append({"turn": n, "card": v.get("card", "None"), "text": v["text"]})
        if items:
            evidence[cat] = items
    return evidence

# --- 3b. CLIPBOARD IMPORT (one-click import from PTCG Live) ---
# PTCG Live does NOT write battle logs to disk (its Player.log is Unity engine
# diagnostics only), so the clipboard is the only automation surface: the game's
# "Copy Battle Log" button is the hand-off point.
# NOTE: this reads the clipboard of the machine running Streamlit. That's your own
# machine when running locally; it would not work on a remotely-hosted deployment.
LAST_PLAYER_FILE = "last_player.json"

def read_clipboard():
    """Read the system clipboard. Returns '' if unavailable."""
    try:
        if sys.platform == "darwin":
            return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout
        if sys.platform == "win32":
            return subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                                  capture_output=True, text=True, timeout=5).stdout
        for cmd in (["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]):
            try:
                return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
            except FileNotFoundError:
                continue
    except Exception:
        return ""
    return ""

def looks_like_battle_log(text):
    """Cheap sanity check so we don't load whatever else happens to be copied."""
    if not text or len(text) < 200:
        return False
    if not re.search(r"'s Turn", text):
        return False
    return any(k in text for k in ("opening hand", "drew", "Prize card", "Active Spot"))

def load_last_player():
    try:
        with open(LAST_PLAYER_FILE) as f:
            return json.load(f).get("player", "")
    except Exception:
        return ""

def save_last_player(name):
    try:
        with open(LAST_PLAYER_FILE, "w") as f:
            json.dump({"player": name}, f)
    except Exception:
        pass

# --- 4. CARD DATA LOOKUPS ---
def _pokemontcg_cards(params):
    """One pokemontcg.io /cards request with retries (the free API is flaky)."""
    for attempt in range(3):
        try:
            r = requests.get("https://api.pokemontcg.io/v2/cards", params=params, timeout=15)
            return r.json().get("data", [])
        except Exception:
            if attempt == 2:
                return []
    return []

def _code_to_set_number(code):
    """PTCGL set-code -> (set_id, number). '(sv5_129)' -> ('sv5', '129').
    Format is <set>_<number>[_suffix]; the set part may contain hyphens."""
    if not code:
        return None, None
    parts = code.split("_")
    if len(parts) < 2:
        return None, None
    num = re.match(r"\d+", parts[1])
    return (parts[0], num.group()) if num else (None, None)

@st.cache_data(ttl=3600)
def get_card_meta(card_name, code=None):
    """Return (image_url, regulation_mark). If a PTCGL set-code is given, look up
    the EXACT printing that was played (set + number) so the replay shows the right
    card art — not just the newest printing that shares the name. Falls back to a
    name search when the code's set isn't in the database."""
    if not card_name or card_name.lower() == "none":
        return None, None
    # 1) exact card by set + number
    setid, num = _code_to_set_number(code)
    if setid and num:
        data = _pokemontcg_cards({"q": f"set.id:{setid} number:{num}", "pageSize": 1})
        if data:
            return data[0].get("images", {}).get("small"), data[0].get("regulationMark")
    # 2) fall back to the newest printing by name
    clean_name = re.sub(r'[^a-zA-Z0-9\s\'-]', '', card_name).strip()
    data = _pokemontcg_cards({"q": f'name:"{clean_name}"', "orderBy": "-set.releaseDate", "pageSize": 1})
    if data:
        return data[0].get("images", {}).get("small"), data[0].get("regulationMark")
    return None, None

def get_card_image(card_name, code=None):
    return get_card_meta(card_name, code)[0]

def _name_candidates(*raws):
    """Generate progressively looser name variants to try against the card DB —
    handles two-Pokemon deck names, trailing qualifiers, and suffixed forms."""
    cands = []
    for raw in raws:
        if not raw:
            continue
        n = re.sub(r'\(.*?\)', '', raw).strip()  # drop parentheticals
        if n:
            cands.append(n)
        # Keep the name up to and including a suffix token (ex / V / VSTAR / VMAX / GX / Radiant).
        m = re.search(r'^(.*?\b(?:ex|GX|V|VSTAR|VMAX|Radiant)\b)', n, re.IGNORECASE)
        if m:
            cands.append(m.group(1).strip())
        words = n.split()
        if len(words) >= 3:
            cands.append(" ".join(words[:3]))
        if len(words) >= 2:
            cands.append(" ".join(words[:2]))
        if words:
            cands.append(words[0])
    seen, out = set(), []
    for c in cands:
        k = c.lower()
        if c and k not in seen:
            seen.add(k)
            out.append(c)
    return out

def resolve_card_visual(key_card, deck_name=""):
    """Best-effort (image_url, regulation_mark) for a tier. Tries the key card first,
    then looser variants derived from the key card and deck name. The regulation mark
    is taken from the actual key card when available (most accurate for legality)."""
    img, mark = get_card_meta(key_card)
    if img:
        return img, mark
    for cand in _name_candidates(key_card, deck_name):
        alt_img, alt_mark = get_card_meta(cand)
        if alt_img:
            return alt_img, (mark if mark is not None else alt_mark)
    return None, mark

def generate_ai_context(turns):
    context = ""
    prev_opp_cards = []   # Pokemon the opponent used an attack/ability with, last turn
    for t in turns:
        if t['is_me']:
            hand_str = ", ".join(t['hand_snapshot']) if t['hand_snapshot'] else "Empty"
            discard_str = ", ".join(t.get('discard_snapshot', [])) if t.get('discard_snapshot') else "Empty"
            context += f"\nTurn {t['number']} (Player):\n"
            if prev_opp_cards:
                context += (f"⚠️ CARRY-OVER CHECK: last turn the opponent used {', '.join(sorted(set(prev_opp_cards)))}. "
                            f"Check those cards' oracle text for any effect lasting 'during your opponent's next turn' "
                            f"(e.g. Item lock) — if present it restricts YOU this turn.\n")
            context += f"Hand at Start: [{hand_str}]\nDiscard Pile: [{discard_str}]\nActions Taken:\n"
            for act in t['actions']: context += f"- {act}\n"
            prev_opp_cards = []
        else:
            context += f"\nTurn {t['number']} (Opponent):\nActions Taken:\n"
            for act in t['actions']: context += f"- {act}\n"
            # Which of the opponent's Pokemon used something this turn? (effect source)
            prev_opp_cards = [clean_card_name(m) for m in
                              re.findall(r"'s \([^)]*\) (.+?) used ", "\n".join(t['actions']))]
    return context

# --- 4b. CARD ORACLE TEXT (Ground-truth card effects from pokemontcg.io) ---
@st.cache_data(show_spinner=False)
def fetch_card_text(card_name: str) -> str:
    """Fetch a card's oracle text from pokemontcg.io. Cached across reruns.
    Retries because the free API is flaky (intermittent timeouts / 500s)."""
    if not card_name or card_name.lower() == "none":
        return ""
    clean_name = re.sub(r'[^a-zA-Z0-9\s\'-]', '', card_name).strip()
    cards = []
    for attempt in range(3):
        try:
            resp = requests.get(
                "https://api.pokemontcg.io/v2/cards",
                params={"q": f'name:"{clean_name}"', "orderBy": "-set.releaseDate", "pageSize": 1},
                timeout=15,
            )
            cards = resp.json().get("data", [])
            break
        except Exception:
            if attempt == 2:
                return ""
    try:
        if not cards:
            return ""
        c = cards[0]
        parts = [f"{c.get('supertype', '')}"]
        for ab in c.get("abilities", []):
            parts.append(f"Ability [{ab.get('name', '')}]: {ab.get('text', '')}")
        for at in c.get("attacks", []):
            cost = "".join(at.get("cost", []))
            parts.append(f"Attack [{at.get('name', '')}] ({cost}): {at.get('text', '')}")
        for rule in c.get("rules", []):
            parts.append(rule)
        body = " | ".join(p for p in parts if p and p.strip())
        return f"{card_name} — {body}"
    except Exception:
        return ""


def fetch_all_card_text(card_names) -> str:
    """Fetch oracle text for many cards in parallel; returns one reference block."""
    names = sorted(set(n for n in card_names if n))
    if not names:
        return ""
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch_card_text, names))
    return "\n".join(t for t in results if t)

def build_card_reference(deck_dict, log_text=""):
    """Oracle text for BOTH the player's deck and every card seen in the log, so the
    coach knows what the opponent's cards do (e.g. an item-lock attack) — not just
    the player's own deck. Without this the coach is blind to effects the opponent
    imposes on the player's turn."""
    names = set(deck_dict.keys())
    if log_text:
        names |= extract_card_names(log_text)
    return fetch_all_card_text(names)

# --- 4c. LIVE FORMAT LEGALITY (single source of truth for the current rotation) ---
@st.cache_data(ttl=86400, show_spinner=False)
def get_legal_regulation_marks(api_key):
    """Determine which regulation marks are currently legal in Standard, grounded by
    web search. Cached for a day. Returns a set of uppercase letters (empty on failure).
    This is the one source of truth for 'what is legal right now' used across the app."""
    if not api_key:
        return set()
    try:
        client = anthropic.Anthropic(api_key=api_key)
        messages = [{"role": "user", "content": "Which regulation marks (the single letters printed on Pokemon TCG cards, e.g. H, I) are legal in the Standard format RIGHT NOW? Search the web to confirm the current rotation. End your reply with a line in EXACTLY this format and nothing after it: MARKS: <comma-separated letters>"}]
        tools = [{"type": "web_search_20260209", "name": "web_search"}]
        response = None
        for _ in range(6):
            response = client.messages.create(
                model="claude-opus-4-8", max_tokens=1024, tools=tools, messages=messages,
            )
            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue
            break
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        m = re.search(r"MARKS:\s*([A-Za-z,\s]+)", text)
        if m:
            return set(re.findall(r"[A-Za-z]", m.group(1).upper()))
        return set()
    except Exception:
        return set()

def find_rotated_cards(deck_dict, legal_marks):
    """Return [(card, mark), ...] for deck cards whose regulation mark is NOT in the
    current legal set. Cards with no mark (basic Energy, unknown, lookup miss) are skipped
    so we never raise a false alarm on something we couldn't verify."""
    if not deck_dict or not legal_marks:
        return []
    names = list(deck_dict.keys())
    def check(name):
        _, mark = get_card_meta(name)
        return (name, mark)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(check, names))
    return [(n, m) for (n, m) in results if m and m not in legal_marks]

def format_legal_era(legal_marks):
    """Build the 'what is legal' line for coaching prompts from the LIVE regulation marks,
    so the coach never contradicts the rotation checker. Falls back to a safe generic
    line when the marks couldn't be determined."""
    if legal_marks:
        marks = ", ".join(sorted(legal_marks))
        return (f"ERA: Current Standard format. The ONLY legal regulation marks right now are: {marks}. "
                f"Treat any card whose regulation mark is NOT in this list as illegal (rotated) — never recommend it.")
    return ("ERA: Current Standard format (post-rotation). If you are unsure whether a specific card is "
            "still legal, say so rather than assuming.")

# --- 5. ENGINE HEURISTICS ---
def analyze_turn_heuristics(actions, is_me, turn_num, player_turn_num):
    recs = []
    if not is_me: return []
    act_str = " ".join(actions).lower()

    # NOTE: Rotation legality is no longer a hardcoded card list. It is checked at the
    # deck level in the Match Overview tab using live regulation-mark data
    # (see get_legal_regulation_marks / find_rotated_cards).

    if player_turn_num == 1 and any(s in act_str for s in ["ultra ball", "capturing aroma", "evolution"]) and ("vstar" in act_str or "ex" in act_str):
        if "put" in act_str and "hand" in act_str: recs.append("⚙️ **Engine Suggestion:** Game rules forbid Turn 1 evolution; search for Basics instead.")

    draw_idx = min([act_str.find(x) for x in ["research", "carmine", "kieran"] if x in act_str], default=-1)
    search_idx = min([act_str.find(x) for x in ["ball", "poffin", "search"] if x in act_str], default=-1)
    if draw_idx != -1 and search_idx != -1 and search_idx > draw_idx:
        recs.append("⚙️ **Engine Suggestion:** Draw supporter played before deck-search. Thin your deck first to improve draw quality.")

    if "attached" not in act_str and len(actions) > 1 and "didn't take an action" not in act_str:
        recs.append("⚙️ **Engine Suggestion:** No Energy attachment detected this turn. Maintain tempo.")

    return recs

# --- 6. AI ANALYSIS (Hand-Aware & Decklist Locked) ---
def get_advanced_ai_review(turns, target_user, api_key, stats, deck_dict, legal_marks=None, log_text=""):
    if not api_key: return "⚠️ Missing API Key — enter your key in the sidebar.", {}, [], {}, {}
    try:
        client = anthropic.Anthropic(api_key=api_key)

        match_context = generate_ai_context(turns)
        deck_str = ", ".join(deck_dict.keys()) if deck_dict else "Unknown (Only use cards seen in hand)"

        # Oracle text for BOTH players' cards — the opponent's effects matter too.
        card_reference = build_card_reference(deck_dict, log_text)

        system_prompt = f"""You are a World-Class Pokemon TCG Coach reviewing a match.
{format_legal_era(legal_marks)}
CRITICAL RULES FOR ADVICE:
1. READ THE ACTIONS TAKEN CAREFULLY. If the log shows the player already played a specific card this turn, DO NOT tell them to play it again!
2. DO NOT suggest a card if it is not in the Player's Decklist or Hand.
3. A card's effect is defined ONLY by the CARD ORACLE TEXT provided. NEVER guess or rely on memory for what a card does. If a card is not in the oracle text and its effect isn't shown in the log, do not speculate about its effect.
4. Each player turn lists Hand at Start, Discard Pile, and Actions Taken. A card in the DISCARD PILE is NOT playable directly — it can only come back via a recovery card (e.g. energy/Pokemon recovery). Never tell the player to play or use a card that is in their discard pile unless they first recover it. Conversely, do not claim a card is "gone" if recovery cards are available to them.
5. OPPONENT EFFECTS THAT CARRY INTO THE PLAYER'S TURN: the CARD ORACLE TEXT covers the OPPONENT'S cards too. Some opponent attacks/abilities impose a restriction that lasts "during your opponent's next turn" — an Item lock, an Ability lock, "can't retreat", etc. When the match data shows a "⚠️ CARRY-OVER CHECK" note, read the oracle text of the named opponent cards: if one restricts this player, that restriction is ACTIVE this turn. NEVER recommend a play it forbids — e.g. do NOT tell the player to play an Item card on a turn where the opponent's previous attack locked Items. (Trainer cards are typed Item / Supporter / Stadium / Tool in their oracle text; an Item lock only blocks Items.)
6. If you recommend a card, put its exact name in BRACKETS at the start of the line.
7. If their turn was optimal, or if they had no better options in hand, reply with: "[None] Optimal play based on your hand."
8. THE LESSON IS THE MOST IMPORTANT PART OF YOUR OUTPUT. Pick the ONE decision that most changed how this game went and teach it properly — depth on a single decision beats shallow notes on every turn. The PRINCIPLE must be transferable: state it so it applies in a future game with completely different cards. Do not put card names in the PRINCIPLE. If they played cleanly, still pick the most instructive moment and teach why that line was correct.
You are a strict, professional Pokemon TCG coach focusing on the current Standard meta. You NEVER hallucinate cards and ALWAYS read the player's actions before giving advice. Your job is to make the player BETTER, not to narrate what happened."""

        prompt = f"""You are reviewing a match for '{target_user}'.
PLAYER'S EXACT DECKLIST: [{deck_str}]

CARD ORACLE TEXT (the ONLY source of truth for what each card does — includes BOTH the player's cards AND the opponent's):
{card_reference if card_reference else "No oracle text available — reason only from the actions shown in the log."}

Format EXACTLY like this (THIS IS JUST A TEMPLATE, DO NOT COPY THIS TEXT):
SUMMARY_START
**Turning Point:** [Your text]
**Blunder:** [Your text]
**Great Move:** [Your text]
SUMMARY_END

ADVICE_START
Turn 1: [Card Name] Explanation of what they should have done differently.
Turn 2: [None] Optimal play based on your hand.
ADVICE_END

LESSON_START
LESSON_TURN: <the single turn number where this player's decision mattered most>
SITUATION: <what their board and hand looked like at that moment, and what was at stake — 1-2 sentences>
YOU_PLAYED: <what they actually did — 1 sentence>
COST: <what that concretely cost them in tempo, prizes, resources or board position — 1-2 sentences>
BETTER_LINE: <the better play, step by step, naming the actual cards that were in their hand that turn>
PRINCIPLE: <the transferable lesson stated generally, with NO card names, so it applies in future games>
TELL: <a concrete cue for recognizing this same situation in a future game — 1 sentence>
LESSON_END

TAGS_START
<One line per weakness category that genuinely applied this game, written as "Category: turn numbers" where the turn numbers are the ADVICE turns where that weakness showed up. Use ONLY these exact category names: Sequencing, Energy Management, Prize Trading, Bench Management, Overextension, Resource Management, Setup / Mulligan, Target Selection. Leave this section empty if they played cleanly. For example a game with sloppy sequencing on two turns and one energy misplay would be two lines reading "Sequencing: 4, 7" and "Energy Management: 2".>
TAGS_END

MATCH DATA & HAND STATES:
{match_context[:6000]}
"""
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=6000,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}]
        )

        full_text = response.content[0].text.replace("```text", "").replace("```", "")
        summary_match = re.search(r"SUMMARY_START(.*?)SUMMARY_END", full_text, re.DOTALL)
        summary = summary_match.group(1).strip() if summary_match else full_text.split("ADVICE_START")[0].replace("SUMMARY_START", "").strip()
        if not summary.strip(): summary = "⚠️ **App Warning:** AI failed to write global summary."

        advice_dict = {}
        matches = re.findall(r"(?i)\bTurn\s*(\d+)[\s:*=\-]+(?:\[(.*?)\])?\s*(.+)", full_text)
        for turn_num, card_name, text in matches:
            if len(text.strip()) > 2:
                advice_dict[int(turn_num)] = {"card": card_name.strip() if card_name else "None", "text": text.strip()}

        # Parse categorized weakness tags (fixed taxonomy) plus the turns each one
        # occurred on. Only known category names are accepted, so the model can't
        # invent a category. Falls back to keyword inference.
        weakness_tags, tag_turns = [], {}
        tm = re.search(r"TAGS_START(.*?)TAGS_END", full_text, re.DOTALL)
        if tm:
            blob = tm.group(1)
            for line in blob.split("\n"):
                s = line.strip()
                if not s or ":" not in s:
                    continue
                label, rest = s.split(":", 1)
                for cat in WEAKNESS_CATEGORIES:
                    if label.strip().lower() == cat.lower():
                        if cat not in weakness_tags:
                            weakness_tags.append(cat)
                        tag_turns[cat] = [int(x) for x in re.findall(r"\d+", rest)]
                        break
            if not weakness_tags:   # model used the older comma-separated form
                weakness_tags = [c for c in WEAKNESS_CATEGORIES if c.lower() in blob.lower()]
        if not weakness_tags:
            weakness_tags = derive_weakness_tags(advice_dict)

        evidence = build_weakness_evidence(weakness_tags, tag_turns, advice_dict)
        lesson = parse_lesson(full_text)
        return summary, advice_dict, weakness_tags, evidence, lesson
    except Exception as e:
        return f"❌ AI Error: {str(e)}", {}, [], {}, {}

# --- 6b. CONVERSATIONAL COACH (multi-turn follow-up chat) ---
def ask_coach(api_key):
    """Continue a conversation with the coach using the stored match context.
    Reads the running message history and match context from st.session_state."""
    try:
        ctx = st.session_state.coach_context
        client = anthropic.Anthropic(api_key=api_key)

        system_prompt = f"""You are a World-Class Pokemon TCG Coach having a follow-up conversation with the player about a match you just reviewed.
{format_legal_era(ctx.get('legal_marks'))}
You have the full match data, the player's decklist, and oracle text for every card below. Answer their questions about plays, sequencing, matchups, and strategy.
RULES:
- A card's effect is defined ONLY by the CARD ORACLE TEXT below. NEVER guess or rely on memory for what a card does.
- A card listed in a turn's Discard Pile is NOT directly playable; it can only return via a recovery card.
- Reference specific turns and cards from this match. Be concise and concrete.
- '{ctx['target_user']}' is the player you are coaching.

PLAYER'S DECKLIST: [{ctx['deck_str']}]

CARD ORACLE TEXT:
{ctx['card_reference'] if ctx['card_reference'] else 'No oracle text available — reason only from the actions in the log.'}

MATCH DATA & HAND STATES:
{ctx['match_context'][:12000]}
"""

        messages = [{"role": m["role"], "content": m["content"]} for m in st.session_state.coach_messages]
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=2048,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        return response.content[0].text
    except Exception as e:
        return f"❌ Coach Error: {str(e)}"

# --- 6c. DECK FINDER (play-style quiz + tiered deck progression) ---
# Each option maps to one play-style archetype; we tally the picks to find the dominant style.
DECK_FINDER_QUESTIONS = [
    {
        "key": "win_con",
        "q": "What sounds like the most fun way to win?",
        "options": {
            "Hit hard and end the game as fast as possible": "Aggro",
            "Build a powerful engine, then unload one huge turn": "Combo",
            "Grind opponents out of resources and options": "Control",
            "Read the matchup and adapt my plan on the fly": "Midrange",
            "Outlast them until they run out of cards": "Mill",
        },
    },
    {
        "key": "complexity",
        "q": "How do you feel about complex, multi-step turns?",
        "options": {
            "Keep it simple — a few clear, strong plays": "Aggro",
            "I love intricate, multi-step combos": "Combo",
            "Methodical lines that grind value": "Control",
            "Some complexity, but I want flexibility": "Midrange",
        },
    },
    {
        "key": "pace",
        "q": "What game pace do you enjoy?",
        "options": {
            "Fast and explosive": "Aggro",
            "Slow and grindy": "Control",
            "Setup turns that build to a big payoff": "Combo",
            "Depends on the matchup": "Midrange",
        },
    },
    {
        "key": "frustration",
        "q": "What frustrates you most in a game?",
        "options": {
            "Games dragging on forever": "Aggro",
            "Bricking on my combo pieces": "Combo",
            "Getting run over before I set up": "Control",
            "Being locked into a single line of play": "Midrange",
        },
    },
    {
        "key": "interaction",
        "q": "How do you like to interact with your opponent?",
        "options": {
            "Race them — ignore what they're doing": "Aggro",
            "Disrupt and deny their game plan": "Control",
            "Stall and exhaust their deck": "Mill",
            "Focus on executing my own plan": "Combo",
            "Trade efficiently and pivot as needed": "Midrange",
        },
    },
]

ARCHETYPE_BLURBS = {
    "Aggro": "Aggro / Tempo — apply pressure early and close games fast.",
    "Combo": "Combo / Setup — assemble an engine and win with explosive turns.",
    "Control": "Control / Disruption — deny resources and grind opponents down.",
    "Midrange": "Midrange / Toolbox — flexible, adaptable, strong in most matchups.",
    "Mill": "Mill / Stall — defensive attrition; win by outlasting the opponent.",
}

def score_play_style(answers):
    """Tally archetype picks; return (winning_archetype, full_score_dict)."""
    scores = Counter()
    for q in DECK_FINDER_QUESTIONS:
        choice = answers.get(q["key"])
        if choice and choice in q["options"]:
            scores[q["options"][choice]] += 1
    if not scores:
        return "Midrange", scores
    # most_common breaks ties by insertion order, which is fine here
    return scores.most_common(1)[0][0], scores

def get_deck_recommendations(api_key, archetype, experience):
    """Ask Claude for a Beginner -> Intermediate -> Advanced deck progression for the
    determined play style. Uses web search to ground recommendations in the CURRENT
    Standard format instead of relying on (possibly stale) training data.
    Returns a parsed dict (with a 'raw' fallback)."""
    try:
        client = anthropic.Anthropic(api_key=api_key)
        system_prompt = """You are an expert Pokemon TCG deck-building mentor recommending decks for the CURRENT Standard format (post the 2026 rotation).

CRITICAL — your training data is OUT OF DATE about which cards are rotated, so you MUST use the web_search tool before recommending anything. Follow these steps:
1. Search for the EXACT list of regulation marks (the letter printed on each card, e.g. "H", "I", ...) that are legal in Standard RIGHT NOW, and which sets carry them. Establish this legal-marks list first.
2. Search for the current Standard meta / recommended decks that match the player's play style.
3. For EACH of the three decks you are about to recommend, verify that its KEY cards' regulation marks are in the legal list from step 1.

⚠️ THE BEGINNER DECK IS THE MOST COMMON MISTAKE. Cheap, popular "starter" decks are very often built on cards that have ALREADY ROTATED OUT (e.g. older Ancient/Future budget decks). Do NOT assume a deck is legal just because it is famous, cheap, or beginner-friendly. Verify the Beginner deck's key cards' regulation marks just as strictly as the others — if its key cards are rotated, pick a different, currently-legal beginner-friendly deck.

Only recommend decks whose key cards you have CONFIRMED carry a currently-legal regulation mark. Never invent card names. If you cannot confirm a deck is currently legal, replace it with one you can confirm.

You will be given the player's PLAY STYLE and SELF-RATED EXPERIENCE. Recommend a progression of THREE real, currently-legal archetype decks that fit that play style: a Beginner deck (forgiving, easy to pilot), an Intermediate deck (more lines and tech choices), and an Advanced deck (high skill ceiling, rewards mastery). The three should form a natural learning path.

FINAL CHECK before you answer: re-read your three picks and confirm each key card's regulation mark is in the current legal list. If any (especially the Beginner) is rotated, replace it now. Also confirm EACH decklist uses ONLY currently-legal cards and the card counts total EXACTLY 60.

Output your FINAL answer EXACTLY in this format and nothing else (no citations, no extra prose, no search commentary in these lines). First the three TIER summary lines, then a full 60-card DECKLIST block for each tier. In the decklists, write one card per line as "<count> <Card Name>" and make each list's counts total exactly 60:
ARCHETYPE_NAME: <short name of the play style>
ARCHETYPE_DESC: <2 sentences describing how this style plays>
TIER||Beginner||<Deck Name>||<single key Pokemon for an image, exact current card name>||<2-3 sentences: why it fits the style and what core skills it teaches>
TIER||Intermediate||<Deck Name>||<key Pokemon>||<2-3 sentences: what new skills it adds over the beginner deck>
TIER||Advanced||<Deck Name>||<key Pokemon>||<2-3 sentences: why it has a high skill ceiling and what mastery looks like>
DECKLIST||Beginner
<count> <Card Name>
<count> <Card Name>
(... continue until counts total exactly 60 ...)
DECKLIST||Intermediate
<count> <Card Name>
(... total exactly 60 ...)
DECKLIST||Advanced
<count> <Card Name>
(... total exactly 60 ...)"""

        prompt = f"""PLAY STYLE: {archetype} ({ARCHETYPE_BLURBS.get(archetype, '')})
SELF-RATED EXPERIENCE: {experience}

Search for the current Standard format and meta, then recommend the three-deck progression for this player using only currently-legal decks."""

        # Web search is a server-side tool; the model may run several searches and
        # return stop_reason "pause_turn" before finishing — resume until it's done.
        messages = [{"role": "user", "content": prompt}]
        tools = [{"type": "web_search_20260209", "name": "web_search"}]
        response = None
        for _ in range(6):
            response = client.messages.create(
                model="claude-opus-4-8",
                max_tokens=4096,
                system=system_prompt,
                tools=tools,
                messages=messages,
            )
            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue
            break

        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")

        result = {"archetype_name": archetype, "archetype_desc": "", "tiers": [], "raw": text}
        name_match = re.search(r"ARCHETYPE_NAME:\s*(.+)", text)
        if name_match: result["archetype_name"] = name_match.group(1).strip()
        desc_match = re.search(r"ARCHETYPE_DESC:\s*(.+)", text)
        if desc_match: result["archetype_desc"] = desc_match.group(1).strip()

        for line in text.split("\n"):
            if line.strip().startswith("TIER||"):
                parts = [p.strip() for p in line.split("||")]
                if len(parts) >= 5:
                    result["tiers"].append({
                        "level": parts[1], "deck": parts[2],
                        "key_card": parts[3], "text": parts[4],
                    })

        # Parse the per-tier DECKLIST blocks: lines of "<count> <name>" under a
        # "DECKLIST||<Level>" header, until the next header / ARCHETYPE line / EOF.
        decklists = {}
        current_level = None
        for line in text.split("\n"):
            s = line.strip()
            if s.startswith("DECKLIST||"):
                current_level = s.split("||", 1)[1].strip()
                decklists[current_level] = []
            elif s.startswith("TIER||") or s.startswith("ARCHETYPE"):
                current_level = None
            elif current_level and re.match(r"^\s*\d+\s+\S", line):
                decklists[current_level].append(s)
        decklists = {k: "\n".join(v) for k, v in decklists.items()}

        # Independent verification source: confirm legality against live regulation marks.
        legal_marks = get_legal_regulation_marks(api_key)
        result["legal_marks"] = sorted(legal_marks)

        # Attach each decklist to its tier and verify it (60-card total + rotation scan).
        for tier in result["tiers"]:
            dl = decklists.get(tier["level"], "")
            tier["decklist"] = dl
            if dl:
                deck_for_check = parse_decklist(dl)  # Counter {clean_name: count}
                tier["deck_total"] = sum(deck_for_check.values())
                tier["rotated_in_list"] = find_rotated_cards(deck_for_check, legal_marks)
            else:
                tier["deck_total"] = 0
                tier["rotated_in_list"] = []

        return result
    except Exception as e:
        return {"error": f"❌ Deck Finder Error: {str(e)}"}

def copy_button(text, key, label="📋 Copy Decklist"):
    """Render a copy-to-clipboard button via a small HTML component. Uses the async
    clipboard API with a document.execCommand fallback so it works inside the iframe."""
    safe = json.dumps(text)
    btn_id = "copybtn_" + re.sub(r'[^A-Za-z0-9]', '', str(key))
    html = """
    <button id="__ID__" style="background:linear-gradient(90deg,#4a47a3 0%,#66fcf1 100%);color:#0b0c10;font-weight:700;border:none;border-radius:8px;padding:8px 16px;cursor:pointer;font-family:sans-serif;font-size:14px;">__LABEL__</button>
    <script>
      (function(){
        const btn = document.getElementById("__ID__");
        const text = __TEXT__;
        function showCopied(){ const old=btn.innerHTML; btn.innerHTML="✅ Copied!"; setTimeout(function(){btn.innerHTML=old;},1500); }
        function fallback(){ const ta=document.createElement("textarea"); ta.value=text; ta.style.position="fixed"; ta.style.opacity="0"; document.body.appendChild(ta); ta.focus(); ta.select(); try{document.execCommand("copy");}catch(e){} document.body.removeChild(ta); showCopied(); }
        btn.addEventListener("click", function(){
          if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(text).then(showCopied, fallback); }
          else { fallback(); }
        });
      })();
    </script>
    """
    html = html.replace("__ID__", btn_id).replace("__LABEL__", label).replace("__TEXT__", safe)
    components.html(html, height=46)

def render_deck_finder(api_key):
    st.subheader("🧭 Find Your Deck")
    st.caption("Answer a few questions and we'll match you to a play style — then suggest a Beginner → Intermediate → Advanced deck path to grow into it.")

    with st.form("deck_finder_quiz"):
        answers = {}
        for q in DECK_FINDER_QUESTIONS:
            answers[q["key"]] = st.radio(q["q"], list(q["options"].keys()), index=None)
        experience = st.select_slider(
            "How experienced are you with the Pokemon TCG?",
            options=["Brand New", "Casual", "Intermediate", "Competitive"],
            value="Casual",
        )
        submitted = st.form_submit_button("🔍 Find My Deck", type="primary")

    if submitted:
        if any(v is None for v in answers.values()):
            st.warning("Please answer every question first.")
        elif not api_key:
            st.warning("Enter your Anthropic API key in the sidebar to get deck recommendations.")
        else:
            archetype, scores = score_play_style(answers)
            with st.spinner("🧠 Matching your play style and checking the current Standard meta (this may take ~20-30s)..."):
                rec = get_deck_recommendations(api_key, archetype, experience)
            st.session_state.deck_finder = {"archetype": archetype, "scores": dict(scores), "rec": rec, "experience": experience}

    # Render the latest result (persists across reruns)
    df = st.session_state.get("deck_finder")
    if df:
        rec = df["rec"]
        if "error" in rec:
            st.error(rec["error"])
            return

        st.divider()
        st.markdown(f"### 🎯 Your Play Style: {rec.get('archetype_name') or df['archetype']}")
        if rec.get("archetype_desc"):
            st.markdown(f'<div class="ai-box">{rec["archetype_desc"]}</div>', unsafe_allow_html=True)

        # Show how the styles scored, for transparency
        if df.get("scores"):
            chips = "  ".join(f"`{k}: {v}`" for k, v in sorted(df["scores"].items(), key=lambda x: -x[1]))
            st.caption(f"Style match: {chips}")

        if rec.get("tiers"):
            st.markdown("### 📈 Your Deck Progression Path")
            legal_marks = set(rec.get("legal_marks", []))
            level_icon = {"Beginner": "🟢", "Intermediate": "🟡", "Advanced": "🔴"}
            cols = st.columns(len(rec["tiers"]))
            for col, tier in zip(cols, rec["tiers"]):
                with col:
                    icon = level_icon.get(tier["level"], "🎴")
                    st.markdown(f"#### {icon} {tier['level']}")
                    img_url, mark = resolve_card_visual(tier["key_card"], tier.get("deck", ""))
                    if img_url:
                        st.image(img_url, width=140, caption=tier["key_card"])
                    else:
                        st.caption(f"🃏 No card image found for '{tier['key_card']}' (may be too new for the card database).")
                    st.markdown(f"**{tier['deck']}**")

                    # Deterministic legality check on the key card's regulation mark.
                    if legal_marks and mark:
                        if mark in legal_marks:
                            st.success(f"✅ Regulation {mark} — legal")
                        else:
                            st.error(f"⚠️ Regulation {mark} — appears ROTATED. Don't build this one; ask again or verify.")
                    elif legal_marks and not mark:
                        st.caption("ℹ️ Couldn't verify the key card's regulation mark.")

                    st.markdown(tier["text"])

            # Full decklists (full width — easier to read than inside the narrow columns).
            if any(t.get("decklist") for t in rec["tiers"]):
                st.markdown("#### 📋 Sample Decklists")
                for tier in rec["tiers"]:
                    if not tier.get("decklist"):
                        continue
                    total = tier.get("deck_total", 0)
                    head = f"{level_icon.get(tier['level'], '🎴')} {tier['level']} — {tier['deck']}"
                    if total:
                        head += f"  ({total} cards)"
                    with st.expander(head):
                        rot = tier.get("rotated_in_list", [])
                        if rot:
                            st.error("⚠️ Likely-rotated cards in this list: " + ", ".join(f"{n} (Reg {m})" for n, m in rot))
                        if total and total != 60:
                            st.warning(f"This list totals {total} cards — a legal deck is exactly 60. Use it as a starting point and adjust.")
                        copy_button(tier["decklist"], key=tier["level"])
                        st.code(tier["decklist"])

            if legal_marks:
                st.caption(f"Legality verified against current legal regulation marks: {', '.join(sorted(legal_marks))}")
        else:
            # Fallback: model didn't follow the format — show its raw answer
            st.markdown(f'<div class="ai-box">{rec.get("raw", "No recommendation produced.")}</div>', unsafe_allow_html=True)

        st.divider()
        st.caption("⚠️ The competitive meta and card legality shift over time — double-check current Standard legality before investing in a deck.")
        if st.button("↺ Retake Quiz"):
            del st.session_state["deck_finder"]
            st.rerun()

# --- 6d. MY PROGRESS (cross-game weakness tracking) ---
def get_cross_game_plan(api_key, history, weakness_counter, matchups):
    """Turn the aggregated cross-game stats into a short, prioritized improvement plan."""
    try:
        client = anthropic.Anthropic(api_key=api_key)
        total = len(history)
        weak_lines = "\n".join(f"- {k}: flagged in {v} of {total} games" for k, v in weakness_counter.most_common()) or "None recorded"
        mu_lines = "\n".join(
            f"- {opp}: {d['W']}W-{d['L']}L ({d['Games']} games)"
            for opp, d in sorted(matchups.items(), key=lambda x: -x[1]["Games"])
        ) or "None recorded"
        prompt = f"""You are a Pokemon TCG coach reviewing a player's history across {total} analyzed games. Here is their aggregated data.

RECURRING WEAKNESSES:
{weak_lines}

MATCHUP RECORDS:
{mu_lines}

Write a short, prioritized improvement plan (3-4 bullet points maximum). Lead with their single biggest recurring weakness and their worst matchup. Be specific and actionable about HABITS to change, not specific card lists. Do not invent card names."""
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        return f"❌ Error: {str(e)}"

def render_progress(api_key):
    st.subheader("📈 My Progress")
    st.caption("Every game you analyze is saved here so recurring patterns become visible over time.")
    history = load_history()

    if not history:
        st.info("No games tracked yet. Analyze a few matches in the Match Analyzer — each one is saved here automatically to reveal your recurring strengths and weaknesses.")
        return

    total = len(history)
    wins = sum(1 for g in history if g.get("result") == "Win")
    losses = sum(1 for g in history if g.get("result") == "Loss")
    known = wins + losses

    c1, c2 = st.columns(2)
    c1.metric("Games Tracked", total)
    c2.metric("Record (detected)", f"{wins}–{losses}" + (f"  ·  {round(100 * wins / known)}%" if known else ""))

    # --- Recurring weaknesses ---
    st.divider()
    st.subheader("🎯 Recurring Weaknesses")
    counter = Counter()
    for g in history:
        for w in g.get("weaknesses", []):
            counter[w] += 1
    if counter:
        dfw = (pd.DataFrame({"Weakness": list(counter.keys()), "Games Flagged": list(counter.values())})
               .set_index("Weakness").sort_values("Games Flagged", ascending=False))
        st.bar_chart(dfw)
        top = counter.most_common(1)[0]
        st.caption(f"Most frequent: **{top[0]}** — flagged in {top[1]} of {total} games.")

        # ---- Drill-down: from a trend to the actual turns it happened on ----
        st.markdown("##### 🔎 Drill into a weakness")
        options = [w for w, _ in counter.most_common()]
        selected = st.selectbox("See the exact turns where this happened:", options)

        occurrences = []
        for g in history:
            for item in (g.get("evidence", {}) or {}).get(selected, []):
                occurrences.append((g, item))

        if occurrences:
            games_hit = len({g.get("id") for g, _ in occurrences})
            st.caption(f"**{len(occurrences)}** occurrence(s) across **{games_hit}** game(s).")
            for g, item in occurrences:
                card = item.get("card", "None")
                head = f"{g.get('date', '?')} · vs {g.get('opponent_deck', 'Unknown')} · Turn {item['turn']}"
                if card and card.lower() != "none":
                    head += f" · {card}"
                with st.expander(head):
                    st.markdown(f'<div class="ai-box">🧠 {item["text"]}</div>', unsafe_allow_html=True)
                    if card and card.lower() != "none":
                        img = get_card_image(card)
                        if img:
                            st.image(img, width=110)
        else:
            st.caption("No turn-level detail stored for this weakness yet — games analyzed "
                       "before this feature only recorded the category. Re-analyze a game to capture it.")
    else:
        st.caption("No weaknesses flagged yet — either clean play or not enough games.")

    # --- Matchup records ---
    st.divider()
    st.subheader("⚔️ Matchup Records")
    matchups = {}
    for g in history:
        opp = g.get("opponent_deck", "Unknown") or "Unknown"
        d = matchups.setdefault(opp, {"Games": 0, "W": 0, "L": 0})
        d["Games"] += 1
        if g.get("result") == "Win": d["W"] += 1
        elif g.get("result") == "Loss": d["L"] += 1
    rows = []
    for opp, d in sorted(matchups.items(), key=lambda x: -x[1]["Games"]):
        wl = d["W"] + d["L"]
        rows.append({"Opponent": opp, "Games": d["Games"], "W": d["W"], "L": d["L"],
                     "Win %": f"{round(100 * d['W'] / wl)}%" if wl else "—"})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # --- Coach's cross-game read ---
    st.divider()
    st.subheader("🧠 Coach's Cross-Game Read")
    if st.button("Generate my improvement plan", type="primary"):
        if not api_key:
            st.warning("Enter your Anthropic API key in the sidebar first.")
        else:
            with st.spinner("Reviewing your game history..."):
                st.session_state.progress_plan = get_cross_game_plan(api_key, history, counter, matchups)
    if st.session_state.get("progress_plan"):
        st.markdown(f'<div class="ai-box">{st.session_state.progress_plan}</div>', unsafe_allow_html=True)

    # --- Recent games ---
    st.divider()
    st.subheader("🕑 Recent Games")
    for g in list(reversed(history))[:10]:
        wtags = ", ".join(g.get("weaknesses", [])) or "None flagged"
        result = g.get("result", "Unknown")
        badge = {"Win": "🟢", "Loss": "🔴"}.get(result, "⚪")
        st.markdown(
            f"{badge} **{g.get('date', '?')}** — vs {g.get('opponent_deck', 'Unknown')} — "
            f"**{result}** · Weaknesses: {wtags}"
        )

    st.divider()
    if st.button("🗑️ Clear All History"):
        clear_history()
        st.session_state.pop("progress_plan", None)
        st.rerun()

# --- 6e. DECISION DRILL (commit to a play BEFORE seeing the answer) ---
def _lesson_turn_obj(lesson, turns):
    """The player's own turn object referenced by the lesson, or None."""
    m = re.search(r"\d+", lesson.get("lesson_turn", ""))
    if not m:
        return None
    n = int(m.group())
    return next((t for t in turns if t["number"] == n and t.get("is_me")), None)

def can_drill(lesson, turns):
    """A drill needs a strong-line answer and a real own-turn to stage."""
    return bool(lesson.get("better_line") and _lesson_turn_obj(lesson, turns))

def _render_spot(lesson, turn_obj):
    hand = ", ".join(turn_obj["hand_snapshot"]) or "Empty / Unknown"
    disc = ", ".join(turn_obj.get("discard_snapshot", [])) or "Empty"
    st.markdown(
        f'<div class="hand-box">🃏 <b>Your hand:</b> {hand}'
        f'<br>🗑️ <b>Discard:</b> {disc}'
        f'<br>🎴 <b>Deck:</b> {turn_obj["deck_snapshot"]} cards</div>',
        unsafe_allow_html=True)
    if lesson.get("situation"):
        st.markdown(f"**The spot** — {lesson['situation']}")

def _render_lesson_answer(lesson):
    if lesson.get("you_played"):
        st.markdown(f"**What you actually played** — {lesson['you_played']}")
    if lesson.get("cost"):
        st.markdown(f"**What it cost you** — {lesson['cost']}")
    if lesson.get("better_line"):
        st.markdown(f'<div class="ai-box">✅ <b>The strong line</b><br>{lesson["better_line"]}</div>',
                    unsafe_allow_html=True)
    if lesson.get("principle"):
        st.success(f"🧠 **The principle:** {lesson['principle']}")
    if lesson.get("tell"):
        st.info(f"👀 **Spot it next time:** {lesson['tell']}")

def grade_drill(api_key, lesson, turn_obj, user_answer):
    """Grade the player's proposed play against the strong line. Returns
    (verdict, feedback) where verdict is STRONG / PARTIAL / OFF."""
    try:
        client = anthropic.Anthropic(api_key=api_key)
        hand = ", ".join(turn_obj["hand_snapshot"]) or "unknown"
        prompt = f"""You are a Pokemon TCG coach grading a training drill. The player was shown a spot from their OWN game and asked what they'd play, without seeing the answer.

SITUATION: {lesson.get('situation', '')}
THEIR HAND THIS TURN: {hand}
THE STRONG LINE (ideal answer): {lesson.get('better_line', '')}
THE PRINCIPLE BEING TESTED: {lesson.get('principle', '')}

THE PLAYER'S PROPOSED PLAY: "{user_answer}"

Grade how well their proposed play matches the strong line and the principle.
Begin your reply with EXACTLY one of these on its own line:
VERDICT: STRONG
VERDICT: PARTIAL
VERDICT: OFF
Then 2-4 sentences: what they got right, what they missed versus the strong line, and reinforce the principle. Be encouraging, specific, and concrete. Only reference cards in their hand or the situation — do not invent cards."""
        resp = client.messages.create(
            model="claude-opus-4-8", max_tokens=700,
            messages=[{"role": "user", "content": prompt}])
        text = resp.content[0].text
        m = re.search(r"VERDICT:\s*(STRONG|PARTIAL|OFF)", text, re.IGNORECASE)
        verdict = m.group(1).upper() if m else "PARTIAL"
        body = re.sub(r"VERDICT:\s*(STRONG|PARTIAL|OFF)", "", text, count=1, flags=re.IGNORECASE).strip()
        return verdict, body
    except Exception as e:
        return "PARTIAL", f"(Couldn't grade automatically: {e})"

def render_drill(lesson, turns, api_key):
    turn_obj = _lesson_turn_obj(lesson, turns)
    lt = turn_obj["number"]
    st.subheader("🎯 Decision Drill")
    st.caption(f"Turn {lt} was the turning point of this game. Before you see the answer — what would YOU play here?")
    _render_spot(lesson, turn_obj)

    drill = st.session_state.get("drill", {})
    if not drill.get("answered"):
        with st.form("drill_form"):
            ans = st.text_area("Your play — what do you do this turn?",
                               placeholder='Describe your line, e.g. "search first with X to thin, then attach and attack ..."')
            c1, c2 = st.columns(2)
            submitted = c1.form_submit_button("Reveal & grade my play", type="primary")
            skip = c2.form_submit_button("Just show the answer")
        if submitted and ans.strip():
            if not api_key:
                st.warning("Enter your Anthropic API key in the sidebar to grade your answer.")
            else:
                with st.spinner("Grading your decision..."):
                    verdict, body = grade_drill(api_key, lesson, turn_obj, ans)
                st.session_state.drill = {"answered": True, "answer": ans, "verdict": verdict, "grade": body}
                st.rerun()
        elif submitted:
            st.warning("Type what you'd play first.")
        elif skip:
            st.session_state.drill = {"answered": True, "answer": None}
            st.rerun()
    else:
        if drill.get("answer"):
            st.markdown(f"**Your play:** {drill['answer']}")
            v, grade = drill.get("verdict", "PARTIAL"), drill.get("grade", "")
            if v == "STRONG":
                st.success(f"✅ **Strong play.** {grade}")
            elif v == "OFF":
                st.error(f"❌ **Missed it.** {grade}")
            else:
                st.warning(f"⚠️ **Partly there.** {grade}")
        st.divider()
        st.markdown("#### 🎓 The Lesson")
        _render_lesson_answer(lesson)
        if st.button("↺ Try this drill again"):
            st.session_state.drill = {}
            st.rerun()

# --- 6f. VISUAL REPLAY (scrub through the game board turn by turn) ---
def _poke_caption(poke):
    bits = []
    if poke.get("energy"):
        bits.append(f"⚡{poke['energy']}")
    if poke.get("damage"):
        bits.append(f"💥{poke['damage']}")
    return "  ".join(bits)

def _render_poke(poke, width, show_name):
    if not poke:
        st.caption("— empty —")
        return
    img = get_card_image(poke["name"], poke.get("code"))
    if img:
        st.image(img, width=width)
    cap = (poke["name"] + "  " if show_name else "") + _poke_caption(poke)
    st.caption(cap.strip() or poke["name"])

def _render_board(board, title, highlight=False):
    head = f"{'🟢 ' if highlight else ''}**{title}** · 🏆 {board['prizes']} prize(s) left"
    st.markdown(head)
    st.caption("Active")
    _render_poke(board.get("active"), 120, show_name=True)
    bench = board.get("bench", [])
    st.caption(f"Bench ({len(bench)}/5)")
    if bench:
        cols = st.columns(5)
        for i, p in enumerate(bench[:5]):
            with cols[i]:
                _render_poke(p, 70, show_name=False)

def _prewarm_board_images(board_states):
    """Fetch every board Pokemon's image in parallel once (by exact set-code), so the
    per-turn render is all cache hits instead of a dozen sequential flaky API calls."""
    pairs = set()
    for s in board_states:
        for b in s["boards"].values():
            for p in ([b.get("active")] + b.get("bench", [])):
                if p and p.get("name"):
                    pairs.add((p["name"], p.get("code") or ""))
    if pairs:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda nc: get_card_image(nc[0], nc[1] or None), pairs))

def render_replay(a):
    board_states = a.get("board_states") or []
    if not board_states:
        st.info("No board data for this game.")
        return
    target_user = a["target_user"]
    advice_map = a.get("advice_map", {})
    lesson = a.get("lesson") or {}
    lt_m = re.search(r"\d+", lesson.get("lesson_turn", ""))
    lesson_turn = int(lt_m.group()) if lt_m else None

    with st.spinner("Loading card images..."):
        _prewarm_board_images(board_states)

    st.caption("Step through the game. Each step shows the board **after** that turn. "
               "Positions, evolutions and prizes are reconstructed from the log; "
               "damage counters are approximate.")

    # Turn navigation via Prev/Next buttons (state persists across reruns).
    n = len(board_states)
    if "replay_idx" not in st.session_state or not (0 <= st.session_state.replay_idx < n):
        st.session_state.replay_idx = n - 1
    idx = st.session_state.replay_idx

    nav_prev, nav_lbl, nav_next = st.columns([1, 3, 1])
    with nav_prev:
        if st.button("◀ Previous", disabled=(idx == 0), use_container_width=True):
            st.session_state.replay_idx = idx - 1
            st.rerun()
    with nav_next:
        if st.button("Next ▶", disabled=(idx == n - 1), use_container_width=True):
            st.session_state.replay_idx = idx + 1
            st.rerun()

    snap = board_states[idx]
    tn = snap["number"]
    nav_lbl.markdown(
        f"<div style='text-align:center;font-size:1.1rem'><b>Turn {snap['number']} — {snap['player']}</b>"
        f"<br><span style='opacity:0.6'>{idx + 1} of {n}</span></div>",
        unsafe_allow_html=True)

    if lesson_turn == tn:
        st.markdown('<div class="ai-box">🎓 <b>This was the turning point of the game</b> — see the '
                    'AI Coach Summary tab for the lesson and drill.</div>', unsafe_allow_html=True)

    boards = snap["boards"]
    opp = next((p for p in boards if p != target_user), None)
    you_col, opp_col = st.columns(2)
    with you_col:
        _render_board(boards[target_user], f"You ({target_user})",
                      highlight=(snap["player"] == target_user))
    with opp_col:
        if opp:
            _render_board(boards[opp], f"Opponent ({opp})",
                          highlight=(snap["player"] == opp))

    # Coaching overlay for this turn, if it was yours and has advice.
    if tn in advice_map:
        st.divider()
        ad = advice_map[tn]
        if "optimal" in ad["text"].lower():
            st.success(f"✅ Turn {tn}: {ad['text']}")
        else:
            st.markdown(f'<div class="ai-box">🧠 <b>Coach on Turn {tn}:</b> {ad["text"]}</div>',
                        unsafe_allow_html=True)

# --- 7. MAIN APP LOOP ---
setup_ui()
st.title("🔮 PTCGL Grandmaster Engine")

saved_decks = load_saved_decks()

with st.sidebar:
    st.header("⚙️ Configuration")
    try: saved_key = st.secrets.get("ANTHROPIC_API_KEY", "")
    except: saved_key = ""
    api_key = st.text_input("Anthropic API Key:", type="password", value=saved_key)

    st.divider()
    app_mode = st.radio("Choose a Tool:", ["🔮 Match Analyzer", "🧭 Deck Finder", "📈 My Progress"])

    # Defaults so analyzer variables always exist regardless of mode.
    user_decklist = ""
    target_card = "None"
    manual_outs = 1
    deck_dict = {}

    if app_mode == "🔮 Match Analyzer":
        st.divider()
        st.subheader("🎴 Deck Manager")
        deck_options = ["-- Paste New Deck --"] + list(saved_decks.keys())
        selected_deck_name = st.selectbox("Choose a Deck:", deck_options)

        if selected_deck_name == "-- Paste New Deck --":
            new_deck_name = st.text_input("New Deck Name (e.g. Pikachu ex Core)")
            user_decklist = st.text_area("Paste Decklist", height=150, placeholder="Paste exported PTCGL decklist here...")
            if st.button("Save Deck"):
                if new_deck_name and user_decklist:
                    save_deck(new_deck_name, user_decklist)
                    st.success(f"Saved '{new_deck_name}'!")
                    st.rerun()
                else: st.warning("Provide a name and a decklist.")
        else:
            user_decklist = saved_decks[selected_deck_name]
            st.success(f"Loaded: {selected_deck_name}")
            with st.expander("View Decklist"): st.code(user_decklist)
            if st.button("Delete this Deck"):
                delete_deck(selected_deck_name)
                st.rerun()

        st.divider()
        st.subheader("🎯 Active Auto-Tracker")

        if user_decklist:
            deck_dict = parse_decklist(user_decklist)
            card_options = ["None"] + sorted(list(deck_dict.keys()))
            target_card = st.selectbox("Select Card to Auto-Track:", card_options)
        else:
            st.caption("Select or save a decklist to unlock automatic card tracking!")
            manual_outs = st.number_input("Or manually guess your 'Outs':", 1, 10, 1)
            deck_dict = {}

# ===== DECK FINDER MODE =====
if app_mode == "🧭 Deck Finder":
    render_deck_finder(api_key)
    st.stop()

# ===== MY PROGRESS MODE =====
if app_mode == "📈 My Progress":
    render_progress(api_key)
    st.stop()

# ===== MATCH ANALYZER MODE (default) =====
# One-click import: PTCG Live's "Copy Battle Log" puts the log on the clipboard,
# and Streamlit runs locally, so we can read it straight out.
imp_col, opt_col = st.columns([1, 2])
with imp_col:
    import_clicked = st.button("📋 Import from PTCG Live", type="primary")
with opt_col:
    auto_analyze = st.checkbox(
        "Analyze immediately on import", value=True,
        help="Runs the analysis as soon as a log is imported, using the last player you analyzed.")

if import_clicked:
    clip = read_clipboard()
    if looks_like_battle_log(clip):
        st.session_state["log_input"] = clip
        if auto_analyze:
            st.session_state["auto_run"] = True
        st.rerun()
    elif clip.strip():
        st.warning("That doesn't look like a battle log. In PTCG Live, click **Copy Battle Log** after the game ends, then try again.")
    else:
        st.warning("Clipboard is empty (or unreadable). Click **Copy Battle Log** in PTCG Live first.")

log_input = st.text_area(
    "Battle Log:", height=150, key="log_input",
    placeholder="Click 'Import from PTCG Live' above after copying the log in-game — or paste it here manually.")

if log_input:
    players = sorted(list(set(re.findall(r"(.+)'s Turn", log_input))))
    if players:
        # Default to whoever you analyzed last, so repeat imports need no clicks.
        remembered = load_last_player()
        default_idx = players.index(remembered) if remembered in players else 0
        target_user = st.selectbox("Select Player to Analyze:", players, index=default_idx)

        # Auto-run only fires when the remembered player is actually in this log.
        auto_run = st.session_state.pop("auto_run", False) and remembered in players

        if st.button("🚀 Execute Analysis Engine", type="primary") or auto_run:
            save_last_player(target_user)

            # All log parsing lives in ptcg_parser (unit-tested against real logs).
            turns = parse_game(log_input, target_user, deck_dict=deck_dict,
                               target_card=target_card, manual_outs=manual_outs)
            board_states = parse_board_states(log_input)

            with st.spinner("🧠 AI Coach is evaluating board states and hand resources..."):
                stats = {"prizes_taken": count_prizes(log_input, target_user)}

                # Determine the current legal regulation marks once, then reuse everywhere
                # (the coach prompt, the rotation check, and the saved context all share it).
                legal_marks_now = get_legal_regulation_marks(api_key)

                # --- Send to Hand-Aware AI ---
                summary, advice_map, weakness_tags, weakness_evidence, lesson = get_advanced_ai_review(
                    turns, target_user, api_key, stats, deck_dict, legal_marks_now, log_text=log_input)

                # Data-driven rotation check (replaces the old hardcoded card list)
                rotated_cards = find_rotated_cards(deck_dict, legal_marks_now)

                # Cross-game tracking inputs
                detected_matchup = detect_opponent_deck(log_input, target_user)
                game_result = detect_result(log_input, target_user)

            # Save this game to cross-game history (dedup by log+player), unless the AI errored.
            saved_to_history = False
            if summary and not str(summary).startswith(("❌", "⚠️")):
                append_game({
                    "id": hashlib.md5((log_input + "|" + target_user).encode()).hexdigest(),
                    "date": datetime.date.today().isoformat(),
                    "player": target_user,
                    "opponent_deck": detected_matchup,
                    "result": game_result,
                    "prizes_taken": stats["prizes_taken"],
                    "turns": len(turns),
                    "weaknesses": weakness_tags,
                    "evidence": weakness_evidence,   # weakness -> the exact turns it happened
                    "lesson": lesson,
                    "summary": summary if isinstance(summary, str) else "",
                })
                saved_to_history = True

            # Persist everything so the page (and the Coach chat) survive Streamlit reruns.
            st.session_state.analysis = {
                "turns": turns, "summary": summary, "advice_map": advice_map,
                "stats": stats, "target_card": target_card,
                "manual_outs": manual_outs, "user_decklist": user_decklist,
                "deck_dict": deck_dict, "log_input": log_input, "target_user": target_user,
                "legal_marks": sorted(legal_marks_now), "rotated_cards": rotated_cards,
                "detected_deck": detected_matchup, "weaknesses": weakness_tags, "lesson": lesson,
                "board_states": board_states,
                "saved_to_history": saved_to_history,
            }
            st.session_state.coach_context = {
                "match_context": generate_ai_context(turns),
                "deck_str": ", ".join(deck_dict.keys()) if deck_dict else "Unknown",
                "card_reference": build_card_reference(deck_dict, log_input),
                "target_user": target_user,
                "legal_marks": sorted(legal_marks_now),
            }
            # Fresh conversation, drill, and replay position for each new analysis.
            st.session_state.coach_messages = []
            st.session_state.drill = {}
            st.session_state.pop("replay_idx", None)
    else:
        st.error("Log Format Error: Paste a complete battle log to begin.")

# --- 8. RESULTS & COACH CHAT (rendered from session_state so the chat survives reruns) ---
if st.session_state.get("analysis"):
    a = st.session_state.analysis
    turns = a["turns"]
    summary = a["summary"]
    advice_map = a["advice_map"]
    stats = a["stats"]
    target_card = a["target_card"]
    manual_outs = a["manual_outs"]
    user_decklist = a["user_decklist"]
    deck_dict = a["deck_dict"]
    log_input = a["log_input"]
    target_user = a["target_user"]

    tab1, tab2, tab3, tab4 = st.tabs(
        ["📊 Match Overview", "🧠 AI Coach Summary", "⚔️ Turn-by-Turn Analysis", "🎬 Replay"])

    with tab1:
        st.subheader("Match Vital Stats")
        c1, c2, c3 = st.columns(3)

        final_deck_size = next((t["deck_snapshot"] for t in reversed(turns) if t["is_me"]), 45)
        final_outs = next((t["outs_snapshot"] for t in reversed(turns) if t["is_me"]), manual_outs)

        if target_card != "None":
            c1.metric(f"Final Odds to Draw {target_card}", f"{calculate_odds(final_deck_size, final_outs, 1)}%")
        else:
            c1.metric("Top Deck Odds", f"{calculate_odds(final_deck_size, manual_outs, 1)}%")

        c2.metric("Prizes Taken", f"{stats['prizes_taken']}/6")

        # Dynamic Opponent Detection Output!
        detected_deck = a.get("detected_deck") or detect_opponent_deck(log_input, target_user)
        c3.metric("Detected Matchup", detected_deck)

        # Data-driven rotation check on the loaded deck.
        rotated_cards = a.get("rotated_cards", [])
        if rotated_cards:
            names = ", ".join(f"{n} (Reg {m})" for n, m in rotated_cards)
            st.error(f"🚨 **Rotation Check:** {len(rotated_cards)} card(s) in this deck appear to have rotated out of Standard — {names}. Verify before your next event.")
        elif a.get("legal_marks") and user_decklist:
            st.success(f"✅ **Rotation Check:** all verifiable deck cards are legal (current marks: {', '.join(a['legal_marks'])}).")

        # Cross-game tracking note
        this_weak = a.get("weaknesses", [])
        if a.get("saved_to_history"):
            wtxt = ", ".join(this_weak) if this_weak else "none flagged — clean game!"
            st.caption(f"✅ Saved to 📈 My Progress. Weaknesses this game: {wtxt}")

        st.divider()
        st.subheader("📈 Momentum Graph (Advantage Timeline)")
        momentum = pd.Series([t["score"] for t in turns]).cumsum()
        st.line_chart(momentum, use_container_width=True)

        if user_decklist:
            st.divider()
            st.subheader("🕵️ Likely Prize Pool (Missing Win-Cons)")
            missing_data = track_prizes(log_input, deck_dict, target_user)
            if missing_data:
                cols = st.columns(len(missing_data))
                for idx, (card, data) in enumerate(missing_data.items()):
                    cols[idx].error(f"**{data['count']}x**\n\n{card}")
            else: st.success("All key cards accounted for!")

    with tab2:
        # ---- The teaching moment: a drill you commit to, or a static lesson ----
        lesson = a.get("lesson") or {}
        if can_drill(lesson, turns):
            render_drill(lesson, turns, api_key)
            st.divider()
        elif lesson:
            st.subheader("🎓 The Lesson From This Game")
            turn_obj = _lesson_turn_obj(lesson, turns)
            if turn_obj:
                _render_spot(lesson, turn_obj)
            elif lesson.get("situation"):
                st.markdown(f"**The spot** — {lesson['situation']}")
            _render_lesson_answer(lesson)
            st.divider()

        st.subheader("🤖 The Grandmaster's Verdict")
        st.markdown(f'<div class="ai-box">{summary}</div>', unsafe_allow_html=True)

        st.divider()
        st.subheader("💬 Ask the Coach")
        st.caption("Have a conversation about any turn, matchup, or decision in this match.")

        if "coach_messages" not in st.session_state:
            st.session_state.coach_messages = []

        # Render the running conversation.
        for msg in st.session_state.coach_messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        user_q = st.chat_input("e.g. Why was Turn 4 a blunder? What should I have searched for?")
        if user_q:
            if not api_key:
                st.warning("Enter your Anthropic API key in the sidebar to chat with the coach.")
            else:
                st.session_state.coach_messages.append({"role": "user", "content": user_q})
                with st.chat_message("user"):
                    st.markdown(user_q)
                with st.chat_message("assistant"):
                    with st.spinner("Coach is thinking..."):
                        reply = ask_coach(api_key)
                    st.markdown(reply)
                st.session_state.coach_messages.append({"role": "assistant", "content": reply})

    with tab3:
        st.subheader("🔍 Deep Sequence Analysis")
        for t in turns:
            icon = "👤" if t['is_me'] else "🔴"
            eval_color = "green" if t['score'] >= 0 else "red"

            with st.expander(f"{icon} Turn {t['number']} - {t['player']} (Eval: :{eval_color}[{t['score']:+.1f}])"):

                if t['is_me']:
                    hand_display = ", ".join(t['hand_snapshot']) if t['hand_snapshot'] else "Empty / Unknown"
                    discard_display = ", ".join(t.get('discard_snapshot', [])) if t.get('discard_snapshot') else "Empty"
                    turn_odds = calculate_odds(t['deck_snapshot'], t['outs_snapshot'], 1)
                    target_label = target_card if target_card != "None" else "Win Condition"

                    st.markdown(f'<div class="hand-box">🃏 <b>Cards in Hand:</b> {hand_display}<br>🗑️ <b>Discard Pile:</b> {discard_display}<br>🎴 <b>Deck Size:</b> {t["deck_snapshot"]} cards | 🎯 <b>Odds to draw {target_label}:</b> {turn_odds}%</div>', unsafe_allow_html=True)

                if t['is_me'] and t['number'] in advice_map:
                    ai_data = advice_map[t['number']]
                    ai_text, ai_card = ai_data['text'], ai_data['card']
                    if "optimal" in ai_text.lower():
                        st.success(f"✅ **Optimal Play:** {ai_text}")
                    else:
                        text_col, img_col = st.columns([5, 1])
                        with text_col: st.markdown(f'<div class="ai-box">🧠 <b>AI Better Move:</b> {ai_text}</div>', unsafe_allow_html=True)
                        with img_col:
                            if ai_card and ai_card.lower() != "none":
                                img_url = get_card_image(ai_card)
                                if img_url: st.image(img_url, width=120)

                heuristics = analyze_turn_heuristics(t['actions'], t['is_me'], t['number'], t.get('player_turn_num', 0))
                for h in heuristics: st.warning(h)

                st.divider()
                st.markdown("**Actions Taken:**")
                for act in t['actions']: st.text(f"• {act}")

    with tab4:
        st.subheader("🎬 Game Replay")
        render_replay(a)
