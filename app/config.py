import os


def get_db_path() -> str:
    return os.getenv("DATABASE_PATH", "data.db")


def get_oncall_api_base() -> str:
    return os.getenv("ONCALL_API_BASE", "https://oncallvos2.southeastasia.cloudapp.azure.com:8080")
