# Coach ground-truth rules

Rules the AI coach has been **explicitly taught** as fixed game knowledge, so it
doesn't give illegal or impossible advice. These live in the system prompts in
[`ptcg_coach.py`](ptcg_coach.py) — the match review (`get_advanced_ai_review`)
and the follow-up chat (`ask_coach`).

## Why this file exists

The coach gets **card effects** only from live oracle text (pokemontcg.io) — it's
told never to rely on memory for those, because they change set to set. But the
**rules of the game** are stable, so it's safe (and necessary) to hardcode them.
This file tracks which rules we've pinned, so we add them deliberately and can
see at a glance what's covered.

When the coach gives advice that breaks a rule, the fix is: add the rule here,
then add it to both system prompts in `ptcg_coach.py`.

## Rules currently taught

### Per-turn action limits
- **One Supporter per turn** — applies across *different* Supporters, not just the
  same card. (Added after the coach suggested playing Crispin *and* Brock's
  Scouting in one turn. Note: Pokégear 3.0 is an **Item**, not a Supporter.)
- **One manual Energy attachment from hand per turn**, unless a card's oracle text
  grants more.
- **One Stadium played per turn.**
- **One retreat per turn**, unless a card says otherwise.
- **No same-turn evolution** — a Pokémon can't evolve the turn it was played, and
  nothing can evolve on your first turn of the game.

### Reasoning constraints (not game rules, but pinned behavior)
- A card already played this turn can't be played again.
- A card in the discard pile isn't directly playable — only via a recovery card.
- Opponent restrictions that carry into your turn (Item lock, Ability lock,
  can't-retreat) are active and must be respected.
- Only suggest cards that are actually in the player's hand/decklist.

## Candidate rules to add (not yet taught)

Add here as we spot gaps, then promote into the prompts:

- _(none yet — add misses as you find them, e.g. prize-count mechanics, special
  conditions (Asleep/Paralyzed/Confused), Rule Box / ex prize values,
  ability-vs-attack timing, first-player-can't-attack-turn-1.)_

## How to add a rule

1. Add it under **Rules currently taught** above.
2. Add it to the `system_prompt` in `get_advanced_ai_review` (the numbered
   CRITICAL RULES / PER-TURN ACTION LIMITS block).
3. Add a condensed version to the `system_prompt` in `ask_coach` (the `RULES:`
   list) so the follow-up chat stays consistent.
4. Re-run a game that previously triggered the bad advice to confirm the fix.
