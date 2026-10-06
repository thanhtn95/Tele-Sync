"""Apply pending DB migrations (the app also does this on startup).

    cd backend && python -m scripts.migrate
"""
import asyncio

from app import db
from app.config import settings


async def main() -> None:
    pool = await db.create_pool(settings.database_url)
    try:
        applied = await db.migrate(pool)
        print("applied: " + (", ".join(applied) if applied else "nothing (up to date)"))
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
