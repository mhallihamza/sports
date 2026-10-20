import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")

    # WhatsApp (Meta Cloud API). Leave blank to run in mock mode.
    WHATSAPP_TOKEN: str = os.getenv("WHATSAPP_TOKEN", "")
    WHATSAPP_PHONE_NUMBER_ID: str = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    WHATSAPP_VERIFY_TOKEN: str = os.getenv("WHATSAPP_VERIFY_TOKEN", "change_me")

    # LLMs. A single Mistral key powers every agent (matches the LangFlow wiring).
    # codestral-latest is the model this account's subscription tier allows.
    MISTRAL_API_KEY: str = os.getenv("MISTRAL_API_KEY", "")
    MISTRAL_MODEL: str = os.getenv("MISTRAL_MODEL", "codestral-latest")

    # Optional knowledge base (LangFlow FAQ flow used Astra DB for RAG).
    ASTRA_DB_API_ENDPOINT: str = os.getenv("ASTRA_DB_API_ENDPOINT", "")
    ASTRA_DB_TOKEN: str = os.getenv("ASTRA_DB_TOKEN", "")
    ASTRA_DB_COLLECTION: str = os.getenv("ASTRA_DB_COLLECTION", "booking_faq")
    # Mistral embedding model used to embed user queries for vector search.
    MISTRAL_EMBEDDING_MODEL: str = os.getenv("MISTRAL_EMBEDDING_MODEL", "mistral-embed")

    # Booking behaviour
    DEFAULT_TIMEZONE: str = os.getenv("DEFAULT_TIMEZONE", "Africa/Casablanca")
    DEFAULT_CURRENCY: str = os.getenv("DEFAULT_CURRENCY", "MAD")
    HOLD_MINUTES: int = int(os.getenv("HOLD_MINUTES", "10"))
    SLOTS_PER_PAGE: int = int(os.getenv("SLOTS_PER_PAGE", "5"))
    WORKING_HOUR_START: int = int(os.getenv("WORKING_HOUR_START", "8"))
    WORKING_HOUR_END: int = int(os.getenv("WORKING_HOUR_END", "23"))

    # Workflow jobs (retry / DLQ / outbox)
    MAX_ATTEMPTS: int = int(os.getenv("MAX_ATTEMPTS", "3"))
    RETRY_BASE_SECONDS: int = int(os.getenv("RETRY_BASE_SECONDS", "5"))
    WORKER_POLL_SECONDS: int = int(os.getenv("WORKER_POLL_SECONDS", "3"))
    OUTBOX_MAX_ATTEMPTS: int = int(os.getenv("OUTBOX_MAX_ATTEMPTS", "5"))


settings = Settings()
