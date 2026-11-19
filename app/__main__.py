import logging

from waitress import serve

from .config import Config
from .server import create_app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    cfg.validate()
    if not cfg.auth_enabled:
        logging.warning("Authentication is OFF. Anyone who can reach this port can "
                        "delete subvolumes and snapshots.")
    if cfg.readonly:
        logging.info("Read-only mode: changes are disabled.")
    app = create_app(cfg)
    app.scheduler.start()
    logging.info("Listening on http://%s:%s", cfg.host, cfg.port)
    serve(app, host=cfg.host, port=cfg.port, threads=8,
          ident="btrfs-ui", clear_untrusted_proxy_headers=True)


if __name__ == "__main__":
    main()
