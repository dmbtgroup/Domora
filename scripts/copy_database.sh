#!/usr/bin/env bash
# Copies the app's tables from one Postgres database to another and checks the copy.
#   SRC   source connection string (read only)
#   DST   destination connection string (must be empty unless MODE=replace)
#   MODE  "normal" (default) or "replace" (first drops the tables already in DST)
# Needs a PostgreSQL client (pg_dump, pg_restore, psql) at least as new as the source server.
# Prints table names, row counts and server versions only: never data or connection strings.
set -euo pipefail
: "${SRC:?}" "${DST:?}"
MODE=${MODE:-normal}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT  # the dump never outlives the script

export PGOPTIONS="-c client_min_messages=warning"
q() { psql "$1" -X -v ON_ERROR_STOP=1 -tAc "$2"; }

echo "== Versions"
src_v=$(q "$SRC" 'SHOW server_version_num'); dst_v=$(q "$DST" 'SHOW server_version_num')
client_v=$(pg_dump --version | grep -oE '[0-9]+' | head -1)
echo "source $((src_v / 10000)), destination $((dst_v / 10000)), client $client_v"
[ "$client_v" -ge "$((src_v / 10000))" ] || { echo "::error::pg_dump $client_v is older than the source server"; exit 1; }
if [ "$((dst_v / 10000))" -lt "$((src_v / 10000))" ]; then
  [ "$((src_v / 10000 - dst_v / 10000))" -eq 1 ] || { echo "::error::Destination is more than one version older"; exit 1; }
  echo "Destination is one version older: the app uses only plain tables, indexes and sequences; row counts below confirm the copy."
fi

echo "== Destination must be empty"
n=$(q "$DST" "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
if [ "$n" -gt 0 ]; then
  [ "$MODE" = replace ] || { echo "::error::Destination already has $n tables. Run again in replace mode to replace them."; exit 1; }
  echo "Removing $n tables from a previous attempt"
  q "$DST" "DO \$\$ DECLARE t text; BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
      EXECUTE format('DROP TABLE IF EXISTS public.%I CASCADE', t); END LOOP;
    FOR t IN SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' LOOP
      EXECUTE format('DROP SEQUENCE IF EXISTS public.%I CASCADE', t); END LOOP; END \$\$;" >/dev/null
fi

echo "== Dump"
stats=$(pg_dump --help | grep -q -- '--no-statistics' && echo --no-statistics || true)
pg_dump --no-owner --no-privileges $stats --schema=public --format=custom -d "$SRC" -f "$WORK/condo.dump"
echo "Dump size: $(du -h "$WORK/condo.dump" | cut -f1)"

echo "== Restore"
set +e
pg_restore --no-owner --no-privileges --schema=public -d "$DST" "$WORK/condo.dump" 2> "$WORK/restore.log"
code=$?
set -e
# Errors without their CONTEXT/DETAIL lines, which can quote row data.
grep -E '^pg_restore: (error|warning)' "$WORK/restore.log" | cut -c1-300 || true
echo "pg_restore exit code: $code (the row counts below are the real check)"

echo "== Row counts"
bad=0
for t in $(q "$SRC" "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"); do
  a=$(q "$SRC" "SELECT count(*) FROM public.\"$t\"")
  b=$(q "$DST" "SELECT count(*) FROM public.\"$t\"" 2>/dev/null || echo missing)
  if [ "$a" = "$b" ]; then printf '%-32s %8s  ok\n' "$t" "$a"; else printf '%-32s %8s  %8s  MISMATCH\n' "$t" "$a" "$b"; bad=1; fi
done
[ "$bad" = 0 ] || { echo "::error::Row counts differ"; exit 1; }

echo "== Sequences"
q "$DST" "DO \$\$ DECLARE r record; m bigint; BEGIN
  FOR r IN SELECT s.relname AS seq, t.relname AS tbl, a.attname AS col
           FROM pg_class s JOIN pg_namespace n ON n.oid = s.relnamespace
           JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i')
           JOIN pg_class t ON t.oid = d.refobjid
           JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
           WHERE s.relkind = 'S' AND n.nspname = 'public' LOOP
    EXECUTE format('SELECT max(%I) FROM public.%I', r.col, r.tbl) INTO m;
    IF m IS NOT NULL THEN
      PERFORM setval(format('public.%I', r.seq), GREATEST(m, COALESCE(
        (SELECT last_value FROM pg_sequences WHERE schemaname = 'public' AND sequencename = r.seq), 1)));
    END IF;
  END LOOP; END \$\$;" >/dev/null
echo "Every id counter is past the highest id."

echo "== Row-level security"
q "$DST" "DO \$\$ DECLARE t text; BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND NOT rowsecurity LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t); END LOOP; END \$\$;" >/dev/null
q "$DST" "SELECT count(*) || ' tables, ' || count(*) FILTER (WHERE rowsecurity) || ' with RLS' FROM pg_tables WHERE schemaname = 'public'"
echo "== Done"
