// GameSense API — Supabase Edge Function (Deno + TypeScript).
// Replaces the old FastAPI server: proxies IGDB with server-side Twitch credentials,
// caches results in Postgres, and builds per-user recommendations.
//
// Request: POST { action: "search" | "category" | "library" | "recommendations", ... }

import { createClient, type SupabaseClient } from "npm:@supabase/supabase-js@2";

const TWITCH_CLIENT_ID = Deno.env.get("TWITCH_CLIENT_ID") ?? "";
const TWITCH_CLIENT_SECRET = Deno.env.get("TWITCH_CLIENT_SECRET") ?? "";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";

const IGDB_URL = "https://api.igdb.com/v4/games";
const CACHE_TTL_MS = 24 * 60 * 60 * 1000; // category lists refresh once a day
const GAME_FIELDS =
  "fields name, summary, total_rating, total_rating_count, first_release_date, " +
  "cover.url, platforms.name, platforms.abbreviation, genres.name, " +
  "screenshots.url, videos.video_id, involved_companies.developer, involved_companies.company.name;";

// Priority used to pick a game's "main" genre across the site.
const GENRE_PRIORITY = [
  "Shooter", "Racing", "Strategy", "Role-playing (RPG)", "Fighting",
  "Sport", "Arcade", "Simulator", "Adventure",
];

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

// deno-lint-ignore no-explicit-any
type Game = Record<string, any>;

class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

// Service-role client: bypasses RLS, used only for the shared game cache.
const admin = createClient(SUPABASE_URL, SERVICE_ROLE_KEY, {
  auth: { persistSession: false },
});

// ---------- Twitch / IGDB ----------

let twitchToken: { value: string; expiresAt: number } | null = null;

async function getTwitchToken(): Promise<string> {
  if (twitchToken && Date.now() < twitchToken.expiresAt) return twitchToken.value;
  const params = new URLSearchParams({
    client_id: TWITCH_CLIENT_ID,
    client_secret: TWITCH_CLIENT_SECRET,
    grant_type: "client_credentials",
  });
  const res = await fetch(`https://id.twitch.tv/oauth2/token?${params}`, { method: "POST" });
  if (!res.ok) throw new HttpError(502, "Could not authenticate with Twitch/IGDB");
  const data = await res.json();
  // Refresh 60 seconds before the real expiry.
  twitchToken = {
    value: data.access_token,
    expiresAt: Date.now() + ((data.expires_in ?? 3600) - 60) * 1000,
  };
  return twitchToken.value;
}

async function igdbQuery(query: string): Promise<Game[]> {
  const token = await getTwitchToken();
  const res = await fetch(IGDB_URL, {
    method: "POST",
    headers: { "Client-ID": TWITCH_CLIENT_ID, Authorization: `Bearer ${token}` },
    body: query,
  });
  if (!res.ok) throw new HttpError(502, `IGDB error ${res.status}`);
  const data = await res.json();
  return Array.isArray(data) ? data : [];
}

// ---------- Game processing (same output shape as the old Python backend) ----------

function genrePriority(g: Game): number {
  const i = GENRE_PRIORITY.indexOf(g?.name ?? "");
  return i === -1 ? GENRE_PRIORITY.length : i;
}

function mainGenreId(game: Game): number | null {
  const genres: Game[] = game.genres ?? [];
  if (genres.length === 0) return null;
  const best = [...genres].sort((a, b) => genrePriority(a) - genrePriority(b))[0];
  return best?.id ?? null;
}

function topScore(game: Game): number {
  const rating = game.total_rating ?? 0;
  const count = game.total_rating_count ?? 0;
  if (count === 0) return 0;
  return rating * Math.pow(Math.log(count), 0.4);
}

function processGames(games: Game[]): Game[] {
  for (const game of games) {
    if (Array.isArray(game.genres) && game.genres.length > 0) {
      game.genres.sort((a: Game, b: Game) => genrePriority(a) - genrePriority(b));
    }
    game.cover_url = game.cover?.url
      ? "https:" + game.cover.url.replace("t_thumb", "t_cover_big")
      : "https://via.placeholder.com/300x400?text=No+Cover";
    game.screenshot_urls = (game.screenshots ?? []).map((s: Game) =>
      "https:" + s.url.replace("t_thumb", "t_720p")
    );
    game.trailer_url = game.videos?.length
      ? `https://www.youtube.com/embed/${game.videos[0].video_id}`
      : null;
    if (game.total_rating != null) game.total_rating = Math.round(game.total_rating);
    game.release_date_formatted = game.first_release_date
      ? new Date(game.first_release_date * 1000).toLocaleDateString("en-US", {
        month: "long", day: "2-digit", year: "numeric", timeZone: "UTC",
      })
      : "TBA";
  }
  return games;
}

// ---------- Cache helpers ----------

async function readCache(category: string): Promise<{ games: Game[]; fresh: boolean }> {
  const { data, error } = await admin
    .from("cached_games")
    .select("game, updated_at")
    .eq("category", category);
  if (error) throw new HttpError(500, error.message);
  if (!data || data.length === 0) return { games: [], fresh: false };
  const oldest = Math.min(...data.map((r) => new Date(r.updated_at).getTime()));
  return { games: data.map((r) => r.game), fresh: Date.now() - oldest < CACHE_TTL_MS };
}

async function writeCache(category: string, games: Game[], replace = true) {
  if (replace) await admin.from("cached_games").delete().eq("category", category);
  if (games.length === 0) return;
  const now = new Date().toISOString();
  const rows = games.map((g) => ({ game_id: g.id, category, game: g, updated_at: now }));
  const { error } = await admin.from("cached_games").upsert(rows);
  if (error) console.error("Cache write failed:", error.message);
}

// ---------- Actions ----------

function categoryQuery(catId: string): string | null {
  const now = Math.floor(Date.now() / 1000);
  const halfYearAgo = now - 180 * 24 * 60 * 60;
  if (catId === "new-releases") {
    return `${GAME_FIELDS} where first_release_date <= ${now} & first_release_date > 0 & cover != null; sort first_release_date desc; limit 250;`;
  }
  if (catId === "top") {
    return `${GAME_FIELDS} where version_parent = null & total_rating_count > 200 & total_rating > 80 & cover != null; sort total_rating desc; limit 250;`;
  }
  if (catId === "trends") {
    return `${GAME_FIELDS} where version_parent = null & hypes > 10 & first_release_date > ${halfYearAgo} & cover != null; sort hypes desc; limit 250;`;
  }
  if (catId === "upcoming") {
    return `${GAME_FIELDS} where first_release_date > ${now} & version_parent = null & cover != null; sort first_release_date asc; limit 250;`;
  }
  if (/^\d+$/.test(catId)) {
    // Genre card (IGDB genre id): the best-known games of that genre.
    return `${GAME_FIELDS} where genres = (${catId}) & version_parent = null & cover != null & total_rating_count > 50; sort total_rating_count desc; limit 250;`;
  }
  return null;
}

async function getCategory(catId: string): Promise<Game[]> {
  const query = categoryQuery(catId);
  if (!query) throw new HttpError(400, "Unknown category");
  const cacheKey = /^\d+$/.test(catId) ? `genre-${catId}` : catId;

  let { games, fresh } = await readCache(cacheKey);
  if (!fresh) {
    try {
      games = processGames(await igdbQuery(query));
      await writeCache(cacheKey, games);
    } catch (e) {
      // IGDB down: serve the stale cache if we have one.
      if (games.length === 0) throw e;
      console.error("Refresh failed, serving stale cache:", e);
    }
  }

  if (catId === "new-releases") {
    games.sort((a, b) => (b.first_release_date ?? 0) - (a.first_release_date ?? 0));
  } else if (catId === "upcoming") {
    games.sort((a, b) => (a.first_release_date ?? 0) - (b.first_release_date ?? 0));
  } else if (catId !== "trends") {
    games.sort((a, b) => topScore(b) - topScore(a));
  }
  return games.slice(0, 100);
}

async function search(q: string): Promise<Game[]> {
  // Strip characters that would break out of the IGDB query string.
  const clean = String(q ?? "").replace(/["\\;]/g, " ").trim().slice(0, 100);
  if (!clean) return [];
  return processGames(await igdbQuery(`${GAME_FIELDS} search "${clean}"; limit 100;`));
}

async function getUserClient(req: Request): Promise<SupabaseClient> {
  const authHeader = req.headers.get("Authorization") ?? "";
  // Client that acts as the calling user, so RLS applies to library queries.
  const client = createClient(SUPABASE_URL, ANON_KEY, {
    global: { headers: { Authorization: authHeader } },
    auth: { persistSession: false },
  });
  const { data, error } = await client.auth.getUser();
  if (error || !data.user) throw new HttpError(401, "Authentication required");
  return client;
}

async function getLibraryIds(client: SupabaseClient): Promise<number[]> {
  const { data, error } = await client.from("library_items").select("game_id");
  if (error) throw new HttpError(500, error.message);
  return (data ?? []).map((r) => Number(r.game_id));
}

async function getGameDetails(ids: number[]): Promise<Game[]> {
  if (ids.length === 0) return [];
  const { data } = await admin.from("cached_games").select("game").in("game_id", ids);
  const byId = new Map<number, Game>();
  for (const row of data ?? []) byId.set(Number(row.game.id), row.game);

  const missing = ids.filter((id) => !byId.has(id));
  if (missing.length > 0) {
    const fetched = processGames(
      await igdbQuery(`${GAME_FIELDS} where id = (${missing.join(",")}); limit 500;`),
    );
    await writeCache("library", fetched, false);
    for (const g of fetched) byId.set(Number(g.id), g);
  }
  return ids.map((id) => byId.get(id)).filter((g): g is Game => Boolean(g));
}

async function getLibrary(req: Request): Promise<Game[]> {
  const client = await getUserClient(req);
  return getGameDetails(await getLibraryIds(client));
}

async function getRecommendations(req: Request): Promise<Game[]> {
  const client = await getUserClient(req);
  const libraryIds = await getLibraryIds(client);
  if (libraryIds.length === 0) return [];

  // 1. Find the "main" genre of every game in the library.
  const libraryGames = await getGameDetails(libraryIds);
  const mainGenres = new Set<number>();
  for (const g of libraryGames) {
    const id = mainGenreId(g);
    if (id != null) mainGenres.add(id);
  }
  if (mainGenres.size === 0) return [];

  // 2. Popular games in those genres that the user doesn't own yet.
  const candidates = await igdbQuery(
    `${GAME_FIELDS} where genres = (${[...mainGenres].join(",")}) & id != (${libraryIds.join(",")}) ` +
      `& cover != null & total_rating_count > 200; limit 500;`,
  );

  // 3. Keep only games whose own main genre matches, rank by weighted rating.
  const filtered = candidates.filter((g) => {
    const id = mainGenreId(g);
    return id != null && mainGenres.has(id);
  });
  filtered.sort((a, b) => topScore(b) - topScore(a));
  return processGames(filtered.slice(0, 100));
}

// ---------- HTTP entry point ----------

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
  });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS_HEADERS });
  if (req.method !== "POST") return json({ detail: "Method not allowed" }, 405);

  try {
    const body = await req.json().catch(() => ({}));
    switch (body.action) {
      case "search":
        return json(await search(body.q));
      case "category":
        return json(await getCategory(String(body.id ?? "")));
      case "library":
        return json(await getLibrary(req));
      case "recommendations":
        return json(await getRecommendations(req));
      default:
        return json({ detail: "Unknown action" }, 400);
    }
  } catch (e) {
    const status = e instanceof HttpError ? e.status : 500;
    console.error(e);
    return json({ detail: e instanceof Error ? e.message : "Server error" }, status);
  }
});
