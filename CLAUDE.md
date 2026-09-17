# backend

Teil der DHBW-AppStore-T3 Organisation — sechs eigenständige Repos, kein Monorepo.
Harness-Gesamtkonzept: siehe https://github.com/DHBW-AppStore-T3/.github/blob/main/docs/HARNESS.md

Details zu diesem Repo:
- Lebendes Übergabedokument: `claude_docs/HANDOVER.md` (zwingend zu Beginn jeder Session lesen und vor Session-Ende aktualisieren)
- API-Contract: FastAPI ist Single Source of Truth, Export via `scripts/export_openapi.py` bzw. `make openapi`
- Architektur: `claude_docs/architecture/`
- Entscheidungen: `claude_docs/decisions/`
- Debugging: `claude_docs/debugging/`
