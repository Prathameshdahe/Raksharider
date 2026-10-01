-- ============================================================================
-- 008_service_role_grants.sql
-- RoadWatch.AI — give the backend (service_role) the table privileges it needs.
-- Idempotent. Run in the Supabase SQL editor after 007.
--
-- Why: on the live project (checked 1 Oct 2026 via pg_class.relacl) the tables created
-- in 002 carry only TRUNCATE/REFERENCES/TRIGGER/MAINTAIN for service_role — no SELECT,
-- INSERT or UPDATE. 003 granted ALL on the v3 tables, but nothing ever granted the
-- 002-era tables. The backend talks to Postgres as service_role, so today:
--   system_settings  → /health?deep=1 "permission denied", /status never sees the worker
--                      heartbeat, /admin/queue + /admin/settings + pause fail, quota falls
--                      back to the default
--   audit_log        → every write_audit() insert is refused (logged, then dropped)
--   vehicle_records  → GET /cases, GET /cases/{id}, GET /videos/{id} cannot enrich plates
--   violation_policy → POST /videos/upload/init with a declared violation fails
-- service_role bypasses RLS, so these are plain GRANTs; no policies change.
-- ============================================================================

grant select, insert, update          on public.system_settings  to service_role;
grant select, insert                  on public.audit_log        to service_role;
grant select                          on public.vehicle_records  to service_role;
grant select                          on public.violation_policy to service_role;
grant select                          on public.notifications    to service_role;
grant select                          on public.score_ledger     to service_role;
grant usage, select on all sequences in schema public to service_role;

-- Future tables created from the SQL editor (as postgres) get the same treatment.
alter default privileges for role postgres in schema public grant select, insert, update, delete on tables to service_role;
alter default privileges for role postgres in schema public grant usage, select on sequences to service_role;

-- ── Verification ─────────────────────────────────────────────────────────────
select t.relname,
       has_table_privilege('service_role', 'public.' || t.relname, 'select')  as can_select,
       has_table_privilege('service_role', 'public.' || t.relname, 'insert')  as can_insert,
       has_table_privilege('service_role', 'public.' || t.relname, 'update')  as can_update
  from pg_class t join pg_namespace n on n.oid = t.relnamespace
 where n.nspname = 'public'
   and t.relname in ('system_settings','audit_log','vehicle_records','violation_policy','videos','cases','findings','profiles')
 order by 1;
-- expect can_select = true on every row; can_insert/can_update = true for system_settings; can_insert = true for audit_log.
-- Then GET https://raksharider.onrender.com/health?deep=1 must report status "healthy" with database.denied = {}.
