-- ISDb — Postgres schema
--
-- Replaces every file the pipeline currently writes to disk:
--   data/match_stats/raw/*.json        -> raw_cache (source='sofascore_match')
--   data/xg_timeline/pl/*.json         -> raw_cache (source='xg_pl')
--   data/xg_timeline/wc/*.json         -> raw_cache (source='xg_wc')
--   data/xg_timeline/pl/pl_features.csv -> pl_xg_features
--   data/xg_timeline/wc/wc_features.csv -> wc_xg_features
--   data/xg_timeline/wc/wc_labelled.csv -> wc_labelled
--   data/match_stats/*_match_stats.csv  -> match_features
--   data/imdb/*.json                    -> imdb_ratings
--   data/schedule/schedule_matches.json -> wc_schedule   (hand-curated, seeded once)
--   teams.json                          -> wc_teams      (hand-curated, seeded once)
--   outputs/pl_excitingness.csv         -> pl_excitingness
--   data/frontend/matches_*.json        -> frontend_matches
--
-- Column names that came from a Python format string like f"xg_home_{window}"
-- (e.g. "xg_home_0-15") are stored here with the hyphen replaced by an
-- underscore ("xg_home_0_15"), because a bare hyphen isn't a legal unquoted
-- Postgres identifier. db.py's upsert_rows() does this same replacement
-- automatically, so the calling scripts don't need to know about it.

-- ===========================================================================
-- Generic cache: every per-match JSON payload the fetchers download,
-- keyed the same way the old filenames were, plus an indexed match_id so
-- predict_excitingness.py can look a match's shots up directly instead of
-- globbing the filesystem.
-- ===========================================================================
create table if not exists raw_cache (
    source      text        not null,   -- 'sofascore_match' | 'xg_pl' | 'xg_wc'
    cache_key   text        not null,   -- old filename stem, e.g. 'wc2026_match_1004'
    match_id    bigint,                 -- extracted numeric id, for lookups
    season_year int,                    -- extracted year, disambiguates xg_wc across WCs
    payload     jsonb       not null,
    fetched_at  timestamptz not null default now(),
    primary key (source, cache_key)
);
create index if not exists raw_cache_match_idx on raw_cache (source, match_id);

-- ===========================================================================
-- match_stats_fetch.py — FEATURE_COLS, one row per Sofascore event
-- ===========================================================================
create table if not exists match_features (
    event_id                 bigint primary key,
    competition               text,
    date                      date,
    home                      text,
    away                      text,
    home_score                int,
    away_score                int,
    goals_total               int,
    goal_difference           int,
    is_draw                   boolean,
    first_goal_minute         int,
    last_goal_minute          int,
    goals_first_15            int,
    goals_last_15             int,
    lead_changes              int,
    comeback                  boolean,
    goals_penalty             int,
    goals_own                 int,
    time_level                int,
    time_home_lead            int,
    time_away_lead            int,
    level_after_75            boolean,
    xg_home                   numeric,
    xg_away                   numeric,
    xg_total                  numeric,
    xg_diff                   numeric,
    loser_higher_xg           boolean,
    goals_minus_xg_home       numeric,
    goals_minus_xg_away       numeric,
    shots_home                numeric,
    shots_away                numeric,
    shots_total                numeric,
    shots_on_target_home      numeric,
    shots_on_target_away      numeric,
    big_chances_home          numeric,
    big_chances_away          numeric,
    big_chances_missed_home   numeric,
    big_chances_missed_away   numeric,
    avg_xg_per_shot           numeric,
    possession_home           numeric,
    possession_away           numeric,
    corners_home              numeric,
    corners_away              numeric,
    yellow_cards              int,
    red_cards                 int,
    first_red_minute          int,
    red_second_half           boolean,
    red_when_close            boolean,
    fouls_home                numeric,
    fouls_away                numeric,
    fouls_total               numeric,
    fouls_diff                numeric,
    mom_peak_home             numeric,
    mom_peak_away             numeric,
    mom_flips                 int,
    mom_avg_abs               numeric,
    updated_at                timestamptz not null default now()
);

-- ===========================================================================
-- pl_xg_timeline_fetch.py / wc_xg_timeline_fetch.py — ALL_COLS (META + FEATURE)
-- ===========================================================================
create table if not exists pl_xg_features (
    match_id            bigint primary key,
    season_year         int,
    competition          text,
    date                 date,
    home                 text,
    away                 text,
    home_score           int,
    away_score           int,
    total_shots          numeric,
    total_xg             numeric,
    largest_xg_swing     numeric,
    largest_xg_swing_window text,
    xg_lead_changes      int,
    xg_first_15_home     numeric,
    xg_first_15_away     numeric,
    xg_last_15_home      numeric,
    xg_last_15_away      numeric,
    xg_home_0_15         numeric,
    xg_away_0_15         numeric,
    xg_diff_0_15         numeric,
    xg_home_15_30        numeric,
    xg_away_15_30        numeric,
    xg_diff_15_30        numeric,
    xg_home_30_45        numeric,
    xg_away_30_45        numeric,
    xg_diff_30_45        numeric,
    xg_home_45_60        numeric,
    xg_away_45_60        numeric,
    xg_diff_45_60        numeric,
    xg_home_60_75        numeric,
    xg_away_60_75        numeric,
    xg_diff_60_75        numeric,
    xg_home_75_90plus    numeric,
    xg_away_75_90plus    numeric,
    xg_diff_75_90plus    numeric,
    updated_at           timestamptz not null default now()
);

create table if not exists wc_xg_features (
    match_id            bigint primary key,
    season_year         int,
    date                 date,
    stage                text,
    home                 text,
    away                 text,
    home_score           int,
    away_score           int,
    home_pens            int,
    away_pens            int,
    extra_time           boolean,
    penalties             boolean,
    total_shots          numeric,
    total_xg             numeric,
    largest_xg_swing     numeric,
    largest_xg_swing_window text,
    xg_lead_changes      int,
    xg_first_15_home     numeric,
    xg_first_15_away     numeric,
    xg_last_15_home      numeric,
    xg_last_15_away      numeric,
    xg_home_0_15         numeric,
    xg_away_0_15         numeric,
    xg_diff_0_15         numeric,
    xg_home_15_30        numeric,
    xg_away_15_30        numeric,
    xg_diff_15_30        numeric,
    xg_home_30_45        numeric,
    xg_away_30_45        numeric,
    xg_diff_30_45        numeric,
    xg_home_45_60        numeric,
    xg_away_45_60        numeric,
    xg_diff_45_60        numeric,
    xg_home_60_75        numeric,
    xg_away_60_75        numeric,
    xg_diff_60_75        numeric,
    xg_home_75_90plus    numeric,
    xg_away_75_90plus    numeric,
    xg_diff_75_90plus    numeric,
    updated_at           timestamptz not null default now()
);

-- wc_xg_features + imdb rating/votes joined in, cached exactly like the old
-- wc_labelled.csv so predict_excitingness.py doesn't redo the join every run.
create table if not exists wc_labelled (
    match_id            bigint primary key,
    season_year         int,
    date                 date,
    stage                text,
    home                 text,
    away                 text,
    home_score           int,
    away_score           int,
    home_pens            int,
    away_pens            int,
    extra_time           boolean,
    penalties             boolean,
    total_shots          numeric,
    total_xg             numeric,
    largest_xg_swing     numeric,
    largest_xg_swing_window text,
    xg_lead_changes      int,
    xg_first_15_home     numeric,
    xg_first_15_away     numeric,
    xg_last_15_home      numeric,
    xg_last_15_away      numeric,
    xg_home_0_15         numeric,
    xg_away_0_15         numeric,
    xg_diff_0_15         numeric,
    xg_home_15_30        numeric,
    xg_away_15_30        numeric,
    xg_diff_15_30        numeric,
    xg_home_30_45        numeric,
    xg_away_30_45        numeric,
    xg_diff_30_45        numeric,
    xg_home_45_60        numeric,
    xg_away_45_60        numeric,
    xg_diff_45_60        numeric,
    xg_home_60_75        numeric,
    xg_away_60_75        numeric,
    xg_diff_60_75        numeric,
    xg_home_75_90plus    numeric,
    xg_away_75_90plus    numeric,
    xg_diff_75_90plus    numeric,
    imdb_rating          numeric,
    imdb_votes           int,
    updated_at           timestamptz not null default now()
);

-- ===========================================================================
-- update_imdb.py — one row per episode (= one WC match)
-- ===========================================================================
create table if not exists imdb_ratings (
    episode_id        text primary key,   -- IMDb episode id, e.g. 'tt33163796'
    series_title_id   text,               -- IMDb id of the parent series
    title              text,
    season             int,
    episode            text,
    year               int,
    month              int,
    day                int,
    rating             numeric,
    votes              int,
    url                text,
    updated_at         timestamptz not null default now()
);

-- ===========================================================================
-- Hand-curated reference data. These aren't scraped — they're small,
-- edited-by-hand files. Keep editing them as JSON in the repo (that's the
-- easy, reviewable way to fix a stadium name or a kickoff time) and let
-- migrate_existing_data.py --seed-only re-upsert them into these tables
-- whenever you change them. Nothing here needs the live-scrape credentials
-- or schedule GitHub Actions runs on.
-- ===========================================================================
create table if not exists wc_schedule (
    match_id        int primary key,
    date             date,
    time_et          text,
    stage            text,
    group_name       text,
    stadium          text,
    city             text,
    country_played   text,
    home             text,
    away             text
);

create table if not exists wc_teams (
    tournament    text not null default 'wc2026',
    group_name    text,
    code          text not null,
    name          text,
    color_1       text,
    color_2       text,
    primary key (tournament, code)
);

-- ===========================================================================
-- predict_excitingness.py output — outputs/pl_excitingness.csv
-- ===========================================================================
create table if not exists pl_excitingness (
    match_id             bigint primary key,
    rank                  int,
    season_year           int,
    date                  date,
    home                  text,
    away                  text,
    home_score            int,
    away_score            int,
    total_xg              numeric,
    total_goals           int,
    chasing_xg            numeric,
    final5_swing_count    int,
    big_chances_60_75     numeric,
    upset                  numeric,
    excitingness          numeric,
    excitingness_final5swing numeric,
    excitingness_goalsonly   numeric,
    updated_at             timestamptz not null default now()
);

-- ===========================================================================
-- Frontend-ready output — data/frontend/matches_wc2026.json,
-- matches_pl2526.json, matches_pl2627.json, unified into one table.
-- This is the ONLY table the public/anon read-only role needs SELECT on.
-- ===========================================================================
create table if not exists frontend_matches (
    competition       text not null,     -- 'wc2026' | 'pl2526' | 'pl2627'
    match_id           bigint not null,
    home               text,
    away               text,
    hg                 int,
    ag                 int,
    et_hg              int,
    et_ag              int,
    pens_hg            int,
    pens_ag            int,
    source             text,
    date               date,
    time_et            text,
    stage              text,
    group_name         text,
    stadium            text,
    city               text,
    country_played     text,
    imdb_score         numeric,
    oneline_comment    text,
    gw                 int,               -- PL only
    excitingness       numeric,           -- PL only
    models             jsonb,             -- PL only: {"final5swing": 8.2, "goalsonly": 7.9}
    updated_at         timestamptz not null default now(),
    primary key (competition, match_id)
);

-- ===========================================================================
-- Lightweight observability for the scheduled jobs (GitHub Actions). Not
-- required for anything to function — just makes "did last night's fetch
-- actually run, and did it error?" a one-row query instead of a log dig.
-- ===========================================================================
create table if not exists pipeline_runs (
    id          bigserial primary key,
    job         text not null,           -- e.g. 'match_stats_fetch:wc2026'
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    status       text,                    -- 'ok' | 'error'
    detail       text
);

-- ===========================================================================
-- Read-only access for the frontend (Supabase: run this after creating the
-- project, in the SQL editor). Skip this block if you decided the frontend
-- will keep reading generated JSON files instead of querying Postgres
-- directly — see MIGRATION.md.
-- ===========================================================================
alter table frontend_matches enable row level security;
drop policy if exists frontend_matches_public_read on frontend_matches;
create policy frontend_matches_public_read
    on frontend_matches for select
    using (true);
-- No insert/update/delete policy is created for the anon role, so the
-- public API key Supabase gives you can only ever read this one table.
