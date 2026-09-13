CREATE TABLE IF NOT EXISTS public.profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    full_name TEXT,
    email TEXT UNIQUE,
    role TEXT NOT NULL DEFAULT 'citizen' CHECK (role IN ('citizen', 'officer', 'admin')),
    phone TEXT,
    badge_number TEXT,
    avatar_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_profiles_email ON public.profiles (lower(email));
ALTER TABLE public.profiles
    ADD COLUMN IF NOT EXISTS requested_role TEXT;

CREATE INDEX IF NOT EXISTS idx_profiles_role ON public.profiles (role);

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION public.is_admin()
RETURNS BOOLEAN AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1
        FROM public.profiles
        WHERE id = auth.uid()
          AND role = 'admin'
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP POLICY IF EXISTS "Profiles are readable by everyone" ON public.profiles;
DROP POLICY IF EXISTS "Users can insert own profile" ON public.profiles;
DROP POLICY IF EXISTS "Users can read own profile" ON public.profiles;
DROP POLICY IF EXISTS "Admins can read all profiles" ON public.profiles;
DROP POLICY IF EXISTS "Users can update own profile" ON public.profiles;
DROP POLICY IF EXISTS "Service role full access to profiles" ON public.profiles;

CREATE POLICY "Users can read own profile"
    ON public.profiles FOR SELECT
    TO authenticated
    USING (auth.uid() = id);

CREATE POLICY "Admins can read all profiles"
    ON public.profiles FOR SELECT
    TO authenticated
    USING (public.is_admin());

CREATE POLICY "Users can insert own profile"
    ON public.profiles FOR INSERT
    TO authenticated
    WITH CHECK (auth.uid() = id);

CREATE POLICY "Users can update own profile"
    ON public.profiles FOR UPDATE
    TO authenticated
    USING (auth.uid() = id)
    WITH CHECK (auth.uid() = id);

-- RLS alone cannot stop a user from setting their own role: an UPDATE policy's
-- WITH CHECK only sees the new row. This trigger pins role/requested_role to
-- their previous values unless the caller is an admin or the service role, so
-- "update profiles set role='admin' where id = auth.uid()" is a no-op.
CREATE OR REPLACE FUNCTION public.protect_profile_role()
RETURNS trigger AS $$
BEGIN
    IF auth.role() = 'service_role' OR public.is_admin() THEN
        RETURN new;
    END IF;
    new.role := old.role;
    new.requested_role := old.requested_role;
    RETURN new;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS protect_profile_role ON public.profiles;
CREATE TRIGGER protect_profile_role
    BEFORE UPDATE ON public.profiles
    FOR EACH ROW EXECUTE FUNCTION public.protect_profile_role();

CREATE POLICY "Service role full access to profiles"
    ON public.profiles FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger AS $$
BEGIN
    -- Roles are never taken from client-supplied metadata: anyone can call
    -- supabase.auth.signUp() with role='officer'. Everyone lands as a citizen
    -- (except the configured admin email) and an officer request is recorded
    -- in requested_role for an admin to approve.
    INSERT INTO public.profiles (id, full_name, email, role, requested_role, badge_number, avatar_url)
    VALUES (
        new.id,
        COALESCE(new.raw_user_meta_data->>'full_name', new.raw_user_meta_data->>'name', split_part(new.email, '@', 1)),
        lower(new.email),
        CASE
            WHEN lower(new.email) = 'prathameshdahe1@gmail.com' THEN 'admin'
            ELSE 'citizen'
        END,
        NULLIF(new.raw_user_meta_data->>'requested_role', 'citizen'),
        new.raw_user_meta_data->>'badge_number',
        new.raw_user_meta_data->>'avatar_url'
    )
    ON CONFLICT (id) DO UPDATE SET
        email = EXCLUDED.email,
        full_name = COALESCE(EXCLUDED.full_name, public.profiles.full_name),
        role = CASE
            WHEN lower(EXCLUDED.email) = 'prathameshdahe1@gmail.com' THEN 'admin'
            ELSE COALESCE(public.profiles.role, EXCLUDED.role)
        END,
        requested_role = COALESCE(EXCLUDED.requested_role, public.profiles.requested_role),
        badge_number = COALESCE(EXCLUDED.badge_number, public.profiles.badge_number),
        avatar_url = COALESCE(EXCLUDED.avatar_url, public.profiles.avatar_url),
        updated_at = now();
    RETURN new;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

GRANT ALL ON TABLE public.profiles TO postgres, service_role;
GRANT SELECT, INSERT, UPDATE ON TABLE public.profiles TO authenticated;
GRANT EXECUTE ON FUNCTION public.is_admin() TO authenticated;

INSERT INTO public.profiles (id, email, full_name, role)
SELECT id, lower(email), 'Prathamesh Dahe', 'admin'
FROM auth.users
WHERE lower(email) = 'prathameshdahe1@gmail.com'
ON CONFLICT (id) DO UPDATE SET
    email = EXCLUDED.email,
    full_name = EXCLUDED.full_name,
    role = 'admin',
    updated_at = now();
