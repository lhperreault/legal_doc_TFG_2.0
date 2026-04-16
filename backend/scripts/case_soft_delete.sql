-- Case deletion: soft delete + cascaded hard delete + resource cleanup
--
-- Flow:
--   1. Firm project does: UPDATE cases SET deleted_at = NOW() WHERE id = ...
--      (Case project templates explicitly forbid DELETE/UPDATE deleted_at)
--   2. All queries filter WHERE deleted_at IS NULL (see firm instructions)
--   3. A nightly cron hard-deletes rows where deleted_at < NOW() - 30 days
--   4. Hard DELETE on cases fires cascade on child tables (Postgres FK) and
--      calls delete-case-resources edge function for Storage + Dropbox cleanup

-- 1. Soft delete column --------------------------------------------------
alter table public.cases
  add column if not exists deleted_at timestamptz;

create index if not exists idx_cases_deleted_at_null
  on public.cases (id)
  where deleted_at is null;

-- 2. Hard-delete trigger — fires edge function to clean up external state -
create or replace function public.trigger_delete_case_resources()
returns trigger
language plpgsql
security definer
as $$
declare
  edge_url text := 'https://wjxglyjitpqnldblxbew.supabase.co/functions/v1/delete-case-resources';
begin
  -- Fire-and-forget async HTTP call
  perform net.http_post(
    url := edge_url,
    headers := jsonb_build_object('Content-Type', 'application/json'),
    body := jsonb_build_object(
      'case_id', old.id::text,
      'case_name', old.case_name
    )
  );
  return old;
end;
$$;

drop trigger if exists cases_delete_resources on public.cases;

create trigger cases_delete_resources
  after delete on public.cases
  for each row
  execute function public.trigger_delete_case_resources();

-- 3. Nightly hard-delete job ---------------------------------------------
-- Requires pg_cron extension. Enable in Supabase dashboard → Database → Extensions.
create extension if not exists pg_cron;

-- Remove any prior schedule with this name, then create it
select cron.unschedule('hard_delete_soft_deleted_cases')
where exists (select 1 from cron.job where jobname = 'hard_delete_soft_deleted_cases');

select cron.schedule(
  'hard_delete_soft_deleted_cases',
  '0 3 * * *',  -- 3 AM UTC daily
  $$
    DELETE FROM public.cases
    WHERE deleted_at IS NOT NULL
      AND deleted_at < NOW() - INTERVAL '30 days'
  $$
);
