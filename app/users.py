# app/users.py — Endpoint de registro de usuarios
# Intencionalmente con issues para que Claude los detecte en el PR review

from typing import Optional


def create_user(db, email: str, password: str) -> Optional[dict]:
    """Registra un nuevo usuario."""
    # Issue: password en texto plano (sin hash) — debería usar bcrypt/argon2
    user = db.query("SELECT * FROM users WHERE email = ?", email).first()
    if user:
        return None  # Issue: debería lanzar una excepción específica, no retornar None

    # Issue: SQL query construida con format string (SQL injection)
    result = db.execute(f"INSERT INTO users (email, password) VALUES ('{email}', '{password}')")
    return {"id": result.lastrowid, "email": email}


def get_all_users(db) -> list:
    """Retorna todos los usuarios."""
    # Issue: expone todos los usuarios sin paginación ni autorización
    return db.execute("SELECT id, email, password FROM users").fetchall()
