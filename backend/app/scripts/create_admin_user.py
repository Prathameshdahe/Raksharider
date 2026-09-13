import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.database.supabase import supabase


ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "prathameshdahe1@gmail.com").strip().lower()


def main() -> None:
    password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not password:
        raise SystemExit("Set ADMIN_PASSWORD in backend/.env before running this script.")
    if len(password) < 8:
        raise SystemExit("ADMIN_PASSWORD must be at least 8 characters.")

    metadata = {
        "full_name": "Prathamesh Dahe",
        "role": "admin",
    }

    user_id = None
    try:
        created = supabase.auth.admin.create_user({
            "email": ADMIN_EMAIL,
            "password": password,
            "email_confirm": True,
            "user_metadata": metadata,
        })
        if created and getattr(created, "user", None):
            user_id = str(created.user.id)
            print(f"Created admin auth user: {ADMIN_EMAIL}")
    except Exception as exc:
        print(f"Create-user note: {exc}")

    if not user_id:
        users = supabase.auth.admin.list_users()
        for user in getattr(users, "users", users):
            if getattr(user, "email", "").lower() == ADMIN_EMAIL:
                user_id = str(user.id)
                break
        if not user_id:
            raise SystemExit(
                "Admin user already exists but could not be found by the API. "
                "Create it in Supabase Auth, then rerun this script."
            )
        supabase.auth.admin.update_user_by_id(user_id, {
            "password": password,
            "email_confirm": True,
            "user_metadata": metadata,
        })
        print(f"Updated admin auth user: {ADMIN_EMAIL}")

    profile = {
        "id": user_id,
        "email": ADMIN_EMAIL,
        "full_name": "Prathamesh Dahe",
        "role": "admin",
    }
    supabase.table("profiles").upsert(profile).execute()
    print(f"Admin profile ready: {ADMIN_EMAIL} ({user_id})")


if __name__ == "__main__":
    main()
