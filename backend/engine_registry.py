"""Engine registry: one table per pipeline stage describing every engine.

Each engine is an :class:`EngineSpec`: its id and UI label, whether it runs
locally, how to tell if its dependency is installed, what (if anything) it
still needs before a job can use it (an API key, a binary), how to build it,
and an optional concurrency cap for local models. The transcription and
summarization packages each expose a :class:`EngineRegistry` built from
their specs; the API serves ``describe()`` to the UI, ``job_settings``
validates jobs against ``requirement()``, and the pipeline calls ``create``.

Adding an engine = write its module and add one ``EngineSpec`` to the
stage's registry. Nothing else in the backend or the UI names engines.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional


@dataclass(frozen=True)
class EngineSpec:
    id: str
    label: str
    local: bool
    installed: Callable[[], bool]
    """True when the engine's Python package or binary is present."""
    requirement: Callable[[], Optional[str]]
    """None when the engine can run now; otherwise what is missing, for humans."""
    create: Callable[..., Any]
    install_hint: str = ""
    max_concurrency: Optional[int] = None
    """Worker cap for local engines; None means the stage's cloud default."""

    def ready(self) -> bool:
        return self.installed() and self.requirement() is None

    def describe(self) -> Dict[str, Any]:
        installed = self.installed()
        requirement = self.requirement() if installed else (self.install_hint or "not installed")
        return {
            "id": self.id,
            "label": self.label,
            "local": self.local,
            "installed": installed,
            "ready": installed and requirement is None,
            "requirement": requirement or "",
        }


class EngineRegistry:
    def __init__(self, stage: str, specs: Iterable[EngineSpec]):
        self.stage = stage
        self._specs: Dict[str, EngineSpec] = {}
        for spec in specs:
            if spec.id in self._specs:
                raise ValueError(f"duplicate {stage} engine id {spec.id!r}")
            self._specs[spec.id] = spec

    def __iter__(self) -> Iterator[EngineSpec]:
        return iter(self._specs.values())

    def ids(self) -> List[str]:
        return list(self._specs)

    def find(self, name: Optional[str]) -> Optional[EngineSpec]:
        return self._specs.get((name or "").strip().lower())

    def get(self, name: str) -> EngineSpec:
        spec = self.find(name)
        if spec is None:
            raise ValueError(
                f"Unknown {self.stage} engine: {name!r}. Available: {', '.join(self.available()) or 'none'}"
            )
        return spec

    def available(self) -> List[str]:
        """Ids of the engines whose dependencies are installed."""
        return [spec.id for spec in self if spec.installed()]

    def describe(self) -> List[Dict[str, Any]]:
        return [spec.describe() for spec in self]

    def create(self, name: str, **kwargs) -> Any:
        """Build an engine, raising ``RuntimeError`` when it cannot run yet."""
        spec = self.get(name)
        if not spec.installed():
            raise RuntimeError(f"{spec.label} is not installed. {spec.install_hint}".strip())
        requirement = spec.requirement()
        if requirement:
            raise RuntimeError(f"{spec.label}: {requirement}")
        return spec.create(**kwargs)
