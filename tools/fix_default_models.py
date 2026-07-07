#!/usr/bin/env python3
"""
Fix missing tenant_model records for default models configured in tenant table,
and repair common provider misconfigurations (e.g. Moonshot base_url missing /v1/).

Usage:
    python tools/fix_default_models.py

This script reads llm_id/embd_id/img2txt_id/asr_id/rerank_id/tts_id from the
 tenant table, resolves provider/instance/model info, and ensures every default
 model has a corresponding active record in tenant_model.
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

# Add project root to sys.path so we can import project modules if needed.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Database connection settings come from environment or fall back to defaults.
# For Docker Compose deployment these match docker/.env values.
DB_HOST = os.getenv("MYSQL_HOST", "localhost")
DB_PORT = int(os.getenv("MYSQL_PORT", "3306"))
DB_NAME = os.getenv("MYSQL_DB_NAME", "rag_flow")
DB_USER = os.getenv("MYSQL_USER", "root")
DB_PASSWORD = os.getenv("MYSQL_PASSWORD", "infini_rag_flow")

LLM_FACTORIES_PATH = PROJECT_ROOT / "conf" / "llm_factories.json"

DEFAULT_MODEL_COLUMNS = [
    ("llm_id", "chat"),
    ("embd_id", "embedding"),
    ("img2txt_id", "image2text"),
    ("asr_id", "speech2text"),
    ("rerank_id", "rerank"),
    ("tts_id", "tts"),
]


def load_factory_infos():
    with open(LLM_FACTORIES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {fac["name"]: fac for fac in data.get("factory_llm_infos", [])}


def get_model_type(factory_info, model_name, fallback_type):
    """Infer model_type from llm_factories.json or fall back to the column-based guess."""
    for llm in factory_info.get("llm", []):
        if llm["llm_name"] == model_name:
            model_type = llm.get("model_type")
            if isinstance(model_type, list):
                # If the factory declares multiple types, prefer the one matching fallback.
                if fallback_type in model_type:
                    return fallback_type
                return model_type[0]
            return model_type or fallback_type
    return fallback_type


def get_max_tokens(factory_info, model_name):
    for llm in factory_info.get("llm", []):
        if llm["llm_name"] == model_name:
            return llm.get("max_tokens", 8192)
    return 8192


def make_id(*parts):
    return hashlib.md5("".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def normalize_base_url(url: str) -> str:
    """Return a clean base_url with a single trailing slash."""
    if not url:
        return url
    return url.rstrip("/") + "/"


def fix_moonshot_base_url(cursor, conn, now_ts, now_dt):
    """Ensure Moonshot/Kimi instances use the /v1/ API path.

    The OpenAI-compatible Kimi coding endpoint requires the path segment /v1/.
    A base_url like https://api.kimi.com/coding/ results in 404 because the SDK
    appends /chat/completions directly, producing /coding/chat/completions.
    """
    cursor.execute(
        """
        SELECT i.id, i.extra, p.provider_name, p.tenant_id
        FROM tenant_model_instance i
        JOIN tenant_model_provider p ON i.provider_id = p.id
        WHERE p.provider_name = 'Moonshot'
        """
    )
    fixed = []
    for row in cursor.fetchall():
        extra = row["extra"] or "{}"
        try:
            extra_obj = json.loads(extra)
        except json.JSONDecodeError:
            extra_obj = {}
        base_url = extra_obj.get("base_url", "")
        if not base_url:
            continue
        normalized = normalize_base_url(base_url)
        # Kimi OpenAI-compatible endpoints need /coding/v1/ (or /v1/ for standard endpoint)
        if "/kimi.com/" in normalized and not normalized.endswith("/v1/"):
            # Convert .../coding/ -> .../coding/v1/
            # Convert .../coding    -> .../coding/v1/
            # Convert .../v1        -> .../v1/
            new_url = normalized.rstrip("/") + "/v1/"
            extra_obj["base_url"] = new_url
            cursor.execute(
                "UPDATE tenant_model_instance SET extra=%s, update_time=%s, update_date=%s WHERE id=%s",
                (json.dumps(extra_obj, ensure_ascii=False), now_ts, now_dt, row["id"]),
            )
            fixed.append(f"tenant={row['tenant_id']} Moonshot instance {row['id']}: {base_url} -> {new_url}")
    return fixed


def main():
    import pymysql

    factory_infos = load_factory_infos()

    conn = pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
    )
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    try:
        # Repair Moonshot/Kimi base_url misconfigurations first.
        now_ts = int(time.time() * 1000)
        now_dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        url_fixed = fix_moonshot_base_url(cursor, conn, now_ts, now_dt)

        # 1. Load all tenants with their default model refs
        cursor.execute("SELECT id, name, " + ", ".join(c[0] for c in DEFAULT_MODEL_COLUMNS) + " FROM tenant")
        tenants = cursor.fetchall()

        # 2. Load all providers and instances into memory
        cursor.execute("SELECT id, provider_name, tenant_id FROM tenant_model_provider")
        providers = {row["id"]: row for row in cursor.fetchall()}

        cursor.execute("SELECT id, instance_name, provider_id, api_key, status, extra FROM tenant_model_instance")
        instances = {}
        for row in cursor.fetchall():
            instances.setdefault(row["provider_id"], {})[row["instance_name"]] = row

        cursor.execute("SELECT id, model_name, provider_id, instance_id, model_type, status, extra FROM tenant_model")
        existing_models = {
            (row["provider_id"], row["instance_id"], row["model_name"], row["model_type"]): row
            for row in cursor.fetchall()
        }

        now_ts = int(time.time() * 1000)
        now_dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

        fixed = []
        skipped = []
        errors = []

        for tenant in tenants:
            tenant_id = tenant["id"]
            for col_name, fallback_type in DEFAULT_MODEL_COLUMNS:
                model_ref = tenant.get(col_name) or ""
                if not model_ref:
                    continue
                # Parse "model_name@instance_name@provider_name"
                parts = model_ref.split("@")
                if len(parts) != 3:
                    skipped.append(f"tenant={tenant_id} {col_name}={model_ref} (unrecognized format)")
                    continue
                model_name, instance_name, provider_name = parts

                provider = next(
                    (p for p in providers.values() if p["provider_name"] == provider_name and p["tenant_id"] == tenant_id),
                    None,
                )
                if not provider:
                    errors.append(f"tenant={tenant_id} {col_name}={model_ref}: provider '{provider_name}' not found")
                    continue

                provider_instances = instances.get(provider["id"], {})
                instance = provider_instances.get(instance_name)
                if not instance:
                    errors.append(f"tenant={tenant_id} {col_name}={model_ref}: instance '{instance_name}' not found")
                    continue

                factory_info = factory_infos.get(provider_name, {})
                model_type = get_model_type(factory_info, model_name, fallback_type)
                max_tokens = get_max_tokens(factory_info, model_name)
                extra = json.dumps({"max_tokens": max_tokens, "is_tools": False}, ensure_ascii=False)

                key = (provider["id"], instance["id"], model_name, model_type)
                if key in existing_models:
                    existing = existing_models[key]
                    if existing["status"] != "active":
                        cursor.execute(
                            "UPDATE tenant_model SET status=%s, update_time=%s, update_date=%s, extra=%s WHERE id=%s",
                            ("active", now_ts, now_dt, extra, existing["id"]),
                        )
                        fixed.append(f"tenant={tenant_id} {col_name}: reactivated {model_name} ({model_type})")
                    else:
                        skipped.append(f"tenant={tenant_id} {col_name}: {model_name} ({model_type}) already active")
                else:
                    model_id = make_id(tenant_id, provider["id"], instance["id"], model_name, model_type)
                    cursor.execute(
                        """
                        INSERT INTO tenant_model
                        (id, create_time, create_date, update_time, update_date, model_name, provider_id, instance_id, model_type, status, extra)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (model_id, now_ts, now_dt, now_ts, now_dt, model_name, provider["id"], instance["id"], model_type, "active", extra),
                    )
                    fixed.append(f"tenant={tenant_id} {col_name}: inserted {model_name} ({model_type})")

        conn.commit()

        print("=" * 60)
        print("fix_default_models.py result")
        print("=" * 60)
        print(f"Base URL repairs: {len(url_fixed)}")
        for item in url_fixed:
            print(f"  ~ {item}")
        print(f"Fixed/inserted: {len(fixed)}")
        for item in fixed:
            print(f"  + {item}")
        print(f"Skipped: {len(skipped)}")
        for item in skipped[:10]:
            print(f"  - {item}")
        if len(skipped) > 10:
            print(f"  ... and {len(skipped) - 10} more")
        if errors:
            print(f"Errors: {len(errors)}")
            for item in errors:
                print(f"  ! {item}")
            sys.exit(1)

    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()
