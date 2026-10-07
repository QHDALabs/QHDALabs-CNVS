import contextlib
import io
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from cnvs.cli import main
from cnvs.configuration import ConfigurationError, validate_configuration
from cnvs.settings import Settings


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"


class RuntimeConfigurationTests(unittest.TestCase):
    def test_repository_configuration_matches_confirmed_mvp_scope(self):
        summary = validate_configuration(CONFIG_DIR)

        self.assertEqual(summary.country_count, 12)
        self.assertEqual(summary.language_count, 11)
        self.assertEqual(summary.event_type_count, 9)
        self.assertEqual(summary.source_type_count, 2)

        countries = yaml.safe_load(
            (CONFIG_DIR / "countries.yaml").read_text(encoding="utf-8")
        )["countries"]
        languages = yaml.safe_load(
            (CONFIG_DIR / "languages.yaml").read_text(encoding="utf-8")
        )["languages"]
        event_types = yaml.safe_load(
            (CONFIG_DIR / "event_types.yaml").read_text(encoding="utf-8")
        )
        source_types = yaml.safe_load(
            (CONFIG_DIR / "source_types.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {
                country["code"]: (country["name"], set(country["languages"]))
                for country in countries
            },
            {
                "PL": ("Poland", {"pl"}),
                "DE": ("Germany", {"de"}),
                "FR": ("France", {"fr"}),
                "GB": ("United Kingdom", {"en"}),
                "FI": ("Finland", {"fi"}),
                "EE": ("Estonia", {"et"}),
                "UA": ("Ukraine", {"uk", "ru"}),
                "MD": ("Moldova", {"ro", "ru"}),
                "GE": ("Georgia", {"ka", "ru"}),
                "BY": ("Belarus", {"be", "ru"}),
                "RU": ("Russia", {"ru"}),
                "US": ("United States", {"en"}),
            },
        )
        self.assertEqual(
            {language["code"]: language["name"] for language in languages},
            {
                "pl": "Polish",
                "de": "German",
                "fr": "French",
                "en": "English",
                "fi": "Finnish",
                "et": "Estonian",
                "uk": "Ukrainian",
                "ro": "Romanian",
                "ka": "Georgian",
                "be": "Belarusian",
                "ru": "Russian",
            },
        )
        self.assertEqual(
            set(event_types["supported_event_types"]),
            {
                "MILITARY",
                "SABOTAGE",
                "INFRASTRUCTURE",
                "CYBER",
                "MARITIME",
                "AVIATION",
                "DIPLOMATIC",
                "POLITICAL",
                "ENERGY",
            },
        )
        self.assertEqual(
            {source["id"] for source in source_types["supported_source_types"]},
            {"RSS", "URL"},
        )

    def test_country_language_references_must_be_known(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            for filename in (
                "countries.yaml",
                "languages.yaml",
                "event_types.yaml",
                "source_types.yaml",
                "source_classes.yaml",
                "source_registry.yaml",
            ):
                shutil.copy(CONFIG_DIR / filename, config_dir / filename)

            countries_path = config_dir / "countries.yaml"
            countries = yaml.safe_load(countries_path.read_text(encoding="utf-8"))
            countries["countries"][0]["languages"] = ["xx"]
            countries_path.write_text(
                yaml.safe_dump(countries, sort_keys=False), encoding="utf-8"
            )

            with self.assertRaisesRegex(ConfigurationError, "unknown languages: xx"):
                validate_configuration(config_dir)

    def test_unreviewed_scope_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            for filename in (
                "countries.yaml",
                "languages.yaml",
                "event_types.yaml",
                "source_types.yaml",
                "source_classes.yaml",
                "source_registry.yaml",
            ):
                shutil.copy(CONFIG_DIR / filename, config_dir / filename)

            countries_path = config_dir / "countries.yaml"
            countries = yaml.safe_load(countries_path.read_text(encoding="utf-8"))
            countries["countries"].pop()
            countries_path.write_text(
                yaml.safe_dump(countries, sort_keys=False), encoding="utf-8"
            )

            with self.assertRaisesRegex(
                ConfigurationError, "do not match the approved MVP scope"
            ):
                validate_configuration(config_dir)

    def test_country_cannot_be_assigned_another_supported_language(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            for filename in (
                "countries.yaml",
                "languages.yaml",
                "event_types.yaml",
                "source_types.yaml",
                "source_classes.yaml",
                "source_registry.yaml",
            ):
                shutil.copy(CONFIG_DIR / filename, config_dir / filename)

            countries_path = config_dir / "countries.yaml"
            countries = yaml.safe_load(countries_path.read_text(encoding="utf-8"))
            countries["countries"][0]["languages"] = ["ru"]
            countries_path.write_text(
                yaml.safe_dump(countries, sort_keys=False), encoding="utf-8"
            )

            with self.assertRaisesRegex(
                ConfigurationError, "country PL name and languages must match"
            ):
                validate_configuration(config_dir)

    def test_malformed_yaml_is_reported_with_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            for filename in (
                "countries.yaml",
                "languages.yaml",
                "event_types.yaml",
                "source_types.yaml",
                "source_classes.yaml",
                "source_registry.yaml",
            ):
                shutil.copy(CONFIG_DIR / filename, config_dir / filename)
            (config_dir / "countries.yaml").write_text("countries: [", encoding="utf-8")

            with self.assertRaisesRegex(
                ConfigurationError, "countries.yaml is not valid YAML"
            ):
                validate_configuration(config_dir)

    def test_environment_configuration_and_cli_override(self):
        previous_level = logging.getLogger().level
        try:
            with patch.dict(
                os.environ,
                {"CNVS_CONFIG_DIR": "from-environment", "CNVS_LOG_LEVEL": "info"},
                clear=False,
            ):
                environment_settings = Settings.from_environment()
                cli_settings = Settings.from_environment(
                    config_dir_override=CONFIG_DIR,
                    log_level_override="ERROR",
                )

            self.assertEqual(
                environment_settings.config_dir,
                (Path.cwd() / "from-environment").resolve(),
            )
            self.assertEqual(environment_settings.log_level, "INFO")
            self.assertEqual(cli_settings.config_dir, CONFIG_DIR.resolve())
            self.assertEqual(cli_settings.log_level, "ERROR")
            self.assertEqual(logging.getLogger().level, logging.ERROR)
        finally:
            logging.getLogger().setLevel(previous_level)

    def test_invalid_log_level_is_reported(self):
        with patch.dict(os.environ, {"CNVS_LOG_LEVEL": "VERBOSE"}, clear=False):
            with self.assertRaisesRegex(ValueError, "Invalid CNVS log level"):
                Settings.from_environment()

    def test_cli_validates_configuration_and_reports_invalid_input(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main(["config", "validate", "--config-dir", str(CONFIG_DIR)])
        self.assertEqual(result, 0)
        self.assertIn(
            "12 countries, 11 languages, 9 event types, 2 source types",
            stdout.getvalue(),
        )

        with contextlib.redirect_stderr(stderr):
            result = main(
                ["config", "validate", "--config-dir", str(CONFIG_DIR / "missing")]
            )
        self.assertEqual(result, 2)
        self.assertIn("Required configuration file is missing", stderr.getvalue())

    def test_cli_uses_configuration_directory_from_environment(self):
        stdout = io.StringIO()
        with patch.dict(os.environ, {"CNVS_CONFIG_DIR": str(CONFIG_DIR)}, clear=False):
            with contextlib.redirect_stdout(stdout):
                result = main(["config", "validate"])

        self.assertEqual(result, 0)
        self.assertIn("Configuration valid", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
