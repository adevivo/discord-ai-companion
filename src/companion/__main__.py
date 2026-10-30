"""Entry point: `python -m companion`.

The Discord connection is not implemented yet; for now this validates the
configuration so the container can be built and smoke-tested.
"""

import logging
import sys

from pydantic import ValidationError

from companion.config import Settings

log = logging.getLogger("companion")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        settings = Settings()
    except ValidationError as exc:
        missing = ", ".join(str(err["loc"][0]).upper() for err in exc.errors())
        log.error("Invalid or missing configuration: %s", missing)
        return 1

    log.info("Configuration OK (model=%s, base_url=%s)", settings.llm_model, settings.llm_base_url)
    log.warning("Discord connection not implemented yet; exiting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
