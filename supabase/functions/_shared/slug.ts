// Slug normalization — Deno/TS mirror of backend/utils/slug.py.
// Rules kept in sync:
//   - lowercase
//   - strip accents via NFKD + drop combining marks
//   - non-alphanumeric runs collapse to a single hyphen
//   - no leading/trailing hyphens

export function slugify(value: string): string {
  if (!value) return "";
  const nfkd = value.normalize("NFKD");
  // Drop combining marks (Unicode category Mn)
  const stripped = nfkd.replace(/\p{M}/gu, "");
  return stripped
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}
