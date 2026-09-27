from app.database import SessionLocal, Base, engine
from app.models import User, Role
from app.security import hash_password


def seed():
    Base.metadata.create_all(bind=engine)  # safe no-op if tables already exist

    db = SessionLocal()
    try:
        seed_users = [
            ("shankar_admin", "admin123", Role.admin),
            ("shankar_user", "user123", Role.user),
            ("shankar_readonly", "readonly123", Role.readonly),
        ]

        for username, password, role in seed_users:
            existing = db.query(User).filter(User.username == username).first()
            if existing:
                print(f"Skipping '{username}' — already exists.")
                continue

            user = User(username=username, hashed_password=hash_password(password), role=role)
            db.add(user)
            print(f"Created '{username}' with role '{role.value}'.")

        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    seed()