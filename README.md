# 🔮 [PTCGL Grandmaster Engine](https://tcg-grandmaster.streamlit.app/)

An AI coaching tool for the Pokémon Trading Card Game. Paste a PTCG Live battle
log and get a turn-by-turn review of your play, grounded in real card data — plus
cross-game tracking that surfaces the mistakes you keep repeating.

Built with [Streamlit](https://streamlit.io/) and the
[Anthropic API](https://docs.anthropic.com/) (Claude).

**▶️ [Live demo](https://tcg-grandmaster.streamlit.app/)** — paste a PTCG Live
battle log and try it in your browser, no setup or API key required. (A few free
analyses per day.)

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

**🧠 Personalized coaching** — once you have a couple of tracked games, each new
review is primed with a compact profile of your recurring tendencies (your most
frequent weakness categories, win/loss record, toughest matchup) so the coach ties
its lesson to the habits you keep repeating. It's retrieval, not training: the
model isn't fine-tuned on your logs, it's handed a summary of them as context, and
it's told not to force a pattern that isn't present in the current game.

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

## Hosted deployment

The app runs in one of two modes automatically, based on whether an
`ANTHROPIC_API_KEY` is present in Streamlit secrets:

- **Local mode** (no secret key): you enter your own key in the sidebar, history
  is saved to local JSON files, and the clipboard "Import from PTCG Live" button
  is available.
- **Hosted mode** (key set in secrets): the owner's key is used for all visitors,
  so no key box is shown; history is kept per browser session instead of on disk;
  the server-clipboard import is hidden (visitors paste their log); and each
  visitor is capped at `MAX_ANALYSES_PER_SESSION` analyses per day to control
  cost. The daily cap is tracked server-side by a hash of the client IP so it
  survives page refreshes; it's best-effort (resets on app reboot, shared IPs
  share a tally, a new network bypasses it), so set a spend cap as the real
  backstop.

To deploy on [Streamlit Community Cloud](https://share.streamlit.io), point it at
this repo with `ptcg_coach.py` as the main file and set these secrets:

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
COACH_MODEL = "claude-sonnet-4-6"      # optional; defaults to claude-opus-4-8
MAX_ANALYSES_PER_SESSION = "5"         # optional; defaults to 5
```

Set a monthly spend limit in the [Anthropic Console](https://console.anthropic.com/)
as a hard cost backstop.

## User accounts (optional)

Accounts are **off by default** — the app works without them (local JSON on your
machine, session-scoped history when hosted). When you configure the two secrets
below, a "Sign in with Google" button appears; signing in is **optional** for
visitors, but a signed-in user's saved decks and game history persist in a
database and sync across devices.

It uses Streamlit's native OIDC login (`st.login`) plus a
[Supabase](https://supabase.com) Postgres database.

**1. Create the database (Supabase, free tier).** Create a project, then in the
SQL editor run:

```sql
create table if not exists games (
  user_email text not null,
  id         text not null,
  created_at timestamptz default now(),
  data       jsonb not null,
  primary key (user_email, id)
);
create table if not exists decks (
  user_email text not null,
  name       text not null,
  decklist   text not null,
  primary key (user_email, name)
);
```

Grab the project URL and the **service_role** key (Settings → API). The key stays
in Streamlit secrets (server-side, never sent to the browser); the app scopes
every query to the signed-in user's email, so one shared key is safe here.

**2. Create a Google OAuth client.** In the Google Cloud console, configure an
OAuth consent screen, then create an **OAuth client ID** (type: Web app) with the
authorized redirect URI set to `https://<your-app>.streamlit.app/oauth2callback`.
Note the client ID and client secret.

**3. Add the secrets:**

```toml
SUPABASE_URL = "https://xxxx.supabase.co"
SUPABASE_KEY = "the-service_role-key"

[auth]
redirect_uri = "https://<your-app>.streamlit.app/oauth2callback"
cookie_secret = "any-long-random-string"
client_id = "xxxx.apps.googleusercontent.com"
client_secret = "xxxx"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
```

Save; the app reboots and the sign-in button appears. Running locally, use
`http://localhost:8501/oauth2callback` as the redirect URI instead.

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
