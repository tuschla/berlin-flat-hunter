"""Entry point for berlin-flat-hunter.

Two modes:

* ``--hunter hunter.yaml`` — **shared-scrape** mode (recommended). One process
  crawls every source once per cycle and fans the results out to all listed
  profiles. This is what the ``bfh-hunter`` systemd service runs.
* ``--config profile.yaml`` — legacy single-profile mode. One profile, one full
  crawl. Kept for one-off runs and backward compatibility.
"""
import argparse
import time
import traceback

import yaml
from flathunter.idmaintainer import IdMaintainer
from flathunter.logging import logger

from berlin_flat_hunter.config import BerlinConfig
from berlin_flat_hunter.hunter import BerlinHunter
from berlin_flat_hunter.orchestrator import Orchestrator


def run_single(config_path: str, once: bool) -> None:
    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    config = BerlinConfig(raw)
    config.init_searchers()  # registers Berlin crawlers + flathunter's defaults
    id_watch = IdMaintainer(config.database_location())
    hunter = BerlinHunter(config, id_watch)

    try:
        if once or not config.loop_is_active():
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


def run_shared(hunter_path: str, once: bool) -> None:
    orch = Orchestrator.from_file(hunter_path)
    try:
        if once:
            orch.run_once()
        else:
            orch.loop()
    except KeyboardInterrupt:
        logger.info("Shutdown requested, exiting.")
    finally:
        orch.close()


def main():
    parser = argparse.ArgumentParser(description="Berlin flat hunter")
    parser.add_argument("--config", help="Path to a single-profile config YAML")
    parser.add_argument("--hunter", help="Path to hunter.yaml (shared-scrape, multi-profile)")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    args = parser.parse_args()

    if args.hunter:
        run_shared(args.hunter, args.once)
    else:
        run_single(args.config or "config.yaml", args.once)


if __name__ == "__main__":
    main()
