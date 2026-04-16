// Supabase Edge Function: create-dropbox-folders
//
// Creates the Dropbox folder tree for a new case:
//   /Legal Intake/{case_name}/_DROP FILES HERE/
//   /Legal Intake/{case_name}/{pleadings|contracts|discovery|...}/
//   /Legal Intake/{case_name}/{parent}/{subslug}/   ← nested per cases.folder_structure
//   /Legal Intake/_external/{case_name}/{case-law|legislation|legal-commentary}/
//
// Invocation (from Postgres trigger after INSERT on cases):
//   { "case_id": "uuid", "case_name": "Epic vs Apple" }
//
// Or from Claude Desktop / callers for arbitrary name-based creation:
//   { "case_name": "Epic vs Apple" }
//
// Secrets required:
//   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY  (to read cases.folder_structure)
//   DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN
//   DROPBOX_WATCH_FOLDER  (optional, defaults to "/Legal Intake")

import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";
import { slugify } from "../_shared/slug.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const DROPBOX_APP_KEY = Deno.env.get("DROPBOX_APP_KEY") ?? "";
const DROPBOX_APP_SECRET = Deno.env.get("DROPBOX_APP_SECRET") ?? "";
const DROPBOX_REFRESH_TOKEN = Deno.env.get("DROPBOX_REFRESH_TOKEN") ?? "";
const WATCH_FOLDER = (Deno.env.get("DROPBOX_WATCH_FOLDER") ?? "/Legal Intake").replace(/\/$/, "");

// Default 7 parent subfolders (plus _DROP FILES HERE) — always created
const DEFAULT_CASE_SUBFOLDERS = [
  "_DROP FILES HERE",
  "pleadings",
  "contracts",
  "discovery",
  "evidence",
  "correspondence",
  "court-orders",
  "administrative",
];
const EXT_SUBFOLDERS = ["case-law", "legislation", "legal-commentary"];

// Only these parents are allowed to contain nested subfolders
const ALLOWED_PARENTS = new Set([
  "pleadings",
  "contracts",
  "discovery",
  "evidence",
  "correspondence",
  "court-orders",
  "administrative",
]);

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

async function createDropboxFolder(accessToken: string, path: string) {
  const resp = await fetch("https://api.dropboxapi.com/2/files/create_folder_v2", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ path, autorename: false }),
  });
  if (resp.ok) return { status: "created" };
  const errText = await resp.text();
  if (resp.status === 409 || errText.includes("conflict") || errText.includes("already")) {
    return { status: "already_existed" };
  }
  return { status: "error", error: errText.slice(0, 300) };
}

/**
 * Build the full list of folder paths to create for a case.
 *
 * @param caseName — human case name, used in the Dropbox path
 * @param folderStructure — JSON from cases.folder_structure, shape:
 *   { "<parent>": ["<subslug1>", "<subslug2>"], ... }
 *   Only parents in ALLOWED_PARENTS are honored; unknown parents are skipped.
 *   All subslugs are re-slugified defensively.
 */
function buildPaths(caseName: string, folderStructure: Record<string, string[]> | null): string[] {
  const caseBase = `${WATCH_FOLDER}/${caseName}`;
  const extBase = `${WATCH_FOLDER}/_external/${caseName}`;

  const paths: string[] = DEFAULT_CASE_SUBFOLDERS.map((s) => `${caseBase}/${s}`);
  paths.push(...EXT_SUBFOLDERS.map((s) => `${extBase}/${s}`));

  if (folderStructure && typeof folderStructure === "object") {
    for (const [parent, subs] of Object.entries(folderStructure)) {
      if (!ALLOWED_PARENTS.has(parent)) continue;
      if (!Array.isArray(subs)) continue;
      for (const sub of subs) {
        const slug = slugify(String(sub));
        if (slug) paths.push(`${caseBase}/${parent}/${slug}`);
      }
    }
  }

  return paths;
}

Deno.serve(async (req) => {
  const cors = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
  };
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });

  try {
    if (!DROPBOX_APP_KEY || !DROPBOX_APP_SECRET || !DROPBOX_REFRESH_TOKEN) {
      return new Response(
        JSON.stringify({ error: "Dropbox secrets not configured" }),
        { status: 500, headers: { ...cors, "Content-Type": "application/json" } },
      );
    }

    const body = await req.json().catch(() => ({}));
    const caseName: string = (body.case_name ?? "").toString().trim();
    const caseId: string = (body.case_id ?? "").toString().trim();

    if (!caseName) {
      return new Response(
        JSON.stringify({ error: "case_name is required" }),
        { status: 400, headers: { ...cors, "Content-Type": "application/json" } },
      );
    }
    if (caseName.includes("/") || caseName.includes("\\")) {
      return new Response(
        JSON.stringify({ error: "case_name cannot contain slashes" }),
        { status: 400, headers: { ...cors, "Content-Type": "application/json" } },
      );
    }

    // Look up folder_structure from the cases table if we have credentials
    let folderStructure: Record<string, string[]> | null = null;
    if (SUPABASE_URL && SUPABASE_SERVICE_ROLE_KEY) {
      const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);
      const query = supabase.from("cases").select("folder_structure");
      const { data, error } = caseId
        ? await query.eq("id", caseId).maybeSingle()
        : await query.eq("case_name", caseName).order("created_at", { ascending: false }).limit(1).maybeSingle();
      if (!error && data && data.folder_structure) {
        folderStructure = data.folder_structure as Record<string, string[]>;
      }
    }

    const accessToken = await getDropboxAccessToken();
    const paths = buildPaths(caseName, folderStructure);

    const created: string[] = [];
    const alreadyExisted: string[] = [];
    const errors: Array<{ path: string; error: string }> = [];

    for (const path of paths) {
      const result = await createDropboxFolder(accessToken, path);
      if (result.status === "created") created.push(path);
      else if (result.status === "already_existed") alreadyExisted.push(path);
      else errors.push({ path, error: result.error ?? "unknown" });
    }

    return new Response(
      JSON.stringify({
        status: errors.length === 0 ? "ok" : "partial",
        case_name: caseName,
        case_id: caseId || null,
        used_folder_structure: folderStructure !== null,
        total_paths: paths.length,
        created,
        already_existed: alreadyExisted,
        errors,
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
