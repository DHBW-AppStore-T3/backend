#!/usr/bin/env python3
"""Export OpenAPI specification for Backend API.

FastAPI serves as the Single Source of Truth for API contracts across all
DHBW AppStore repositories (see HARNESS.md Section 1.3).
This script extracts the complete OpenAPI 3.1 specification directly from the
FastAPI app and saves it as JSON without requiring a running database or broker.
"""
import json
import os
import sys
from pathlib import Path

# Safe fallbacks for schema generation (no real connection needed)
os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
os.environ.setdefault(
    "CREDENTIAL_ENCRYPTION_KEY",
    "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="
)
os.environ.setdefault("DISABLE_BACKGROUND_TASKS", "1")

# Add backend root to Python module path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

try:
    from app.main import app
except ImportError as err:
    sys.stderr.write(f"Error importing app.main: {err}\n")
    sys.exit(1)


def generate_openapi():
    """Extract the OpenAPI specification from the FastAPI app instance."""
    return app.openapi()


def main():
    target_path = sys.argv[1] if len(sys.argv) > 1 else str(backend_dir / "openapi.json")
    schema = generate_openapi()

    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Successfully exported OpenAPI specification to: {target_path}")


if __name__ == "__main__":
    main()
