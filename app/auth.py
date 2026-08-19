import bcrypt
import pymysql

from app import database as db
from app.errors import AuthError, ValidationError

MAX_FAILED_ATTEMPTS = 5


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValidationError("Password must be at least 8 characters")
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def register_user(username, password, customer_id=None, role="CUSTOMER") -> int:
    if role not in ("CUSTOMER", "TELLER", "ADMIN"):
        raise ValidationError("role must be CUSTOMER, TELLER or ADMIN")
    if role == "CUSTOMER" and customer_id is None:
        raise ValidationError("A CUSTOMER login must be linked to a customer_id")
    if role != "CUSTOMER":
        customer_id = None

    password_hash = hash_password(password)
    with db.transaction() as cur:
        try:
            cur.execute(
                """
                INSERT INTO users (customer_id, username, password_hash, role)
                VALUES (%s, %s, %s, %s)
                """,
                (customer_id, username.strip(), password_hash, role),
            )
        except pymysql.err.IntegrityError as exc:
            raise ValidationError(f"Cannot create user: {exc.args[1]}") from exc
        return cur.lastrowid


def login(username, password, ip_address=None) -> dict:
    user = db.query_one("SELECT * FROM users WHERE username = %s", (username,))

    if user is None:
        with db.transaction() as cur:
            db.audit(cur, "LOGIN_FAILED", ip_address=ip_address,
                     details={"username": username, "reason": "no such user"})
        raise AuthError("Invalid username or password")

    if user["status"] != "ACTIVE":
        with db.transaction() as cur:
            db.audit(cur, "LOGIN_FAILED", user_id=user["user_id"], ip_address=ip_address,
                     details={"reason": f"account {user['status']}"})
        raise AuthError(f"Account is {user['status']}")

    if not verify_password(password, user["password_hash"]):
        with db.transaction() as cur:
            cur.execute(
                """
                UPDATE users
                SET failed_login_attempts = failed_login_attempts + 1,
                    status = IF(failed_login_attempts + 1 >= %s, 'LOCKED', status)
                WHERE user_id = %s
                """,
                (MAX_FAILED_ATTEMPTS, user["user_id"]),
            )
            db.audit(cur, "LOGIN_FAILED", user_id=user["user_id"], ip_address=ip_address,
                     details={"reason": "bad password"})
        raise AuthError("Invalid username or password")

    with db.transaction() as cur:
        cur.execute(
            """
            UPDATE users
            SET failed_login_attempts = 0, last_login_at = CURRENT_TIMESTAMP(3)
            WHERE user_id = %s
            """,
            (user["user_id"],),
        )
        db.audit(cur, "LOGIN", user_id=user["user_id"], ip_address=ip_address)

    user.pop("password_hash", None)
    return user


def get_user(user_id) -> dict | None:
    row = db.query_one(
        """
        SELECT user_id, customer_id, username, role, status,
               failed_login_attempts, last_login_at, created_at
        FROM users WHERE user_id = %s
        """,
        (user_id,),
    )
    return row
