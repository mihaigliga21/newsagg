-- newsagg schema. Safe to run more than once (`newsagg migrate`).
-- Each stage reads rows the previous stage wrote, claims them with
-- FOR UPDATE SKIP LOCKED, and wakes on NOTIFY sent by the triggers below.

create schema if not exists newsagg;
set search_path to newsagg, public;

create table if not exists source_state (
    source_id            text primary key,
    last_fetch_at        timestamptz,
    last_ok_at           timestamptz,
    last_error           text,
    consecutive_failures int not null default 0,
    etag                 text,
    last_modified        text
);

-- Stage 2 output: one row per story (a cluster of items about the same event).
create table if not exists stories (
    id           bigserial primary key,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    norm_title   text not null,
    title        text not null,
    summary      text,
    url          text not null,
    source_id    text not null,
    source_priority int not null default 5,
    source_count int not null default 1,
    region       text,
    section      text,
    topic        text,
    importance   int not null default 3,
    status       text not null default 'pending_write'
                 check (status in ('pending_write','writing','written','write_failed','skipped','posted')),
    line         jsonb,
    digest_id    bigint,
    attempts     int not null default 0
);
create index if not exists stories_status_idx on stories (status, created_at);
create index if not exists stories_created_idx on stories (created_at);

-- Stage 1 output: every item fetched from a source.
create table if not exists raw_items (
    id            bigserial primary key,
    source_id     text not null,
    external_id   text not null,
    url           text not null,
    title         text not null,
    summary       text,
    published_at  timestamptz,
    fetched_at    timestamptz not null default now(),
    status        text not null default 'new'
                  check (status in ('new','processing','accepted','duplicate','rejected','error')),
    reject_reason text,
    story_id      bigint references stories(id) on delete set null,
    claimed_at    timestamptz,
    unique (source_id, external_id)
);
create index if not exists raw_items_status_idx on raw_items (status, id);
create index if not exists raw_items_url_idx on raw_items (url);

-- Stage 4 output: digests sent for review and/or posted.
create table if not exists digests (
    id                  bigserial primary key,
    created_at          timestamptz not null default now(),
    status              text not null default 'pending_review'
                        check (status in ('pending_review','approved','posted','skipped','failed')),
    parts               jsonb not null,
    story_ids           bigint[] not null,
    review_message_ids  jsonb,
    channel_message_ids jsonb,
    posted_at           timestamptz,
    error               text
);

create table if not exists kv (
    key   text primary key,
    value jsonb not null
);

create or replace function newsagg.notify_raw_items() returns trigger language plpgsql as $$
begin
    perform pg_notify('newsagg_raw_items', '');
    return null;
end $$;

create or replace function newsagg.notify_stories() returns trigger language plpgsql as $$
begin
    if new.status = 'pending_write' then
        perform pg_notify('newsagg_stories', '');
    end if;
    return null;
end $$;

drop trigger if exists raw_items_notify on raw_items;
create trigger raw_items_notify after insert on raw_items
    for each statement execute function newsagg.notify_raw_items();

drop trigger if exists stories_notify on stories;
create trigger stories_notify after insert or update of status on stories
    for each row execute function newsagg.notify_stories();
