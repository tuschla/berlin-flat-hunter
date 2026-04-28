"""Entry point for berlin-flat-hunter"""
import argparse
import time
import traceback

import yaml
from flathunter.idmaintainer import IdMaintainer
from flathunter.logging import logger

from berlin_flat_hunter.config import BerlinConfig
from berlin_flat_hunter.hunter import BerlinHunter


def main():
    parser = argparse.ArgumentParser(description="Berlin flat hunter")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    with open(args.config) as f:
        raw = yaml.safe_load(f) or {}

    config = BerlinConfig(raw)
    config.init_searchers()  # registers Gewobag, Wbm, Gesobau and flathunter's defaults
    id_watch = IdMaintainer(config.database_location())
    hunter = BerlinHunter(config, id_watch)

    try:
        if args.once or not config.loop_is_active():
            hunter.hunt_flats()
            return

        while True:
            try:
                hunter.hunt_flats()
            except Exception:
                logger.error("Hunt cycle failed:\n%s", traceback.format_exc())
            time.sleep(config.loop_period_seconds())
    except KeyboardInterrupt:
        logger.info("Shutdown requested, exiting.")
    finally:
        try:
            hunter.close()
        except Exception as exc:
            logger.warning("Hunter close failed: %s", exc)


if __name__ == "__main__":
    main()
