import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_client = None

def require_openai_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("API_KEY")
        if not api_key:
            raise RuntimeError("Missing API_KEY in .env")
        _client = OpenAI(api_key=api_key, base_url=os.getenv("API_URL"))
    return _client


def get_response(prompt: str) -> str:
    client = require_openai_client()
    response = client.chat.completions.create(
        messages=[
            {"role": "system", "content": "你是一个代码专家"},
            {"role": "user", "content": prompt},
        ],
        model=os.getenv("MODEL", "qwen-plus-latest"),
    )
    return response.choices[0].message.content