import httpx

from autolid.config import load_config

cfg = load_config().ai
key = str(cfg.api_key)

r = httpx.get(
    cfg.base_url.rstrip("/") + "/models",
    headers={"Authorization": f"Bearer {key}", "x-api-key": key, "anthropic-version": "2023-06-01"},
    timeout=30,
)
r.raise_for_status()
models = r.json().get("data", [])
for m in models:
    print(m.get("id"))
print(f"Всего: {len(models)}")
