-- Core tables for WhatsApp Voice Bot on Supabase (Postgres)

create table if not exists public.admin_users (
  id bigserial primary key,
  email text not null unique,
  display_name text not null default '',
  password_hash text not null,
  status text not null default 'active',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  last_login_at timestamptz
);

create table if not exists public.whitelist_contacts (
  chat_id text primary key,
  label text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.conversation_logs (
  id bigserial primary key,
  chat_id text not null,
  direction text not null,
  role text not null,
  source_type text not null,
  message_text text not null,
  created_at timestamptz not null default now()
);

create index if not exists idx_conversation_logs_chat_created
  on public.conversation_logs (chat_id, created_at desc, id desc);

create table if not exists public.user_memories (
  id bigserial primary key,
  chat_id text not null,
  content text not null,
  created_by text not null,
  status text not null default 'active',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_user_memories_chat_status
  on public.user_memories (chat_id, status, created_at desc, id desc);

create table if not exists public.user_preferences (
  chat_id text primary key,
  voice text,
  language text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_admin_users_updated_at on public.admin_users;
create trigger trg_admin_users_updated_at
before update on public.admin_users
for each row execute function public.set_updated_at();

drop trigger if exists trg_whitelist_contacts_updated_at on public.whitelist_contacts;
create trigger trg_whitelist_contacts_updated_at
before update on public.whitelist_contacts
for each row execute function public.set_updated_at();

drop trigger if exists trg_user_memories_updated_at on public.user_memories;
create trigger trg_user_memories_updated_at
before update on public.user_memories
for each row execute function public.set_updated_at();

drop trigger if exists trg_user_preferences_updated_at on public.user_preferences;
create trigger trg_user_preferences_updated_at
before update on public.user_preferences
for each row execute function public.set_updated_at();

create or replace function public.list_known_users()
returns table (
  chat_id text,
  label text,
  last_message text,
  last_message_at timestamptz,
  whitelisted boolean
)
language sql
stable
as $$
  with users as (
    select chat_id from public.whitelist_contacts
    union
    select chat_id from public.conversation_logs
    union
    select chat_id from public.user_memories
    union
    select chat_id from public.user_preferences
  )
  select
    u.chat_id,
    coalesce(w.label, '') as label,
    (
      select c.message_text
      from public.conversation_logs c
      where c.chat_id = u.chat_id
      order by c.id desc
      limit 1
    ) as last_message,
    (
      select c.created_at
      from public.conversation_logs c
      where c.chat_id = u.chat_id
      order by c.id desc
      limit 1
    ) as last_message_at,
    exists(
      select 1 from public.whitelist_contacts w2 where w2.chat_id = u.chat_id
    ) as whitelisted
  from users u
  left join public.whitelist_contacts w on w.chat_id = u.chat_id
  order by last_message_at desc nulls last, u.chat_id;
$$;
