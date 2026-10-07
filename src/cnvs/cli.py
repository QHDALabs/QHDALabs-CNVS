import argparse
import sys
from collections.abc import Sequence

from cnvs import __version__
from cnvs.configuration import ConfigurationError, validate_configuration
from cnvs.settings import Settings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cnvs",
        description="CNVS project and configuration utilities.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    config_parser = commands.add_parser("config", help="Inspect runtime configuration.")
    config_commands = config_parser.add_subparsers(dest="config_command", required=True)
    validate_parser = config_commands.add_parser(
        "validate", help="Validate the MVP YAML configuration set."
    )
    validate_parser.add_argument(
        "--config-dir",
        help="Configuration directory (overrides CNVS_CONFIG_DIR; defaults to ./config).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_environment(config_dir_override=args.config_dir)
        summary = validate_configuration(settings.config_dir)
    except (ConfigurationError, OSError, ValueError) as error:
        print(f"cnvs: error: {error}", file=sys.stderr)
        return 2

    print(
        "Configuration valid: "
        f"{summary.country_count} countries, "
        f"{summary.language_count} languages, "
        f"{summary.event_type_count} event types, "
        f"{summary.source_type_count} source types."
    )
    return 0
