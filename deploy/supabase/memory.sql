-- Apply once in the Supabase SQL editor. Credentials stay in environment variables.
create extension if not exists vector;
create table if not exists public.varak_memory (
    id text primary key,
    content text not null,
    source text not null default '',
    metadata jsonb not null default '{}',
    created_at double precision not null,
    embedding vector(768)
);
alter table public.varak_memory enable row level security;
-- Backend service-role access only; no anon/authenticated policies.
revoke all on public.varak_memory from anon, authenticated;
create index if not exists varak_memory_embedding_idx
    on public.varak_memory using hnsw (embedding vector_cosine_ops);
create or replace function public.varak_match_memory(
    query_embedding vector(768), match_count integer default 5
) returns table(content text, source text, metadata jsonb, score double precision)
language sql stable security invoker set search_path = public as $$
    select content, source, metadata, 1 - (embedding <=> query_embedding) as score
    from public.varak_memory where embedding is not null
    order by embedding <=> query_embedding limit greatest(0, least(match_count, 100));
$$;
revoke all on function public.varak_match_memory(vector, integer) from public;
grant execute on function public.varak_match_memory(vector, integer) to service_role;
