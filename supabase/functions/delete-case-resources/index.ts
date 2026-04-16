// Supabase Edge Function: delete-case-resources
//
// Cleans up the Supabase Storage objects + Dropbox folders for a deleted case.
// Called by the cases_hard_delete trigger after a row is actually removed from
// the cases table (i.e. the 30-day soft-delete window has elapsed, or the
// admin force-deleted the row manually).
//
// The Postgres CASCADE handles all child table cleanup. This function only
// handles the external side: Supabase Storage and Dropbox.
//
// Request body: { case_id: "uuid", case_name: "string" }
//
// Secrets required:
//   SUPABASE_URL
//   SUPABASE_SERVICE_ROLE_KEY
//   DROPBOX_APP_KEY
//   DROPBOX_APP_SECRET
//   DROPBOX_REFRESH_TOKEN
//   DROPBOX_WATCH_FOLDER   (optional, defaults to "/Legal Intake")

import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const DROPBOX_APP_KEY = Deno.env.get("DROPBOX_APP_KEY") ?? "";
const DROPBOX_APP_SECRET = Deno.env.get("DROPBOX_APP_SECRET") ?? "";
const DROPBOX_REFRESH_TOKEN = Deno.env.get("DROPBOX_REFRESH_TOKEN") ?? "";
const WATCH_FOLDER = (Deno.env.get("DROPBOX_WATCH_FOLDER") ?? "/Legal Intake").replace(/\/$/, "");

const CASE_BUCKETS = ["case-files", "external-law", "intake-queue"];

async function getDropboxAccessToken(): Promise<string> {
  const basic = btoa(`${DROPBOX_APP_KEY}:${DROPBOX_APP_SECRET}`);
  const resp = await fetch("https://api.dropbox.com/oauth2/token", {
    method: "POST",
    headers: {
      "Authorization": `Basic ${basic}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: DROPBOX_REFRESH_TOKEN,
    }),
  });
  if (!resp.ok) {
    throw new Error(`Dropbox token exchange failed (${resp.status}): ${(await resp.text()).slice(0, 300)}`);
  }
  return (await resp.json()).access_token as string;
}

async function deleteDropboxFolder(accessToken: string, path: string) {
  const resp = await fetch("https://api.dropboxapi.com/2/files/delete_v2", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ path }),
  });
  if (resp.ok) return { status: "deleted" };
  const errText = await resp.text();
  // path/not_found — folder already gone, treat as success
  if (errText.includes("not_found")) return { status: "already_gone" };
  return { status: "error", error: errText.slice(0, 300) };
}

async function deleteSupabaseStoragePrefix(
  supabase: ReturnType<typeof createClient>,
  bucket: string,
  prefix: string,
): Promise<{ deleted: number; error?: string }> {
  // List all files under the prefix (paginated)
  const { data: files, error: listErr } = await supabase.storage
    .from(bucket)
    .list(prefix, { limit: 1000, offset: 0 });

  if (listErr) {
    return { deleted: 0, error: `list failed: ${listErr.message}` };
  }
  if (!files || files.length === 0) return { deleted: 0 };

  // Recurse into subfolders
  let totalDeleted = 0;
  const topLevelFiles: string[] = [];

  for (const item of files) {
    const fullPath = `${prefix}/${item.name}`;
    // Supabase list() returns folders as entries with no `id` / metadata
    if (item.id === null || item.metadata === null) {
      const sub = await deleteSupabaseStoragePrefix(supabase, bucket, fullPath);
      totalDeleted += sub.deleted;
    } else {
      topLevelFiles.push(fullPath);
    }
  }

  if (topLevelFiles.length > 0) {
    const { data: deleted, error: rmErr } = await supabase.storage
      .from(bucket)
      .remove(topLevelFiles);
    if (rmErr) {
      return { deleted: totalDeleted, error: `remove failed: ${rmErr.message}` };
    }
    totalDeleted += deleted?.length ?? 0;
  }

  return { deleted: totalDeleted };
}

Deno.serve(async (req) => {
  const cors = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
  };
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });

  try {
    if (!SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY) {
      return new Response(
        JSON.stringify({ error: "Supabase credentials not configured" }),
        { status: 500, headers: { ...cors, "Content-Type": "application/json" } },
      );
    }
    if (!DROPBOX_APP_KEY || !DROPBOX_APP_SECRET || !DROPBOX_REFRESH_TOKEN) {
      return new Response(
        JSON.stringify({ error: "Dropbox secrets not configured" }),
        { status: 500, headers: { ...cors, "Content-Type": "application/json" } },
      );
    }

    const body = await req.json().catch(() => ({}));
    const caseId: string = (body.case_id ?? "").toString().trim();
    const caseName: string = (body.case_name ?? "").toString().trim();

    if (!caseId || !caseName) {
      return new Response(
        JSON.stringify({ error: "case_id and case_name are required" }),
        { status: 400, headers: { ...cors, "Content-Type": "application/json" } },
      );
    }

    const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

    // 1. Delete Supabase Storage objects in each bucket under {case_id}/
    const storageResults: Record<string, { deleted: number; error?: string }> = {};
    for (const bucket of CASE_BUCKETS) {
      storageResults[bucket] = await deleteSupabaseStoragePrefix(supabase, bucket, caseId);
    }

    // 2. Delete Dropbox folders
    const accessToken = await getDropboxAccessToken();
    const caseFolder = `${WATCH_FOLDER}/${caseName}`;
    const extFolder = `${WATCH_FOLDER}/_external/${caseName}`;

    const dropboxResults = {
      [caseFolder]: await deleteDropboxFolder(accessToken, caseFolder),
      [extFolder]: await deleteDropboxFolder(accessToken, extFolder),
    };

    const anyError =
      Object.values(storageResults).some((r) => r.error) ||
      Object.values(dropboxResults).some((r) => r.status === "error");

    return new Response(
      JSON.stringify({
        status: anyError ? "partial" : "ok",
        case_id: caseId,
        case_name: caseName,
        storage: storageResults,
        dropbox: dropboxResults,
      }),
      { headers: { ...cors, "Content-Type": "application/json" } },
    );
  } catch (e) {
    return new Response(
      JSON.stringify({ error: e instanceof Error ? e.message : String(e) }),
      { status: 500, headers: { ...cors, "Content-Type": "application/json" } },
    );
  }
});
