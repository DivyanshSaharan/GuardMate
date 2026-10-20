"""Never let test collection inherit the developer's live deployment credentials."""

import os

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
for _name in list(os.environ):
    if _name.startswith(("MONGODB_", "TINKER_", "GUARDMATE_", "RENDER")):
        os.environ.pop(_name, None)
