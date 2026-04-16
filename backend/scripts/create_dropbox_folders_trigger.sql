-- Postgres trigger: when a new case is inserted, call the create-dropbox-folders
-- edge function to create the Dropbox folder tree automatically.
--
-- This means Claude Desktop only needs to INSERT into cases; the Dropbox side
-- happens server-side with zero manual steps, zero bash/HTTP calls from the
-- chat client.
--
-- Prerequisites:
--   1. Edge function 'create-dropbox-folders' deployed with --no-verify-jwt
--      (so the trigger can call it without passing a JWT)
--   2. pg_net extension enabled (Supabase has it by default)

-- Enable pg_net (idempotent)
create extension if not exists pg_net with schema extensions;

-- Trigger function
create or replace function public.trigger_create_dropbox_folders()
returns trigger
language plpgsql
security definer
as $$
declare
  edge_url text := 'https://wjxglyjitpqnldblxbew.supabase.co/functions/v1/create-dropbox-folders';
begin
  -- Fire-and-forget async HTTP call; pg_net returns a request_id we ignore.
  perform net.http_post(
    url := edge_url,
    headers := jsonb_build_object('Content-Type', 'application/json'),
    body := jsonb_build_object(
      'case_id', new.id::text,
      'case_name', new.case_name
    )
  );
  return new;
end;
$$;

-- Drop and recreate the trigger (idempotent)
drop trigger if exists cases_create_dropbox_folders on public.cases;

create trigger cases_create_dropbox_folders
  after insert on public.cases
  for each row
  execute function public.trigger_create_dropbox_folders();
