-- Per-case folder structure (matter-aware subfolders inside the 7 parents)
--
-- cases.matter_type       — e.g. "contract_dispute", "patent_infringement", slug form
-- cases.folder_structure  — JSON tree: { "<parent>": ["<subslug>", "<subslug>"], ... }
--                           where <parent> is one of the 7 defaults
-- cases.folder_labels     — JSON map: { "<subslug>": { "en": "Display (EN)", "es": "Display (ES)" } }
--
-- documents.folder_parent — e.g. "pleadings"            (one of the 7)
-- documents.folder_subslug— e.g. "motion-to-dismiss"    (nested subfolder under the parent; nullable)

alter table public.cases
  add column if not exists matter_type text,
  add column if not exists folder_structure jsonb,
  add column if not exists folder_labels jsonb;

alter table public.documents
  add column if not exists folder_parent text,
  add column if not exists folder_subslug text;

create index if not exists idx_documents_folder
  on public.documents (folder_parent, folder_subslug)
  where folder_parent is not null;
