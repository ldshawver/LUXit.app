# Supported Python Version

LUXit.app is tested on Python 3.11 and 3.12. Do **not** use Python 3.14 for this repo until all pinned native dependencies publish compatible wheels.

Recommended setup:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
pytest
```
