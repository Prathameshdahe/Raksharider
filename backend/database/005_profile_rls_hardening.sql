-- 005_profile_rls_hardening.sql
-- -------------------------------------------------------------
-- Hardens public.profiles against direct PostgREST privilege escalation.
--
-- Background:
-- The "Users can update own profile" policy in auth_admin_setup.sql allows
-- authenticated users to update their own profile row.
-- Without column-level grant restrictions, an authenticated user can send
-- a direct PATCH request via PostgREST to update privileged columns like
-- `role`, `requested_role`, `role_requested_at`, `badge_number`, `verified_via`.
--
-- The backend FastAPI PUT /auth/profile endpoint only allows:
-- full_name, phone, avatar_url.
--
-- Fix:
-- Revoke generic UPDATE on public.profiles from authenticated, and grant
-- UPDATE only on the safe, user-editable columns.

REVOKE UPDATE ON public.profiles FROM authenticated;

GRANT UPDATE (full_name, phone, avatar_url, updated_at) ON public.profiles TO authenticated;

-- Ensure postgres and service_role retain full access
GRANT ALL ON public.profiles TO postgres, service_role;
