import argparse
import sqlite3
import sys
from collections.abc import Sequence

from cnvs import __version__
from cnvs.configuration import ConfigurationError, validate_configuration
from cnvs.settings import Settings
from cnvs.storage import Database, StorageError


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
    database_parser = commands.add_parser("db", help="Manage the local database.")
    database_commands = database_parser.add_subparsers(
        dest="database_command", required=True
    )
    for name, help_text in (
        ("migrate", "Apply pending database migrations."),
        ("status", "Show applied database migrations."),
    ):
        command_parser = database_commands.add_parser(name, help=help_text)
        command_parser.add_argument(
            "--database",
            help=(
                "SQLite database file "
                "(overrides CNVS_DATABASE_PATH; defaults to .data/cnvs.sqlite3)."
            ),
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "config":
            settings = Settings.from_environment(config_dir_override=args.config_dir)
            summary = validate_configuration(settings.config_dir)
            print(
                "Configuration valid: "
                f"{summary.country_count} countries, "
                f"{summary.language_count} languages, "
                f"{summary.event_type_count} event types, "
                f"{summary.source_type_count} source types."
            )
            return 0

        settings = Settings.from_environment(database_path_override=args.database)
        database = Database(settings.database_path)
        migrations = database.migration_status()
        if args.database_command == "migrate":
            print(f"Database ready; {len(migrations)} migration(s) applied.")
        else:
            print(f"Database: {settings.database_path}")
            if not migrations:
                print("No migrations applied.")
            for version, applied_at in migrations:
                print(f"{version} applied at {applied_at}")
        return 0
    except (
        ConfigurationError,
        OSError,
        sqlite3.Error,
        ValueError,
        StorageError,
    ) as error:
        print(f"cnvs: error: {error}", file=sys.stderr)
        return 2
