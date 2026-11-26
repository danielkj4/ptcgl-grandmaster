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

### Card subtypes & conditional effects
- The oracle text for every card now leads with its **supertype AND subtypes**
  (e.g. `Pokémon - Stage 2, ex`, `Energy - Special`, `Trainer - Supporter`), so
  the coach can see a Pokémon's **stage** and a Trainer's **type**.
- A **Stage 1 / Stage 2 Pokémon is an "Evolution Pokémon"; a Basic is not.** The
  coach must apply effects that hinge on this — e.g. **Ignition Energy** provides
  3 Colorless only on an Evolution, and just 1 on a Basic. (Added after the coach
  misread Ignition Energy because the card reference omitted the Pokémon's stage.)

### Sequencing & activation conditions
- **Setup moves are not blunders — judge the line, not the isolated turn.** Before
  calling a play a mistake, the coach must check whether it satisfies an
  **activation condition** of another of the player's cards. (Added after it
  flagged *benching Mega Kangaskhan ex* as bad — when that bench play is exactly
  the "Colorless Mega Evolution Pokémon ex in play" condition **Talonflame ex's
  Excited Dive** needs to come down from hand.)
- **Read each Pokémon's ROLE from its Ability — don't judge support by offense.**
  Many Pokémon are played for a passive/utility Ability (free retreat, energy
  acceleration, draw, damage prevention), not to attack. The coach must not say a
  card "contributed nothing" just because it dealt no damage. If a support card is
  also a prize liability (a 2-Prize ex exposed to Boss's Orders), it's a **trade-off
  to weigh**, not a clear-cut blunder. (Added after it flagged benching **Latias ex**
  — whose "Skyliner" gives all Basics free retreat, powering the Joltik→Kangaskhan
  charge/pivot engine — as a pointless blunder for "not attacking".)
- **Activation conditions are extracted and flagged explicitly.** `fetch_card_text`
  now scans each Ability/attack for gating clauses (`if you have … in play`,
  `if this Pokémon is in your hand`, `as long as …`, `only if …`) via
  `_activation_notes()` and appends a `⚙️ Activation conditions —` line to the
  oracle entry. This turns an implicit requirement the model had to *notice* into
  an explicit checklist line it can't miss. (Deterministic regex over text we
  already fetch — no ML, no extra API calls.)

### Prize values & self-applying abilities
- **Prize values come from the card's own rule — never guessed.** Regular ex/V = 2
  Prizes; **Mega Evolution Pokémon ex** (subtype MEGA, "Mega Evolution ex Rule") =
  3 Prizes; other Pokémon = 1. (Added after the coach called **Latias ex** — a
  regular ex worth **2** Prizes — a "3 Prize target", leaking the 3-Prize value
  from the deck's Mega ex like Mega Golisopod/Kangaskhan ex.)
- **A Pokémon gets its own Abilities, incl. retreat reduction.** The printed Retreat
  Cost can be zeroed by an ability. **Latias ex** is a Basic, so its own Skyliner
  ("your Basic Pokémon have no Retreat Cost") makes ITS retreat 0 — it needs no Tool
  (Air Balloon) to retreat. The coach must not claim a retreat/Tool problem when an
  in-play ability already makes retreat free. (Note: oracle `convertedRetreatCost`
  shows the PRINTED cost (2 for Latias), so the model must apply the ability itself.)

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
