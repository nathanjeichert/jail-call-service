"""The engine registries: description shape, readiness, and job validation."""

import unittest
from unittest.mock import patch

from backend import summarization, transcription
from backend.engine_registry import EngineRegistry, EngineSpec
from backend.job_settings import RuntimeSelection, validate_runtime_selection


def _spec(id, *, local=False, installed=True, requirement=None, cap=None):
    return EngineSpec(
        id=id, label=f"{id.title()} ({'Local' if local else 'Cloud'})", local=local,
        installed=lambda: installed, requirement=lambda: requirement,
        create=lambda **kw: ("engine", id, kw), install_hint="install it", max_concurrency=cap,
    )


class RegistryTests(unittest.TestCase):
    def test_real_registries_describe_every_engine(self):
        for registry in (transcription.REGISTRY, summarization.REGISTRY):
            rows = registry.describe()
            self.assertEqual([r["id"] for r in rows], registry.ids())
            for row in rows:
                self.assertEqual(set(row), {"id", "label", "local", "installed", "ready", "requirement"})
                self.assertTrue(row["label"])
            self.assertEqual(registry.available(), [r["id"] for r in rows if r["installed"]])

    def test_unknown_engine_raises_value_error(self):
        with self.assertRaises(ValueError):
            transcription.get_engine("nope")
        with self.assertRaises(ValueError):
            summarization.get_engine("nope")

    def test_create_reports_missing_dependency_and_requirement(self):
        registry = EngineRegistry("test", [
            _spec("absent", installed=False),
            _spec("keyless", requirement="KEY is not set"),
            _spec("ok"),
        ])
        with self.assertRaisesRegex(RuntimeError, "not installed. install it"):
            registry.create("absent")
        with self.assertRaisesRegex(RuntimeError, "KEY is not set"):
            registry.create("keyless")
        self.assertEqual(registry.create("ok", model="m"), ("engine", "ok", {"model": "m"}))
        self.assertEqual(registry.available(), ["keyless", "ok"])
        self.assertEqual([r["ready"] for r in registry.describe()], [False, False, True])

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            EngineRegistry("test", [_spec("a"), _spec("a")])


class RuntimeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.transcription = EngineRegistry("transcription", [
            _spec("cloud-t"), _spec("local-t", local=True, cap=2), _spec("dead-t", installed=False),
        ])
        self.summarization = EngineRegistry("summarization", [
            _spec("cloud-s", requirement="GEMINI_API_KEY is not set in .env"), _spec("local-s", local=True, cap=1),
        ])
        self.patches = [
            patch("backend.job_settings._transcription_registry", lambda: self.transcription),
            patch("backend.job_settings._summarization_registry", lambda: self.summarization),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _sel(self, t, s, skip=False):
        return RuntimeSelection(transcription_engine=t, summarization_engine=s, skip_summary=skip, auto_message_mode=None)

    def test_local_flags_and_worker_caps_come_from_the_registry(self):
        both_local = self._sel("local-t", "local-s")
        self.assertTrue(both_local.all_local)
        self.assertEqual(both_local.transcription_workers(50), 2)
        self.assertEqual(both_local.summarization_workers(50), 1)

        cloud = self._sel("cloud-t", "cloud-s")
        self.assertFalse(cloud.transcription_is_local)
        self.assertFalse(cloud.summarization_is_local)
        self.assertGreater(cloud.transcription_workers(500), 2)

        unknown = self._sel("ghost", "ghost")
        self.assertFalse(unknown.all_local)

    def test_validation_names_what_is_missing(self):
        with self.assertRaisesRegex(RuntimeError, "Transcription engine 'dead-t' is unavailable"):
            validate_runtime_selection(self._sel("dead-t", "local-s"))
        with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY is not set"):
            validate_runtime_selection(self._sel("cloud-t", "cloud-s"))
        validate_runtime_selection(self._sel("cloud-t", "cloud-s", skip=True))  # summary skipped: key not needed
        validate_runtime_selection(self._sel("local-t", "local-s"))


if __name__ == "__main__":
    unittest.main()
