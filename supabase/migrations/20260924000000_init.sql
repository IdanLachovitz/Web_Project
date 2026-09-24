-- GameSense schema: per-user library + shared IGDB game cache.

-- Each row = one game in one user's library.
create table if not exists public.library_items (
    user_id    uuid        not null default auth.uid() references auth.users (id) on delete cascade,
    game_id    bigint      not null,
    created_at timestamptz not null default now(),
    primary key (user_id, game_id)
);

-- Row Level Security: a user can only see and change their own library.
alter table public.library_items enable row level security;

create policy "Users read their own library"
    on public.library_items for select to authenticated
    using (auth.uid() = user_id);

create policy "Users add to their own library"
    on public.library_items for insert to authenticated
    with check (auth.uid() = user_id);

create policy "Users remove from their own library"
    on public.library_items for delete to authenticated
    using (auth.uid() = user_id);

grant select, insert, delete on public.library_items to authenticated;

-- Cache of processed IGDB game objects, grouped by category
-- (new-releases, top, trends, upcoming, genre-<id>, library).
-- RLS on with no policies: only the Edge Function (service role) can read/write it.
create table if not exists public.cached_games (
    game_id    bigint      not null,
    category   text        not null,
    game       jsonb       not null,
    updated_at timestamptz not null default now(),
    primary key (game_id, category)
);

alter table public.cached_games enable row level security;

create index if not exists cached_games_category_idx on public.cached_games (category);
