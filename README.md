# GameSense

**Live:** https://idanlachovitz.github.io/Web_Project/

GameSense is a game discovery platform. You can browse games by genre, new releases, top rated, trending and upcoming, search the full IGDB catalog, keep a personal library, and get recommendations based on that library.

## Architecture

```
GitHub Pages (static frontend)
   │  supabase-js
   ├── Supabase Auth ............ register / login (JWT sessions)
   ├── Postgres + RLS ........... library_items: each user can only read/write their own rows
   └── Edge Function "igdb" ..... Deno/TypeScript serverless API
          ├── Twitch OAuth (client credentials, token cached in memory)
          ├── IGDB API queries (search, categories, genres)
          ├── cached_games table: 24h Postgres cache per category, stale-cache fallback
          └── recommendation engine (main-genre matching + weighted rating score)
```

| Part | Tech |
|---|---|
| Frontend | HTML, JavaScript, Tailwind CSS (hosted on GitHub Pages) |
| API | Supabase Edge Functions (Deno, TypeScript) |
| Database | PostgreSQL (Supabase) with Row Level Security |
| Auth | Supabase Auth |
| Data | IGDB API (Twitch) |
| Ops | GitHub Actions scheduled keep-alive |

### Recommendation logic
1. Take each game in the user's library and pick its **main genre**, using a site-wide genre priority list.
2. Query IGDB for well-rated games in those genres that the user does not own yet.
3. Keep only candidates whose own main genre matches, then rank them by `rating × log(rating_count)^0.4`. This weighting favors games that are both highly rated and widely rated.

## Project structure
```
docs/index.html                       frontend (served by GitHub Pages)
supabase/migrations/*.sql             database schema + RLS policies
supabase/functions/igdb/index.ts      serverless API
.github/workflows/keep-alive.yml      scheduled ping
main.py                               original FastAPI + SQLite version (legacy)
```

## Running your own copy
1. Create a Supabase project. Under **Authentication → Sign In / Providers → Email**, turn **Confirm email** off (usernames map to internal addresses).
2. `npx supabase login` → `npx supabase link --project-ref <ref>` → `npx supabase db push`
3. `npx supabase secrets set TWITCH_CLIENT_ID=... TWITCH_CLIENT_SECRET=...`
4. `npx supabase functions deploy igdb`
5. Put your project URL and anon key at the top of `docs/index.html`.
6. On GitHub, go to **Settings → Pages**, choose "Deploy from a branch", then `main` and `/docs`.

Game data is provided by [IGDB](https://www.igdb.com/) and Twitch.
