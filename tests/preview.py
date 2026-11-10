"""Open the screens with made-up data and no API keys: python -m tests.preview

This is only for looking at the design. It is not used by the real app.
"""
import os
import tempfile
from pathlib import Path

os.environ["DATABASE_PATH"] = str(Path(tempfile.mkdtemp()) / "preview.db")

import uvicorn  # noqa: E402

from app import main  # noqa: E402
from tests.fakes import Fake, FakeMailer  # noqa: E402

if __name__ == "__main__":
    main.app.state.providers = Fake(delay=0.45)
    main.app.state.mailer = FakeMailer()  # nothing can really be sent from the preview
    uvicorn.run(main.app, host="127.0.0.1", port=8765, log_level="warning")
