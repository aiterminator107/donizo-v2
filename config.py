from pathlib import Path
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    # Gemini extractor is not used in this version; removed Gemini config.
    chroma_path: str = str(BASE_DIR / "data" / "chroma")
    products_path: str = str(BASE_DIR / "data" / "products")
    products_jsonl: str = str(BASE_DIR / "scrapper" / "products.jsonl")
    feedback_db: str = str(BASE_DIR / "data" / "feedback.db")

    embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    chroma_collection: str = "products"

    # Optional HF token for faster model downloads from the Hugging Face Hub.
    hf_token: str | None = None

    scrape_delay_min: float = 1.0
    scrape_delay_max: float = 3.0

    class Config:
        env_file = str(BASE_DIR / ".env")
        env_file_encoding = "utf-8"


settings = Settings()
