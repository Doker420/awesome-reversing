import aiosqlite
import asyncio
import time
from typing import Optional, List, Tuple
from config import DB_PATH, FREE_TOKENS, REFERRALS_NEEDED, REFERRAL_BONUS_TOKENS


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._lock = asyncio.Lock()

    # ─────────────────────────────────────────────────────────
    # Инициализация / миграция
    # ─────────────────────────────────────────────────────────
    async def init_db(self):
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL")

                # Пользователи
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id          INTEGER PRIMARY KEY,
                        username         TEXT,
                        first_name       TEXT,
                        tokens           INTEGER DEFAULT 0,
                        generations      INTEGER DEFAULT 0,   -- устаревшее, оставлено для совместимости
                        referrer_id      INTEGER,
                        referrals_count  INTEGER DEFAULT 0,
                        language         TEXT DEFAULT 'ru',
                        created_at       INTEGER,
                        last_active      INTEGER,
                        referral_notified_5m   INTEGER DEFAULT 0,
                        referral_notified_12h  INTEGER DEFAULT 0,
                        referral_notified_24h  INTEGER DEFAULT 0
                    )
                """)

                # Миграция: добавляем новые колонки если их нет
                cursor = await db.execute("PRAGMA table_info(users)")
                existing = {row[1] for row in await cursor.fetchall()}

                for col, definition in [
                    ("tokens",                "INTEGER DEFAULT 0"),
                    ("api_key",               "TEXT"),
                    ("referral_notified_5m",  "INTEGER DEFAULT 0"),
                    ("referral_notified_12h", "INTEGER DEFAULT 0"),
                    ("referral_notified_24h", "INTEGER DEFAULT 0"),
                    ("referrals_count",       "INTEGER DEFAULT 0"),
                ]:
                    if col not in existing:
                        try:
                            await db.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
                            await db.commit()
                        except Exception as e:
                            pass

                # Уникальный индекс для быстрого поиска и уникальности API ключа
                await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_api_key ON users(api_key)")
                await db.commit()

                # Если есть старые пользователи с generations, конвертируем
                # (250к токенов за каждую старую генерацию)
                if "generations" in existing:
                    await db.execute("""
                        UPDATE users SET tokens = tokens + generations * 250000
                        WHERE generations > 0 AND tokens = 0
                    """)

                # История генераций
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS generations (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id     INTEGER,
                        mode        TEXT DEFAULT 'landing',
                        prompt      TEXT,
                        file_path   TEXT,
                        status      TEXT,
                        tokens_used INTEGER DEFAULT 0,
                        created_at  INTEGER,
                        FOREIGN KEY (user_id) REFERENCES users (user_id)
                    )
                """)

                # Миграция generations
                cursor2 = await db.execute("PRAGMA table_info(generations)")
                gen_cols = {row[1] for row in await cursor2.fetchall()}
                for col, definition in [
                    ("mode",        "TEXT DEFAULT 'landing'"),
                    ("tokens_used", "INTEGER DEFAULT 0"),
                ]:
                    if col not in gen_cols:
                        try:
                            await db.execute(f"ALTER TABLE generations ADD COLUMN {col} {definition}")
                        except aiosqlite.OperationalError:
                            pass

                # Промокоды
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS promocodes (
                        code        TEXT PRIMARY KEY,
                        tokens      INTEGER DEFAULT 0,
                        generations INTEGER DEFAULT 0,  -- совместимость
                        max_uses    INTEGER,
                        used_count  INTEGER DEFAULT 0,
                        created_at  INTEGER,
                        expires_at  INTEGER
                    )
                """)

                # Использование промокодов
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS promocode_usage (
                        user_id  INTEGER,
                        code     TEXT,
                        used_at  INTEGER,
                        PRIMARY KEY (user_id, code)
                    )
                """)

                # Платежи
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS payments (
                        external_id TEXT PRIMARY KEY,
                        user_id     INTEGER,
                        amount      REAL,
                        plan        TEXT,
                        tokens      INTEGER DEFAULT 0,
                        method      TEXT,
                        status      TEXT DEFAULT 'pending',
                        created_at  INTEGER
                    )
                """)

                # Миграция payments
                cursor3 = await db.execute("PRAGMA table_info(payments)")
                pay_cols = {row[1] for row in await cursor3.fetchall()}
                if "tokens" not in pay_cols:
                    try:
                        await db.execute("ALTER TABLE payments ADD COLUMN tokens INTEGER DEFAULT 0")
                    except aiosqlite.OperationalError:
                        pass

                await db.commit()

    # ─────────────────────────────────────────────────────────
    # Пользователи
    # ─────────────────────────────────────────────────────────
    async def add_user(self, user_id: int, username: str, first_name: str,
                       referrer_id: Optional[int] = None, language: str = 'ru') -> bool:
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    now = int(time.time())
                    await db.execute("""
                        INSERT INTO users
                            (user_id, username, first_name, tokens, generations,
                             referrer_id, language, created_at, last_active, referrals_count)
                        VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, 0)
                    """, (user_id, username, first_name, FREE_TOKENS, referrer_id, language, now, now))

                    if referrer_id:
                        await db.execute(
                            "UPDATE users SET referrals_count = referrals_count + 1 WHERE user_id = ?",
                            (referrer_id,)
                        )
                        cursor = await db.execute(
                            "SELECT referrals_count FROM users WHERE user_id = ?", (referrer_id,)
                        )
                        row = await cursor.fetchone()
                        if row and row[0] % REFERRALS_NEEDED == 0:
                            await db.execute(
                                "UPDATE users SET tokens = tokens + ? WHERE user_id = ?",
                                (REFERRAL_BONUS_TOKENS, referrer_id)
                            )

                    await db.commit()
                    return True
                except aiosqlite.IntegrityError:
                    return False

    async def get_user(self, user_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_user_by_api_key(self, api_key: str) -> Optional[dict]:
        if not api_key:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE api_key = ?", (api_key,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def set_user_api_key(self, user_id: int, api_key: str) -> bool:
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("UPDATE users SET api_key = ? WHERE user_id = ?", (api_key, user_id))
                    await db.commit()
                    return True
                except aiosqlite.IntegrityError:
                    return False

    async def get_or_create_api_key(self, user_id: int) -> str:
        """Возвращает существующий API ключ или создаёт новый для пользователя."""
        user = await self.get_user(user_id)
        if user and user.get("api_key"):
            return user["api_key"]
        
        import secrets
        new_key = f"lg_{secrets.token_hex(20)}"
        await self.set_user_api_key(user_id, new_key)
        return new_key

    async def regenerate_api_key(self, user_id: int) -> str:
        """Перегенерирует API ключ (старый перестаёт работать)."""
        import secrets
        new_key = f"lg_{secrets.token_hex(20)}"
        await self.set_user_api_key(user_id, new_key)
        return new_key

    async def update_last_active(self, user_id: int):
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE users SET last_active = ? WHERE user_id = ?", (int(time.time()), user_id)
                )
                await db.commit()

    async def set_language(self, user_id: int, language: str):
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE users SET language = ? WHERE user_id = ?", (language, user_id)
                )
                await db.commit()

    async def update_language(self, user_id: int, language: str):
        await self.set_language(user_id, language)

    # ─────────────────────────────────────────────────────────
    # Токены
    # ─────────────────────────────────────────────────────────
    async def add_tokens(self, user_id: int, amount: int) -> bool:
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
                if not await cursor.fetchone():
                    return False
                await db.execute(
                    "UPDATE users SET tokens = tokens + ? WHERE user_id = ?", (amount, user_id)
                )
                await db.commit()
                return True

    async def spend_tokens(self, user_id: int, amount: int) -> bool:
        """Списываем токены. Возвращает False если недостаточно."""
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT tokens FROM users WHERE user_id = ?", (user_id,)
                )
                row = await cursor.fetchone()
                if not row or row[0] < amount:
                    return False
                await db.execute(
                    "UPDATE users SET tokens = tokens - ? WHERE user_id = ?", (amount, user_id)
                )
                await db.commit()
                return True

    # Обратная совместимость с bot.py (старый API)
    async def add_generations(self, user_id: int, amount: int) -> bool:
        """Добавляем токены эквивалентные N генерациям (250к за штуку)."""
        from config import COST_IMAGE
        return await self.add_tokens(user_id, amount * COST_IMAGE)

    async def use_generation(self, user_id: int) -> bool:
        """Совместимость: списываем токены как за 1 лендинг."""
        from config import COST_LANDING
        return await self.spend_tokens(user_id, COST_LANDING)

    # ─────────────────────────────────────────────────────────
    # Рефералы
    # ─────────────────────────────────────────────────────────
    async def add_referral(self, referrer_id: int, user_id: int) -> bool:
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT user_id FROM users WHERE user_id = ? AND referrer_id = ?",
                    (user_id, referrer_id)
                )
                if await cursor.fetchone():
                    return False

                await db.execute(
                    "UPDATE users SET referrals_count = referrals_count + 1 WHERE user_id = ?",
                    (referrer_id,)
                )
                await db.execute(
                    "UPDATE users SET referrer_id = ? WHERE user_id = ?", (referrer_id, user_id)
                )

                cursor = await db.execute(
                    "SELECT referrals_count FROM users WHERE user_id = ?", (referrer_id,)
                )
                row = await cursor.fetchone()
                if row and row[0] % REFERRALS_NEEDED == 0:
                    await db.execute(
                        "UPDATE users SET tokens = tokens + ? WHERE user_id = ?",
                        (REFERRAL_BONUS_TOKENS, referrer_id)
                    )

                await db.commit()
                return True

    # ─────────────────────────────────────────────────────────
    # История генераций
    # ─────────────────────────────────────────────────────────
    async def add_generation_record(self, user_id: int, prompt: str, file_path: str,
                                    status: str, mode: str = "landing", tokens_used: int = 0):
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT INTO generations (user_id, mode, prompt, file_path, status, tokens_used, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (user_id, mode, prompt, file_path, status, tokens_used, int(time.time())))
                await db.commit()

    async def get_user_history(self, user_id: int, limit: int = 10) -> List[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM generations WHERE user_id = ?
                ORDER BY created_at DESC LIMIT ?
            """, (user_id, limit))
            return [dict(r) for r in await cursor.fetchall()]

    # ─────────────────────────────────────────────────────────
    # Промокоды
    # ─────────────────────────────────────────────────────────
    async def create_promocode(self, code: str, tokens: int, max_uses: int,
                               expires_at: Optional[int] = None) -> bool:
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("""
                        INSERT INTO promocodes (code, tokens, max_uses, used_count, created_at, expires_at)
                        VALUES (?, ?, ?, 0, ?, ?)
                    """, (code, tokens, max_uses, int(time.time()), expires_at))
                    await db.commit()
                    return True
                except aiosqlite.IntegrityError:
                    return False

    async def use_promocode(self, user_id: int, code: str) -> Tuple[bool, str]:
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("SELECT * FROM promocodes WHERE code = ?", (code.upper(),))
                promo = await cursor.fetchone()

                if not promo:
                    return False, "❌ Промокод не найден"

                # Колонки: code, tokens, generations, max_uses, used_count, created_at, expires_at
                p_code = promo[0]
                p_tokens = promo[1] or 0
                p_generations = promo[2] or 0
                max_uses = promo[3]
                used_count = promo[4]
                expires_at = promo[6]

                if expires_at and int(time.time()) > expires_at:
                    return False, "❌ Промокод истёк"
                if used_count >= max_uses:
                    return False, "❌ Промокод больше не действителен"

                cursor = await db.execute(
                    "SELECT * FROM promocode_usage WHERE user_id = ? AND code = ?", (user_id, p_code)
                )
                if await cursor.fetchone():
                    return False, "❌ Вы уже использовали этот промокод"

                # Конвертируем старые промокоды с generations в токены
                total_tokens = p_tokens + p_generations * 250_000

                await db.execute(
                    "UPDATE users SET tokens = tokens + ? WHERE user_id = ?", (total_tokens, user_id)
                )
                await db.execute(
                    "UPDATE promocodes SET used_count = used_count + 1 WHERE code = ?", (p_code,)
                )
                await db.execute(
                    "INSERT INTO promocode_usage (user_id, code, used_at) VALUES (?, ?, ?)",
                    (user_id, p_code, int(time.time()))
                )
                await db.commit()

                def fmt(n):
                    if n >= 1_000_000:
                        return f"{n/1_000_000:.1f}М"
                    if n >= 1_000:
                        return f"{n//1_000}К"
                    return str(n)

                return True, f"✅ Промокод активирован! +{fmt(total_tokens)} токенов"

    # ─────────────────────────────────────────────────────────
    # Статистика
    # ─────────────────────────────────────────────────────────
    async def get_stats(self) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            day_ago = int(time.time()) - 86400
            total_users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
            new_users_24h = (await (await db.execute(
                "SELECT COUNT(*) FROM users WHERE created_at > ?", (day_ago,)
            )).fetchone())[0]
            active_users_24h = (await (await db.execute(
                "SELECT COUNT(*) FROM users WHERE last_active > ?", (day_ago,)
            )).fetchone())[0]
            total_gens = (await (await db.execute("SELECT COUNT(*) FROM generations")).fetchone())[0]
            success_gens = (await (await db.execute(
                "SELECT COUNT(*) FROM generations WHERE status = 'success'"
            )).fetchone())[0]
            total_tokens_issued = (await (await db.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM payments WHERE status = 'paid'"
            )).fetchone())[0]
            return {
                'total_users': total_users,
                'new_users_24h': new_users_24h,
                'active_users_24h': active_users_24h,
                'total_generations': total_gens,
                'successful_generations': success_gens,
                'total_tokens_issued': total_tokens_issued,
                'total_promocodes': (await (await db.execute(
                    "SELECT COUNT(*) FROM promocodes")).fetchone())[0],
                'used_promocodes': (await (await db.execute(
                    "SELECT COUNT(*) FROM promocode_usage")).fetchone())[0],
            }

    async def get_top_referrers(self, limit: int = 10) -> List[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT user_id, username, first_name, referrals_count
                FROM users WHERE referrals_count > 0
                ORDER BY referrals_count DESC LIMIT ?
            """, (limit,))
            return [dict(row) for row in await cursor.fetchall()]

    async def get_all_user_ids(self) -> List[int]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT user_id FROM users")
            return [row[0] for row in await cursor.fetchall()]

    async def get_all_users(self) -> List[int]:
        return await self.get_all_user_ids()

    # ─────────────────────────────────────────────────────────
    # Платежи
    # ─────────────────────────────────────────────────────────
    async def create_payment(self, user_id: int, amount: float, plan: str,
                             method: str, external_id: str, tokens: int = 0) -> bool:
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("""
                        INSERT INTO payments (external_id, user_id, amount, plan, tokens, method, status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                    """, (external_id, user_id, amount, plan, tokens, method, int(time.time())))
                    await db.commit()
                    return True
                except aiosqlite.IntegrityError:
                    return False

    async def get_payment_by_external_id(self, external_id: str) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM payments WHERE external_id = ?", (external_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_payment_status(self, external_id: str, status: str) -> bool:
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT external_id FROM payments WHERE external_id = ?", (external_id,)
                )
                if not await cursor.fetchone():
                    return False
                await db.execute(
                    "UPDATE payments SET status = ? WHERE external_id = ?", (status, external_id)
                )
                await db.commit()
                return True