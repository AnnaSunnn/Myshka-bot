import os
from dotenv import load_dotenv
load_dotenv()
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
DB_PATH = os.getenv("DB_PATH", "/data/bot.db")

# Скільки символів контексту (відібраних фрагментів) йде в LLM за один виклик.
# Обмежено через ліміти безкоштовного рівня Groq.
CONTEXT_CHARS = 6000

GROUPS = ["хвойний", "листяний", "біля річки"]

# Рукавичка -> частини лісу, до яких вона належить.
PLACES = {
    "Під ялиною": ["хвойний"],
    "У соснах": ["хвойний"],
    "Біля мурашника": ["хвойний"],
    "Під дубом": ["листяний"],
    "У ліщині": ["листяний"],
    "Під вербою": ["листяний", "біля річки"],
    "Біля бобрової греблі": ["біля річки"],
    "На узліссі": [],
}
