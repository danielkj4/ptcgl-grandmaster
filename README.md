# 🔮 PTCGL Grandmaster Engine

An AI coaching tool for the Pokémon Trading Card Game. Paste a PTCG Live battle
log and get a turn-by-turn review of your play, grounded in real card data — plus
cross-game tracking that surfaces the mistakes you keep repeating.

Built with [Streamlit](https://streamlit.io/) and the
[Anthropic API](https://docs.anthropic.com/) (Claude).

## Features

**🔮 Match Analyzer** — parses a battle log into per-turn game state (hand, discard
pile, deck size, draw odds) and has Claude review each decision.
- Turn-by-turn "better move" suggestions with card images
- A conversational coach you can ask follow-up questions
- Momentum graph, likely prize pool, and opponent-deck detection
- Rotation check: flags deck cards whose regulation mark is no longer Standard-legal

**🧭 Deck Finder** — a play-style quiz that matches you to an archetype, then
recommends a Beginner → Intermediate → Advanced deck progression with full
sample decklists.

**📈 My Progress** — aggregates every analyzed game to reveal recurring weaknesses,
matchup records, and a prioritized improvement plan. Drill into any weakness to see
the exact turns across games where it happened.

## Anti-hallucination design

The coach is deliberately constrained so it can't invent cards or rules:
- **Card effects** come from the [pokemontcg.io](https://pokemontcg.io/) API and are
  injected as ground truth — the model is told never to rely on memory.
- **Format legality** is resolved live via web search into a set of legal regulation
  marks, then every card is verified against it. No hardcoded rotation lists.
- **Weakness categories** come from a fixed taxonomy; unrecognized ones are rejected.

## Setup

```bash
pip install streamlit anthropic requests pandas
streamlit run ptcg_coach.py
```

Enter your Anthropic API key in the sidebar, or create `.streamlit/secrets.toml`:

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
```

> `.streamlit/secrets.toml` is gitignored — never commit your API key.

## Tests

Log parsing lives in `ptcg_parser.py` with no Streamlit dependency, so it can be
tested directly against real battle logs:

```bash
python3 tests/test_parser.py
```

The fixtures in `tests/fixtures/` are real PTCG Live logs. They exist because the
parser was originally written against guessed formatting and silently fed the AI
bad data — these tests pin the real format so that can't regress.

## Project layout

| File | Purpose |
| --- | --- |
| `ptcg_coach.py` | Streamlit app: UI, Claude calls, all three modes |
| `ptcg_parser.py` | Pure log parsing and game-state reconstruction |
| `tests/test_parser.py` | Regression tests against real logs |
| `.streamlit/config.toml` | Dark theme |

## Notes

Card data is provided by the community-run pokemontcg.io API, which can lag behind
the newest sets — very recent cards may have no image or regulation mark available.

Pokémon and the Pokémon TCG are trademarks of Nintendo / Creatures Inc. / GAME FREAK inc.
This is an unofficial fan project and is not affiliated with or endorsed by them.
