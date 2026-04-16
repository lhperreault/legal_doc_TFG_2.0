// Supabase Edge Function: create-dropbox-folders
//
// Creates the Dropbox folder tree for a new case:
//   /Legal Intake/{case_name}/_DROP FILES HERE/
//   /Legal Intake/{case_name}/{pleadings|contracts|discovery|...}/
//   /Legal Intake/_external/{case_name}/{case-law|legislation|legal-commentary}/
//
// Invocation (from Claude Desktop via Supabase MCP):
//   supabase.functions.invoke("create-dropbox-folders", {
//     body: { case_name: "Epic vs Apple" }
//   })
//
// Secrets required (set once with `supabase secrets set ...`):
//   DROPBOX_APP_KEY
//   DROPBOX_APP_SECRET
//   DROPBOX_REFRESH_TOKEN
//   DROPBOX_WATCH_FOLDER   (optional, defaults to "/Legal Intake")

import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const DROPBOX_APP_KEY = Deno.env.get("DROPBOX_APP_KEY") ?? "";
const DROPBOX_APP_SECRET = Deno.env.get("DROPBOX_APP_SECRET") ?? "";
const DROPBOX_REFRESH_TOKEN = Deno.env.get("DROPBOX_REFRESH_TOKEN") ?? "";
const WATCH_FOLDER = (Deno.env.get("DROPBOX_WATCH_FOLDER") ?? "/Legal Intake").replace(/\/$/, "");

const CASE_SUBFOLDERS = [
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
    const text = await resp.text();
    throw new Error(`Dropbox token exchange failed (${resp.status}): ${text.slice(0, 300)}`);
  }
  const json = await resp.json();
  return json.access_token as string;
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
  // 409 conflict = already exists, which is fine
  if (resp.status === 409 || errText.includes("conflict") || errText.includes("already")) {
    return { status: "already_existed" };
  }
  return { status: "error", error: errText.slice(0, 300) };
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

    const accessToken = await getDropboxAccessToken();

    const caseBase = `${WATCH_FOLDER}/${caseName}`;
    const extBase = `${WATCH_FOLDER}/_external/${caseName}`;

    const paths: string[] = [
      ...CASE_SUBFOLDERS.map((s) => `${caseBase}/${s}`),
      ...EXT_SUBFOLDERS.map((s) => `${extBase}/${s}`),
    ];

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
        dropbox_case_folder: caseBase,
        dropbox_external_folder: extBase,
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
