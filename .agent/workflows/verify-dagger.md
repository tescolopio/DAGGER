---
description: Run DAGGER Python tests and frontend build verification.
---

# Verify DAGGER

Run the full verification suite for the DAGGER control plane:

1. Run the Python test suite:
   ```bash
   uv run pytest -v
   ```

2. Build the frontend:
   ```bash
   cd frontend && npm run build
   ```
